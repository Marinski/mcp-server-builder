"""In-memory per-stage run-state report for the pipeline runner.

Computes each stage's state by scanning the manifest and comparing the
recorded artifact stats (exists/size/mtime per output path) against the files
on disk. Nothing is persisted (spec §2): every call recomputes from the
manifest plus filesystem, so a "run-state" file never exists.

A stage's state is one of:
  not-started          no attempts recorded in the manifest
  running              the lock at <docs>/.run.lock is held by a live pid and
                       this is the first unit whose state is not 'done'
  done                 last attempt succeeded and the tracked artifact exists
                       and matches what the manifest recorded
  done-but-no-artifact last attempt succeeded but a tracked output file is
                       missing from disk
  modified-since       last attempt succeeded but a tracked output differs
                       from what the manifest recorded (size or mtime)
  failed               the last attempt did not succeed

Stage 5 is phase-tracked: it is reported per phase unit '5 N/M' with M taken
from the manifest, falling back to a single unit '5' when no phase was ever
recorded. Stages 5 and 9 have no checkable artifact, so 'done-but-no-artifact'
and 'modified-since' can never apply to them.

The manifest (<docs>/run-manifest.jsonl) is a JSONL log written in run order,
so the *last* record for a unit is the current one and earlier records stay
available for later orchestration.
"""
from __future__ import annotations

import json
from pathlib import Path

from lock import RunLock  # type: ignore[import]
from stage_contract import STAGE_ORDER  # type: ignore[import]

MANIFEST_FILENAME = "run-manifest.jsonl"
LOCK_FILENAME = ".run.lock"

STATE_NOT_STARTED = "not-started"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_DONE_NO_ARTIFACT = "done-but-no-artifact"
STATE_MODIFIED = "modified-since"

# Stages whose artifact shape leaves nothing to diff on disk. Stage 5 tracks
# phases, not files; stage 9 is no-checkable-artifact by contract.
_NO_ARTIFACT_STAGES = {"5", "9"}


def live_lock_pid(docs: Path | None) -> int | None:
    """Return the live pid holding ``<docs>/.run.lock``, else None."""
    if docs is None:
        return None
    lock_path = docs / LOCK_FILENAME
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if isinstance(pid, int) and pid > 0 and RunLock._pid_alive(pid):
            return pid
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _read_manifest(manifest_path: Path | None) -> list[dict]:
    """Parse the manifest JSONL into records, tolerating missing/bad lines."""
    if manifest_path is None or not manifest_path.is_file():
        return []
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _ok_of(rec: dict) -> bool | None:
    """Whether a manifest record reports a successful attempt.

    Records may store ``ok`` (bool) or rely on ``returncode == 0``; None means
    the record cannot say either way.
    """
    ok = rec.get("ok")
    if isinstance(ok, bool):
        return ok
    if isinstance(ok, int):
        return ok != 0
    rc = rec.get("returncode")
    if isinstance(rc, int):
        return rc == 0
    return None


def _phase_numbers(rec: dict) -> tuple[int, int] | None:
    """Parse a record's ``phase`` value as (n, m); None when it is not N/M."""
    raw = rec.get("phase")
    if not isinstance(raw, str):
        return None
    parts = raw.split("/")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def _split_units(all_records: list[dict]) -> list[tuple[str, list[dict]]]:
    """Ordered (unit, records) pairs following STAGE_ORDER.

    Stage 5 expands to per-phase units '5 1/M' … '5 M/M' where M is the total
    of the newest phase-carrying record; when no phase was ever recorded the
    unit is plain '5'.
    """
    units: list[tuple[str, list[dict]]] = []
    for stage in STAGE_ORDER:
        stage_recs = [r for r in all_records if r.get("stage") == stage]
        if stage != "5":
            units.append((stage, stage_recs))
            continue

        phase_recs = [r for r in stage_recs if _phase_numbers(r) is not None]
        if not phase_recs:
            units.append(("5", stage_recs))
            continue

        totals = sorted({total for _, total in (_phase_numbers(r) for r in phase_recs)})
        total = totals[-1] if totals else 1
        for n in range(1, total + 1):
            unit_recs = [r for r in phase_recs if _phase_numbers(r)[0] == n]
            units.append((f"5 {n}/{total}", unit_recs))
    return units


def _tracked_state(stage: str, ok_rec: dict, docs: Path | None) -> str:
    """State of a successful unit by diffing tracked artifacts on disk."""
    if stage in _NO_ARTIFACT_STAGES:
        return STATE_DONE

    tracks = ok_rec.get("outputs") or []
    paths = [o for o in tracks if isinstance(o, dict) and o.get("path")]
    if not paths:
        # Records without an output table (older runs, no --docs) fall back to
        # the contract filenames so the report stays meaningful.
        from stage_contract import STAGE_CONTRACT  # type: ignore[import]

        contract = STAGE_CONTRACT.get(stage) or {}
        filenames = contract.get("postflight_outputs") or []
        if not filenames:
            return STATE_DONE
        missing = [f for f in filenames if docs is None or not (docs / f).is_file()]
        return STATE_DONE_NO_ARTIFACT if missing else STATE_DONE

    for item in paths:
        fpath = Path(item["path"])
        if not fpath.is_file():
            return STATE_DONE_NO_ARTIFACT
        if (item.get("exists") in (False, 0)
                or item.get("size") != fpath.stat().st_size
                or item.get("mtime") != fpath.stat().st_mtime):
            return STATE_MODIFIED
    return STATE_DONE


def _unit_state(unit: str, recs: list[dict], docs: Path | None) -> str:
    """Base state (no lock/running overlay) for a unit."""
    if not recs:
        return STATE_NOT_STARTED
    last = recs[-1]
    if _ok_of(last):
        return _tracked_state(unit.split()[0], last, docs)
    return STATE_FAILED


def compute_run_states(manifest_path: Path | None,
                       docs: Path | None) -> dict[str, str]:
    """Per-unit state dict in stage order, with the running overlay.

    When <docs>/.run.lock is held by a live pid, the first unit whose base
    state is not 'done' is marked 'running'.
    """
    records = _read_manifest(manifest_path)
    units = _split_units(records)
    states = {unit: _unit_state(unit, recs, docs) for unit, recs in units}

    lock_pid = live_lock_pid(docs)
    if lock_pid is not None:
        for unit, recs in units:
            if _unit_state(unit, recs, docs) != STATE_DONE:
                states[unit] = STATE_RUNNING
                break
    return states


def first_pending_unit(manifest_path: Path | None, docs: Path | None,
                       from_stage: str | None = None) -> str | None:
    """First unit at/after ``from_stage`` whose state is not 'done'.

    'done' is the only state that is skipped: failed, done-but-no-artifact and
    modified-since units are returned so they can be rerun. Returns None when
    every unit from ``from_stage`` onward is done. Raises ValueError for an
    unknown ``from_stage``.
    """
    records = _read_manifest(manifest_path)
    units = _split_units(records)

    if from_stage is not None:
        if from_stage not in STAGE_ORDER:
            raise ValueError(
                f"unknown stage '{from_stage}' "
                f"(expected one of {', '.join(STAGE_ORDER)})")
        start = STAGE_ORDER.index(from_stage)
    else:
        start = 0

    for unit, recs in units:
        unit_stage = unit.split()[0]
        if unit_stage not in STAGE_ORDER:
            continue
        if STAGE_ORDER.index(unit_stage) < start:
            continue
        if _unit_state(unit, recs, docs) != STATE_DONE:
            return unit
    return None