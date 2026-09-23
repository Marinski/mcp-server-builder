"""Tests for the stage-runner's child-process environment construction.

The runner used to hand the stage CLI a full ``os.environ.copy()``, which
forwarded every credential the invoking shell happened to export (AWS keys,
GitHub tokens, ...) into an agent session it does not control. These tests pin
the replacement: a minimal allowlist plus the one provider credential and
endpoint the attempt actually resolved.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import run_stage
from lock import RunLock


def _write_env_dump_stub(dir_path: Path, name: str = "dump-env") -> Path:
    """Create an executable that prints its own environment as JSON."""
    stub = dir_path / name
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "print(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _write_argv_dump_stub(dir_path: Path, name: str = "dump-argv") -> Path:
    """Create an executable that prints its own argv (after the program name)
    as JSON, one arg per line."""
    stub = dir_path / name
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "print(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _provider_config(runner_overrides: dict | None = None,
                     provider_overrides: dict | None = None) -> dict:
    runner = {
        "cmd": "dump-env",
        "args": [],
        "api_key_var": "OPENAI_API_KEY",
        "base_url_var": "OPENAI_BASE_URL",
    }
    runner.update(runner_overrides or {})
    provider = {
        "runner": "stub",
        "api_key_env": "STUB_API_KEY",
        "base_url_env": "STUB_BASE_URL",
    }
    provider.update(provider_overrides or {})
    return {
        "runners": {"stub": runner},
        "providers": {"stubby": provider},
    }


def test_build_command_child_env_excludes_parent_secrets(tmp_path, monkeypatch):
    """A real child launched from build_command's argv/env sees the provider
    credential and PATH/HOME but not an unrelated exported secret."""
    _write_env_dump_stub(tmp_path)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(tmp_path), os.environ.get("PATH", "")]))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "decoy-secret")
    monkeypatch.setenv("STUB_API_KEY", "provider-key")
    monkeypatch.setenv("STUB_BASE_URL", "https://stub.invalid/v1")

    cfg = _provider_config()
    attempt = {"provider": "stubby", "model": "stub-model"}

    argv, env = run_stage.build_command(
        cfg, attempt, "1a", tmp_path / "prompt.txt", [], strict=True)
    assert argv == ["dump-env"]

    proc = subprocess.run(
        argv, env=env, capture_output=True, text=True, check=True)
    child_env = json.loads(proc.stdout)

    # The decoy never crosses the boundary.
    assert "AWS_SECRET_ACCESS_KEY" not in child_env
    # The parent's raw var name is not forwarded either — only the runner's
    # mapped variable carries the value.
    assert "STUB_API_KEY" not in child_env
    assert "STUB_BASE_URL" not in child_env
    # The resolved provider key and endpoint are present on the runner's vars.
    assert child_env["OPENAI_API_KEY"] == "provider-key"
    assert child_env["OPENAI_BASE_URL"] == "https://stub.invalid/v1"
    # Launch prerequisites survive.
    assert child_env["PATH"] == env["PATH"]
    assert child_env["HOME"] == str(tmp_path / "home")


def test_build_child_env_returns_only_allowlist_and_provider_vars():
    """build_child_env returns no parent key outside the base allowlist and
    the explicit provider pass-through."""
    parent_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/stub",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LC_CTYPE": "UTF-8",
        "AWS_SECRET_ACCESS_KEY": "decoy",
        "GITHUB_TOKEN": "decoy",
        "STUB_API_KEY": "provider-key",
        "STUB_BASE_URL": "https://stub.invalid/v1",
        "SOME_UNRELATED_VAR": "nope",
    }
    cfg = _provider_config()
    attempt = {"provider": "stubby", "model": "stub-model"}

    env = run_stage.build_child_env(cfg, attempt, parent_env, strict=True)

    allowed = {
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
    }
    assert set(env) <= allowed
    assert env["OPENAI_API_KEY"] == "provider-key"
    assert env["OPENAI_BASE_URL"] == "https://stub.invalid/v1"
    for leaked in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "SOME_UNRELATED_VAR"):
        assert leaked not in env


def test_build_child_env_leaves_unset_variables_unset(tmp_path, monkeypatch):
    """A variable the parent does not set is absent, never invented or blank."""
    cfg = _provider_config(runner_overrides={"auth_optional": True})
    attempt = {"provider": "stubby", "model": "stub-model"}

    env = run_stage.build_child_env(
        cfg, attempt, {"PATH": "/usr/bin"}, strict=False)

    assert env == {"PATH": "/usr/bin"}
    assert "HOME" not in env
    assert "LANG" not in env


def test_build_child_env_missing_custom_base_url_exits_when_strict():
    """An unset custom endpoint stays a hard error under strict=True."""
    cfg = _provider_config()
    attempt = {"provider": "stubby", "model": "stub-model"}

    with pytest.raises(SystemExit):
        run_stage.build_child_env(
            cfg, attempt, {"PATH": "/usr/bin"}, strict=True)


def test_build_child_env_missing_custom_base_url_warns_when_not_strict(capsys):
    """Under strict=False the same misconfiguration warns and is omitted."""
    cfg = _provider_config()
    attempt = {"provider": "stubby", "model": "stub-model"}

    env = run_stage.build_child_env(
        cfg, attempt, {"PATH": "/usr/bin"}, strict=False)

    assert "OPENAI_BASE_URL" not in env
    assert "$STUB_BASE_URL is not set" in capsys.readouterr().err


def test_build_child_env_auth_optional_missing_key_notes_without_exiting(capsys):
    """An OAuth-capable runner may launch with no API key at all."""
    cfg = _provider_config(
        runner_overrides={"auth_optional": True},
        provider_overrides={"base_url_env": "ANTHROPIC_BASE_URL"},
    )
    attempt = {"provider": "stubby", "model": "stub-model"}

    env = run_stage.build_child_env(
        cfg, attempt, {"PATH": "/usr/bin"}, strict=True)

    assert "OPENAI_API_KEY" not in env
    assert "relying on the CLI's own auth" in capsys.readouterr().err


def test_base_child_env_adds_windows_vars_on_win32(monkeypatch):
    """The Windows launch variables are copied only when sys.platform is win32."""
    monkeypatch.setattr(run_stage.sys, "platform", "win32")
    parent_env = {
        "PATH": "C:\\Windows\\System32",
        "HOME": "C:\\Users\\stub",
        "LANG": "en_US.UTF-8",
        "SYSTEMROOT": "C:\\Windows",
        "TEMP": "C:\\Temp",
        "USERPROFILE": "C:\\Users\\stub",
        "AWS_SECRET_ACCESS_KEY": "decoy",
    }

    env = run_stage._base_child_env(parent_env)

    assert env["SYSTEMROOT"] == "C:\\Windows"
    assert env["TEMP"] == "C:\\Temp"
    assert env["USERPROFILE"] == "C:\\Users\\stub"
    assert "AWS_SECRET_ACCESS_KEY" not in env


# ── Per-stage, runner-owned permissions (findings 5404, 5417) ──────────────


def _contract_driven_provider_config(runner_overrides: dict | None = None,
                                     cmd: str = "dump-argv") -> dict:
    """A claude-like runner that interpolates the stage contract's permission
    placeholders and owns the settings file, like models.example.yaml's claude
    runner block."""
    runner = {
        "cmd": cmd,
        "args": [
            "-p",
            "--model", "{model}",
            "--permission-mode", "{permission_mode}",
            "--settings", "{settings_file}",
            "--strict-mcp-config",
        ],
        "allowed_tool_flag": "--allowedTools",
        "disallowed_tool_flag": "--disallowedTools",
        "settings_file": "runner/settings/claude-settings.json",
        "api_key_var": "ANTHROPIC_API_KEY",
        "base_url_var": "ANTHROPIC_BASE_URL",
        "auth_optional": True,
    }
    runner.update(runner_overrides or {})
    provider = {
        "runner": "stub",
        "api_key_env": "STUB_API_KEY",
        "base_url_env": "STUB_BASE_URL",
    }
    return {"runners": {"stub": runner}, "providers": {"stubby": provider}}


def _put_stub_on_path(tmp_path, monkeypatch) -> None:
    _write_argv_dump_stub(tmp_path)
    _write_env_dump_stub(tmp_path)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(tmp_path), os.environ.get("PATH", "")]))


def _build_probe(cfg, stage, tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("probe", encoding="utf-8")
    return run_stage.build_command(
        cfg, {"provider": "stubby", "model": "probe-model"},
        stage, prompt, [], strict=False)


def test_build_command_emits_per_stage_permission_surface(tmp_path, monkeypatch):
    """Stage 3 (the only fetching stage) gets a WebFetch allowlist; every other
    stage drops WebFetch and declines it explicitly. Read-only stages (1a, 1b)
    resolve the non-editing 'default' mode; artifact-writing stages keep
    acceptEdits. build_command's argv is therefore no longer identical for
    every stage."""
    _put_stub_on_path(tmp_path, monkeypatch)
    cfg = _contract_driven_provider_config()

    argv_3, _ = _build_probe(cfg, "3", tmp_path)
    argv_1a, _ = _build_probe(cfg, "1a", tmp_path)
    argv_5, _ = _build_probe(cfg, "5", tmp_path)

    assert argv_3 != argv_1a
    # Read-only stage 1 runs a non-editing mode; the write stage keeps
    # acceptEdits so its artifact (and the project it modifies) can land.
    assert argv_1a[argv_1a.index("--permission-mode") + 1] == "default"
    assert argv_5[argv_5.index("--permission-mode") + 1] == "acceptEdits"

    # Only stage 3 auto-approves WebFetch (per-domain allowlist).
    assert "--allowedTools" in argv_3
    allowed_3 = {argv_3[i + 1] for i, a in enumerate(argv_3)
                 if a == "--allowedTools"}
    assert "WebFetch(domain:registry.npmjs.org)" in allowed_3
    assert "WebFetch(domain:github.com)" in allowed_3
    assert not any(t.startswith("Bash") for t in allowed_3)
    # Non-fetching stages get no allowlist and an explicit WebFetch decline.
    assert "--allowedTools" not in argv_1a
    assert argv_1a[argv_1a.index("--disallowedTools") + 1] == "WebFetch(*)"

    # No stage's argv auto-approves Bash anywhere.
    for argv in (argv_3, argv_1a):
        assert not any(a == "--allowedTools"
                       and str(argv[i + 1]).startswith("Bash")
                       for i, a in enumerate(argv))


def test_build_command_settings_flag_points_at_runner_owned_file(tmp_path, monkeypatch):
    """The claude runner's --settings points at a settings file under THIS
    checkout (the protected default), so the target repo's .claude/settings.json
    is never the permission authority."""
    _put_stub_on_path(tmp_path, monkeypatch)
    cfg = _contract_driven_provider_config()

    argv, _ = _build_probe(cfg, "1a", tmp_path)

    assert "--settings" in argv
    settings_path = Path(argv[argv.index("--settings") + 1])
    assert settings_path == run_stage.DEFAULT_CLAUDE_SETTINGS
    assert settings_path.is_file()
    assert "--strict-mcp-config" in argv
    # The shipped posture is a WebFetch allowlist, and Bash is not auto-approved.
    shipped = json.loads(settings_path.read_text(encoding="utf-8"))
    permissions = shipped.get("permissions", {})
    allow = permissions.get("allow", [])
    deny = permissions.get("deny", [])
    assert any(t.startswith("WebFetch(domain:") for t in allow)
    assert not any(str(t).startswith("Bash") for t in allow)
    # Hard denies are what beat the lower-scope allows a clone can merge in.
    assert "Bash(*)" in deny
    assert "mcp__*" in deny


def test_probe_permissive_project_settings_do_not_widen_runner_surface(tmp_path, monkeypatch):
    """Spec probe: in a scratch repo whose .claude/settings.json auto-approves
    Bash(*), a probe stage must still see Bash prompt/decline. The runner points
    --settings at its own file instead of honoring the project file, so the
    clone's allow cannot widen the permission set: allow rules merge across
    scopes, so the runner-owned file also hard-denies Bash(*) and mcp__* — a
    deny from any scope beats an allow from a lower one. If --settings does not
    outrank the project file, run_stage's isolate_config fallback isolates
    CLAUDE_CONFIG_DIR/HOME.

    The prompt/decline itself happens inside the claude CLI, so this probe
    launches the stage's real child command (a stub that dumps its argv/env)
    with the scratch repo as cwd and pins what the child is told.
    """
    _put_stub_on_path(tmp_path, monkeypatch)

    # The malicious/third-party clone: it grants Bash(*) in its own settings.
    scratch = tmp_path / "scratch-repo"
    (scratch / ".claude").mkdir(parents=True)
    (scratch / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(*)"], "deny": [], "ask": []},
    }), encoding="utf-8")

    cfg = _contract_driven_provider_config()
    # Probe a write stage (5): the run that "legitimately modifies the
    # project" is the one whose widen attempt would matter.
    argv, env = _build_probe(cfg, "5", tmp_path)

    # Run the stage's child command against the scratch repo: the argv it sees
    # must be exactly what build_command produced (sans the program name, which
    # the stub drops like any real CLI).
    child = subprocess.run(argv, cwd=str(scratch), env=env,
                           capture_output=True, text=True, check=True)
    assert json.loads(child.stdout) == argv[1:]

    # The runner's own settings file — not the scratch repo's — is the surface.
    assert "--settings" in argv
    own_settings = Path(argv[argv.index("--settings") + 1])
    assert own_settings == run_stage.DEFAULT_CLAUDE_SETTINGS
    assert own_settings != scratch / ".claude" / "settings.json"
    shipped = json.loads(own_settings.read_text(encoding="utf-8"))
    permissions = shipped.get("permissions", {})
    allow = permissions.get("allow", [])
    deny = permissions.get("deny", [])
    assert not any(str(t).startswith("Bash") for t in allow)
    # The clone's merged allow "Bash(*)" cannot override a deny from any scope.
    assert "Bash(*)" in deny
    assert "mcp__*" in deny

    # acceptEdits covers file edits only; no CLI arg approves Bash for the stage.
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert not any(a == "--allowedTools"
                   and str(argv[i + 1]).startswith("Bash")
                   for i, a in enumerate(argv))

    # Fallback: with isolate_config on, the child env points CLAUDE_CONFIG_DIR
    # at a fresh empty dir, so user-level ~/.claude settings cannot leak either.
    # Run the env-dumping child against the same scratch cwd and confirm the
    # isolation actually reaches it.
    cfg_iso = _contract_driven_provider_config(
        {"isolate_config": True}, cmd="dump-env")
    argv_iso, env_iso = _build_probe(cfg_iso, "5", tmp_path)
    assert "CLAUDE_CONFIG_DIR" in env_iso
    iso = Path(env_iso["CLAUDE_CONFIG_DIR"])
    assert iso.is_dir()
    assert json.loads((iso / "settings.json").read_text(encoding="utf-8")) == {}
    child_iso = subprocess.run(argv_iso, cwd=str(scratch), env=env_iso,
                               capture_output=True, text=True, check=True)
    assert json.loads(child_iso.stdout)["CLAUDE_CONFIG_DIR"] == str(iso)

    # Off by default: the child keeps the runner-provided environment (proved
    # by actually launching it, not just by inspecting the env dict).
    cfg_env = _contract_driven_provider_config(cmd="dump-env")
    argv_env, env_off = _build_probe(cfg_env, "5", tmp_path)
    assert "CLAUDE_CONFIG_DIR" not in env_off
    child_off = subprocess.run(argv_env, cwd=str(scratch), env=env_off,
                               capture_output=True, text=True, check=True)
    assert "CLAUDE_CONFIG_DIR" not in json.loads(child_off.stdout)


def test_every_stage_resolves_permission_mode_and_contract_validates():
    """Acceptance: every stage id resolves a permission_mode, and the extended
    contract shape (permission_mode + allowed_tools/disallowed_tools) passes
    validate_contract()."""
    import stage_contract

    stage_contract.validate_contract()

    for stage_id in stage_contract.STAGE_ORDER:
        mode, allowed, disallowed = run_stage.stage_permission_surface(stage_id)
        assert mode in stage_contract.PERMISSION_MODES
        if stage_id == "3":
            # The one stage that fetches: WebFetch is allowlisted, not denied.
            assert any(t.startswith("WebFetch(domain:") for t in allowed)
            assert "WebFetch(*)" not in disallowed
        else:
            # WebFetch is dropped from stages that do not fetch.
            assert not any(t.startswith("WebFetch") for t in allowed)
            assert "WebFetch(*)" in disallowed
        # Bash is never auto-approved (the runner-owned posture), so no stage
        # lists a Bash pattern in its own surface.
        assert not any(str(t).startswith("Bash")
                       for t in allowed + disallowed)

    # The read-only split pinned by the probe (runner/probe_read_only.py):
    # stages 1a/1b resolve a non-editing mode; every stage that writes its
    # artifact (2+ writing docs/mcp or the project) resolves acceptEdits.
    for stage_id in stage_contract.READ_ONLY_STAGES:
        mode, _, _ = run_stage.stage_permission_surface(stage_id)
        assert mode != "acceptEdits", \
            f"read-only stage {stage_id} must not resolve an editing mode"
    for stage_id in stage_contract.STAGE_ORDER:
        if stage_id in stage_contract.READ_ONLY_STAGES:
            continue
        mode, _, _ = run_stage.stage_permission_surface(stage_id)
        assert mode == "acceptEdits", \
            f"artifact-writing stage {stage_id} must resolve acceptEdits"


def test_validate_contract_rejects_bad_permission_surface(monkeypatch):
    """validate_contract() fails closed on an invalid permission_mode or a
    malformed tool list, so a bad contract cannot silently widen a stage."""
    import stage_contract

    # Work from a pristine snapshot; each case below is built from it so one
    # mutation cannot leak into the next.
    pristine = {k: dict(v) for k, v in stage_contract.STAGE_CONTRACT.items()}

    bad_mode = {k: dict(v) for k, v in pristine.items()}
    bad_mode["2"]["permission_mode"] = "bypassPermissions"
    monkeypatch.setattr(stage_contract, "STAGE_CONTRACT", bad_mode)
    with pytest.raises(ValueError, match="permission_mode"):
        stage_contract.validate_contract()

    missing = {k: dict(v) for k, v in pristine.items()}
    del missing["2"]["permission_mode"]
    monkeypatch.setattr(stage_contract, "STAGE_CONTRACT", missing)
    with pytest.raises(ValueError, match="missing required key: permission_mode"):
        stage_contract.validate_contract()

    bad_tools = {k: dict(v) for k, v in pristine.items()}
    bad_tools["2"]["disallowed_tools"] = ["WebFetch("]
    monkeypatch.setattr(stage_contract, "STAGE_CONTRACT", bad_tools)
    with pytest.raises(ValueError, match="disallowed_tools"):
        stage_contract.validate_contract()

    # The WebFetch surface cross-check: exactly one fetching stage, nobody else
    # may allow WebFetch, everyone must decline it, and the fetching stage is
    # domain-scoped only.
    extra_fetch = {k: dict(v) for k, v in pristine.items()}
    extra_fetch["4"]["allowed_tools"] = ["WebFetch(domain:example.com)"]
    monkeypatch.setattr(stage_contract, "STAGE_CONTRACT", extra_fetch)
    with pytest.raises(ValueError, match="exactly one stage may allow WebFetch"):
        stage_contract.validate_contract()

    wildcard_fetch = {k: dict(v) for k, v in pristine.items()}
    wildcard_fetch["3"]["allowed_tools"] = ["WebFetch(*)"]
    monkeypatch.setattr(stage_contract, "STAGE_CONTRACT", wildcard_fetch)
    with pytest.raises(ValueError, match="domain-scoped WebFetch"):
        stage_contract.validate_contract()

    no_decline = {k: dict(v) for k, v in pristine.items()}
    no_decline["1a"]["disallowed_tools"] = []
    monkeypatch.setattr(stage_contract, "STAGE_CONTRACT", no_decline)
    with pytest.raises(ValueError, match="must decline WebFetch"):
        stage_contract.validate_contract()


def test_validate_contract_rejects_editing_mode_on_read_only_stage(monkeypatch):
    """A read-only stage must not silently regain acceptEdits: validate_contract
    rejects it, keeping the probe-pinned read-only surface intact."""
    import stage_contract

    pristine = {k: dict(v) for k, v in stage_contract.STAGE_CONTRACT.items()}
    bad = {k: dict(v) for k, v in pristine.items()}
    bad["1a"]["permission_mode"] = "acceptEdits"
    monkeypatch.setattr(stage_contract, "STAGE_CONTRACT", bad)
    with pytest.raises(ValueError, match="read-only"):
        stage_contract.validate_contract()


def test_cli_dry_run_prints_different_argv_per_stage(tmp_path, monkeypatch, capsys):
    """Acceptance: --dry-run prints a different argv per stage. Both probe
    stages use the same model, so the only difference is the permission
    surface from the stage contract."""
    _put_stub_on_path(tmp_path, monkeypatch)
    cfg_path = tmp_path / "models.yaml"
    cfg_path.write_text("""
runners:
  stub:
    cmd: dump-argv
    args: ["-p", "--model", "{model}", "--fallback-model", "{fallback}",
           "--permission-mode", "{permission_mode}",
           "--settings", "{settings_file}", "--strict-mcp-config",
           "--mcp-config", "{}"]
    allowed_tool_flag: "--allowedTools"
    disallowed_tool_flag: "--disallowedTools"
    settings_file: runner/settings/claude-settings.json
providers:
  stubby:
    runner: stub
    api_key_env: STUB_KEY
    base_url_env: STUB_URL
defaults:
  provider: stubby
  model: stub-model
stages:
  "1a": {}
  "3": {}
""", encoding="utf-8")

    monkeypatch.setattr(sys, "argv",
                        ["run_stage.py", "1a", "--dry-run",
                         "--config", str(cfg_path)])
    assert run_stage.main() == 0
    err_1a = capsys.readouterr().err

    monkeypatch.setattr(sys, "argv",
                        ["run_stage.py", "3", "--dry-run",
                         "--config", str(cfg_path)])
    assert run_stage.main() == 0
    err_3 = capsys.readouterr().err

    assert "--settings" in err_3
    assert "WebFetch(domain:registry.npmjs.org)" in err_3
    assert "--allowedTools" in err_3
    assert "WebFetch(*)" in err_1a
    assert "--allowedTools" not in err_1a
    assert "--strict-mcp-config --mcp-config {}" in err_3


# ── Symlink-safe artifact writes (finding 5405) ────────────────────────────


def _record_args(docs: Path) -> argparse.Namespace:
    """The minimal argparse namespace record_attempt needs."""
    return argparse.Namespace(stage="1a", docs=docs, manifest=None)


def test_log_capture_refuses_symlinked_logs_dir(tmp_path):
    """A repo shipping docs/mcp/logs as a symlink must not turn the runner
    into an arbitrary-file write: LogCapture refuses instead of writing
    through the link (finding 5405)."""
    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (docs / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        with run_stage.LogCapture("1a", docs):
            pass  # pragma: no cover — __enter__ raises first

    assert list(outside.iterdir()) == []


def test_record_attempt_refuses_symlinked_manifest(tmp_path):
    """A repo shipping run-manifest.jsonl as a symlink must not turn the
    runner into an arbitrary-file append primitive (finding 5405)."""
    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (docs / "run-manifest.jsonl").symlink_to(
        outside_dir / "manifest.jsonl")

    attempt = {"provider": "stub", "model": "stub-model"}
    with pytest.raises(OSError, match="symlink"):
        run_stage.record_attempt(
            _record_args(docs), attempt, 0, 1, 0,
            duration_s=1.0, timed_out=False)

    assert not (outside_dir / "manifest.jsonl").exists()


def test_record_attempt_refuses_symlinked_manifest_parent(tmp_path):
    """An ancestor of the manifest shipped as a symlink is refused too: the
    check walks every component of the manifest path under --docs."""
    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (docs / "logs").symlink_to(outside_dir, target_is_directory=True)

    attempt = {"provider": "stub", "model": "stub-model"}
    args = argparse.Namespace(
        stage="1a", docs=docs, manifest=docs / "logs" / "run-manifest.jsonl")
    with pytest.raises(OSError, match="symlink"):
        run_stage.record_attempt(
            args, attempt, 0, 1, 0, duration_s=1.0, timed_out=False)

    assert list(outside_dir.iterdir()) == []


def test_safe_open_refuses_symlink_target(tmp_path):
    """safe_open refuses a final-component symlink even when no ancestor is
    a link."""
    outside = tmp_path / "evil.txt"
    link = tmp_path / "victim"
    link.symlink_to(outside)

    with pytest.raises(OSError, match="symlink"):
        run_stage.safe_open(link, "w", root=tmp_path, what="file")

    assert not outside.exists()


def test_safe_open_refuses_path_escaping_docs_root(tmp_path):
    """safe_open refuses when the resolved real path escapes the docs root,
    even with no symlink anywhere (e.g. a .. component)."""
    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)
    escape = docs / ".." / ".." / "escape.txt"

    with pytest.raises(OSError, match="escapes"):
        run_stage.safe_open(escape, "w", root=docs, what="file")

    assert not (tmp_path / "escape.txt").exists()


def test_runner_artifacts_normal_run_unaffected(tmp_path):
    """A normal (non-symlinked) run writes the tee'd log and appends the
    manifest exactly as before."""
    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)

    with run_stage.LogCapture("1a", docs) as (log_path, lc):
        lc.write("hello\n")
        assert lc.tail() == "hello"

    log = Path(log_path)
    assert log.is_file()
    assert not log.is_symlink()
    assert log.read_text(encoding="utf-8") == "hello\n"

    attempt = {"provider": "stub", "model": "stub-model"}
    run_stage.record_attempt(
        _record_args(docs), attempt, 0, 1, 0,
        duration_s=1.0, timed_out=False,
        log_path=str(log), output_tail="hello")

    manifest = docs / "run-manifest.jsonl"
    assert manifest.is_file()
    assert not manifest.is_symlink()
    records = [json.loads(line) for line in
               manifest.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["stage"] == "1a"
    assert records[0]["ok"] is True
    assert records[0]["log_path"] == str(log)


# ── redact() and secret scrubbing (findings 5388, 5406, 5411, 5416) ───────


def test_redact_masks_a_known_secret_value():
    text = "auth failed for key sk-live-abcdef1234567890 on request"
    out = run_stage.redact(text, {"OPENAI_API_KEY": "sk-live-abcdef1234567890"})
    assert "sk-live-abcdef1234567890" not in out
    assert "[REDACTED:OPENAI_API_KEY]" in out


def test_redact_masks_generic_credential_shapes_without_a_known_value():
    # These are secrets the runner never injected (e.g. minted by the
    # stage's own tool calls) and so cannot be in secret_values — redact()
    # must still catch the shape.
    samples = {
        "sk-ant-api03-abcdefghijklmnopqrstuvwx": "openai/anthropic-style key",
        "ghp_abcdefghijklmnopqrstuvwxyz012345": "github PAT",
        "github_pat_11ABCDEFG0abcdefghijklmnop": "github fine-grained PAT",
        "AKIAABCDEFGHIJKLMNOP": "AWS access key id",
        "xoxb-test-fixture-not-a-real-token": "slack token",
        "Bearer abcdefghijklmnopqrstuvwx": "bearer token",
    }
    for secret, label in samples.items():
        out = run_stage.redact(f"line before {secret} line after")
        assert secret not in out, f"{label} leaked through redact()"
        assert "[REDACTED]" in out


def test_redact_masks_pem_private_key_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAK...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = run_stage.redact(f"leaked cred:\n{pem}\ndone")
    assert "MIIBOgIBAAJBAK" not in out
    assert "[REDACTED]" in out


def test_redact_leaves_non_secret_content_unchanged():
    """A control string that merely looks credential-adjacent (a git SHA,
    ordinary prose) must pass through untouched — a redactor that mangles
    valid output is its own failure mode."""
    text = "commit abc123def456 fixed the build; see PATH=/usr/bin"
    assert run_stage.redact(text) == text


def test_secret_values_from_env_matches_credential_shaped_names_only():
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/stub",
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "OPENAI_API_KEY": "sk-live-abcdef1234567890",
        "GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "DB_PASSWORD": "hunter2",
    }
    secrets = run_stage.secret_values_from_env(env)
    assert secrets == {
        "OPENAI_API_KEY": "sk-live-abcdef1234567890",
        "GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "DB_PASSWORD": "hunter2",
    }


def test_log_capture_redacts_secret_values_before_writing_log_and_tail(tmp_path):
    """The injected provider credential must not reach the persisted log
    file or the in-memory tail, even though LogCapture only ever sees
    already-decoded subprocess output — not the env dict directly."""
    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)
    secret_values = {"OPENAI_API_KEY": "sk-live-abcdef1234567890"}

    with run_stage.LogCapture("1a", docs, secret_values=secret_values) as (log_path, lc):
        lc.write("auth header: sk-live-abcdef1234567890\n")
        assert "sk-live-abcdef1234567890" not in lc.tail()
        assert "[REDACTED:OPENAI_API_KEY]" in lc.tail()

    logged = Path(log_path).read_text(encoding="utf-8")
    assert "sk-live-abcdef1234567890" not in logged
    assert "[REDACTED:OPENAI_API_KEY]" in logged


def test_log_capture_redacts_console_stream(tmp_path, capsys):
    """The console (stderr) stream is redacted too, not just the file."""
    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)
    secret_values = {"OPENAI_API_KEY": "sk-live-abcdef1234567890"}

    with run_stage.LogCapture("1a", docs, secret_values=secret_values) as (_, lc):
        lc.write("auth header: sk-live-abcdef1234567890\n")

    err = capsys.readouterr().err
    assert "sk-live-abcdef1234567890" not in err
    assert "[REDACTED:OPENAI_API_KEY]" in err


def test_record_attempt_redacts_output_tail_before_persisting(tmp_path):
    """A tail that somehow still carries a secret (e.g. a future caller that
    bypasses LogCapture) is redacted again at the last stop before it is
    durably written to the manifest."""
    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)
    attempt = {"provider": "stub", "model": "stub-model"}

    run_stage.record_attempt(
        _record_args(docs), attempt, 0, 1, 0,
        duration_s=1.0, timed_out=False,
        output_tail="token leaked: sk-live-abcdef1234567890",
        secret_values={"OPENAI_API_KEY": "sk-live-abcdef1234567890"})

    manifest = docs / "run-manifest.jsonl"
    record = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    assert "sk-live-abcdef1234567890" not in record["output_tail"]
    assert "[REDACTED:OPENAI_API_KEY]" in record["output_tail"]


def test_end_to_end_stage_run_never_persists_the_provider_credential(tmp_path, monkeypatch):
    """A real subprocess that echoes the provider credential to stdout (the
    exact scenario in findings 5388/5406/5411/5416 — a CLI's own verbose or
    error output echoing its auth header) must not leave that value in
    either the log file or the manifest."""
    stub = tmp_path / "echo-secret"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "print('connecting with key', os.environ['OPENAI_API_KEY'])\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(tmp_path), os.environ.get("PATH", "")]))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STUB_API_KEY", "sk-live-abcdef1234567890")
    monkeypatch.setenv("STUB_BASE_URL", "https://stub.invalid/v1")

    docs = tmp_path / "docs" / "mcp"
    docs.mkdir(parents=True)
    cfg = _provider_config(runner_overrides={"cmd": "echo-secret"})
    attempt = {"provider": "stubby", "model": "stub-model"}
    argv, env = run_stage.build_command(
        cfg, attempt, "1a", tmp_path / "prompt.txt", [], strict=True)
    secret_values = run_stage.secret_values_from_env(env)

    with run_stage.LogCapture("1a", docs, secret_values=secret_values) as (log_path, lc):
        proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        lc.write(proc.stdout.decode("utf-8"))
        tail = lc.tail()

    run_stage.record_attempt(
        _record_args(docs), attempt, 0, 1, proc.returncode,
        duration_s=1.0, timed_out=False, log_path=log_path,
        output_tail=tail, secret_values=secret_values)

    logged = Path(log_path).read_text(encoding="utf-8")
    manifest_record = json.loads(
        (docs / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "sk-live-abcdef1234567890" not in logged
    assert "sk-live-abcdef1234567890" not in manifest_record["output_tail"]


# ── new-project.sh symlink-safe writes (finding 5405) ─────────────────────


def _run_new_project(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    script = Path(__file__).resolve().parent.parent / "new-project.sh"
    return subprocess.run(
        ["bash", str(script), "--repo", str(repo),
         "--project", "Acme Server", "--server", "acme-server", *extra],
        capture_output=True, text=True,
    )


def test_new_project_refuses_symlinked_run_config_env(tmp_path):
    """run-config.env shipped as a symlink makes new-project.sh refuse
    instead of writing through the link."""
    repo = tmp_path / "repo"
    (repo / "docs" / "mcp").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "docs" / "mcp" / "run-config.env").symlink_to(
        outside, target_is_directory=True)

    proc = _run_new_project(repo)

    assert proc.returncode != 0
    assert "symlink" in proc.stderr.lower()
    assert list(outside.iterdir()) == []


def test_new_project_refuses_symlinked_docs_dir(tmp_path):
    """A repo shipping docs/mcp (the default DOCS dir) as a symlink is
    refused too, before any write happens."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "docs" / "mcp").symlink_to(outside, target_is_directory=True)

    proc = _run_new_project(repo)

    assert proc.returncode != 0
    assert "symlink" in proc.stderr.lower()
    assert list(outside.iterdir()) == []


def test_new_project_scaffolds_normally(tmp_path):
    """A normal scaffold still writes run-config.env and 00-decisions.md."""
    repo = tmp_path / "repo"
    repo.mkdir()

    proc = _run_new_project(repo)

    assert proc.returncode == 0, proc.stderr
    env_file = repo / "docs" / "mcp" / "run-config.env"
    assert env_file.is_file()
    assert not env_file.is_symlink()
    assert "SERVER=acme-server" in env_file.read_text()
    decisions = repo / "docs" / "mcp" / "00-decisions.md"
    assert decisions.is_file()
    assert not decisions.is_symlink()
    assert "# Decisions — Acme Server MCP server" in decisions.read_text()


# ── pipeline-lock coverage + postflight enforcement (merged from the
# lock-atomicity PR, which landed on main while this branch was still
# open) ─────────────────────────────────────────────────────────────


RUNNER_DIR = Path(__file__).parent
RUN_STAGE = RUNNER_DIR / "run_stage.py"
STOP_ENV = "FAKE_STAGE_STOP"


def _write_config(tmp_path: Path) -> Path:
    """A minimal models.yaml whose runner is an executable stub on disk.

    The stub's command is an absolute path, so ``shutil.which`` resolves it
    without touching PATH.

    ``pass_env: [FAKE_STAGE_STOP]`` opts the blocking stub's control
    variable through ``build_child_env``'s allowlist: the stage subprocess
    no longer inherits the invoking test's whole environment, so without
    this the stub would never see ``$FAKE_STAGE_STOP`` and would poll on an
    empty path forever, regardless of the test ever touching the real stop
    file.
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
        "    args: []\n"
        "    pass_env: [FAKE_STAGE_STOP]\n",
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