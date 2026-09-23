"""Behavior tests for upstream dependency tracking in ``run_state.py``.

A unit is 'done' only when its own tracked artifact is intact *and* no
upstream dependency changed after it was marked done. These tests exercise
that second half against a small manifest: stage 1a records 01-instructions.md
(which stage 2 reads), stage 1b records 01-signatures.md, and stage 2 records
02-capability-inventory.md. Stage 2's contract preflight inputs are
00-decisions.md, 01-instructions.md and 01-signatures.md.

Timestamps are whole epoch seconds chosen far apart so mtime-vs-done comparisons
never tie at filesystem granularity.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from run_state import (
    STATE_DONE,
    STATE_MODIFIED,
    STATE_STALE,
    compute_run_states,
    first_pending_unit,
)

T_INST = 1_700_000_000.0   # mtime of the stage-1 artifacts on disk
T_1A = 1_700_000_100.0     # stage 1a completes
T_1B = 1_700_000_200.0     # stage 1b completes
T_2 = 1_700_000_300.0      # stage 2 completes


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).isoformat()


def _write(docs: Path, name: str, content: str, mtime: float) -> dict:
    """Create ``docs/<name>`` at a known mtime; return its output-table entry."""
    fpath = docs / name
    fpath.write_text(content, encoding="utf-8")
    os.utime(fpath, (mtime, mtime))
    st = fpath.stat()
    return {"path": str(fpath), "shape": "single-file", "exists": True,
            "size": st.st_size, "mtime": st.st_mtime}


def _ok(stage: str, ts: float, outputs: list[dict]) -> dict:
    return {"ts": _iso(ts), "stage": stage, "ok": True,
            "returncode": 0, "outputs": outputs}


def _setup(tmp_path: Path) -> tuple[Path, Path, dict, dict, dict]:
    """A done 1a -> 1b -> 2 chain; returns (docs, manifest, inst, sig, cap)."""
    docs = tmp_path / "docs"
    docs.mkdir()
    mf = docs / "run-manifest.jsonl"

    (docs / "00-decisions.md").write_text("MODE: wrap\n", encoding="utf-8")
    os.utime(docs / "00-decisions.md", (T_INST, T_INST))

    inst = _write(docs, "01-instructions.md", "onboarding\n", T_INST)
    sig = _write(docs, "01-signatures.md", "signatures\n", T_INST)
    cap = _write(docs, "02-capability-inventory.md", "capabilities\n", T_INST)

    records = [
        _ok("1a", T_1A, [inst]),
        _ok("1b", T_1B, [sig]),
        _ok("2", T_2, [cap]),
    ]
    mf.write_text("".join(json.dumps(r) + "\n" for r in records),
                  encoding="utf-8")
    return docs, mf, inst, sig, cap


def _states(mf: Path, docs: Path) -> dict[str, str]:
    return compute_run_states(mf, docs)


def test_done_when_dependencies_unchanged(tmp_path: Path) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    states = _states(mf, docs)
    assert states["1a"] == STATE_DONE
    assert states["1b"] == STATE_DONE
    assert states["2"] == STATE_DONE
    assert states["3"] == "not-started"


def test_stale_when_dependency_mtime_newer_than_done(tmp_path: Path) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    # Stage 1a's artifact is touched after stage 2 completed. Stage 2's own
    # artifact is untouched, so it is stale — not modified-since.
    os.utime(docs / "01-instructions.md", (T_2 + 100.0, T_2 + 100.0))
    states = _states(mf, docs)
    assert states["1a"] == STATE_MODIFIED  # its own artifact drifted
    assert states["1b"] == STATE_STALE      # manifest-derived dep changed
    assert states["2"] == STATE_STALE


def test_stale_when_dependency_content_changed_after_done(
    tmp_path: Path,
) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    # Same-size content edit after stage 2 completed: mtime moves, size does
    # not — the mtime fingerprint still marks stage 2 stale.
    fpath = docs / "01-signatures.md"
    fpath.write_text("signatures!\n", encoding="utf-8")
    os.utime(fpath, (T_2 + 100.0, T_2 + 100.0))
    states = _states(mf, docs)
    assert states["1b"] == STATE_MODIFIED  # its own artifact drifted
    assert states["2"] == STATE_STALE


def test_stale_when_upstream_rerun_after_done(tmp_path: Path) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    # Stage 1a is rerun after stage 2 finished, regenerating its artifact and
    # appending a newer record with a newer mtime.
    inst2 = _write(docs, "01-instructions.md", "onboarding v2\n", T_2 + 50.0)
    with mf.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_ok("1a", T_2 + 60.0, [inst2])) + "\n")
    states = _states(mf, docs)
    assert states["1a"] == STATE_DONE           # its artifact matches again
    assert states["1b"] == STATE_STALE           # dep mtime moved past 1b
    assert states["2"] == STATE_STALE


def test_stale_via_manifest_snapshot_when_disk_mtime_reset(
    tmp_path: Path,
) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    # An upstream rerun regenerated the dependency after stage 2 was done, then
    # something restored the on-disk mtime to before stage 2 (git checkout,
    # cp -p). The manifest snapshot still records the newer mtime, so the
    # content is known to have changed after stage 2 was done.
    inst2 = _write(docs, "01-instructions.md", "onboarding v2\n", T_2 + 50.0)
    with mf.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_ok("1a", T_2 + 60.0, [inst2])) + "\n")
    os.utime(docs / "01-instructions.md", (T_INST, T_INST))  # mtime restored
    states = _states(mf, docs)
    assert states["1b"] == STATE_STALE   # snapshot mtime moved past 1b
    assert states["2"] == STATE_STALE


def test_not_stale_when_dependency_changed_before_done(tmp_path: Path) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    # 01-instructions.md is rewritten after 1a completes but before 1b and 2
    # complete: both consumers read the newer content, so neither is stale.
    # Stage 1a's own artifact no longer matches its record, so 1a reports
    # modified-since.
    fpath = docs / "01-instructions.md"
    fpath.write_text("onboarding edited-before\n", encoding="utf-8")
    os.utime(fpath, (T_1A + 50.0, T_1A + 50.0))  # after 1a, before 1b and 2
    states = _states(mf, docs)
    assert states["1a"] == STATE_MODIFIED
    assert states["1b"] == STATE_DONE
    assert states["2"] == STATE_DONE


def test_missing_dependency_is_stale(tmp_path: Path) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    (docs / "01-instructions.md").unlink()
    assert _states(mf, docs)["2"] == STATE_STALE


def test_untracked_preflight_input_change_is_stale(tmp_path: Path) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    # 00-decisions.md comes from stage 0 and never appears in the manifest;
    # its mtime is the only signal, and it changed after stage 2 was done.
    os.utime(docs / "00-decisions.md", (T_2 + 100.0, T_2 + 100.0))
    states = _states(mf, docs)
    assert states["1a"] == STATE_DONE   # not a dependency of 1a
    assert states["2"] == STATE_STALE


def test_stage_5_phase_stale_when_spec_changed(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    mf = docs / "run-manifest.jsonl"
    inst = _write(docs, "01-instructions.md", "onboarding\n", T_INST)
    sig = _write(docs, "01-signatures.md", "signatures\n", T_INST)
    cap = _write(docs, "02-capability-inventory.md", "capabilities\n", T_INST)
    surface = _write(docs, "03-mcp-surface.md", "surface\n", T_INST)
    spec = _write(docs, "04-spec.md", "spec\n", T_INST)
    _write(docs, "00-decisions.md", "MODE: wrap\n", T_INST)
    _write(docs, "05-test-plan.md", "tests\n", T_INST)

    recs = [
        _ok("1a", T_1A, [inst]),
        _ok("1b", T_1B, [sig]),
        _ok("2", T_2, [cap]),
        _ok("3", T_2 + 100.0, [surface]),
        _ok("4", T_2 + 200.0, [spec]),
        {"ts": _iso(T_2 + 300.0), "stage": "5", "phase": "1/1", "ok": True,
         "returncode": 0, "outputs": []},
    ]
    mf.write_text("".join(json.dumps(r) + "\n" for r in recs),
                  encoding="utf-8")

    t_before = T_2 + 400.0
    assert _states(mf, docs)["5 1/1"] == STATE_DONE

    # Stage 4's artifact changes after the phase completed -> phase is stale.
    os.utime(docs / "04-spec.md", (t_before, t_before))
    assert _states(mf, docs)["5 1/1"] == STATE_STALE


def test_first_pending_unit_returns_stale_stage(tmp_path: Path) -> None:
    docs, mf, _, _, _ = _setup(tmp_path)
    # 1a/1b/2 done, everything later not-started -> 3 is the first pending unit.
    assert first_pending_unit(mf, docs) == "3"

    # Change a stage-2-only dependency (stage-0 file) so 1a/1b stay done and
    # stage 2 becomes stale, which now outranks the not-started stages.
    os.utime(docs / "00-decisions.md", (T_2 + 100.0, T_2 + 100.0))
    assert first_pending_unit(mf, docs) == "2"


def test_unparseable_done_ts_skips_time_based_stale(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    mf = docs / "run-manifest.jsonl"
    inst = _write(docs, "01-instructions.md", "onboarding\n", T_INST)
    cap = _write(docs, "02-capability-inventory.md", "capabilities\n", T_INST)
    decisions = docs / "00-decisions.md"
    decisions.write_text("MODE: wrap\n", encoding="utf-8")
    os.utime(decisions, (T_INST, T_INST))
    sig = docs / "01-signatures.md"
    sig.write_text("signatures\n", encoding="utf-8")
    os.utime(sig, (T_INST, T_INST))
    recs = [
        _ok("1a", T_1A, [inst]),
        {"ts": "not-a-date", "stage": "2", "ok": True,
         "returncode": 0, "outputs": [cap]},
    ]
    mf.write_text("".join(json.dumps(r) + "\n" for r in recs),
                  encoding="utf-8")
    os.utime(docs / "01-instructions.md", (T_2 + 100.0, T_2 + 100.0))
    # No parseable "marked done" anchor: time-based staleness cannot be
    # decided, so the conservative non-stale answer stands (the artifact diff
    # still applies; the dependency file still exists on disk).
    assert _states(mf, docs)["2"] == STATE_DONE


def test_missing_dependency_is_stale_without_done_ts(tmp_path: Path) -> None:
    # A gone dependency is stale even when the done record's ts does not
    # parse: "the file no longer exists" needs no timestamp anchor.
    docs, mf, _, _, _ = _setup(tmp_path)
    recs = [l for l in mf.read_text(encoding="utf-8").splitlines() if l]
    replacement = []
    for line in recs:
        rec = json.loads(line)
        if rec.get("stage") == "2":
            rec["ts"] = "not-a-date"
        replacement.append(json.dumps(rec))
    mf.write_text("\n".join(replacement) + "\n", encoding="utf-8")
    (docs / "01-instructions.md").unlink()
    assert _states(mf, docs)["2"] == STATE_STALE