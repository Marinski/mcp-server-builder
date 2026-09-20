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
  stale                last attempt succeeded and the tracked artifact is
                       intact, but an upstream dependency changed after this
                       unit was marked done
  failed               the last attempt did not succeed

Stage 5 is phase-tracked: it is reported per phase unit '5 N/M' with M taken
from the manifest, falling back to a single unit '5' when no phase was ever
recorded. Stages 5 and 9 have no checkable artifact, so 'done-but-no-artifact'
and 'modified-since' can never apply to them — but both still consume upstream
files (stage 5 reads 04-spec.md, stage 9 reviews the whole run), so they are
eligible for 'stale'.

Upstream dependency tracking: each stage's dependencies come from the two
sources of truth the runner already keeps. The contract's ``preflight_inputs``
for the stage are the exact files it must find on disk before it runs (they
include stage-0 files such as 00-decisions.md that never enter the manifest).
And every output path recorded by the last successful manifest record of a
strictly earlier stage is an upstream dependency — "when a stage completes,
record the files it modified", the recorded outputs being the files the
downstream stages consume. A done unit is 'stale' when one of those
dependencies changed after the unit was marked done: the file is missing, its
current mtime is newer than the unit's completion timestamp, or the manifest
snapshot for it still records an mtime newer than that timestamp (an upstream
rerun regenerated the file after this unit finished, even if the on-disk mtime
was later preserved). Content edits bump mtime, so mtime is the content
fingerprint — the same convention 'modified-since' and drift detection use.

The manifest (<docs>/run-manifest.jsonl) is a JSONL log written in run order,
so the *last* record for a unit is the current one and earlier records stay
available for later orchestration.
"""
from __future__ import annotations

import datetime
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
STATE_STALE = "stale"

# Stages whose artifact shape leaves nothing to diff on disk. Stage 5 tracks
# phases, not files; stage 9 is no-checkable-artifact by contract. They still
# have upstream dependencies, so 'stale' applies to them.
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


def _last_ok_record(stage: str, records: list[dict]) -> dict | None:
    """The last record reporting success for ``stage``, else None."""
    last = None
    for rec in records:
        if rec.get("stage") == stage and _ok_of(rec):
            last = rec
    return last


def _epoch_seconds(ts: object) -> float | None:
    """Epoch seconds for a manifest ``ts`` value; None when not parseable.

    The runner writes aware UTC timestamps; older or hand-written records may
    be naive (assumed local wall-clock, not UTC) or use a trailing 'Z'.
    """
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _snapshot_from_rec(path: str, rec: dict) -> dict | None:
    """The {size, mtime} ``rec`` recorded for ``path``, else None.

    Returns None when the record has no output table or no entry for the path.
    """
    outputs = rec.get("outputs")
    if not isinstance(outputs, list):
        return None
    for item in outputs:
        if isinstance(item, dict) and item.get("path") == path:
            if "size" in item or "mtime" in item:
                return {"size": item.get("size"), "mtime": item.get("mtime")}
    return None


def _snapshot_for_path(path: str, records: list[dict]) -> dict | None:
    """The most recent successful record's snapshot for ``path``, else None."""
    for rec in reversed(records):
        if not _ok_of(rec):
            continue
        snapshot = _snapshot_from_rec(path, rec)
        if snapshot is not None:
            return snapshot
    return None


def _recorded_output_paths(stage: str, rec: dict, docs: Path | None) -> list[str]:
    """Output paths a successful record claims the stage produced.

    Prefers the record's ``outputs`` table; records without one (older runs,
    no --docs) fall back to the contract filenames resolved under ``docs``.
    """
    outputs = rec.get("outputs")
    if isinstance(outputs, list):
        paths: list[str] = []
        for o in outputs:
            if isinstance(o, dict):
                path = o.get("path")
                if isinstance(path, str):
                    paths.append(path)
        if paths:
            return paths
    from stage_contract import STAGE_CONTRACT  # type: ignore[import]

    contract = STAGE_CONTRACT.get(stage) or {}
    if docs is None:
        return []
    return [str(docs / f) for f in contract.get("postflight_outputs", [])]


def _dependencies(stage: str, records: list[dict],
                  docs: Path | None) -> dict[str, dict | None]:
    """Resolve ``stage``'s upstream dependencies to {path: recorded snapshot}.

    Two sources, both already kept by the runner:

      * the contract's ``preflight_inputs`` for the stage, resolved under
        ``docs`` — the exact files the stage must find before it runs. These
        include stage-0 files (00-decisions.md) that never enter the manifest.
      * every output path recorded by the last successful manifest record of a
        strictly earlier stage — the files earlier stages modified when they
        completed. This is the manifest-derived upstream dependency graph.

    The snapshot is the {size, mtime} the producing record claimed for the
    file; None when the file is untracked (e.g. stage-0 inputs).
    """
    deps: dict[str, dict | None] = {}

    from stage_contract import STAGE_CONTRACT  # type: ignore[import]

    contract = STAGE_CONTRACT.get(stage) or {}
    if docs is not None:
        for filename in contract.get("preflight_inputs", []):
            path = str(docs / filename)
            deps.setdefault(path, _snapshot_for_path(path, records))

    try:
        stage_idx = STAGE_ORDER.index(stage)
    except ValueError:
        return deps
    for prev_stage in STAGE_ORDER[:stage_idx]:
        prev_rec = _last_ok_record(prev_stage, records)
        if prev_rec is None:
            continue
        for path in _recorded_output_paths(prev_stage, prev_rec, docs):
            deps.setdefault(path, _snapshot_from_rec(path, prev_rec))
    return deps


def _is_stale(stage: str, ok_rec: dict, records: list[dict],
              docs: Path | None) -> bool:
    """Whether an upstream dependency changed after the unit was marked done.

    Anchors "marked done" on the unit's completion timestamp
    (``ok_rec["ts"]``) and compares it to every dependency of ``stage``. A
    dependency triggers 'stale' when it is missing from disk (decidable
    without any timestamp), when its current mtime is newer than the
    completion time, or when the manifest snapshot for it still records an
    mtime newer than the completion time (an upstream rerun regenerated the
    file after this unit finished, even if the on-disk mtime was later reset).
    Content edits bump mtime, so mtime is the content fingerprint — the same
    convention 'modified-since' and drift detection use.

    Returns False for time-based signals when the completion time cannot be
    parsed: without a "marked done" anchor there is no way to tell whether a
    change came after it. A missing dependency is stale either way.
    """
    done_ts = _epoch_seconds(ok_rec.get("ts"))
    for path, baseline in _dependencies(stage, records, docs).items():
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return True  # a dependency the unit read no longer exists
        if done_ts is None:
            continue  # time-based signals need a parseable "marked done" time
        if mtime > done_ts:
            return True
        if baseline is not None:
            rec_mtime = baseline.get("mtime")
            if (isinstance(rec_mtime, (int, float))
                    and rec_mtime > done_ts):
                return True
    return False


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


def _tracked_state(stage: str, ok_rec: dict, docs: Path | None,
                   records: list[dict]) -> str:
    """State of a successful unit by diffing tracked artifacts on disk.

    'done' only when the unit's own tracked artifacts are intact *and* no
    upstream dependency changed after the unit was marked done ('stale').
    """
    if stage not in _NO_ARTIFACT_STAGES:
        tracks = ok_rec.get("outputs") or []
        paths = [o for o in tracks if isinstance(o, dict) and o.get("path")]
        if not paths:
            # Records without an output table (older runs, no --docs) fall back
            # to the contract filenames so the report stays meaningful.
            from stage_contract import STAGE_CONTRACT  # type: ignore[import]

            contract = STAGE_CONTRACT.get(stage) or {}
            filenames = contract.get("postflight_outputs") or []
            if not filenames:
                if _is_stale(stage, ok_rec, records, docs):
                    return STATE_STALE
                return STATE_DONE
            missing = [f for f in filenames if docs is None or not (docs / f).is_file()]
            if missing:
                return STATE_DONE_NO_ARTIFACT

        for item in paths:
            fpath = Path(item["path"])
            if not fpath.is_file():
                return STATE_DONE_NO_ARTIFACT
            if (item.get("exists") in (False, 0)
                    or item.get("size") != fpath.stat().st_size
                    or item.get("mtime") != fpath.stat().st_mtime):
                return STATE_MODIFIED

    if _is_stale(stage, ok_rec, records, docs):
        return STATE_STALE
    return STATE_DONE


def _unit_state(unit: str, recs: list[dict], docs: Path | None,
                records: list[dict]) -> str:
    """Base state (no lock/running overlay) for a unit."""
    if not recs:
        return STATE_NOT_STARTED
    last = recs[-1]
    if _ok_of(last):
        return _tracked_state(unit.split()[0], last, docs, records)
    return STATE_FAILED


def compute_run_states(manifest_path: Path | None,
                       docs: Path | None) -> dict[str, str]:
    """Per-unit state dict in stage order, with the running overlay.

    When <docs>/.run.lock is held by a live pid, the first unit whose base
    state is not 'done' is marked 'running'.
    """
    records = _read_manifest(manifest_path)
    units = _split_units(records)
    states = {unit: _unit_state(unit, recs, docs, records)
              for unit, recs in units}

    lock_pid = live_lock_pid(docs)
    if lock_pid is not None:
        for unit, state in states.items():
            if state != STATE_DONE:
                states[unit] = STATE_RUNNING
                break
    return states


def first_pending_unit(manifest_path: Path | None, docs: Path | None,
                       from_stage: str | None = None) -> str | None:
    """First unit at/after ``from_stage`` whose state is not 'done'.

    'done' is the only state that is skipped: failed, done-but-no-artifact,
    modified-since and stale units are returned so they can be rerun. Returns
    None when every unit from ``from_stage`` onward is done. Raises ValueError
    for an unknown ``from_stage``.
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
        if _unit_state(unit, recs, docs, records) != STATE_DONE:
            return unit
    return None