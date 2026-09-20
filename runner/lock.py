"""PID lock file for pipeline exclusivity.

Acquires ``<docs>/mcp/.run.lock`` containing ``{pid, started_at, mode}``.
A live-pid check (``os.kill(pid, 0)``) prevents two pipeline runs from
clobbering each other; a stale lock (dead pid) is reclaimed with a warning.
Only a positive integer pid that provably names a running process counts
as live; an invalid pid (0, negative, oversized, bool, non-int, or missing)
is treated as stale/invalid and reclaimed, so a corrupt record can never
block the current process.

Acquisition is atomic: a claim is a single ``os.link`` step, so the lock
path first appears with a complete record in the same atomic operation
that decides a claim — two claims can never both succeed on the path. A
stale or unreadable lock is seized with an atomic rename to a private
quarantine name and only removed after the seized record is checked; a
live holder's seized record is restored with a guarded link, so a
reclaimer can never clobber a claim that a rival has just placed. A live
pid makes ``acquire`` raise ``LockError``.

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
import uuid
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

        # A claim (os.link) is a single atomic step, so the lock path can
        # never be won by two claims at once. On FileExistsError the holder
        # is inspected: a provably live pid is a hard LockError; a stale or
        # unreadable lock is seized with an atomic rename and the claim
        # retried. Two passes are enough: a retry claims the lock, observes
        # the (new) holder's live pid, or re-reclaims a stale/corrupt lock;
        # if the path is still contended afterwards we fail closed rather
        # than risk a double holder.
        for _ in range(2):
            try:
                try:
                    self._claim()
                except FileExistsError:
                    existing = self._read()
                    if existing is not None:
                        owner_pid = existing.get("pid")
                        # A positive integer pid with a genuinely running
                        # process is a hard LockError; anything else (dead
                        # process, pid 0/negative/non-int/missing) is a
                        # stale or invalid lock the current process may take
                        # over.
                        if self._pid_alive(owner_pid):
                            raise LockError(
                                f"lock held by pid {owner_pid} "
                                f"(started {existing.get('started_at', '?')}, "
                                f"mode={existing.get('mode', '?')}); "
                                f"refusing to acquire"
                            )
                        # Stale or invalid lock — no live owner to defer to.
                        print(
                            f"warn: reclaiming stale lock "
                            f"(pid {owner_pid} not alive)",
                            file=sys.stderr,
                        )
                    self._reclaim_stale()
                    continue
                self._acquired = True
                return
            except OSError as exc:
                # mkstemp/link/rename failed at the filesystem level (e.g.
                # ENOSPC, EACCES): surface a clean LockError rather than a
                # raw traceback (run_stage handles only LockError).
                raise LockError(
                    f"cannot acquire {self._lock_path.name}: {exc}"
                ) from exc

        raise LockError(
            f"could not acquire {self._lock_path.name}: "
            f"contended by another process"
        )

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
        return self._read_at(self._lock_path)

    def _read_at(self, path: Path) -> dict | None:
        """Read a lock record file; return parsed JSON or ``None`` (missing,
        unreadable, or a non-dict payload)."""
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _reclaim_stale(self) -> None:
        """Atomically seize and remove the stale lock at the lock path.

        The current record is re-checked first: a lock whose pid reads as
        *live* is never moved — the claim arbitration decides against it.
        Only a stale/corrupt record is seized, by renaming the lock path to
        a unique quarantine name. If the seized record turns out to be a
        live holder's anyway (a rival claimed it in the single-syscall
        window between the re-check and the rename), it is restored with a
        guarded link — ``os.link`` fails if the path was re-claimed in the
        gap, so a rival's fresh claim is never clobbered.
        """
        existing = self._read()
        if existing is not None:
            pid = existing.get("pid")
            if self._pid_alive(pid):
                return  # a live holder appeared; do not move its record

        quarantine = self._lock_path.with_name(
            f"{self._lock_path.name}.stale-"
            f"{os.getpid()}-{uuid.uuid4().hex}"
        )
        try:
            os.rename(self._lock_path, quarantine)
        except FileNotFoundError:
            return  # another reclaimer already removed it

        data = self._read_at(quarantine)
        if data is not None:
            pid = data.get("pid")
            if self._pid_alive(pid):
                # We seized a live lock in the window between the re-check
                # above and the rename.
                try:
                    os.link(quarantine, self._lock_path)
                except FileExistsError:
                    # The path was re-claimed in the gap. The fresh claim is
                    # left untouched — never clobbered — and the prior live
                    # holder's record stays under the quarantine name for
                    # forensics. Residual corner (no purely rename-based
                    # protocol can prevent it once a live record has been
                    # physically seized): that prior holder still believes it
                    # holds the lock, so two _acquired processes can exist;
                    # the warning names the stranded holder's pid.
                    print(
                        f"warn: lock path re-claimed while restoring a live "
                        f"lock (pid {pid}) during stale cleanup; prior "
                        f"holder's record left at {quarantine.name}",
                        file=sys.stderr,
                    )
                    return
                os.unlink(quarantine)
                return
        try:
            quarantine.unlink()
        except OSError:
            pass

    def _claim(self) -> None:
        """Atomically publish our lock record; raise ``FileExistsError`` if
        the lock path is already taken.

        The record is written to a unique temp file in the lock's directory
        and then hard-linked into place. The lock path therefore first
        appears with its *complete* record in the same atomic step that
        decides the winner — there is never an empty or half-written lock
        that a competitor could mistake for a stale one.
        """
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
            try:
                os.link(tmp_path, self._lock_path)
            except FileExistsError:
                raise
            except OSError as exc:
                # Hard links unsupported (or refused): without them we cannot
                # claim atomically, so fail loudly instead of regressing to a
                # racy read-then-write.
                raise LockError(
                    f"cannot create atomic lock {self._lock_path.name}: "
                    f"{exc}"
                ) from exc
        finally:
            # On success tmp_path and _lock_path are the same inode; drop the
            # temp name. On failure the temp file must not be left behind.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _pid_alive(pid: object) -> bool:
        """Return True only if *pid* is a positive integer naming a running
        process.

        Invalid pids — non-int values, bools, zero, negatives, and integers
        past the platform's pid range — are reported dead rather than
        probed. Probing them would be unsafe or meaningless: ``os.kill(0,
        0)`` signals the caller's own process group, ``os.kill(-1, 0)``
        broadcasts to every signalable process, a bool degenerates to pid
        0/1, a non-int pid raises TypeError, and an oversized pid raises
        OverflowError. Treating them as dead lets lock arbitration classify
        the record as stale/invalid and reclaim it instead of minting a live
        holder.
        """
        if type(pid) is not int or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we lack signal permission — still alive.
            return True
        except (OverflowError, ValueError):
            # Outside the platform's pid_t range — cannot name a real
            # process, so it is invalid rather than alive.
            return False
