#!/usr/bin/env python3
"""Run one pipeline stage against the model configured for it.

Resolves stage -> provider -> runner from models.yaml, builds the CLI invocation,
and walks the fallback chain when a stage fails.

Cross-provider fallback lives here because neither CLI can do it: `claude` has
--fallback-model but only within one provider, and `opencode` has no fallback flag
at all. Falling back from a hosted model to a local one is the case that matters
when credits run out, so the runner owns it.

The stage's CLI process runs with the *target repo* as its working directory
(--cwd, derived from --docs when not given). Claude Code confines file access to
the directory it is launched from, so inheriting whatever directory this script
happened to be invoked from silently sandboxes the stage out of the repo it is
supposed to read and write.

  ./runner/run_stage.py 2 --docs /path/to/repo/docs/mcp --dry-run

Per-stage state is computed in memory from the manifest and the artifacts on
disk, and is never persisted to a file:

  ./runner/run_stage.py --status --docs /path/to/repo/docs/mcp
  ./runner/run_stage.py --from 3 --docs /path/to/repo/docs/mcp --dry-run
  ./runner/run_stage.py --version
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent

VERSION = "0.1.0"


def build_outputs(stage: str, docs: Path | None) -> list[dict]:
    """Collect per-file shape metadata for the artifact-shape table.

    Returns a list of objects, one per tracked output file:
        {path, shape, exists, size, mtime}

    Empty array for stage 9 (no-checkable-artifact) and when --docs is not given.
    """
    # Import here to avoid circular imports; matches the existing pattern.
    from stage_contract import STAGE_CONTRACT, SHAPE_NO_CHECKABLE  # type: ignore[import]

    contract = STAGE_CONTRACT.get(stage)
    if contract is None or contract["artifact_shape"] == SHAPE_NO_CHECKABLE:
        return []

    outputs: list[dict] = []
    for filename in contract.get("postflight_outputs", []):
        if docs is None:
            continue
        fpath = docs / filename
        exists = fpath.is_file()
        outputs.append({
            "path": str(fpath) if docs else filename,
            "shape": contract["artifact_shape"],
            "exists": exists,
            "size": fpath.stat().st_size if exists else 0,
            "mtime": fpath.stat().st_mtime if exists else 0.0,
        })
    return outputs


def preflight_check(stage: str, docs: Path | None,
                    phase: str | None = None,
                    manifest_path: Path | None = None) -> None:
    """Verify required input artifacts exist on disk before spawning a subprocess.

    Stage 9 requires no pre-flight check.
    Stage 5 additionally validates --phase N/M and M consistency.
    """
    if stage == "9":
        return
    if docs is None:
        return

    from stage_contract import STAGE_CONTRACT  # type: ignore[import]

    contract = STAGE_CONTRACT.get(stage)
    if contract is None:
        return

    missing = [f for f in contract["preflight_inputs"]
               if not (docs / f).is_file()]
    if missing:
        sys.exit(f"pre-flight: missing input(s) for stage {stage}: "
                 + ", ".join(str(docs / f) for f in missing))

    if stage == "5":
        if not phase:
            sys.exit("pre-flight: stage 5 requires --phase N/M")
        m = re.fullmatch(r"(\d+)/(\d+)", phase)
        if not m:
            sys.exit(f"pre-flight: --phase must be N/M format, got '{phase}'")
        n, phase_total = int(m.group(1)), int(m.group(2))
        if n < 1 or n > phase_total:
            sys.exit(f"pre-flight: phase {n}/{phase_total} is out of range")
        # Verify M is consistent with prior stage-5 manifest entries.
        if manifest_path and manifest_path.is_file():
            for line in manifest_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (rec.get("stage") == "5"
                        and rec.get("phase")
                        and rec.get("ok")):
                    prev_m = int(rec["phase"].split("/")[1])
                    if prev_m != phase_total:
                        sys.exit(
                            f"pre-flight: --phase M={phase_total} inconsistent "
                            f"with prior stage-5 manifest entry M={prev_m}")


def postflight_check(stage: str, docs: Path | None,
                     outputs: list[dict]) -> list[dict]:
    """After a successful run, record exists/size/mtime for tracked output paths.

    Stage 5: skip existence check (completion = ok attempt for phase N/M).
    Stage 9: skip entirely (returns empty).
    """
    if stage == "9" or docs is None:
        return outputs

    from stage_contract import STAGE_CONTRACT  # type: ignore[import]

    contract = STAGE_CONTRACT.get(stage)
    if contract is None:
        return outputs

    if stage == "5":
        return outputs  # skip existence check per spec

    result: list[dict] = []
    for filename in contract.get("postflight_outputs", []):
        fpath = docs / filename
        exists = fpath.is_file()
        result.append({
            "path": str(fpath),
            "shape": contract["artifact_shape"],
            "exists": exists,
            "size": fpath.stat().st_size if exists else 0,
            "mtime": fpath.stat().st_mtime if exists else 0.0,
        })
    return result


class LogCapture:
    """Tee stdout+stderr to a log file while streaming to console.

    Records the last ~50 lines as output_tail for the manifest.
    Used as a context manager: returns (log_path_str, output_tail_str).
    Falls back to (None, None) when --docs is not given.
    """

    TAIL_LINES = 50

    def __init__(self, stage: str, docs: Path | None,
                 phase: str | None = None) -> None:
        self.stage = stage
        self.docs = docs
        self.phase = phase
        self._fh = None
        self._log_path = None
        self._tail_lines: list[str] = []

    def __enter__(self):
        if self.docs is None:
            return None, None
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        log_dir = self.docs / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        name = f"{self.stage}"
        if self.phase:
            name += f"-phase{self.phase.split('/')[0]}"
        name += f"-{ts}.log"
        self._log_path = str(log_dir / name)
        self._fh = open(self._log_path, "w", encoding="utf-8")
        return self._log_path, self

    def write(self, text: str) -> None:
        """Write text to both console (stderr) and log file."""
        sys.stderr.write(text)
        sys.stderr.flush()
        if self._fh:
            self._fh.write(text)
            self._fh.flush()
        for line in text.splitlines(keepends=True):
            self._tail_lines.append(line)
            if len(self._tail_lines) > self.TAIL_LINES:
                self._tail_lines.pop(0)

    def tail(self) -> str:
        return "".join(self._tail_lines).strip()

    def __exit__(self, *exc_info):
        if self._fh:
            self._fh.close()
            self._fh = None


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"no config at {path}\nCopy models.example.yaml to models.yaml and edit it.")
    return yaml.safe_load(path.read_text())


DEFAULT_TIMEOUT = 1800


def resolve_timeout(cfg: dict, stage: str, cli_timeout: int | None) -> int:
    """Resolve subprocess timeout: CLI flag > per-stage models.yaml > default."""
    if cli_timeout is not None:
        return cli_timeout
    override = (cfg.get("stages") or {}).get(stage) or {}
    if "timeout" in override:
        return int(override["timeout"])
    return DEFAULT_TIMEOUT


def resolve_chain(cfg: dict, stage: str) -> list[dict]:
    """The ordered list of {provider, model} attempts for a stage.

    A stage's own `fallback` replaces the default chain rather than extending it,
    so a stage that must not silently drop to a weak model can say so.
    """
    defaults = cfg.get("defaults", {})
    override = (cfg.get("stages") or {}).get(stage) or {}

    primary = {
        "provider": override.get("provider", defaults.get("provider")),
        "model": override.get("model", defaults.get("model")),
    }
    if not primary["provider"] or not primary["model"]:
        sys.exit(f"stage {stage}: no provider/model resolved; check defaults in models.yaml")

    chain = [primary]
    for entry in override.get("fallback", defaults.get("fallback", [])):
        # A fallback entry may omit provider, meaning "same provider, other model".
        chain.append({
            "provider": entry.get("provider", primary["provider"]),
            "model": entry["model"],
        })

    # Drop consecutive duplicates so a stage override matching a fallback entry
    # does not run the same model twice.
    deduped: list[dict] = []
    for item in chain:
        if item not in deduped:
            deduped.append(item)
    return deduped


def build_command(cfg: dict, attempt: dict, prompt_file: Path,
                  same_provider_fallbacks: list[str], strict: bool = True) -> tuple[list[str], dict]:
    provider_name = attempt["provider"]
    provider = (cfg.get("providers") or {}).get(provider_name)
    if not provider:
        sys.exit(f"unknown provider '{provider_name}' — is it commented out in models.yaml?")

    runner_name = provider.get("runner")
    runner = (cfg.get("runners") or {}).get(runner_name)
    if not runner:
        sys.exit(f"provider '{provider_name}' names unknown runner '{runner_name}'")

    cmd_name = runner["cmd"]
    if not shutil.which(cmd_name):
        raise FileNotFoundError(cmd_name)

    # Only claude consumes --fallback-model; every other runner relies on the
    # loop below for fallback.
    fallback_arg = ",".join(same_provider_fallbacks) if same_provider_fallbacks else attempt["model"]

    # A runner that takes the provider as its own argument (pi does) names it
    # here; otherwise {provider} expands to the provider's `remote` name or the
    # config key, which is what runners expecting "provider/model" need.
    provider_arg = provider.get("remote", provider_name)

    argv = [cmd_name]
    for raw in runner.get("args", []):
        argv.append(
            raw.replace("{model}", attempt["model"])
               .replace("{provider}", provider_arg)
               .replace("{fallback}", fallback_arg)
               .replace("{prompt_file}", str(prompt_file))
        )

    env = os.environ.copy()
    # Point the runner at this provider's endpoint. Values come from the
    # environment, never from the config file, so models.yaml stays committable.
    base_url_env = provider.get("base_url_env")
    if base_url_env:
        value = os.environ.get(base_url_env)
        if value:
            env[runner.get("base_url_var", "OPENAI_BASE_URL")] = value
        elif base_url_env != "ANTHROPIC_BASE_URL":
            # An explicitly configured custom endpoint that is unset is a
            # misconfiguration, not a silent fall-through to the vendor default.
            msg = f"provider '{provider_name}': ${base_url_env} is not set"
            if strict:
                sys.exit(msg)
            print(f"  warn: {msg}", file=sys.stderr)

    key_env = provider.get("api_key_env")
    if key_env:
        value = os.environ.get(key_env)
        if value:
            env[runner.get("api_key_var", "OPENAI_API_KEY")] = value
        else:
            # Claude Code can be authenticated by OAuth, in which case no API key
            # exists and demanding one would block a perfectly working setup. Every
            # other runner talks to an endpoint that genuinely needs a credential.
            msg = f"provider '{provider_name}': ${key_env} is not set"
            if runner.get("auth_optional"):
                print(f"  note: {msg}; relying on the CLI's own auth", file=sys.stderr)
            elif strict:
                sys.exit(msg)
            else:
                print(f"  warn: {msg}", file=sys.stderr)

    return argv, env


def record_attempt(args, attempt: dict, index: int, total: int, returncode: int,
                    *, duration_s: float, timed_out: bool,
                    phase: str | None = None,
                    outputs: list[dict] | None = None,
                    log_path: str | None = None,
                    output_tail: str | None = None) -> None:
    """Append what actually ran to a manifest.

    An artifact does not record which model produced it, so a run is otherwise
    unreproducible and unauditable: a stage that silently fell back to a weaker
    model looks identical to one that did not. The manifest is what lets a later
    reader tell the difference, and what makes a bad artifact traceable to a
    routing decision rather than to the prompt.

    New fields per spec §3:
      duration_s   – wall-clock seconds for the subprocess
      timed_out    – True when the process was killed by timeout
      phase        – 'N/M' string, stage 5 only
      outputs      – [{path, shape, exists, size, mtime}], empty array for stage 9
      log_path     – path to the tee'd log file (if given)
      output_tail  – last ~50 lines of stdout+stderr (if captured)
    """
    path = args.manifest or (args.docs / "run-manifest.jsonl" if args.docs else None)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stage": args.stage,
        "provider": attempt["provider"],
        "model": attempt["model"],
        "attempt": f"{index + 1}/{total}",
        "returncode": returncode,
        "ok": returncode == 0,
        "duration_s": round(duration_s, 3),
        "timed_out": timed_out,
        "phase": phase,
        "outputs": outputs if outputs is not None else [],
        "log_path": log_path,
        "output_tail": output_tail,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _last_ok_record(stage: str, manifest_path: Path | None,
                    phase: str | None = None) -> dict | None:
    """Return the last successful manifest record for a stage (and phase, if given).

    Returns None when no successful record exists.
    """
    if manifest_path is None or not manifest_path.is_file():
        return None
    last_ok = None
    for line in manifest_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (rec.get("stage") == stage
                and rec.get("ok") is True):
            if phase is not None and rec.get("phase") != phase:
                continue
            last_ok = rec
    return last_ok


def detect_drift(stage: str, manifest_path: Path | None) -> list[dict]:
    """Check tracked output files for mtime/size drift against manifest.

    Returns a list of drift records, one per drifted file:
      {path, expected_size, actual_size, expected_mtime, actual_mtime}
    Empty list when no drift is detected (or no tracked outputs exist).
    """
    if stage in ("5", "9"):
        return []

    rec = _last_ok_record(stage, manifest_path)
    if rec is None:
        return []

    outputs = rec.get("outputs") or []
    drift: list[dict] = []
    for item in outputs:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        fpath = Path(item["path"])
        if not fpath.is_file():
            continue
        cur_size = fpath.stat().st_size
        cur_mtime = fpath.stat().st_mtime
        if (item.get("size") != cur_size
                or item.get("mtime") != cur_mtime):
            drift.append({
                "path": str(fpath),
                "expected_size": item.get("size"),
                "actual_size": cur_size,
                "expected_mtime": item.get("mtime"),
                "actual_mtime": cur_mtime,
            })
    return drift


def run_stage_once(stage: str, cfg: dict, args: argparse.Namespace,
                   prompt_file: Path, *, phase: str | None = None) -> int:
    """Execute a single stage through its full lifecycle.

    pre-flight → subprocess with timeout → post-flight → manifest record.
    Returns the exit code (0 for success, non-zero for failure).
    """
    chain = resolve_chain(cfg, stage)
    timeout_s = resolve_timeout(cfg, stage, args.timeout)

    preflight_check(stage, args.docs, phase,
                    args.manifest or (args.docs / "run-manifest.jsonl"
                                      if args.docs else None))

    cwd = args.cwd or (args.docs.resolve().parent.parent if args.docs else None)
    if cwd is not None:
        cwd = cwd.resolve()

    agent = (cfg.get("agents") or {}).get(stage)
    print(f"stage {stage}"
          + (f"  agent={agent}" if agent else "")
          + (f"  docs={args.docs}" if args.docs else "")
          + (f"  cwd={cwd}" if cwd else "")
          + (f"  timeout={timeout_s}s" if timeout_s else ""),
          file=sys.stderr)
    print("  chain: " + " -> ".join(
        f"{c['provider']}/{c['model']}" for c in chain), file=sys.stderr)

    outputs = build_outputs(stage, args.docs)

    for i, attempt in enumerate(chain):
        same_provider = [c["model"] for c in chain[i + 1:]
                         if c["provider"] == attempt["provider"]]
        try:
            argv, env = build_command(cfg, attempt, prompt_file, same_provider,
                                      strict=True)
        except FileNotFoundError as exc:
            print(f"  skip {attempt['provider']}/{attempt['model']}: "
                  f"'{exc}' not on PATH", file=sys.stderr)
            continue

        label = f"{attempt['provider']}/{attempt['model']}"
        print(f"  [{i + 1}/{len(chain)}] running {label}", file=sys.stderr)
        stdin = prompt_file.open() if args.prompt else subprocess.DEVNULL
        timed_out = False
        t_start = time.monotonic()
        log_path = None
        output_tail = None
        with LogCapture(stage, args.docs, phase) as (lp, lc):
            log_path = lp
            try:
                proc = subprocess.Popen(
                    argv, stdin=stdin, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, cwd=cwd)
            except FileNotFoundError as exc:
                if args.prompt:
                    stdin.close()
                print(f"  skip {label}: '{exc}' not on PATH", file=sys.stderr)
                continue
            try:
                for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace")
                    if lc is not None:
                        lc.write(line)
                    else:
                        sys.stderr.write(line)
                        sys.stderr.flush()
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()
                if lc is not None:
                    lc.write("\n")
            finally:
                if args.prompt:
                    stdin.close()
            duration_s = time.monotonic() - t_start
            tail = lc.tail() if lc is not None else None

        postflight = postflight_check(stage, args.docs, outputs)
        record_attempt(args, attempt, i, len(chain), proc.returncode,
                       duration_s=duration_s, timed_out=timed_out,
                       phase=phase, outputs=postflight,
                       log_path=log_path, output_tail=tail)
        if proc.returncode == 0:
            print(f"  ok: {label}", file=sys.stderr)
            return 0
        print(f"  failed ({proc.returncode}): {label}", file=sys.stderr)

    print("all attempts in the chain failed", file=sys.stderr)
    return 1


def _resolve_prior_phase_total(manifest_path: Path | None) -> int | None:
    """Find the total phase count M from a prior stage-5 manifest entry.

    Returns M if found, None if no phase data exists in the manifest.
    """
    if manifest_path is None or not manifest_path.is_file():
        return None
    total = None
    for line in manifest_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("stage") == "5" and rec.get("phase"):
            parts = rec["phase"].split("/")
            if len(parts) == 2 and parts[1].isdigit():
                total = int(parts[1])
    return total


def _last_completed_phase(manifest_path: Path | None) -> int:
    """Return the highest completed phase number N for stage 5.

    Returns 0 when no phase has been recorded or none succeeded.
    """
    if manifest_path is None or not manifest_path.is_file():
        return 0
    last_n = 0
    for line in manifest_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (rec.get("stage") == "5"
                and rec.get("ok") is True
                and rec.get("phase")):
            parts = rec["phase"].split("/")
            if len(parts) == 2 and parts[0].isdigit():
                n = int(parts[0])
                if n > last_n:
                    last_n = n
    return last_n


def run_batch(args: argparse.Namespace) -> int:
    """Execute --batch: contiguous non-gated spans with human-gate stops.

    Span A runs 1a then 1b sequentially (under the pipeline lock that
    main() holds for the entire execution block).
    Stops before human-gated stages {2, 3, 4}.
    Span B handles stage 5's phase loop (last-completed+1 .. M, with
    automated verify-gate between phases).
    Span C runs 6, 7, 8, 9 sequentially.
    """
    cfg = load_config(args.config)
    mf_path = args.manifest or (args.docs / "run-manifest.jsonl"
                                if args.docs else None)

    from run_state import first_pending_unit  # type: ignore[import]
    from stage_contract import STAGE_CONTRACT, STAGE_ORDER  # type: ignore[import]

    # The pipeline lock is acquired once in main() before dispatch and held
    # for the whole run, so batch mode does not acquire it itself.

    # Determine the starting stage.
    if args.from_stage:
        if args.from_stage not in STAGE_ORDER:
            sys.exit(f"unknown stage '{args.from_stage}'")
        start_stage = args.from_stage
    else:
        unit = first_pending_unit(mf_path, args.docs, None)
        if unit is None:
            print("nothing to run: every stage is done", file=sys.stderr)
            return 0
        start_stage = unit.split()[0]

    # Starting at a human-gated stage in batch mode is an error.
    if STAGE_CONTRACT.get(start_stage, {}).get("human_gated"):
        print(
            f"batch: stage {start_stage} requires a human gate — "
            f"run it yourself: ./runner/run_stage.py {start_stage}"
            f" --docs {args.docs}",
            file=sys.stderr,
        )
        return 0

    start_idx = STAGE_ORDER.index(start_stage)
    prompt_file = args.prompt.resolve() if args.prompt else Path(os.devnull)

    for idx in range(start_idx, len(STAGE_ORDER)):
        stage = STAGE_ORDER[idx]

        # Human-gated stage: stop and instruct.
        if STAGE_CONTRACT.get(stage, {}).get("human_gated"):
            print(
                f"next stage {stage} requires a human gate — "
                f"run it yourself: ./runner/run_stage.py {stage}"
                f" --docs {args.docs}",
                file=sys.stderr,
            )
            return 0

        # ── Stage 5: phase loop (Span B) ──────────────────────────
        if stage == "5":
            # Determine M (total phases).
            if args.phases is not None:
                phase_total = args.phases
            else:
                phase_total = _resolve_prior_phase_total(mf_path)
                if phase_total is None:
                    print(
                        "stage 5 needs --phases M — rerun as: "
                        "run_stage.py --batch --phases M",
                        file=sys.stderr,
                    )
                    return 0

            # Find the last completed phase.
            last_completed = _last_completed_phase(mf_path)

            for n in range(last_completed + 1, phase_total + 1):
                phase = f"{n}/{phase_total}"
                print(f"\n── batch: stage 5 phase {phase} ──",
                      file=sys.stderr)

                # Re-read cfg to pick up any changes.
                cfg = load_config(args.config)
                rc = run_stage_once(
                    "5", cfg, args, prompt_file, phase=phase)
                if rc != 0:
                    print(
                        f"batch: stage 5 phase {phase} failed",
                        file=sys.stderr,
                    )
                    return rc

                # Automated verify-gate between phases (not after
                # the last phase).
                if n < phase_total:
                    print(
                        f"\n── verify-gate: stage 5 phase {phase} "
                        f"complete ──", file=sys.stderr)
                    # The verify-gate runs outside this script; the
                    # user must run ready-to-push or equivalent.

            continue  # proceed to Span C

        # ── Spans A & C: run non-gated stages ─────────────────────
        print(f"\n── batch: stage {stage} ──", file=sys.stderr)

        # Re-read cfg each stage.
        cfg = load_config(args.config)

        # Check if stage is already done.
        from run_state import compute_run_states  # type: ignore[import]
        states = compute_run_states(mf_path, args.docs)
        # For stage 5 (not in this branch) and stage 9, state is
        # always "done" if any record exists. For other stages,
        # check the unit name directly.
        stage_state = states.get(stage, "not-started")

        if stage_state == "done" and not args.force:
            print(f"  stage {stage}: done, skipping", file=sys.stderr)
            continue

        if stage_state == "done" and args.force:
            # --force re-runs a done stage; check for drift first.
            drift = detect_drift(stage, mf_path)
            if drift and not args.confirm_overwrite:
                print(
                    f"stage {stage}: drift detected on "
                    f"{len(drift)} tracked output(s):",
                    file=sys.stderr,
                )
                for d in drift:
                    print(
                        f"  {d['path']}: "
                        f"size {d['expected_size']}→{d['actual_size']}, "
                        f"mtime {d['expected_mtime']}→{d['actual_mtime']}",
                        file=sys.stderr,
                    )
                print(
                    f"use --force --confirm-overwrite to proceed",
                    file=sys.stderr,
                )
                return 1
            if drift:
                print(
                    f"  stage {stage}: --force overriding "
                    f"{len(drift)} drifted output(s)",
                    file=sys.stderr,
                )

        rc = run_stage_once(stage, cfg, args, prompt_file)
        if rc != 0:
            print(f"batch: stage {stage} failed", file=sys.stderr)
            return rc

    print("\nbatch: all non-gated stages complete", file=sys.stderr)
    return 0


def run_single(args: argparse.Namespace, mf_path: Path | None) -> int:
    """Execute one stage — positional or auto-selected — to completion.

    Runs under the pipeline lock that main() acquired for the whole
    execution block: resolve chain → pre-flight → subprocess(es) with
    timeout → post-flight → manifest record, walking the fallback chain.
    Returns the exit code (0 for success, non-zero for failure).
    """
    if args.stage is None:
        from run_state import first_pending_unit  # type: ignore[import]
        try:
            unit = first_pending_unit(mf_path, args.docs, args.from_stage)
        except ValueError as exc:
            sys.exit(str(exc))
        if unit is None:
            print("nothing to run: every stage is done", file=sys.stderr)
            return 0
        args.stage = unit.split()[0]
        if args.stage == "5":
            parts = unit.split(maxsplit=1)
            if len(parts) == 2:
                detected_phase = parts[1]
                if args.phase and args.phase != detected_phase:
                    sys.exit(f"--phase {args.phase} conflicts with detected "
                             f"phase {detected_phase}")
                args.phase = detected_phase
            elif args.phase is None:
                sys.exit("auto-select: stage 5 has no phase info in the manifest; "
                         "pass --phase N/M")
        print("auto-selected stage " + args.stage
              + (f"  phase={args.phase}" if args.phase else ""), file=sys.stderr)

    cfg = load_config(args.config)
    chain = resolve_chain(cfg, args.stage)
    timeout_s = resolve_timeout(cfg, args.stage, args.timeout)

    # Pre-flight: verify required input artifacts exist before spawning anything.
    preflight_check(args.stage, args.docs, args.phase, mf_path)

    cwd = args.cwd or (args.docs.resolve().parent.parent if args.docs else None)
    if cwd is not None:
        cwd = cwd.resolve()
        if not cwd.is_dir():
            sys.exit(f"stage working dir {cwd} is not a directory (from "
                     + ("--cwd" if args.cwd else "--docs") + ")")
        if args.docs and not args.docs.resolve().is_relative_to(cwd):
            sys.exit(f"--docs {args.docs} is outside the stage working dir {cwd}; "
                     "the stage session could not write its artifact. Pass --cwd "
                     "with a directory that contains --docs.")

    agent = (cfg.get("agents") or {}).get(args.stage)
    print(f"stage {args.stage}"
          + (f"  agent={agent}" if agent else "")
          + (f"  docs={args.docs}" if args.docs else "")
          + (f"  cwd={cwd}" if cwd else "")
          + (f"  timeout={timeout_s}s" if timeout_s else ""), file=sys.stderr)
    print("  chain: " + " -> ".join(f"{c['provider']}/{c['model']}" for c in chain), file=sys.stderr)

    # Absolute, because the child process may run in a different working directory.
    prompt_file = args.prompt.resolve() if args.prompt else Path(os.devnull)

    # Pre-compute outputs once; the same set applies to every attempt in the chain.
    outputs = build_outputs(args.stage, args.docs)

    for i, attempt in enumerate(chain):
        # Models later in the chain that share this provider can be handed to
        # claude's own --fallback-model, saving a process restart.
        same_provider = [c["model"] for c in chain[i + 1:] if c["provider"] == attempt["provider"]]
        try:
            argv, env = build_command(cfg, attempt, prompt_file, same_provider,
                                      strict=not args.dry_run)
        except FileNotFoundError as exc:
            print(f"  skip {attempt['provider']}/{attempt['model']}: '{exc}' not on PATH",
                  file=sys.stderr)
            continue

        label = f"{attempt['provider']}/{attempt['model']}"
        if args.dry_run:
            print(f"  [{i + 1}/{len(chain)}] {label}: {' '.join(argv)}", file=sys.stderr)
            continue

        print(f"  [{i + 1}/{len(chain)}] running {label}", file=sys.stderr)
        stdin = prompt_file.open() if args.prompt else subprocess.DEVNULL
        timed_out = False
        t_start = time.monotonic()
        log_path = None
        output_tail = None
        with LogCapture(args.stage, args.docs, args.phase) as (lp, lc):
            log_path = lp
            try:
                proc = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, env=env, cwd=cwd)
            except FileNotFoundError as exc:
                if args.prompt:
                    stdin.close()
                print(f"  skip {label}: '{exc}' not on PATH", file=sys.stderr)
                continue
            try:
                for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace")
                    if lc is not None:
                        lc.write(line)
                    else:
                        sys.stderr.write(line)
                        sys.stderr.flush()
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()
                if lc is not None:
                    lc.write("\n")
            finally:
                if args.prompt:
                    stdin.close()
            duration_s = time.monotonic() - t_start
            tail = lc.tail() if lc is not None else None
        postflight = postflight_check(args.stage, args.docs, outputs)
        record_attempt(args, attempt, i, len(chain), proc.returncode,
                        duration_s=duration_s, timed_out=timed_out,
                        phase=args.phase, outputs=postflight,
                        log_path=log_path, output_tail=tail)
        if proc.returncode == 0:
            print(f"  ok: {label}", file=sys.stderr)
            return 0
        print(f"  failed ({proc.returncode}): {label}", file=sys.stderr)

    if args.dry_run:
        return 0
    print("all attempts in the chain failed", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", nargs="?", default=None,
                    help="stage id as used in models.yaml (1a, 1b, 2, ... 9); "
                         "omit to auto-select the next stage that is not 'done'")
    ap.add_argument("--from", dest="from_stage", default=None,
                    help="start at this stage, skipping stages already 'done'; "
                         "cannot be combined with a positional stage")
    ap.add_argument("--status", action="store_true",
                    help="print every stage's computed state and exit "
                         "(requires --docs)")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {VERSION}")
    ap.add_argument("--config", type=Path, default=ROOT / "models.yaml")
    ap.add_argument("--prompt", type=Path, help="file containing the stage prompt")
    ap.add_argument("--docs", type=Path,
                    help="artifact dir; also the default for --manifest and --cwd")
    ap.add_argument("--cwd", type=Path,
                    help="working directory for the stage's CLI process (default: two levels "
                         "above --docs, which is the target repo when DOCS is <repo>/docs/mcp). "
                         "Claude Code confines file access to the directory it is launched "
                         "from, so this must contain the target repo — not wherever this "
                         "script was invoked from.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved chain and commands without running; "
                         "still holds the run lock when --docs is given")
    ap.add_argument("--manifest", type=Path,
                    help="append a record of what actually ran to this JSONL file "
                         "(default: <docs>/run-manifest.jsonl when --docs is given)")
    ap.add_argument("--phase", type=str, default=None,
                     help="phase tracking string 'N/M' (stage 5 only, e.g. '2/5')")
    ap.add_argument("--log-path", type=str, default=None,
                     help="path to the tee'd log file for this run")
    ap.add_argument("--timeout", type=int, default=None,
                     help=f"subprocess timeout in seconds (default: {DEFAULT_TIMEOUT})")
    ap.add_argument("--batch", action="store_true",
                     help="run contiguous non-gated spans in one process; "
                          "stops before human-gated stages {2, 3, 4} "
                          "and requires --phases for stage 5")
    ap.add_argument("--phases", type=int, default=None, metavar="M",
                     help="total number of phases for stage 5 (e.g. 5 for '1/5' .. '5/5')")
    ap.add_argument("--force", action="store_true",
                     help="re-run a 'done' stage, checking for output drift first")
    ap.add_argument("--confirm-overwrite", action="store_true",
                     help="proceed past drift detection when used with --force")
    args = ap.parse_args()

    mf_path = args.manifest or (args.docs / "run-manifest.jsonl" if args.docs else None)

    if args.status:
        if args.docs is None:
            sys.exit("--status requires --docs (the manifest lives at "
                     "<docs>/run-manifest.jsonl)")
        from run_state import compute_run_states, live_lock_pid  # type: ignore[import]
        states = compute_run_states(mf_path, args.docs)
        width = max([len(u) for u in states] + [len("stage")])
        print(f"{'stage':<{width}}  state")
        for unit, state in states.items():
            print(f"{unit:<{width}}  {state}")
        lock_pid = live_lock_pid(args.docs)
        if lock_pid is not None:
            print(f"# run lock held by live pid {lock_pid}", file=sys.stderr)
        return 0

    if args.from_stage is not None and args.stage is not None:
        sys.exit("--from STAGE cannot be combined with a positional stage")

    # --confirm-overwrite requires --force.
    if args.confirm_overwrite and not args.force:
        sys.exit("--confirm-overwrite requires --force")

    # --batch: run contiguous non-gated spans with human-gate stops.
    # Usage validation happens before the lock: a bad invocation must be
    # reported even while another run holds the lock.
    if args.batch:
        if args.docs is None:
            sys.exit("--batch requires --docs")
        if args.stage is not None:
            sys.exit("--batch cannot be combined with a positional stage")
        if args.phases is not None and args.phases < 1:
            sys.exit("--phases M must be >= 1")

    # ── Pipeline lock: held for the entire execution block ──────────────
    # Every mode that executes a stage — --batch, a positional stage, an
    # auto-selected stage, and --dry-run — acquires the run lock, so two
    # invocations cannot clobber the same docs tree or manifest. Previously
    # only --batch mode did, leaving single-stage runs unguarded. --status
    # stays lock-free: its job is to observe a run in progress. Without
    # --docs there is no lock path and nothing is written, so there is
    # nothing to serialize.
    from lock import RunLock, LockError  # type: ignore[import]

    lock = None
    if args.docs is not None:
        try:
            lock = RunLock(args.docs,
                           mode="batch" if args.batch else "single")
            lock.acquire()
        except LockError as exc:
            sys.exit(str(exc))

    try:
        if args.batch:
            return run_batch(args)
        return run_single(args, mf_path)
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
