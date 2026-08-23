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
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"no config at {path}\nCopy models.example.yaml to models.yaml and edit it.")
    return yaml.safe_load(path.read_text())


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


def record_attempt(args, attempt: dict, index: int, total: int, returncode: int) -> None:
    """Append what actually ran to a manifest.

    An artifact does not record which model produced it, so a run is otherwise
    unreproducible and unauditable: a stage that silently fell back to a weaker
    model looks identical to one that did not. The manifest is what lets a later
    reader tell the difference, and what makes a bad artifact traceable to a
    routing decision rather than to the prompt.
    """
    path = args.manifest or (args.docs / "run-manifest.jsonl" if args.docs else None)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "stage": args.stage,
            "provider": attempt["provider"],
            "model": attempt["model"],
            "attempt": f"{index + 1}/{total}",
            "returncode": returncode,
            "ok": returncode == 0,
        }) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", help="stage id as used in models.yaml (1a, 1b, 2, ... 9)")
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
                    help="print the resolved chain and commands without running")
    ap.add_argument("--manifest", type=Path,
                    help="append a record of what actually ran to this JSONL file "
                         "(default: <docs>/run-manifest.jsonl when --docs is given)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    chain = resolve_chain(cfg, args.stage)

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
          + (f"  cwd={cwd}" if cwd else ""), file=sys.stderr)
    print("  chain: " + " -> ".join(f"{c['provider']}/{c['model']}" for c in chain), file=sys.stderr)

    # Absolute, because the child process may run in a different working directory.
    prompt_file = args.prompt.resolve() if args.prompt else Path(os.devnull)

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
        try:
            result = subprocess.run(argv, env=env, stdin=stdin, cwd=cwd)
        finally:
            if args.prompt:
                stdin.close()
        record_attempt(args, attempt, i, len(chain), result.returncode)
        if result.returncode == 0:
            print(f"  ok: {label}", file=sys.stderr)
            return 0
        print(f"  failed ({result.returncode}): {label}", file=sys.stderr)

    if args.dry_run:
        return 0
    print("all attempts in the chain failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
