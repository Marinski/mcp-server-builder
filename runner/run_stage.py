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

The argv a stage runs is per-stage: permission_mode and the allowed/
disallowed tool lists come from runner/stage_contract.py, and the claude
runner points --settings at a settings file shipped under this checkout
(runner/settings/claude-settings.json). Claude Code merges permission rules
across settings scopes, so the shipped file pairs its WebFetch allowlist
with hard deny rules (Bash(*) and mcp__*); a deny from any scope beats an
allow from a lower scope, which is what keeps a third-party clone's
.claude/settings.json from widening the permission set. See README
"Non-interactive sessions and permissions".

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
import tempfile
import time
from pathlib import Path
from typing import IO, Iterable

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

    Empty array when --docs is not given.
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

    Stage 5 additionally validates --phase N/M and M consistency.
    """
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
    """
    if docs is None:
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


def _ensure_regular_target(path: Path | str, *, root: Path | str | None = None,
                           what: str = "file") -> None:
    """Refuse (OSError) when *path* or any component under *root* is a symlink.

    The runner's own artifact writes must never follow a symlink: git stores
    symlinks as the link itself, so a target repo that ships e.g.
    ``docs/mcp/logs`` or ``docs/mcp/run-manifest.jsonl`` as a symlink would
    otherwise turn the runner into an arbitrary-file write/append primitive
    (finding 5405). This check runs *before* mkdir() so a symlinked directory
    cannot be silently reused either; the containment of the resolved real
    path within *root* is enforced by :func:`safe_open`.

    Only components at or below *root* are inspected: ancestors above the
    artifact dir are the operator's own environment, not repo-shipped content.
    """
    path = Path(path)
    # lstat the existing target and refuse on a symlink. os.path.islink does
    # an lstat under the hood and is False for nonexistent paths.
    if os.path.islink(path):
        raise OSError(f"refusing to write {what} {path}: it is a symlink")

    if root is not None:
        root = Path(root)
        try:
            rel = path.relative_to(root)
        except ValueError:
            # Not lexically under root (e.g. an explicit --manifest outside
            # --docs); nothing below root to walk, containment is checked in
            # safe_open().
            rel = None
        if rel is not None:
            cur = root
            for part in rel.parts:
                cur = cur / part
                if os.path.islink(cur):
                    raise OSError(
                        f"refusing to write {what} {path}: {cur} is a symlink")


def safe_open(path: Path | str, mode: str, *, root: Path | str | None = None,
              what: str = "file") -> IO[str]:
    """Open *path* in *mode* without following symlinks.

    Used for the runner's own artifact writes (the tee'd log under
    ``<docs>/logs`` and the ``run-manifest.jsonl`` append). Refuses with
    OSError when the target or any component under *root* is a symlink
    (:func:`_ensure_regular_target`), when the resolved real path escapes
    *root* (a repo-shipped ``docs/mcp/logs`` → /tmp link must not become an
    arbitrary-file write), and opens with ``os.O_NOFOLLOW`` where the OS
    supports it so the target cannot be swapped for a link between the check
    and the open.
    """
    path = Path(path)
    _ensure_regular_target(path, root=root, what=what)

    if root is not None:
        real_root = Path(os.path.realpath(root))
        real = Path(os.path.realpath(path))
        if not real.is_relative_to(real_root):
            raise OSError(
                f"refusing to write {what} {path}: resolved path "
                f"{real} escapes {real_root}")

    flags = {
        "r": os.O_RDONLY,
        "r+": os.O_RDWR,
        "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        "w+": os.O_RDWR | os.O_CREAT | os.O_TRUNC,
        "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        "a+": os.O_RDWR | os.O_CREAT | os.O_APPEND,
    }.get(mode)
    if flags is None:
        raise ValueError(f"unsupported mode {mode!r} for {what} {path}")
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding="utf-8")


# Generic credential-shaped patterns, matched even when the exact value
# isn't known ahead of time (a secret minted by the stage's own tool calls,
# not just the one this runner injected). Deliberately narrow enough not to
# mangle non-secret content like a git SHA or a long hex hash.
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),              # OpenAI/Anthropic-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),          # GitHub PAT/OAuth/app tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),        # GitHub fine-grained PAT
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),           # AWS access key id
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),        # Slack tokens
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{10,}"),      # Bearer <token>
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
               r".*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]

# Env var *names* that look credential-shaped, regardless of provenance
# (the resolved provider credential lands on runner['api_key_var'], e.g.
# OPENAI_API_KEY/ANTHROPIC_API_KEY; a pass_env entry like GITHUB_TOKEN
# matches the same way). HTTPS_PROXY or PATH do not match.
_SECRET_ENV_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


def secret_values_from_env(env: dict) -> dict[str, str]:
    """Env entries whose *name* looks credential-shaped, for redact().

    Called on the child env build_command already resolved, so it covers
    both the injected provider credential and any pass_env value the
    operator opted into, without redact() needing to know which is which.
    """
    return {name: value for name, value in env.items()
            if value and _SECRET_ENV_NAME.search(name)}


def redact(text: str, secret_values: dict[str, str] | None = None) -> str:
    """Mask known secret values and generic credential-shaped patterns.

    ``secret_values`` maps an env var name to its value (typically from
    :func:`secret_values_from_env`); each occurrence is replaced with a
    stable ``[REDACTED:NAME]`` placeholder so redacted output stays
    diffable. Generic credential shapes are masked even when the exact
    value wasn't known ahead of time (findings 5388, 5406, 5411, 5416).
    """
    for name, value in (secret_values or {}).items():
        if value:
            text = text.replace(value, f"[REDACTED:{name}]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class LogCapture:
    """Tee stdout+stderr to a log file while streaming to console.

    Records the last ~50 lines as output_tail for the manifest. Every write
    is redacted first (:func:`redact`), so neither the console stream, the
    persisted log file, nor the in-memory tail can carry a known secret
    value or a generic credential-shaped string.
    Used as a context manager: returns (log_path_str, output_tail_str).
    Falls back to (None, None) when --docs is not given.
    """

    TAIL_LINES = 50

    def __init__(self, stage: str, docs: Path | None,
                 phase: str | None = None,
                 secret_values: dict[str, str] | None = None) -> None:
        self.stage = stage
        self.docs = docs
        self.phase = phase
        self.secret_values = secret_values
        self._fh = None
        self._log_path = None
        self._tail_lines: list[str] = []

    def __enter__(self):
        if self.docs is None:
            return None, None
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        log_dir = self.docs / "logs"
        name = f"{self.stage}"
        if self.phase:
            name += f"-phase{self.phase.split('/')[0]}"
        name += f"-{ts}.log"
        self._log_path = str(log_dir / name)
        # The log lives under the artifact dir, which the target repo owns:
        # refuse to follow a repo-shipped symlink (finding 5405) instead of
        # turning the runner into an arbitrary-file write. Checked before
        # mkdir so a symlinked docs/logs cannot be quietly reused, and again
        # at open time with O_NOFOLLOW.
        log_path = log_dir / name
        _ensure_regular_target(log_path, root=self.docs, what="log file")
        log_dir.mkdir(parents=True, exist_ok=True)
        self._fh = safe_open(log_path, "w", root=self.docs, what="log file")
        return self._log_path, self

    def write(self, text: str) -> None:
        """Write text to both console (stderr) and log file, redacted."""
        text = redact(text, self.secret_values)
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


def _resolve_provider_runner(cfg: dict, attempt: dict) -> tuple[dict, dict]:
    """Resolve an attempt's provider and its runner from the config."""
    provider_name = attempt["provider"]
    provider = (cfg.get("providers") or {}).get(provider_name)
    if not provider:
        sys.exit(f"unknown provider '{provider_name}' — is it commented out in models.yaml?")

    runner_name = provider.get("runner")
    runner = (cfg.get("runners") or {}).get(runner_name)
    if not runner:
        sys.exit(f"provider '{provider_name}' names unknown runner '{runner_name}'")

    return provider, runner


# Environment variables the child CLI needs in order to start at all. Anything
# not named here is withheld: the stage agent has no business seeing the
# invoking shell's full environment, and forwarding it hands unrelated
# credentials (AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, ...) to a process we do not
# control. Values are copied only when the parent actually sets them, so an
# unset variable stays unset in the child.
_CHILD_ENV_ALWAYS = ("PATH", "HOME", "LANG")
# Windows CLIs resolve their runtime and temporary directories through these;
# POSIX CLIs do not need them.
_CHILD_ENV_WINDOWS = ("SYSTEMROOT", "TEMP", "USERPROFILE")


# Explicit opt-in pass-through: a `pass_env` list on the provider (or, as a
# broader opt-in, the runner) names ambient variables the stage's CLI needs and
# the base allowlist does not cover — HTTPS_PROXY in a corporate network, say.
# Names are validated against a known-safe pattern so a typo in models.yaml
# cannot smuggle a shell metacharacter or a nonsense key into the child, and a
# name absent from the parent is skipped silently: pass_env grants
# pass-through, not invention.
_PASS_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _pass_env_names(cfg: dict, provider: dict, runner: dict) -> list[str]:
    """pass_env names declared on the provider, then on the runner (union)."""
    names: list = []
    for source in (runner.get("pass_env"), provider.get("pass_env")):
        if not source:
            continue
        if not isinstance(source, list) or not all(
                isinstance(n, str) and _PASS_ENV_NAME.match(n) for n in source):
            sys.exit("pass_env must be a list of variable names matching "
                     f"{_PASS_ENV_NAME.pattern} — got {source!r}")
        names += [n for n in source if n not in names]
    return names


def _apply_pass_env(env: dict, cfg: dict, provider: dict, runner: dict,
                    parent_env: dict) -> None:
    """Copy pass_env names present in the parent into env.

    Applied *before* the provider credential overlay so a pass_env entry can
    never overwrite the resolved credential, whatever the name collision.
    """
    for name in _pass_env_names(cfg, provider, runner):
        if name in parent_env:
            env[name] = parent_env[name]


def _base_child_env(parent_env: dict) -> dict:
    """The minimal environment a CLI needs to launch, before provider overlay."""
    names = list(_CHILD_ENV_ALWAYS)
    if sys.platform == "win32":
        names += _CHILD_ENV_WINDOWS
    env: dict = {name: parent_env[name] for name in names if name in parent_env}
    # Locale is set per-category (LC_ALL, LC_CTYPE, LC_TIME, ...); a CLI that
    # renders text before the provider config is read needs all of them.
    for name, value in parent_env.items():
        if name.startswith("LC_"):
            env[name] = value
    return env


# The runner-owned settings file for the claude runner. Permissions for a
# stage come from THIS file (under the pipeline checkout), never from the
# target repo's .claude/settings.json — a third-party clone must not be able
# to widen the permission set (findings 5404, 5417). models.yaml's claude
# runner references it via --settings {settings_file}; `settings_file` on a
# runner overrides the path.
DEFAULT_CLAUDE_SETTINGS = ROOT / "runner" / "settings" / "claude-settings.json"


def resolve_settings_file(runner: dict) -> str:
    """Absolute path to a runner's settings file.

    Defaults to the file shipped under this checkout (DEFAULT_CLAUDE_SETTINGS);
    models.yaml can override with `settings_file` on the runner. Relative
    paths resolve against the checkout root so the value stays committable
    and portable.
    """
    value = runner.get("settings_file")
    if not value:
        return str(DEFAULT_CLAUDE_SETTINGS)
    p = Path(value)
    return str(p if p.is_absolute() else ROOT / p)


def _isolated_config_dir() -> str:
    """A fresh, empty config dir for a stage's CLI process.

    This is the opt-in fallback for claude versions whose --settings flag
    does not outrank the target repo's .claude/settings.json (see the probe
    in test_run_stage.py): pointing CLAUDE_CONFIG_DIR at this scratch dir
    removes the invoking user's ~/.claude settings, skills and OAuth
    credentials from the session, and gives the stage a deterministic empty
    settings file.

    Enable per runner with `isolate_config: true`. With the shipped default
    this isolation is NOT applied: the invoking user's ~/.claude settings
    (whose allow rules merge with the runner-owned file), skills and OAuth
    credentials are part of every session — treat the invocation user as
    trusted, and prefer running stages in a container (see README
    "Sandboxing"). Isolating CLAUDE_CONFIG_DIR also hides any OAuth login
    stored in ~/.claude, so a stage then needs a key via the provider's
    api_key_env. The scratch dir is left in place so the child can keep
    reading it for the lifetime of the session.
    """
    d = Path(tempfile.mkdtemp(prefix="mcp-builder-claude-config-"))
    (d / "settings.json").write_text("{}", encoding="utf-8")
    return str(d)


def build_child_env(cfg: dict, attempt: dict, parent_env: dict,
                    strict: bool = True) -> dict:
    """Build the environment for a stage's CLI subprocess.

    Starts from a small allowlist (``_base_child_env``) rather than
    ``os.environ.copy()`` so the child inherits only what it needs to launch,
    then the provider's/runner's explicit ``pass_env`` names that the parent
    actually sets, and finally overlays the resolved provider's endpoint and
    credential — so a pass_env entry can never overwrite the credential.
    Nothing else crosses the boundary: a variable the parent sets and this
    function does not name is simply absent in the child.
    """
    provider_name = attempt["provider"]
    provider, runner = _resolve_provider_runner(cfg, attempt)

    env = _base_child_env(parent_env)
    _apply_pass_env(env, cfg, provider, runner, parent_env)

    # Fallback isolation: point the CLI's config dir at a fresh scratch dir so
    # the invoking user's ~/.claude (settings that could widen permissions,
    # skills, OAuth credentials) never leaks into the session. Off by default
    # because claude's own --settings flag already replaces the target repo's
    # project settings, and because isolating the config dir hides OAuth
    # logins. Enable per runner with `isolate_config: true`.
    if runner.get("isolate_config"):
        env["CLAUDE_CONFIG_DIR"] = _isolated_config_dir()

    # Point the runner at this provider's endpoint. Values come from the
    # environment, never from the config file, so models.yaml stays committable.
    base_url_env = provider.get("base_url_env")
    if base_url_env:
        value = parent_env.get(base_url_env)
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
        value = parent_env.get(key_env)
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

    return env


def stage_permission_surface(stage: str) -> tuple[str, list[str], list[str]]:
    """The stage contract's permission surface: (permission_mode, allowed_tools,
    disallowed_tools).

    Unknown stages fall back to the least permissive mode ("default") so a
    typo in a routing config can never silently widen a stage's surface.
    """
    from stage_contract import STAGE_CONTRACT  # type: ignore[import]

    entry = STAGE_CONTRACT.get(stage) or {}
    return (
        entry.get("permission_mode", "default"),
        list(entry.get("allowed_tools", [])),
        list(entry.get("disallowed_tools", [])),
    )


def build_command(cfg: dict, attempt: dict, stage: str, prompt_file: Path,
                  same_provider_fallbacks: list[str], strict: bool = True) -> tuple[list[str], dict]:
    """Build the argv/env for one stage attempt.

    The emitted argv is per-stage: the ``{permission_mode}``, ``{settings_file}``
    and ``{stage}`` placeholders and the per-stage allowed/disallowed tool
    lists come from runner/stage_contract.py, so every stage gets its own
    permission surface instead of one identical surface for all (finding 5404).
    The ``--settings`` flag points at a settings file shipped under THIS
    checkout (runner/settings/claude-settings.json by default), never at the
    target repo's .claude/settings.json. Claude Code merges permission rules
    across scopes, so the file also hard-denies Bash(*) and mcp__*: a deny
    rule from any scope beats an allow from a lower scope, which is what
    stops a third-party clone from widening the permission set (finding 5417 —
    a clone can at most add allow rules, and those cannot override the deny).
    """
    provider_name = attempt["provider"]
    provider, runner = _resolve_provider_runner(cfg, attempt)

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

    # The per-stage permission surface. Every stage resolves a permission_mode;
    # allowed/disallowed tools are the stage contract's per-stage lists.
    permission_mode, allowed_tools, disallowed_tools = stage_permission_surface(stage)

    # Resolve once per stage attempt, not once per arg token below.
    settings_file = resolve_settings_file(runner)

    argv = [cmd_name]
    for raw in runner.get("args", []):
        argv.append(
            raw.replace("{model}", attempt["model"])
               .replace("{provider}", provider_arg)
               .replace("{fallback}", fallback_arg)
               .replace("{prompt_file}", str(prompt_file))
               .replace("{stage}", stage)
               .replace("{permission_mode}", permission_mode)
               .replace("{settings_file}", settings_file)
        )

    # Per-stage tool allow/deny, emitted as repeated flag/value pairs (claude
    # accumulates repeated --allowedTools/--disallowedTools). Only runners that
    # name a *_tool_flag get them; opencode/pi have no such CLI surface.
    for tool in allowed_tools:
        if runner.get("allowed_tool_flag"):
            argv += [runner["allowed_tool_flag"], tool]
    for tool in disallowed_tools:
        if runner.get("disallowed_tool_flag"):
            argv += [runner["disallowed_tool_flag"], tool]

    env = build_child_env(cfg, attempt, os.environ, strict=strict)
    return argv, env


def record_attempt(args, attempt: dict, index: int, total: int, returncode: int,
                    *, duration_s: float, timed_out: bool,
                    phase: str | None = None,
                    outputs: list[dict] | None = None,
                    log_path: str | None = None,
                    output_tail: str | None = None,
                    secret_values: dict[str, str] | None = None) -> None:
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
      outputs      – [{path, shape, exists, size, mtime}]
      log_path     – path to the tee'd log file (if given)
      output_tail  – last ~50 lines of stdout+stderr (if captured)

    ``output_tail`` is redacted again here (:func:`redact`), even though a
    caller sourced from :class:`LogCapture` already redacted it on the way
    in: this is the last stop before the value is durably persisted to the
    manifest, so it stays safe even if a future caller passes an
    unredacted tail directly (findings 5388, 5406, 5411, 5416).
    """
    if output_tail is not None:
        output_tail = redact(output_tail, secret_values)
    path = args.manifest or (args.docs / "run-manifest.jsonl" if args.docs else None)
    if path is None:
        return
    path = Path(path)
    # The manifest records what actually ran. Append it without ever
    # following a symlink: git stores symlinks, so a target repo could
    # otherwise ship run-manifest.jsonl as a link that turns the runner
    # into an arbitrary-file append primitive (finding 5405). Root
    # containment applies when the manifest lives under --docs; an explicit
    # --manifest elsewhere is the operator's own path, so only the
    # final-component and O_NOFOLLOW guards apply there.
    root = None
    if args.docs is not None:
        try:
            path.relative_to(args.docs)
            root = args.docs
        except ValueError:
            root = None
    _ensure_regular_target(path, root=root, what="manifest")
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
    with safe_open(path, "a", root=root, what="manifest") as fh:
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
    if stage == "5":
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
            argv, env = build_command(cfg, attempt, stage, prompt_file,
                                      same_provider, strict=True)
        except FileNotFoundError as exc:
            print(f"  skip {attempt['provider']}/{attempt['model']}: "
                  f"'{exc}' not on PATH", file=sys.stderr)
            continue
        secret_values = secret_values_from_env(env)

        label = f"{attempt['provider']}/{attempt['model']}"
        print(f"  [{i + 1}/{len(chain)}] running {label}", file=sys.stderr)
        stdin = prompt_file.open() if args.prompt else subprocess.DEVNULL
        timed_out = False
        t_start = time.monotonic()
        log_path = None
        output_tail = None
        with LogCapture(stage, args.docs, phase, secret_values) as (lp, lc):
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
                        sys.stderr.write(redact(line, secret_values))
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
                       log_path=log_path, output_tail=tail,
                       secret_values=secret_values)
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

    Span A runs 1a then 1b sequentially (holding the lock).
    Stops before human-gated stages {2, 3, 4}.
    Span B handles stage 5's phase loop (last-completed+1 .. M, with
    automated verify-gate between phases).
    Span C runs 6, 7, 8, 9 sequentially.
    """
    cfg = load_config(args.config)
    mf_path = args.manifest or (args.docs / "run-manifest.jsonl"
                                if args.docs else None)

    from lock import RunLock, LockError  # type: ignore[import]
    from run_state import first_pending_unit  # type: ignore[import]
    from stage_contract import STAGE_CONTRACT, STAGE_ORDER  # type: ignore[import]

    # Acquire the lock for the entire batch duration.
    try:
        lock = RunLock(args.docs, mode="batch")
        lock.acquire()
    except LockError as exc:
        sys.exit(str(exc))

    try:
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

    finally:
        lock.release()


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
                    help="print the resolved chain and commands without running")
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
    if args.batch:
        if args.docs is None:
            sys.exit("--batch requires --docs")
        if args.stage is not None:
            sys.exit("--batch cannot be combined with a positional stage")
        if args.phases is not None and args.phases < 1:
            sys.exit("--phases M must be >= 1")
        return run_batch(args)

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
            argv, env = build_command(cfg, attempt, args.stage, prompt_file,
                                      same_provider, strict=not args.dry_run)
        except FileNotFoundError as exc:
            print(f"  skip {attempt['provider']}/{attempt['model']}: '{exc}' not on PATH",
                  file=sys.stderr)
            continue
        secret_values = secret_values_from_env(env)

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
        with LogCapture(args.stage, args.docs, args.phase, secret_values) as (lp, lc):
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
                        sys.stderr.write(redact(line, secret_values))
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
                        log_path=log_path, output_tail=tail,
                        secret_values=secret_values)
        if proc.returncode == 0:
            print(f"  ok: {label}", file=sys.stderr)
            return 0
        print(f"  failed ({proc.returncode}): {label}", file=sys.stderr)

    if args.dry_run:
        return 0
    print("all attempts in the chain failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
