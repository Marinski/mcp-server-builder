"""Behavior tests for pipeline-lock coverage in the stage runner.

The runner makes a single ``RunLock`` acquisition in ``main()`` for the
whole execution block, so every mode — single-stage, auto-selected, and
``--batch`` — holds ``<docs>/.run.lock`` for the duration of the run and
releases it on every exit path. ``--status`` stays lock-free: its job is
to observe a run in progress.

These are process-level tests (like ``test_lock``'s concurrency test) so
they exercise the real ``main()`` dispatch rather than a unit seam.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from lock import RunLock

RUNNER_DIR = Path(__file__).parent
RUN_STAGE = RUNNER_DIR / "run_stage.py"
STOP_ENV = "FAKE_STAGE_STOP"


def _write_config(tmp_path: Path) -> Path:
    """A minimal models.yaml whose runner is an executable stub on disk.

    The stub's command is an absolute path, so ``shutil.which`` resolves it
    without touching PATH.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "defaults:\n"
        "  provider: testp\n"
        "  model: test-model\n"
        "providers:\n"
        "  testp:\n"
        "    runner: fake\n"
        "    auth_optional: true\n"
        "runners:\n"
        "  fake:\n"
        f"    cmd: {bin_dir / 'fake-runner'}\n"
        "    args: []\n",
        encoding="utf-8",
    )
    return cfg


def _write_blocking_runner(tmp_path: Path) -> Path:
    """A runner stub that blocks until the parent creates ``$FAKE_STAGE_STOP``.

    Lets the test observe the lock *while a stage is genuinely executing*
    instead of racing a run that finishes instantly.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "fake-runner"
    stub.write_text(
        "#!/bin/sh\n"
        "while [ ! -f \"$FAKE_STAGE_STOP\" ]; do sleep 0.05; done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _wait_for_lock(docs: Path, proc: subprocess.Popen | None = None,
                   timeout_s: float = 10.0) -> dict:
    """Poll for ``<docs>/.run.lock`` and return its parsed record."""
    lock_path = docs / ".run.lock"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if lock_path.is_file():
            return json.loads(lock_path.read_text(encoding="utf-8"))
        time.sleep(0.05)
    detail = ""
    if proc is not None and proc.poll() is not None:
        detail = f"; child exited {proc.poll()}: {proc.stderr.read()}"
    raise AssertionError(
        f"lock file never appeared at {lock_path}{detail}")


def test_single_stage_run_holds_lock_while_executing_and_releases(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    # The fake runner never touches the filesystem, so stage 1a's tracked
    # output must already be a valid artifact for postflight to pass.
    (docs / "01-instructions.md").write_text(
        "# Instructions\n\nReal content.\n", encoding="utf-8")
    stop = tmp_path / "stop"
    _write_blocking_runner(tmp_path)
    env = os.environ.copy()
    env[STOP_ENV] = str(stop)

    proc = subprocess.Popen(
        [sys.executable, str(RUN_STAGE), "1a",
         "--docs", str(docs), "--config", str(_write_config(tmp_path))],
        cwd=str(RUNNER_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        rec = _wait_for_lock(docs, proc)
        # The lock names the running stage process and the single mode.
        assert rec["pid"] == proc.pid
        assert rec["mode"] == "single"

        # A concurrent invocation, any mode, is refused while we hold it.
        rival = subprocess.run(
            [sys.executable, str(RUN_STAGE), "1a",
             "--docs", str(docs), "--config", str(_write_config(tmp_path)),
             "--dry-run"],
            cwd=str(RUNNER_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert rival.returncode != 0
        assert "refusing to acquire" in rival.stderr
        # The rival never got far enough to touch the manifest.
        assert not (docs / "run-manifest.jsonl").exists()

        # Release the stage: it finishes and records its attempt.
        stop.touch()
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        assert "ok: testp/test-model" in stderr
    finally:
        stop.touch()
        if proc.poll() is None:
            proc.kill()
        proc.communicate()

    # The lock is released on the success path.
    assert not (docs / ".run.lock").exists()
    records = [json.loads(l) for l in
               (docs / "run-manifest.jsonl").read_text().splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["ok"] is True


def test_single_stage_run_refused_while_another_process_holds_lock(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_blocking_runner(tmp_path)
    cfg = _write_config(tmp_path)

    holder = RunLock(docs, mode="batch")
    holder.acquire()
    try:
        # --dry-run still reaches main()'s lock acquisition, so a guard
        # regression fails crisply here instead of hanging on the stub.
        proc = subprocess.run(
            [sys.executable, str(RUN_STAGE), "1a",
             "--docs", str(docs), "--config", str(cfg), "--dry-run"],
            cwd=str(RUNNER_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0
        assert "refusing to acquire" in proc.stderr
        # Nothing was written: pre-flight/manifest untouched.
        assert not (docs / "run-manifest.jsonl").exists()
    finally:
        holder.release()


def test_batch_run_holds_lock_through_main_and_releases(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    # Batch runs 1a then 1b before the human gate at stage 2; the fake
    # runner never touches the filesystem, so both tracked outputs must
    # already be valid artifacts for postflight to pass.
    (docs / "01-instructions.md").write_text(
        "# Instructions\n\nReal content.\n", encoding="utf-8")
    (docs / "01-signatures.md").write_text(
        "# Signatures\n\nReal content.\n", encoding="utf-8")
    stop = tmp_path / "stop"
    _write_blocking_runner(tmp_path)
    env = os.environ.copy()
    env[STOP_ENV] = str(stop)

    proc = subprocess.Popen(
        [sys.executable, str(RUN_STAGE), "--batch",
         "--docs", str(docs), "--config", str(_write_config(tmp_path))],
        cwd=str(RUNNER_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        rec = _wait_for_lock(docs, proc)
        assert rec["pid"] == proc.pid
        assert rec["mode"] == "batch"

        stop.touch()
        _, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        # Batch ran 1a and 1b, then stopped at the human gate.
        assert "next stage 2 requires a human gate" in stderr
    finally:
        stop.touch()
        if proc.poll() is None:
            proc.kill()
        proc.communicate()

    assert not (docs / ".run.lock").exists()


def test_status_remains_lock_free_and_reports_live_lock(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()

    holder = RunLock(docs, mode="batch")
    holder.acquire()
    try:
        # --status must work while a run holds the lock (that is its job).
        proc = subprocess.run(
            [sys.executable, str(RUN_STAGE), "--status", "--docs", str(docs)],
            cwd=str(RUNNER_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0
        assert f"run lock held by live pid {os.getpid()}" in proc.stderr
    finally:
        holder.release()


class TestPostflightEnforcement:
    """postflight_check's content_valid/header_valid must actually gate
    success, not just ride along in the manifest as inert metadata — a
    subprocess that exits 0 but leaves a missing, empty, or (for a
    single-file artifact) header-less output is not a successful attempt.
    """

    def test_ok_true_only_when_every_output_is_fully_valid(self) -> None:
        from run_stage import _postflight_ok

        assert _postflight_ok([
            {"exists": True, "content_valid": True, "header_valid": True},
        ])
        assert _postflight_ok([
            {"exists": True, "content_valid": True, "header_valid": True},
            {"exists": True, "content_valid": True, "header_valid": True},
        ])

    def test_missing_output_fails(self) -> None:
        from run_stage import _postflight_ok

        assert not _postflight_ok([
            {"exists": False, "content_valid": False, "header_valid": True},
        ])

    def test_empty_or_whitespace_only_output_fails(self) -> None:
        from run_stage import _postflight_ok

        assert not _postflight_ok([
            {"exists": True, "content_valid": False, "header_valid": True},
        ])

    def test_single_file_artifact_missing_header_fails(self) -> None:
        from run_stage import _postflight_ok

        assert not _postflight_ok([
            {"exists": True, "content_valid": True, "header_valid": False},
        ])

    def test_one_bad_output_among_several_fails_the_whole_attempt(self) -> None:
        from run_stage import _postflight_ok

        assert not _postflight_ok([
            {"exists": True, "content_valid": True, "header_valid": True},
            {"exists": True, "content_valid": False, "header_valid": True},
        ])

    def test_entries_without_validity_fields_pass_trivially(self) -> None:
        """postflight_check returns the raw ``outputs`` argument unchanged
        for stage 5, stage 9, and when --docs is not given — those entries
        carry no exists/content_valid/header_valid keys at all and must not
        be mistaken for failures."""
        from run_stage import _postflight_ok

        assert _postflight_ok([{"stage": "5", "phase": "1/3"}])
        assert _postflight_ok([])

    def test_postflight_check_flags_empty_file_as_content_invalid(
        self, tmp_path: Path,
    ) -> None:
        from run_stage import postflight_check

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "01-instructions.md").write_text("", encoding="utf-8")

        result = postflight_check("1a", docs, [])

        assert len(result) == 1
        assert result[0]["exists"] is True
        assert result[0]["content_valid"] is False

    def test_postflight_check_flags_single_file_artifact_missing_header(
        self, tmp_path: Path,
    ) -> None:
        from run_stage import postflight_check

        docs = tmp_path / "docs"
        docs.mkdir()
        # Non-empty, so content_valid — but no markdown header, and stage
        # 1a's artifact_shape is single-file.
        (docs / "01-instructions.md").write_text(
            "just a plain paragraph, no header\n", encoding="utf-8")

        result = postflight_check("1a", docs, [])

        assert result[0]["content_valid"] is True
        assert result[0]["header_valid"] is False

    def test_postflight_check_passes_a_real_valid_artifact(
        self, tmp_path: Path,
    ) -> None:
        from run_stage import postflight_check, _postflight_ok

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "01-instructions.md").write_text(
            "# Instructions\n\nReal content.\n", encoding="utf-8")

        result = postflight_check("1a", docs, [])

        assert _postflight_ok(result)