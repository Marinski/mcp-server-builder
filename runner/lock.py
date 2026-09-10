"""PID lock file for pipeline exclusivity.

Acquires ``<docs>/mcp/.run.lock`` containing ``{pid, started_at, mode}``.
A live-pid check (``os.kill(pid, 0)``) prevents two pipeline runs from
clobbering each other; a stale lock (dead pid) is reclaimed with a warning.

Usage::

    with RunLock(docs_dir, mode="batch") as lock:
        # … pipeline runs here …
        pass  # lock released automatically on any exit path

Spec: §8.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


class LockError(Exception):
    """Raised when the lock cannot be acquired."""


class RunLock:
    """PID-based lock guarding ``<docs>/mcp/.run.lock``.

    Parameters
    ----------
    docs_dir:
        The ``--docs`` directory (e.g. ``/path/to/repo/docs/mcp``).
    mode:
        Caller-supplied label stored in the lock (e.g. ``"batch"``,
        ``"status"``).  Informative only — no behavioral difference.

    Raises
    ------
    LockError
        If the lock file exists and is held by a **live** process.
    """

    def __init__(self, docs_dir: Path, *, mode: str = "default") -> None:
        self._lock_path: Path = docs_dir / ".run.lock"
        self._mode: str = mode
        self._acquired: bool = False

    # -- public API ---------------------------------------------------------

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()

    def acquire(self) -> None:
        """Try to acquire the lock; raise ``LockError`` if another live
        process holds it."""
        if self._acquired:
            return

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

        existing = self._read()

        if existing is not None:
            owner_pid = existing.get("pid")
            if owner_pid is not None and self._pid_alive(owner_pid):
                raise LockError(
                    f"lock held by pid {owner_pid} "
                    f"(started {existing.get('started_at', '?')}, "
                    f"mode={existing.get('mode', '?')}); "
                    f"refusing to acquire"
                )
            # Stale lock — the owning process is gone.
            print(
                f"warn: reclaiming stale lock "
                f"(dead pid {owner_pid})",
                file=sys.stderr,
            )

        self._write()
        self._acquired = True

    def release(self) -> None:
        """Remove the lock file if we own it."""
        if not self._acquired:
            return
        self._acquired = False
        try:
            if self._lock_path.exists():
                # Only remove if the pid in the file is ours.
                data = self._read()
                if data is not None and data.get("pid") == os.getpid():
                    self._lock_path.unlink()
        except OSError:
            pass

    # -- internals ----------------------------------------------------------

    def _read(self) -> dict | None:
        """Read the lock file; return parsed JSON or ``None``."""
        try:
            raw = self._lock_path.read_text(encoding="utf-8")
            return json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _write(self) -> None:
        """Atomically write the lock file via temp-file + rename."""
        record = {
            "pid": os.getpid(),
            "started_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "mode": self._mode,
        }
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._lock_path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2)
                fh.write("\n")
            os.replace(tmp_path, str(self._lock_path))
        except BaseException:
            # Clean up the temp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Return True if *pid* is a running process."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we lack signal permission — still alive.
            return True
