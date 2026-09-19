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
from pathlib import Path

import pytest

import run_stage


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
    stage drops WebFetch and declines it explicitly. build_command's argv is
    therefore no longer identical for every stage."""
    _put_stub_on_path(tmp_path, monkeypatch)
    cfg = _contract_driven_provider_config()

    argv_3, _ = _build_probe(cfg, "3", tmp_path)
    argv_1a, _ = _build_probe(cfg, "1a", tmp_path)

    assert argv_3 != argv_1a
    assert argv_1a[argv_1a.index("--permission-mode") + 1] == "acceptEdits"

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
    argv, env = _build_probe(cfg, "1a", tmp_path)

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
    argv_iso, env_iso = _build_probe(cfg_iso, "1a", tmp_path)
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
    argv_env, env_off = _build_probe(cfg_env, "1a", tmp_path)
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
