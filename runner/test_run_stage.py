"""Tests for the stage-runner's child-process environment construction.

The runner used to hand the stage CLI a full ``os.environ.copy()``, which
forwarded every credential the invoking shell happened to export (AWS keys,
GitHub tokens, ...) into an agent session it does not control. These tests pin
the replacement: a minimal allowlist plus the one provider credential and
endpoint the attempt actually resolved.
"""
from __future__ import annotations

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
        cfg, attempt, tmp_path / "prompt.txt", [], strict=True)
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
