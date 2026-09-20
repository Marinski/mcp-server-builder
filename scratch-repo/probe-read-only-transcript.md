# Probe transcript — read-only enforcement vs. scratch repo

Date: 2026-09-20 00:50 UTC
Command: python3 runner/probe_read_only.py
Target repo: scratch-repo/   (artifact dir DOCS = scratch-repo/docs/mcp)

Grounding: mcp-server-creation-workflow.md §Stage 1 ("Run in: fresh session, repo mounted, read-only"); models.example.yaml claude runner (per-stage --permission-mode, --allowedTools/--disallowedTools, --settings → runner-owned file); per-stage permission contract from Task 1 (runner/stage_contract.py: stage ids, permission_mode, allowed_tools/disallowed_tools).

Environment:
- claude CLI on PATH: no — the permission decision is exercised by the probe stub (embedded in this script), which applies Claude Code's documented non-interactive -p semantics (deny beats allow; unapproved Write/Edit is declined) against the exact argv the runner emits. The stub performs the write itself, so the file-exists assertions below are real filesystem outcomes. The stub declines absolute and '..'-escaped write paths; it does not model symlink escapes (the shipped surfaces are all exercised with repo-relative paths).

This file is a snapshot of the last probe run; rerun `python3 runner/probe_read_only.py` to refresh it.
- preflight stubs already present under scratch-repo/docs/mcp

## 1. Per-stage --dry-run argv vs. contract

Every stage's emitted --dry-run argv must carry exactly the contract's permission_mode, the contract's allowed/disallowed tool flags, and the runner-owned --settings file.

### stage 1a — permission_mode=`default`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode default --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

### stage 1b — permission_mode=`default`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode default --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

### stage 2 — permission_mode=`acceptEdits`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

### stage 3 — permission_mode=`acceptEdits`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --allowedTools 'WebFetch(domain:github.com)' --allowedTools 'WebFetch(domain:registry.npmjs.org)' --allowedTools 'WebFetch(domain:raw.githubusercontent.com)'`

conformance: PASS

### stage 4 — permission_mode=`acceptEdits`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

### stage 5 — permission_mode=`acceptEdits`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

### stage 6 — permission_mode=`acceptEdits`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

### stage 7 — permission_mode=`acceptEdits`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

### stage 8 — permission_mode=`acceptEdits`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

### stage 9 — permission_mode=`acceptEdits`

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

conformance: PASS

## 2. Enforcement: file creation outside docs/mcp

Launch each stage's emitted argv against the scratch repo (cwd = scratch-repo/) with the probe stub as the claude CLI.

### Read-only stage 1a (contract permission_mode=`default`)

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode default --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

- prompt: `[[WRITE probe-outside.txt]]`
  stub: `mode=default write=probe-outside.txt decision=DENY (mode default, unapproved in -p)`
  file created: no
  ✓ read-only stage FAILS to create a file outside docs/mcp

- prompt: `[[WRITE docs/mcp/probe-scoped.txt]]`
  stub: `mode=default write=docs/mcp/probe-scoped.txt decision=DENY (mode default, unapproved in -p)`
  file created: no
  ✓ read-only stage cannot auto-accept even its own artifact write — the artifact-write exception is approximated (capture 01-instructions.md / 01-signatures.md from the stage's output)

- prompt: `[[BASH echo probe]]`
  stub: `bash=echo probe decision=DENY (settings deny Bash(*))`
  ✓ Bash stays denied (runner-owned settings deny Bash(*))

### Write stage 5 (contract permission_mode=`acceptEdits`)

argv: `$PROBE_STUB -p --model probe-model --fallback-model probe-model --permission-mode acceptEdits --settings runner/settings/claude-settings.json --strict-mcp-config --mcp-config '{}' --disallowedTools 'WebFetch(*)'`

- prompt: `[[WRITE probe-outside.txt]]`
  stub: `mode=acceptEdits write=probe-outside.txt decision=ALLOW (acceptEdits)`
  file created: yes (removed after the probe)
  ✓ write stage CAN create a file outside docs/mcp — stages 5-8 legitimately modify the project

## 3. Open Question 2: path-scoped rules like Edit(docs/mcp/**)

The spec asks whether claude supports path-scoped rules so a read-only stage can write its own artifact but not target source. The harness honors the glob syntax (below); this checkout is offline with no claude binary, so the rule syntax could not be positively confirmed against a deployed claude build. Per the spec's prescription the shipped contract therefore does NOT rely on it: read-only stages fall back to the non-editing mode `default` and the artifact-write exception is approximated (noted in runner/stage_contract.py).

Harness experiment (mode=`default` + `--allowedTools Edit(docs/mcp/**)`) — NOT the shipped surface:

- prompt: `[[WRITE docs/mcp/probe-scoped.txt]]`
  stub: `mode=default write=docs/mcp/probe-scoped.txt decision=ALLOW (rule Edit(docs/mcp/**))`
  file created: yes (removed after the probe)

- prompt: `[[WRITE probe-outside.txt]]`
  stub: `mode=default write=probe-outside.txt decision=DENY (mode default, unapproved in -p)`
  file created: no
  ✓ path-scoped semantics: artifact writes allowed, source blocked

## Result

PASS — the read-only stage cannot create a file outside docs/mcp while the write stage can, and every stage's emitted --dry-run argv matches the contract.

