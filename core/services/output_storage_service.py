"""
Output storage service for PyRunner.

Handles spooling of oversized run output (stdout/stderr) to disk so the
database doesn't become a multi-GB blob store. Designed for scripts that
produce megabytes or gigabytes of output.

Two-tier strategy:
  - Inline:  output stays in run.stdout / run.stderr (TextField)
  - Spooled: output is written to a file under OUTPUT_SPOOL_DIR; the DB
             row stores only a path + size + sha256 prefix for dedup.

The executor decides which tier to use based on OUTPUT_SPOOL_THRESHOLD.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import IO, Iterable

from django.conf import settings

logger = logging.getLogger(__name__)


class OutputStorageService:
    """Manages on-disk spool files for oversized run output."""

    SPOOL_DIR: Path = settings.OUTPUT_SPOOL_DIR

    @classmethod
    def _ensure_dir(cls) -> None:
        cls.SPOOL_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def spool_path_for(cls, run_id, stream: str) -> Path:
        """Return the canonical spool path for a (run_id, stream) pair."""
        safe_run = str(run_id).replace("-", "_")
        return cls.SPOOL_DIR / f"run_{safe_run}.{stream}.log"

    @classmethod
    def spool_stream(
        cls,
        run_id,
        stream: str,
        source: IO[bytes] | Iterable[bytes],
        max_bytes: int | None = None,
    ) -> dict:
        """
        Spool a binary stream to disk in chunks.

        Args:
            run_id:   Run instance ID (UUID or str)
            stream:   "stdout" | "stderr"
            source:   file-like object (binary) OR iterable of bytes chunks
            max_bytes: hard cap; further bytes are discarded and `truncated=True`

        Returns:
            dict with keys: path, size, sha256, truncated, max_reached
        """
        cls._ensure_dir()
        path = cls.spool_path_for(run_id, stream)
        cap = max_bytes or settings.MAX_OUTPUT_SPOOL_BYTES
        sha = hashlib.sha256()
        written = 0
        truncated = False

        try:
            with open(path, "wb") as out:
                if hasattr(source, "read"):
                    # File-like: read in 256KB chunks
                    while True:
                        chunk = source.read(262_144)
                        if not chunk:
                            break
                        if written + len(chunk) > cap:
                            allowed = cap - written
                            if allowed > 0:
                                out.write(chunk[:allowed])
                                sha.update(chunk[:allowed])
                                written += allowed
                            truncated = True
                            break
                        out.write(chunk)
                        sha.update(chunk)
                        written += len(chunk)
                else:
                    # Iterable of bytes
                    for chunk in source:
                        if not chunk:
                            continue
                        if written + len(chunk) > cap:
                            allowed = cap - written
                            if allowed > 0:
                                out.write(chunk[:allowed])
                                sha.update(chunk[:allowed])
                                written += allowed
                            truncated = True
                            break
                        out.write(chunk)
                        sha.update(chunk)
                        written += len(chunk)
        except OSError as e:
            logger.error(f"Failed to spool {stream} for run {run_id}: {e}")
            return {
                "path": None,
                "size": 0,
                "sha256": "",
                "truncated": False,
                "error": str(e),
            }

        return {
            "path": str(path),
            "size": written,
            "sha256": sha.hexdigest(),
            "truncated": truncated,
            "error": None,
        }

    @classmethod
    def write_text(cls, run_id, stream: str, text: str, max_bytes: int | None = None) -> dict:
        """Spool a Python `str` (UTF-8) — convenience wrapper."""
        cap = max_bytes or settings.MAX_OUTPUT_SPOOL_BYTES
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > cap:
            encoded = encoded[:cap]
            truncated = True
        else:
            truncated = False

        cls._ensure_dir()
        path = cls.spool_path_for(run_id, stream)
        sha = hashlib.sha256(encoded).hexdigest()
        try:
            with open(path, "wb") as out:
                out.write(encoded)
        except OSError as e:
            logger.error(f"Failed to spool {stream} for run {run_id}: {e}")
            return {"path": None, "size": 0, "sha256": "", "truncated": False, "error": str(e)}
        return {
            "path": str(path),
            "size": len(encoded),
            "sha256": sha,
            "truncated": truncated,
            "error": None,
        }

    @classmethod
    def read_stream(
        cls,
        run_id,
        stream: str,
        start: int = 0,
        end: int | None = None,
    ) -> tuple[bytes, int, bool]:
        """
        Read a slice of a spooled stream.

        Returns (data_bytes, total_size, exists).
        """
        path = cls.spool_path_for(run_id, stream)
        if not path.exists():
            return b"", 0, False
        total = path.stat().st_size
        with open(path, "rb") as f:
            if start:
                f.seek(start)
            if end is None:
                data = f.read()
            else:
                data = f.read(max(0, end - start))
        return data, total, True

    @classmethod
    def delete_for_run(cls, run_id) -> int:
        """Delete any spooled files for a run; returns count removed."""
        safe_run = str(run_id).replace("-", "_")
        prefix = f"run_{safe_run}."
        removed = 0
        if not cls.SPOOL_DIR.exists():
            return 0
        for entry in cls.SPOOL_DIR.glob(f"{prefix}*"):
            try:
                entry.unlink()
                removed += 1
            except OSError as e:
                logger.warning(f"Could not delete spool {entry}: {e}")
        return removed

    @classmethod
    def cleanup_orphans(cls, valid_run_ids: set) -> int:
        """Remove spool files whose run_id is no longer in `valid_run_ids`."""
        if not cls.SPOOL_DIR.exists():
            return 0
        removed = 0
        for entry in cls.SPOOL_DIR.glob("run_*.log"):
            # Extract run_id from filename: run_<safe_id>.<stream>.log
            try:
                name = entry.stem  # run_<safe_id>.<stream>
                safe_id = name.split(".", 1)[0][len("run_"):]
                # Re-hyphenate
                run_uuid = safe_id.replace("_", "-")
                # Try as UUID, fall back to direct compare
                try:
                    from uuid import UUID
                    UUID(run_uuid)
                    key = run_uuid
                except ValueError:
                    key = safe_id
                if key not in valid_run_ids:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except Exception as e:
                logger.warning(f"Could not process spool {entry}: {e}")
        return removed
