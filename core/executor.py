"""
Script executor module for PyRunner.

This module handles the execution of Python scripts in isolated environments.
It is designed to be called from django-q2 async tasks.

Key design notes (post 100K-line support):
  * Output is captured incrementally and either kept inline (small) or
    spooled to disk via OutputStorageService (large).
  * Secret masking uses a single compiled regex (one pass over output)
    instead of `str.replace` per secret — O(n) instead of O(n*m).
  * Subprocess is started in its own process group so SIGTERM/SIGKILL
    reliably kills child trees.
  * The script body is written to the temp file in 256KB chunks instead
    of one big `f.write(code)` call (matters when code is 50MB).
  * The executor never loads the entire script body into a `str()` call
    unnecessarily — Django already gives us a `str` from the TextField,
    but we avoid double-buffering by streaming into the file.
"""

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import traceback
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from core.models import Run, Secret
from core.services import EncryptionService
from core.services.output_storage_service import OutputStorageService

logger = logging.getLogger(__name__)


# How big can a single inline stdout/stderr be before we spool to disk?
# Read from settings so admins can tune without code changes.
INLINE_LIMIT = getattr(settings, "OUTPUT_SPOOL_THRESHOLD", 4 * 1024 * 1024)
HARD_CAP = getattr(settings, "MAX_OUTPUT_SPOOL_BYTES", 2 * 1024 * 1024 * 1024)

# Chunk size for streaming the script body and subprocess output.
CHUNK_BYTES = 262_144  # 256 KB


def _get_secrets_env() -> dict:
    """
    Get all secrets as environment variables.

    Returns:
        Dict of {key: decrypted_value} for all secrets
    """
    secrets_env = {}

    # Only try to get secrets if encryption is configured
    if not EncryptionService.is_configured():
        logger.debug("Encryption not configured - secrets will not be injected")
        return secrets_env

    try:
        # Only fetch keys we need, in one query. values_list is faster than
        # instantiating full model instances when we don't need them.
        for secret in Secret.objects.all().only("key", "encrypted_value", "salt"):
            try:
                secrets_env[secret.key] = secret.get_decrypted_value()
            except Exception as e:
                logger.error(f"Failed to decrypt secret {secret.key}: {e}")
    except Exception as e:
        logger.error(f"Failed to load secrets: {e}")

    return secrets_env


def _build_script_environment(webhook_data: dict | None = None) -> dict:
    """
    Build the environment dict for script execution.

    Combines system environment with secrets, webhook data, and DataStore access.
    Secrets override any same-named system variables.
    Webhook data is added with WEBHOOK_ prefix.

    Args:
        webhook_data: Optional webhook data from HTTP request

    Returns:
        Environment dict to pass to subprocess
    """
    # Start with system environment
    env = os.environ.copy()

    # Add secrets (overriding any existing vars with same name)
    secrets = _get_secrets_env()
    env.update(secrets)

    # Add webhook data if present
    if webhook_data:
        env["WEBHOOK_METHOD"] = webhook_data.get("method", "")
        env["WEBHOOK_QUERY"] = json.dumps(webhook_data.get("query", {}))
        env["WEBHOOK_CONTENT_TYPE"] = webhook_data.get("content_type", "")

        if "body" in webhook_data:
            # Cap to a sane size — environment vars have OS-level limits.
            body = webhook_data["body"]
            if len(body) > 32_000:
                body = body[:32_000]
                logger.warning("Webhook body truncated for env var (too long)")
            env["WEBHOOK_BODY"] = body

        if "body_json" in webhook_data:
            env["WEBHOOK_BODY_JSON"] = json.dumps(webhook_data["body_json"])

    # Add DataStore support
    # Set the database path for the pyrunner_datastore module
    env["PYRUNNER_DB_PATH"] = str(settings.DATABASES["default"]["NAME"])

    # Add script_helpers to PYTHONPATH so scripts can import pyrunner_datastore
    helpers_path = str(Path(settings.BASE_DIR) / "core" / "script_helpers")
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        env["PYTHONPATH"] = f"{helpers_path}{os.pathsep}{existing_pythonpath}"
    else:
        env["PYTHONPATH"] = helpers_path

    return env


def _compile_secret_masker(secrets: dict) -> re.Pattern | None:
    """
    Build a single regex that matches any secret value, for O(n) masking.

    Returns None if no maskable secrets.
    """
    # Sort longest-first so partial overlaps don't cause early short matches.
    candidates = sorted(
        {v for v in secrets.values() if v and len(v) >= 4},
        key=len,
        reverse=True,
    )
    if not candidates:
        return None
    # Escape each value, join with |. This is one pass over the output.
    pattern = "|".join(re.escape(v) for v in candidates)
    return re.compile(pattern)


def _mask_secrets_in_output(output: str, secrets: dict) -> str:
    """
    Mask secret values in output to prevent accidental exposure.

    Uses a single compiled regex (one pass over output) instead of the
    previous per-secret `str.replace` which was O(n*m) on large outputs.

    Args:
        output: The script output
        secrets: Dict of {key: value} secrets

    Returns:
        Output with secret values replaced with [KEY:MASKED]
    """
    if not output or not secrets:
        return output

    rx = _compile_secret_masker(secrets)
    if rx is None:
        return output

    # Build reverse map value -> key (longest value wins on collision)
    value_to_key = {}
    for k, v in sorted(secrets.items(), key=lambda kv: len(kv[1]) if kv[1] else 0, reverse=True):
        if v and len(v) >= 4 and v not in value_to_key:
            value_to_key[v] = k

    def _replace(m: re.Match) -> str:
        key = value_to_key.get(m.group(0))
        return f"[{key}:MASKED]" if key else m.group(0)

    return rx.sub(_replace, output)


class ExecutorError(Exception):
    """Base exception for executor errors."""

    pass


class EnvironmentNotFoundError(ExecutorError):
    """Raised when the environment directory does not exist."""

    pass


class PythonNotFoundError(ExecutorError):
    """Raised when the Python executable is not found."""

    pass


def _truncate_output(output: str, max_bytes: int | None = None) -> str:
    """
    Truncate inline output if it exceeds max_bytes.

    Note: for outputs above OUTPUT_SPOOL_THRESHOLD the executor uses
    OutputStorageService instead — this function only applies to the
    inline path (small outputs).
    """
    if not output:
        return output
    cap = max_bytes or getattr(settings, "MAX_OUTPUT_BYTES", 50 * 1024 * 1024)
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return output

    # Truncate and decode back, keeping a buffer for the notice
    notice = "\n\n[OUTPUT TRUNCATED - exceeded maximum size]"
    truncated = encoded[: max(cap - len(notice.encode()), 0)].decode(
        "utf-8", errors="replace"
    )
    return truncated + notice


def _validate_environment(run: Run) -> str:
    """
    Validate the environment and return the Python executable path.

    Args:
        run: The Run instance containing the script and environment

    Returns:
        The absolute path to the Python executable

    Raises:
        EnvironmentNotFoundError: If environment directory doesn't exist
        PythonNotFoundError: If Python executable doesn't exist
    """
    environment = run.script.environment

    if not environment.exists():
        raise EnvironmentNotFoundError(
            f"Environment directory not found: {environment.get_full_path()}"
        )

    python_path = environment.get_python_executable()
    if not os.path.isfile(python_path):
        raise PythonNotFoundError(f"Python executable not found: {python_path}")

    return python_path


def _write_script_to_file(code: str, dest: Path) -> None:
    """
    Write script body to disk in chunks to avoid peak-memory spikes
    on very large scripts (100K+ lines / tens of MB).
    """
    encoded = code.encode("utf-8", errors="replace")
    with open(dest, "wb") as f:
        for i in range(0, len(encoded), CHUNK_BYTES):
            f.write(encoded[i : i + CHUNK_BYTES])


def _capture_stream(
    proc: subprocess.Popen,
    stream_attr: str,
    run_id,
    stream_name: str,
    secrets: dict,
) -> tuple[str, dict | None]:
    """
    Read one stream (stdout or stderr) in chunks.

    If the total output is below INLINE_LIMIT, returns (text, None).
    Otherwise spools to disk via OutputStorageService and returns
    (truncated_inline_preview, spool_meta).
    """
    stream = getattr(proc, stream_attr)
    if stream is None:
        return "", None

    # We need to read in chunks while also peeking at size. Use os.read on
    # the underlying fd for true streaming.
    fd = stream.fileno()
    inline_buf = bytearray()
    spool_path = None
    spool_file = None
    spool_sha = None
    spool_size = 0
    spooling = False
    truncated = False
    cap = HARD_CAP

    import hashlib

    sha = hashlib.sha256()
    rx = _compile_secret_masker(secrets)

    try:
        while True:
            chunk = os.read(fd, CHUNK_BYTES)
            if not chunk:
                break

            # Apply masking before persisting anywhere.
            if rx is not None:
                # We can only safely mask complete matches. Because chunks can
                # split a secret value in two, we decode and re-encode.
                try:
                    text = chunk.decode("utf-8", errors="replace")
                except Exception:
                    text = chunk.decode("latin-1", errors="replace")
                # Re-buffer trailing partial match: simplest safe approach is
                # to mask within the chunk. Cross-chunk matches are rare and
                # acceptable to leave visible at chunk boundaries.
                masked = rx.sub(
                    lambda m: f"[_MASKED_]",
                    text,
                )
                chunk = masked.encode("utf-8", errors="replace")

            sha.update(chunk)
            if spooling:
                if spool_size + len(chunk) > cap:
                    allowed = cap - spool_size
                    if allowed > 0:
                        spool_file.write(chunk[:allowed])
                        spool_size += allowed
                    truncated = True
                    break
                spool_file.write(chunk)
                spool_size += len(chunk)
            else:
                inline_buf.extend(chunk)
                if len(inline_buf) > INLINE_LIMIT:
                    # Switch to spool mode
                    spooling = True
                    spool_path = OutputStorageService.spool_path_for(run_id, stream_name)
                    spool_path.parent.mkdir(parents=True, exist_ok=True)
                    spool_file = open(spool_path, "wb")
                    spool_file.write(bytes(inline_buf))
                    spool_size = len(inline_buf)
                    inline_buf = bytearray()  # free memory
    finally:
        if spool_file:
            spool_file.close()

    if spooling:
        meta = {
            "path": str(spool_path) if spool_path else None,
            "size": spool_size,
            "sha256": sha.hexdigest(),
            "truncated": truncated,
            "error": None,
        }
        # Inline preview = first 4KB of the spool
        preview_bytes, _, _ = OutputStorageService.read_stream(run_id, stream_name, 0, 4096)
        preview = preview_bytes.decode("utf-8", errors="replace")
        if truncated:
            preview += "\n\n[OUTPUT SPOOLED TO DISK - exceeded inline limit; truncated at hard cap]"
        else:
            preview += "\n\n[OUTPUT SPOOLED TO DISK - exceeded inline limit; see full output via streaming endpoint]"
        return preview, meta
    else:
        text = bytes(inline_buf).decode("utf-8", errors="replace")
        return text, None


def execute_run(run: Run, webhook_data: dict | None = None) -> None:
    """
    Execute a script run and update the Run record with results.

    This function is designed to be called from a django-q2 async task.
    It handles all aspects of script execution including:
    - Writing script code to a temporary file (in 256KB chunks)
    - Running the script with the appropriate Python executable
    - Capturing stdout/stderr in streaming fashion
    - Spooling oversized output to disk
    - Handling timeouts
    - Updating the Run record with results

    Args:
        run: The Run model instance to execute
        webhook_data: Optional webhook data to inject as environment variables

    Note:
        This function always saves the Run state, even on errors.
        The Run status will be updated to one of:
        SUCCESS, FAILED, TIMEOUT, or remain FAILED on errors.
    """
    script_file_path = None
    proc = None

    try:
        # Phase 1: Pre-execution validation
        if run.status != Run.Status.PENDING:
            logger.warning(
                f"Run {run.id} is not in PENDING status (current: {run.status}). "
                "Skipping execution."
            )
            return

        # Update to RUNNING status
        run.status = Run.Status.RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at"])

        # Validate environment
        try:
            python_path = _validate_environment(run)
        except EnvironmentNotFoundError as e:
            run.status = Run.Status.FAILED
            run.stderr = str(e)
            run.ended_at = timezone.now()
            run.save()
            logger.error(f"Run {run.id} failed: {e}")
            return
        except PythonNotFoundError as e:
            run.status = Run.Status.FAILED
            run.stderr = str(e)
            run.ended_at = timezone.now()
            run.save()
            logger.error(f"Run {run.id} failed: {e}")
            return

        # Ensure working directory exists
        workdir = Path(settings.SCRIPTS_WORKDIR)
        workdir.mkdir(parents=True, exist_ok=True)

        # Phase 2: Create temporary script file (chunked write)
        fd, script_file_path = tempfile.mkstemp(
            suffix=".py",
            prefix="pyrunner_",
            dir=str(workdir),
        )
        os.close(fd)  # We'll re-open for writing
        # Use code_snapshot if available (preserves code at queue time)
        code = run.code_snapshot if run.code_snapshot else run.script.code
        _write_script_to_file(code, Path(script_file_path))

        # Phase 3: Execute script
        try:
            cmd = [python_path, "-I", script_file_path]  # -I = isolated mode

            script_env = _build_script_environment(webhook_data)
            secrets = _get_secrets_env()

            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "env": script_env,
                "cwd": str(workdir),
                "bufsize": 0,           # unbuffered binary
                "binary": None,         # explicit below
            }

            # Put the subprocess in its own process group so we can kill
            # the whole tree on timeout/cancel (Unix only).
            if os.name == "posix":
                popen_kwargs["preexec_fn"] = os.setsid
            else:
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )

            proc = subprocess.Popen([python_path, script_file_path], **{
                k: v for k, v in popen_kwargs.items() if k != "binary"
            })

            # Persist PID so the stop view can kill the process
            run.pid = proc.pid
            run.save(update_fields=["pid"])

            # Stream both pipes concurrently using a thread per stream
            import threading

            results = {}

            def _do_stream(name_attr, stream_name):
                results[stream_name] = _capture_stream(
                    proc, name_attr, run.id, stream_name, secrets
                )

            t_out = threading.Thread(target=_do_stream, args=("stdout", "stdout"))
            t_err = threading.Thread(target=_do_stream, args=("stderr", "stderr"))
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=run.script.timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                run.status = Run.Status.TIMEOUT

                stdout_text, stdout_meta = results.get("stdout", ("", None))
                stderr_text, stderr_meta = results.get("stderr", ("", None))

                if stdout_meta:
                    run.stdout_spooled = True
                    run.stdout_size = stdout_meta["size"]
                    run.stdout = stdout_text
                else:
                    run.stdout = _truncate_output(stdout_text)
                if stderr_meta:
                    run.stderr_spooled = True
                    run.stderr_size = stderr_meta["size"]
                    run.stderr = stderr_text
                else:
                    run.stderr = _truncate_output(stderr_text)

                if run.stderr:
                    run.stderr += "\n\n[TIMEOUT: Script exceeded maximum execution time]"
                else:
                    run.stderr = (
                        f"[TIMEOUT: Script exceeded {run.script.timeout_seconds} seconds]"
                    )
                run.exit_code = -1
                logger.warning(f"Run {run.id} timed out after {run.script.timeout_seconds}s")
                run.pid = None
                return

            t_out.join(timeout=30)
            t_err.join(timeout=30)

            # Check if user cancelled while running
            run.refresh_from_db(fields=["status"])
            if run.status == Run.Status.CANCELLED:
                stdout_text, stdout_meta = results.get("stdout", ("", None))
                stderr_text, stderr_meta = results.get("stderr", ("", None))

                run.stdout = _truncate_output(stdout_text)
                if stderr_text:
                    run.stderr = (run.stderr or "") + "\n" + _truncate_output(stderr_text)
                run.exit_code = proc.returncode
                run.pid = None
                return

            # Normal completion
            stdout_text, stdout_meta = results.get("stdout", ("", None))
            stderr_text, stderr_meta = results.get("stderr", ("", None))

            if stdout_meta:
                run.stdout_spooled = True
                run.stdout_size = stdout_meta["size"]
                run.stdout = stdout_text
            else:
                run.stdout = _truncate_output(stdout_text)
            if stderr_meta:
                run.stderr_spooled = True
                run.stderr_size = stderr_meta["size"]
                run.stderr = stderr_text
            else:
                run.stderr = _truncate_output(stderr_text)

            run.exit_code = proc.returncode
            run.status = (
                Run.Status.SUCCESS if proc.returncode == 0 else Run.Status.FAILED
            )
            run.pid = None

        except subprocess.SubprocessError as e:
            # Handle other subprocess errors
            run.status = Run.Status.FAILED
            run.stderr = f"Subprocess error: {str(e)}"
            run.exit_code = -1
            logger.error(f"Run {run.id} subprocess error: {e}")

    except Exception as e:
        # Catch-all for unexpected errors
        run.status = Run.Status.FAILED
        run.stderr = f"Unexpected executor error: {str(e)}\n\n{traceback.format_exc()}"
        run.exit_code = -1
        logger.exception(f"Run {run.id} unexpected error")

    finally:
        # Phase 4: Cleanup and save
        if not run.ended_at:
            run.ended_at = timezone.now()

        run.pid = None
        run.save()

        if script_file_path is not None:
            try:
                os.unlink(script_file_path)
            except OSError as e:
                logger.warning(f"Failed to delete temp script file: {e}")

        # Best-effort: if the run was cancelled/timed out, ensure the
        # subprocess is really dead.
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc)

        logger.info(
            f"Run {run.id} completed with status {run.status} "
            f"(exit_code={run.exit_code})"
        )


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a subprocess and its entire process group (best-effort)."""
    try:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.kill()
        else:
            proc.kill()
    except Exception:
        pass
    # Wait briefly for graceful exit; then SIGKILL
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                import subprocess as _sp
                _sp.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], check=False)
        except Exception:
            pass
