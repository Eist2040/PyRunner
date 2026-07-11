"""
Script executor module for PyRunner.

This module handles the execution of Python scripts in isolated environments.
It is designed to be called from django-q2 async tasks.

Design notes (post 100K-line support + secrets hotfix):
  * Script body is written to the temp file in 256KB chunks (avoids peak
    memory spikes on 50MB scripts).
  * Subprocess is started in its own process group (Unix: setsid) so
    SIGTERM/SIGKILL reliably kills the whole child tree.
  * Output capture uses the proven `proc.communicate()` pattern — it
    buffers stdout/stderr in memory, but we cap with _truncate_output
    and spool oversized outputs to disk AFTER capture (simpler and
    safer than streaming reads, which had UTF-8 split bugs).
  * Secret masking uses a single compiled regex (one pass over output)
    instead of `str.replace` per secret — O(n) instead of O(n*m).
  * Secrets are loaded with `.only("key", "encrypted_value")` — the
    Secret model has NO `salt` field (Fernet doesn't use one), so
    adding it to `.only()` raises FieldError and silently breaks all
    secret injection.
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

# Chunk size for streaming the script body to disk.
CHUNK_BYTES = 262_144  # 256 KB

# Threshold above which captured output is spooled to disk instead of
# stored inline in the DB. Below this, we keep it in run.stdout/stderr.
SPOOL_THRESHOLD = getattr(settings, "OUTPUT_SPOOL_THRESHOLD", 4 * 1024 * 1024)


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
        # IMPORTANT: The Secret model only has `key` and `encrypted_value`
        # fields relevant to decryption — there is NO `salt` field (Fernet
        # doesn't use one). Do NOT add fields here without checking the
        # model, or Django will raise FieldError and silently block ALL
        # secret injection (which makes every secret-dependent script fail).
        for secret in Secret.objects.all().only("key", "encrypted_value"):
            try:
                secrets_env[secret.key] = secret.get_decrypted_value()
            except Exception as e:
                logger.error(f"Failed to decrypt secret {secret.key}: {e}")
    except Exception as e:
        # Use logger.exception so the traceback is visible — a bare
        # logger.error here previously swallowed the FieldError silently.
        logger.exception(f"Failed to load secrets: {e}")

    if secrets_env:
        logger.debug(f"Loaded {len(secrets_env)} secret(s) for script execution")
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
    for k, v in sorted(
        secrets.items(),
        key=lambda kv: len(kv[1]) if kv[1] else 0,
        reverse=True,
    ):
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
    Truncate output if it exceeds max_bytes.

    Args:
        output: The output string to potentially truncate
        max_bytes: Maximum size in bytes (default from settings.MAX_OUTPUT_BYTES)

    Returns:
        The original or truncated output with notice
    """
    if not output:
        return output

    cap = max_bytes or getattr(settings, "MAX_OUTPUT_BYTES", 50 * 1024 * 1024)
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return output

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


def _spool_if_large(run: Run, stream_name: str, text: str, secrets: dict) -> None:
    """
    If `text` exceeds SPOOL_THRESHOLD, write it to disk via
    OutputStorageService and replace the inline field with a small preview.
    Otherwise, mask secrets and store inline.

    Mutates the run object's stdout/stderr + spool flag/size fields.
    """
    masked = _mask_secrets_in_output(text, secrets)
    encoded_len = len(masked.encode("utf-8", errors="replace"))

    if encoded_len <= SPOOL_THRESHOLD:
        # Inline path — just set the field
        setattr(run, stream_name, _truncate_output(masked))
        setattr(run, f"{stream_name}_spooled", False)
        setattr(run, f"{stream_name}_size", 0)
        return

    # Spool path — write to disk, keep only a small preview inline
    meta = OutputStorageService.write_text(run.id, stream_name, masked)
    if meta.get("path"):
        # Keep a 4KB preview so the run-detail UI shows something useful
        preview_bytes, _, _ = OutputStorageService.read_stream(
            run.id, stream_name, 0, 4096
        )
        preview = preview_bytes.decode("utf-8", errors="replace")
        if meta.get("truncated"):
            preview += (
                "\n\n[OUTPUT SPOOLED TO DISK - exceeded inline limit; "
                "truncated at hard cap]"
            )
        else:
            preview += (
                "\n\n[OUTPUT SPOOLED TO DISK - exceeded inline limit; "
                "use the streaming endpoint to view full output]"
            )
        setattr(run, stream_name, preview)
        setattr(run, f"{stream_name}_spooled", True)
        setattr(run, f"{stream_name}_size", meta["size"])
    else:
        # Spool failed — fall back to inline truncate so we don't lose
        # the output entirely
        logger.error(
            f"Failed to spool {stream_name} for run {run.id}: {meta.get('error')}"
        )
        setattr(run, stream_name, _truncate_output(masked))
        setattr(run, f"{stream_name}_spooled", False)
        setattr(run, f"{stream_name}_size", 0)


def execute_run(run: Run, webhook_data: dict | None = None) -> None:
    """
    Execute a script run and update the Run record with results.

    This function is designed to be called from a django-q2 async task.
    It handles all aspects of script execution including:
    - Writing script code to a temporary file (in 256KB chunks)
    - Running the script with the appropriate Python executable
    - Capturing stdout/stderr via communicate() (reliable, no deadlocks)
    - Spooling oversized output to disk AFTER capture
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

        # Phase 2: Create temporary script file (chunked write for big scripts)
        fd, script_file_path = tempfile.mkstemp(
            suffix=".py",
            prefix="pyrunner_",
            dir=str(workdir),
        )
        os.close(fd)  # We'll re-open for writing
        # Use code_snapshot if available (preserves code at queue time).
        # The new smart-snapshot logic may leave code_snapshot empty when
        # the script's code hasn't changed since the last run — in that
        # case we fall back to the current script.code.
        code = run.code_snapshot if run.code_snapshot else run.script.code
        _write_script_to_file(code, Path(script_file_path))

        # Phase 3: Execute script
        try:
            # Build subprocess arguments
            cmd = [python_path, script_file_path]

            # Build environment with secrets and webhook data injected.
            # We load secrets ONCE here and reuse for both env injection
            # and post-capture masking — saves a second DB+decrypt pass.
            secrets = _get_secrets_env()
            script_env = _build_script_environment(webhook_data)
            # _build_script_environment already merged secrets in, but it
            # calls _get_secrets_env() internally too. That's a tiny
            # duplicate cost we accept for clarity.

            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": script_env,
                "cwd": str(workdir),
                "bufsize": -1,  # default buffering — communicate() handles it
            }

            # Put the subprocess in its own process group so SIGTERM/SIGKILL
            # reliably kills the whole child tree (Unix). On Windows, hide
            # the console window and use a new process group.
            if os.name == "posix":
                popen_kwargs["preexec_fn"] = os.setsid
            else:
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )

            proc = subprocess.Popen(cmd, **popen_kwargs)

            # Persist PID so the stop view can kill the process
            run.pid = proc.pid
            run.save(update_fields=["pid"])

            try:
                stdout_data, stderr_data = proc.communicate(
                    timeout=run.script.timeout_seconds
                )
            except subprocess.TimeoutExpired:
                # Kill the whole process group, then drain the pipes
                _kill_process_tree(proc)
                try:
                    stdout_data, stderr_data = proc.communicate(timeout=10)
                except Exception:
                    stdout_data, stderr_data = "", ""

                run.status = Run.Status.TIMEOUT
                _spool_if_large(run, "stdout", stdout_data or "", secrets)
                _spool_if_large(run, "stderr", stderr_data or "", secrets)

                if run.stderr:
                    run.stderr += "\n\n[TIMEOUT: Script exceeded maximum execution time]"
                else:
                    run.stderr = (
                        f"[TIMEOUT: Script exceeded {run.script.timeout_seconds} seconds]"
                    )
                run.exit_code = -1
                logger.warning(
                    f"Run {run.id} timed out after {run.script.timeout_seconds}s"
                )
                run.pid = None
                return

            # Check if user cancelled while running
            run.refresh_from_db(fields=["status"])
            if run.status == Run.Status.CANCELLED:
                # Process already killed by stop view — preserve whatever
                # output we captured and add a note.
                _spool_if_large(run, "stdout", stdout_data or "", secrets)
                if stderr_data:
                    run.stderr = (run.stderr or "") + "\n"
                _spool_if_large(run, "stderr", stderr_data or "", secrets)
                run.exit_code = proc.returncode
                run.pid = None
                return

            # Normal completion — spool if large, mask secrets, store.
            _spool_if_large(run, "stdout", stdout_data or "", secrets)
            _spool_if_large(run, "stderr", stderr_data or "", secrets)

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

        # Cleanup temporary file
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
