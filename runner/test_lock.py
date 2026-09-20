"""Behavior tests for the pipeline pid lock (``runner/lock.py``)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lock import LockError, RunLock


def test_acquire_publishes_complete_record_and_release_removes_it(
    tmp_path: Path,
) -> None:
    with RunLock(tmp_path, mode="probe"):
        raw = (tmp_path / ".run.lock").read_text(encoding="utf-8")
        rec = json.loads(raw)
        assert rec["pid"] == os.getpid()
        assert rec["mode"] == "probe"
        assert "started_at" in rec
    assert not (tmp_path / ".run.lock").exists()


def test_second_acquirer_is_refused_while_holder_live(tmp_path: Path) -> None:
    first = RunLock(tmp_path, mode="batch")
    first.acquire()
    try:
        with pytest.raises(LockError, match="refusing to acquire"):
            RunLock(tmp_path, mode="batch").acquire()
    finally:
        first.release()
    # After release the lock is claimable again.
    with RunLock(tmp_path, mode="batch"):
        pass


def test_reclaims_stale_lock_with_dead_pid(tmp_path: Path, capsys) -> None:
    dead_pid = _dead_pid()
    assert not RunLock._pid_alive(dead_pid)  # keep the fixture honest
    (tmp_path / ".run.lock").write_text(
        json.dumps({"pid": dead_pid, "started_at": "earlier",
                    "mode": "batch"}),
        encoding="utf-8",
    )
    lock = RunLock(tmp_path, mode="status")
    lock.acquire()
    try:
        rec = json.loads((tmp_path / ".run.lock").read_text(encoding="utf-8"))
        assert rec["pid"] == os.getpid()
        assert rec["mode"] == "status"
    finally:
        lock.release()
    assert "reclaiming stale lock" in capsys.readouterr().err
    assert not list(tmp_path.glob(".run.lock.stale-*"))  # no quarantine litter


def test_reclaims_corrupt_lock_file(tmp_path: Path) -> None:
    (tmp_path / ".run.lock").write_text("{not json!", encoding="utf-8")
    lock = RunLock(tmp_path)
    lock.acquire()
    try:
        rec = json.loads((tmp_path / ".run.lock").read_text(encoding="utf-8"))
        assert rec["pid"] == os.getpid()
    finally:
        lock.release()


def test_reclaims_non_dict_json_lock_file(tmp_path: Path) -> None:
    # Valid JSON that is not an object: no live owner is provable, so it is
    # treated as stale/corrupt and reclaimed (never a crash on `.get`).
    (tmp_path / ".run.lock").write_text("[]", encoding="utf-8")
    lock = RunLock(tmp_path)
    lock.acquire()
    try:
        rec = json.loads((tmp_path / ".run.lock").read_text(encoding="utf-8"))
        assert rec["pid"] == os.getpid()
    finally:
        lock.release()


def test_reclaim_stale_leaves_live_lock_untouched(tmp_path: Path) -> None:
    # A lock whose pid reads as live must never be seized by a stale-cleanup
    # pass — the pre-seize re-check is the guard against moving a live record.
    (tmp_path / ".run.lock").write_text(
        json.dumps({"pid": os.getpid(), "started_at": "now",
                    "mode": "batch"}),
        encoding="utf-8",
    )
    lock = RunLock(tmp_path)
    lock._reclaim_stale()  # white-box: exercise the risky branch directly
    rec = json.loads((tmp_path / ".run.lock").read_text(encoding="utf-8"))
    assert rec["pid"] == os.getpid()  # untouched
    assert not list(tmp_path.glob(".run.lock.stale-*"))
    with pytest.raises(LockError, match="refusing to acquire"):
        lock.acquire()  # arbitration still observes the live holder


def test_reclaim_restores_live_lock_seized_in_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The single-syscall race: the pre-seize check sees nothing (patched),
    # the seize renames a *live* holder's lock away, and the guarded restore
    # must put it back atomically.
    (tmp_path / ".run.lock").write_text(
        json.dumps({"pid": os.getpid(), "started_at": "now",
                    "mode": "batch"}),
        encoding="utf-8",
    )
    lock = RunLock(tmp_path)
    monkeypatch.setattr(lock, "_read", lambda: None)  # blind pre-check
    lock._reclaim_stale()
    rec = json.loads((tmp_path / ".run.lock").read_text(encoding="utf-8"))
    assert rec["pid"] == os.getpid()  # restored, not destroyed
    assert not list(tmp_path.glob(".run.lock.stale-*"))  # quarantine cleaned


def test_reclaim_never_clobbers_fresh_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # If the path is re-claimed between the seize and the restore, the
    # guarded os.link must fail cleanly: the fresh claim survives untouched
    # (os.rename would have silently replaced it — the original bug).
    (tmp_path / ".run.lock").write_text(
        json.dumps({"pid": os.getpid(), "started_at": "now",
                    "mode": "batch"}),
        encoding="utf-8",
    )
    lock = RunLock(tmp_path)
    monkeypatch.setattr(lock, "_read", lambda: None)  # blind pre-check

    real_link = os.link

    def _link_with_rival(src: str, dst: str) -> None:
        (tmp_path / ".run.lock").write_text(
            json.dumps({"pid": os.getpid(), "started_at": "fresh-claim",
                        "mode": "probe"}),
            encoding="utf-8",
        )
        real_link(src, dst)  # raises FileExistsError: dst occupied

    monkeypatch.setattr(os, "link", _link_with_rival)
    lock._reclaim_stale()
    assert "re-claimed while restoring" in capsys.readouterr().err
    # The fresh claim at the path is intact — the restore did not clobber it.
    rec = json.loads((tmp_path / ".run.lock").read_text(encoding="utf-8"))
    assert rec["started_at"] == "fresh-claim"
    # The seized live record stays quarantined for forensics.
    quarantines = list(tmp_path.glob(".run.lock.stale-*"))
    assert len(quarantines) == 1


def test_lock_file_consumable_by_state_report(tmp_path: Path) -> None:
    from run_state import live_lock_pid

    lock = RunLock(tmp_path, mode="batch")
    lock.acquire()
    try:
        assert live_lock_pid(tmp_path) == os.getpid()
    finally:
        lock.release()
    assert live_lock_pid(tmp_path) is None


def test_concurrent_acquirers_have_single_winner(tmp_path: Path) -> None:
    helper = textwrap.dedent(
        """\
        import sys
        from pathlib import Path

        from lock import LockError, RunLock

        try:
            RunLock(Path(sys.argv[1]), mode="probe").acquire()
            print("WON", flush=True)
            sys.stdin.read()  # hold the lock until the parent closes stdin
        except LockError:
            print("LOST", flush=True)
        """
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", helper, str(tmp_path)],
            cwd=str(Path(__file__).parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    # The winner stays alive (blocked on stdin) while every rival attempts,
    # so reclaim-of-stale cannot interfere: only one may hold the lock.
    outcomes = [proc.stdout.readline().strip() for proc in procs]
    for proc in procs:
        proc.stdin.close()
    for proc in procs:
        proc.wait()
    assert outcomes.count("WON") == 1
    # No rival may have crashed with a traceback instead of a clean LockError.
    errors = [proc.stderr.read() for proc in procs]
    assert all(err == "" for err in errors), errors
    # The winner's record is intact and never truncated mid-claim.
    rec = json.loads((tmp_path / ".run.lock").read_text(encoding="utf-8"))
    assert rec["pid"] > 0
    assert rec["mode"] == "probe"


def _dead_pid() -> int:
    """Return a pid that is guaranteed not to be running."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _stderr = proc.communicate()
    return int(stdout.strip())