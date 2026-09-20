#!/usr/bin/env python3
"""Probe the per-stage permission surface's read-only enforcement against the
committed scratch repo (scratch-repo/).

Why: the playbook documents Stage 1 as read-only (mcp-server-creation-workflow.md
§Stage 1: "fresh session, repo mounted, read-only"), but the shipped contract
used to give every stage the same --permission-mode acceptEdits. The spec's
Open Question 2 asks whether claude supports path-scoped rules like
Edit(docs/mcp/**) so a stage can write its own artifact but not target source.
Using the per-stage permission contract from Task 1 (runner/stage_contract.py:
stage ids, permission_mode, allowed_tools/disallowed_tools) plus the Task 1
runner settings (models.example.yaml claude runner: --permission-mode,
--allowedTools/--disallowedTools, --settings pointing at the runner-owned
file), this probe:

  1. emits every stage's --dry-run argv and checks it against the contract;
  2. runs the read-only stage's surface against the scratch repo and confirms
     it CANNOT create a file outside docs/mcp, while a write stage's surface
     CAN (stages 5-8 legitimately modify the project);
  3. answers Open Question 2's mechanics (path-scoped Edit(docs/mcp/**)) in
     the harness and — because no deployed claude build can be probed in this
     offline checkout to positively confirm the syntax — records that the
     shipped contract uses the spec's prescribed fallback: read-only stages
     run the non-editing mode 'default' and the artifact-write exception is
     approximated (noted in runner/stage_contract.py).

The probe environment has no `claude` binary, so the permission decision is
exercised by an embedded stub that models Claude Code's documented
non-interactive -p semantics against the exact argv the runner emits: a deny
rule (settings `deny` or --disallowedTools) beats any allow; an allow rule
(settings `allow` or --allowedTools, path globs honored) auto-approves;
acceptEdits auto-approves file edits; any other mode declines an unapproved
Write/Edit (no TTY to ask). The stub performs the write itself, so the
transcript's "file created" assertions are real filesystem outcomes.

Writes the transcript to scratch-repo/probe-read-only-transcript.md and seeds
the missing preflight inputs under scratch-repo/docs/mcp so every stage's
--dry-run can emit argv against the fixture repo.

Run:  python3 runner/probe_read_only.py
Exit code is 0 only when every conformance check and enforcement scenario
passes and the transcript is written.
"""
from __future__ import annotations

import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

import run_stage
from stage_contract import STAGE_ORDER

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "runner"
SCRATCH_REPO = ROOT / "scratch-repo"
DOCS = SCRATCH_REPO / "docs" / "mcp"
TRANSCRIPT = SCRATCH_REPO / "probe-read-only-transcript.md"

# Directories the probe's write attempts must or must not reach. Everything
# outside DOCS (i.e. the rest of the scratch repo, "target source") is what a
# read-only stage must not be able to create.
OUTSIDE_DOCS_RELPATH = "probe-outside.txt"
INSIDE_DOCS_RELPATH = "docs/mcp/probe-scoped.txt"

# The scratch repo ships minimal fixtures (04-spec.md, placeholder.md) in its
# commit; preflight inputs 00-decisions..03-mcp-surface are not among them.
# The probe seeds those preflight inputs as stable fixtures (deterministic
# content, committed with the branch) so every stage's --dry-run can emit argv;
# files that already exist are left untouched (idempotent).
PREFLIGHT_STUBS: dict[str, str] = {
    "00-decisions.md": "# Decisions — probe fixture\nMODE: wrap\n",
    "01-instructions.md": "# Instructions — probe fixture (empty stub)\n",
    "01-signatures.md": "# Signatures — probe fixture (empty stub)\n",
    "02-capability-inventory.md": "# Capability inventory — probe fixture (empty stub)\n",
    "03-mcp-surface.md": "# MCP surface — probe fixture (empty stub)\n",
}

# The probe claude: models Claude Code's documented non-interactive permission
# decision for Write/Edit/Bash tool calls so the probe can exercise the exact
# argv the runner emits (--permission-mode, --allowedTools/--disallowedTools,
# --settings) without a deployed claude binary. The probe drives it with
# [[WRITE <repo-relative path>]] and [[BASH <cmd>]] directives in the stdin
# prompt; allowed writes actually touch the filesystem (relative to the working
# directory), so the probe's file-exists assertions are real outcomes.
STUB_CLAUDE = r'''#!/usr/bin/env __PYTHON__
"""Probe stub: documented Claude Code -p permission semantics for a small tool
surface (Write/Edit path rules, Bash, WebFetch* ignored for file writes).

Decision order (deny beats allow; a -p session has no TTY, so an unapproved
request is declined):
  1. hard deny: settings permissions.deny plus --disallowedTools, matched by
     fnmatch against Edit/Write(<path>) rules on the repo-relative path;
  2. allow: settings permissions.allow plus --allowedTools, matched the same
     way - path-scoped rules like Edit(docs/mcp/**) are honored as globs;
  3. --permission-mode acceptEdits auto-approves file edits;
  4. any other mode declines an unapproved Write/Edit.
"""
import fnmatch
import json
import re
import sys
from pathlib import Path


def _rule_parts(rule):
    name = rule.split("(", 1)[0]
    if "(" not in rule:
        return name, "*"
    return name, rule[rule.find("(") + 1:rule.rfind(")")]


def _file_rule_matches(rule, rel_path):
    name, body = _rule_parts(rule)
    if name not in ("Edit", "Write", "Read"):
        return False
    return fnmatch.fnmatch(rel_path, body or "*")


def _parse_argv(argv):
    mode = "default"
    allowed = []
    disallowed = []
    settings_path = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--permission-mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
        elif arg == "--allowedTools" and i + 1 < len(argv):
            allowed.append(argv[i + 1])
            i += 2
        elif arg == "--disallowedTools" and i + 1 < len(argv):
            disallowed.append(argv[i + 1])
            i += 2
        elif arg == "--settings" and i + 1 < len(argv):
            settings_path = argv[i + 1]
            i += 2
        else:
            i += 1
    return mode, allowed, disallowed, settings_path


def _load_permissions(settings_path):
    empty = {"allow": [], "deny": [], "ask": []}
    if not settings_path:
        return empty
    try:
        data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    except OSError:
        return empty
    return data.get("permissions", empty)


def _decide(mode, allowed, disallowed, rel_path):
    if Path(rel_path).is_absolute() or ".." in rel_path.replace("\\", "/").split("/"):
        return "DENY (path traversal)"
    for rule in disallowed:
        if _file_rule_matches(rule, rel_path):
            return "DENY (disallowed rule {})".format(rule)
    for rule in allowed:
        if _file_rule_matches(rule, rel_path):
            return "ALLOW (rule {})".format(rule)
    if mode == "acceptEdits":
        return "ALLOW (acceptEdits)"
    return "DENY (mode {}, unapproved in -p)".format(mode)


def main():
    mode, allowed, disallowed, settings_path = _parse_argv(sys.argv[1:])
    perms = _load_permissions(settings_path)
    allowed_rules = allowed + [str(r) for r in perms.get("allow", [])]
    disallowed_rules = disallowed + [str(r) for r in perms.get("deny", [])]
    prompt = sys.stdin.read()
    for kind, arg in re.findall(r"\[\[(WRITE|BASH) ([^\]]+)\]\]", prompt):
        if kind == "BASH":
            if any(str(r).startswith("Bash(") for r in disallowed_rules):
                print("bash={} decision=DENY (settings deny Bash(*))".format(arg))
            else:
                print("bash={} decision=ALLOW".format(arg))
            continue
        rel_path = arg.strip()
        decision = _decide(mode, allowed_rules, disallowed_rules, rel_path)
        print("mode={} write={} decision={}".format(mode, rel_path, decision))
        if decision.startswith("ALLOW"):
            target = Path.cwd() / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("probe-claude wrote this file\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# Probe routing config: mirrors models.example.yaml's claude runner (the Task 1
# runner settings) so build_command emits the same surface, with the cmd
# pointed at the probe stub instead of a deployed claude binary.
PROBE_CONFIG_YAML = """\
runners:
  probe-claude:
    cmd: {STUB}
    args: ["-p", "--model", "{model}", "--fallback-model", "{fallback}",
           "--permission-mode", "{permission_mode}",
           "--settings", "{settings_file}",
           "--strict-mcp-config", "--mcp-config", "{}"]
    allowed_tool_flag: "--allowedTools"
    disallowed_tool_flag: "--disallowedTools"
    settings_file: runner/settings/claude-settings.json
    api_key_var: PROBE_KEY
    base_url_var: PROBE_URL
    auth_optional: true
providers:
  probe:
    runner: probe-claude
    api_key_env: PROBE_KEY
    base_url_env: PROBE_URL
defaults:
  provider: probe
  model: probe-model
stages:
  "1a": {}
  "1b": {}
  "2": {}
  "3": {}
  "4": {}
  "5": {}
  "6": {}
  "7": {}
  "8": {}
  "9": {}
"""


def _write_stub_claude(path: Path) -> None:
    path.write_text(STUB_CLAUDE.replace("__PYTHON__", sys.executable),
                    encoding="utf-8")
    path.chmod(0o755)


def _write_probe_config(tdir: Path, stub: Path) -> Path:
    cfg_path = tdir / "models.yaml"
    cfg_path.write_text(PROBE_CONFIG_YAML.replace("{STUB}", str(stub)),
                        encoding="utf-8")
    return cfg_path


def _seed_preflight_stubs() -> list[str]:
    """Create the preflight input files stage 2/3/4 need --dry-run to emit argv
    against the scratch repo. Returns paths that were created."""
    created = []
    for name, content in PREFLIGHT_STUBS.items():
        path = DOCS / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            created.append(str(path.relative_to(ROOT)))
    return created


def _clean_probe_files() -> None:
    """Remove files the probe's ALLOW scenarios may have left behind."""
    for rel in (OUTSIDE_DOCS_RELPATH, INSIDE_DOCS_RELPATH):
        leftover = SCRATCH_REPO / rel
        if leftover.is_file() and not leftover.is_symlink():
            leftover.unlink()


def _dry_run_cli(stage: str, cfg_path: Path) -> tuple[int, str, list[str] | None]:
    """Run the real runner CLI in --dry-run for one stage and return
    (returncode, stderr, parsed argv). The CLI resolves build_command the same
    way a real run does, minus execution."""
    cmd = [sys.executable, str(RUNNER / "run_stage.py"), stage, "--dry-run",
           "--config", str(cfg_path), "--docs", str(DOCS)]
    if stage == "5":
        cmd += ["--phase", "1/5"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    argv: list[str] | None = None
    for line in proc.stderr.splitlines():
        m = re.match(r"^\s*\[1/1\] \S+: (.*)$", line)
        if m:
            argv = shlex.split(m.group(1))
    return proc.returncode, proc.stderr, argv


def _argv_problems(stage: str, argv: list[str]) -> list[str]:
    """Check one stage's emitted argv against the contract. Returns a list of
    problems (empty = conforms)."""
    mode, allowed, disallowed = run_stage.stage_permission_surface(stage)
    problems: list[str] = []

    if "--permission-mode" not in argv:
        problems.append("argv has no --permission-mode")
    else:
        emitted_mode = argv[argv.index("--permission-mode") + 1]
        if emitted_mode != mode:
            problems.append(
                f"--permission-mode {emitted_mode!r} != contract {mode!r}")

    if "--settings" not in argv:
        problems.append("argv has no --settings")
    else:
        emitted_settings = argv[argv.index("--settings") + 1]
        if emitted_settings != str(run_stage.DEFAULT_CLAUDE_SETTINGS):
            problems.append(
                "--settings does not point at the runner-owned file: "
                f"{emitted_settings}")

    emitted_allowed: set[str] = set()
    emitted_disallowed: set[str] = set()
    for i, tok in enumerate(argv[:-1]):
        if tok == "--allowedTools":
            emitted_allowed.add(argv[i + 1])
        elif tok == "--disallowedTools":
            emitted_disallowed.add(argv[i + 1])
    if emitted_allowed != set(allowed):
        problems.append(
            f"--allowedTools {sorted(emitted_allowed)} != contract {allowed}")
    if emitted_disallowed != set(disallowed):
        problems.append(
            f"--disallowedTools {sorted(emitted_disallowed)} != "
            f"contract {disallowed}")
    if any(str(t).startswith("Bash") for t in emitted_allowed):
        problems.append("Bash auto-approved via --allowedTools (posture)")

    return problems


def _render_argv(argv: list[str], stub: Path) -> str:
    """Render an argv for the transcript: repo-relative, machine-independent."""
    rendered = " ".join(shlex.quote(str(a)) for a in argv)
    rendered = rendered.replace(shlex.quote(str(stub)), "$PROBE_STUB")
    return rendered.replace(
        shlex.quote(str(run_stage.DEFAULT_CLAUDE_SETTINGS)),
        "runner/settings/claude-settings.json")


def _run_stub_stage(stage: str, cfg: dict, prompt_text: str,
                    stub: Path) -> subprocess.CompletedProcess:
    """Launch one stage's emitted argv (build_command) against the scratch
    repo with the probe stub as the 'claude' CLI."""
    argv, env = run_stage.build_command(
        cfg, {"provider": "probe", "model": "probe-model"},
        stage, Path(os.devnull), [], strict=True)
    return subprocess.run(argv, cwd=str(SCRATCH_REPO), env=env,
                          input=prompt_text, capture_output=True, text=True)


def main() -> int:
    if not SCRATCH_REPO.is_dir():
        print(f"scratch repo missing: {SCRATCH_REPO}", file=sys.stderr)
        return 1

    _clean_probe_files()
    seeded = _seed_preflight_stubs()
    all_ok = True
    lines: list[str] = []
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M %Z")

    def out(line: str = "") -> None:
        lines.append(line)

    out("# Probe transcript — read-only enforcement vs. scratch repo")
    out()
    out(f"Date: {now}")
    out("Command: python3 runner/probe_read_only.py")
    out(f"Target repo: scratch-repo/   (artifact dir DOCS = scratch-repo/docs/mcp)")
    out()
    out("Grounding: mcp-server-creation-workflow.md §Stage 1 (\"Run in: fresh "
        "session, repo mounted, read-only\"); models.example.yaml claude runner "
        "(per-stage --permission-mode, --allowedTools/--disallowedTools, "
        "--settings → runner-owned file); per-stage permission contract from "
        "Task 1 (runner/stage_contract.py: stage ids, permission_mode, "
        "allowed_tools/disallowed_tools).")
    out()
    out("Environment:")
    out(f"- claude CLI on PATH: {'yes' if shutil.which('claude') else 'no'} — the "
        "permission decision is exercised by the probe stub (embedded in this "
        "script), which applies Claude Code's documented non-interactive -p "
        "semantics (deny beats allow; unapproved Write/Edit is declined) against "
        "the exact argv the runner emits. The stub performs the write itself, so "
        "the file-exists assertions below are real filesystem outcomes. The stub "
        "declines absolute and '..'-escaped write paths; it does not model "
        "symlink escapes (the shipped surfaces are all exercised with "
        "repo-relative paths).")
    out()
    out("This file is a snapshot of the last probe run; rerun "
        "`python3 runner/probe_read_only.py` to refresh it.")
    if seeded:
        out(f"- seeded preflight stubs into scratch-repo/docs/mcp (for --dry-run "
            "argv of stages 2/3/4): {', '.join(seeded)}")
    else:
        out("- preflight stubs already present under scratch-repo/docs/mcp")
    out()

    # ── Part 1: per-stage --dry-run argv vs. contract ─────────────────────
    out("## 1. Per-stage --dry-run argv vs. contract")
    out()
    out("Every stage's emitted --dry-run argv must carry exactly the contract's "
        "permission_mode, the contract's allowed/disallowed tool flags, and the "
        "runner-owned --settings file.")
    out()

    with tempfile.TemporaryDirectory(prefix="probe-read-only-") as td:
        tdir = Path(td)
        stub = tdir / "probe-claude"
        _write_stub_claude(stub)
        cfg_path = _write_probe_config(tdir, stub)
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        os.environ.setdefault("PROBE_KEY", "probe-key")
        os.environ.setdefault("PROBE_URL", "https://probe.invalid/v1")

        for stage in STAGE_ORDER:
            mode, allowed, disallowed = run_stage.stage_permission_surface(stage)
            rc, stderr, argv = _dry_run_cli(stage, cfg_path)
            if rc != 0:
                all_ok = False
                out(f"### stage {stage} — DRY-RUN FAILED (rc={rc})")
                out("```")
                out(stderr.strip())
                out("```")
                out()
                continue
            if argv is None:
                all_ok = False
                out(f"### stage {stage} — NO ARGV EMITTED")
                out()
                continue
            problems = _argv_problems(stage, argv)
            if problems:
                all_ok = False
                out(f"### stage {stage} — CONFORMANCE FAILED")
                out()
                out(f"emitted argv: `{_render_argv(argv, stub)}`")
                out()
                out("problems:")
                for p in problems:
                    out(f"- {p}")
                out()
            else:
                out(f"### stage {stage} — permission_mode=`{mode}`")
                out()
                out(f"argv: `{_render_argv(argv, stub)}`")
                out()
                out("conformance: PASS")
                out()

        # ── Part 2: enforcement probe ──────────────────────────────────────
        out("## 2. Enforcement: file creation outside docs/mcp")
        out()
        out("Launch each stage's emitted argv against the scratch repo (cwd = "
            "scratch-repo/) with the probe stub as the claude CLI.")

        # Scenario A: read-only stage 1a
        read_only_argv, _ = run_stage.build_command(
            cfg, {"provider": "probe", "model": "probe-model"},
            "1a", Path(os.devnull), [], strict=True)
        out()
        out("### Read-only stage 1a (contract permission_mode=`default`)")
        out()
        out(f"argv: `{_render_argv(read_only_argv, stub)}`")
        out()

        proc = _run_stub_stage("1a", cfg,
                               f"[[WRITE {OUTSIDE_DOCS_RELPATH}]]\n", stub)
        created = (SCRATCH_REPO / OUTSIDE_DOCS_RELPATH).exists()
        ok = "decision=DENY" in proc.stdout and not created
        all_ok = all_ok and ok
        out(f"- prompt: `[[WRITE {OUTSIDE_DOCS_RELPATH}]]`")
        out(f"  stub: `{proc.stdout.strip()}`")
        out(f"  file created: {'yes' if created else 'no'}")
        out(f"  ✓ read-only stage FAILS to create a file outside docs/mcp"
            if ok else "  ✗ read-only stage created a file outside docs/mcp")
        out()

        proc = _run_stub_stage("1a", cfg,
                               f"[[WRITE {INSIDE_DOCS_RELPATH}]]\n", stub)
        created = (SCRATCH_REPO / INSIDE_DOCS_RELPATH).exists()
        ok = "decision=DENY" in proc.stdout and not created
        all_ok = all_ok and ok
        out(f"- prompt: `[[WRITE {INSIDE_DOCS_RELPATH}]]`")
        out(f"  stub: `{proc.stdout.strip()}`")
        out(f"  file created: {'yes' if created else 'no'}")
        out(f"  ✓ read-only stage cannot auto-accept even its own artifact "
            f"write — the artifact-write exception is approximated (capture "
            f"01-instructions.md / 01-signatures.md from the stage's output)"
            if ok
            else "  ✗ read-only stage wrote its artifact via the permission surface")
        out()

        proc = _run_stub_stage("1a", cfg, "[[BASH echo probe]]\n", stub)
        ok = "decision=DENY" in proc.stdout
        all_ok = all_ok and ok
        out(f"- prompt: `[[BASH echo probe]]`")
        out(f"  stub: `{proc.stdout.strip()}`")
        out(f"  ✓ Bash stays denied (runner-owned settings deny Bash(*))"
            if ok else "  ✗ Bash allowed")
        out()

        # Scenario B: write stage 5
        out("### Write stage 5 (contract permission_mode=`acceptEdits`)")
        write_argv, _ = run_stage.build_command(
            cfg, {"provider": "probe", "model": "probe-model"},
            "5", Path(os.devnull), [], strict=True)
        out()
        out(f"argv: `{_render_argv(write_argv, stub)}`")
        out()
        proc = _run_stub_stage("5", cfg,
                               f"[[WRITE {OUTSIDE_DOCS_RELPATH}]]\n", stub)
        created = (SCRATCH_REPO / OUTSIDE_DOCS_RELPATH).is_file()
        ok = "decision=ALLOW" in proc.stdout and created
        all_ok = all_ok and ok
        out(f"- prompt: `[[WRITE {OUTSIDE_DOCS_RELPATH}]]`")
        out(f"  stub: `{proc.stdout.strip()}`")
        out(f"  file created: {'yes' if created else 'no'}"
            + (" (removed after the probe)" if created else ""))
        out(f"  ✓ write stage CAN create a file outside docs/mcp — stages 5-8 "
            f"legitimately modify the project" if ok
            else "  ✗ write stage could not create a file outside docs/mcp")
        out()
        # Remove the write stage's file now so the path-scoped experiment below
        # starts from a clean tree (a leftover would false-positive its
        # "file created" assertion).
        _clean_probe_files()

        # ── Part 3: Open Question 2 — path-scoped Edit(docs/mcp/**) ────────
        out("## 3. Open Question 2: path-scoped rules like Edit(docs/mcp/**)")
        out()
        out("The spec asks whether claude supports path-scoped rules so a "
            "read-only stage can write its own artifact but not target source. "
            "The harness honors the glob syntax (below); this checkout is "
            "offline with no claude binary, so the rule syntax could not be "
            "positively confirmed against a deployed claude build. Per the "
            "spec's prescription the shipped contract therefore does NOT rely "
            "on it: read-only stages fall back to the non-editing mode "
            "`default` and the artifact-write exception is approximated "
            "(noted in runner/stage_contract.py).")
        out()
        out("Harness experiment (mode=`default` + `--allowedTools "
            "Edit(docs/mcp/**)`) — NOT the shipped surface:")
        out()
        scoped_argv = list(read_only_argv)
        scoped_argv += ["--allowedTools", "Edit(docs/mcp/**)"]
        scoped_prompt = f"[[WRITE {INSIDE_DOCS_RELPATH}]]\n"
        proc_in = subprocess.run(
            scoped_argv, cwd=str(SCRATCH_REPO),
            input=scoped_prompt, capture_output=True, text=True)
        created = (SCRATCH_REPO / INSIDE_DOCS_RELPATH).is_file()
        ok = "decision=ALLOW" in proc_in.stdout and created
        all_ok = all_ok and ok
        out(f"- prompt: `[[WRITE {INSIDE_DOCS_RELPATH}]]`")
        out(f"  stub: `{proc_in.stdout.strip()}`")
        out(f"  file created: {'yes' if created else 'no'}"
            + (" (removed after the probe)" if created else ""))
        out()
        proc_out = subprocess.run(
            scoped_argv, cwd=str(SCRATCH_REPO),
            input=f"[[WRITE {OUTSIDE_DOCS_RELPATH}]]\n",
            capture_output=True, text=True)
        created_out = (SCRATCH_REPO / OUTSIDE_DOCS_RELPATH).exists()
        ok = "decision=DENY" in proc_out.stdout and not created_out
        all_ok = all_ok and ok
        out(f"- prompt: `[[WRITE {OUTSIDE_DOCS_RELPATH}]]`")
        out(f"  stub: `{proc_out.stdout.strip()}`")
        out(f"  file created: {'yes' if created_out else 'no'}")
        out("  ✓ path-scoped semantics: artifact writes allowed, source "
            "blocked" if ok else "  ✗ path-scoped semantics not observed")
        out()

        # ── Result ─────────────────────────────────────────────────────────
        verdict = "PASS" if all_ok else "FAIL"
        out("## Result")
        out()
        if verdict == "PASS":
            out("PASS — the read-only stage cannot create a file outside "
                "docs/mcp while the write stage can, and every stage's emitted "
                "--dry-run argv matches the contract.")
        else:
            out("FAIL — one or more checks above did not pass (see the ✗ "
                "markers).")
        out()

    _clean_probe_files()
    TRANSCRIPT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"transcript: {TRANSCRIPT}", file=sys.stderr)
    if not all_ok:
        print("probe FAILED — see transcript", file=sys.stderr)
        return 1
    print("probe OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())