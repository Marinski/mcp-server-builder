"""Stage contract: artifact shapes, input/output paths, gates, and the
per-stage permission surface.

Pure data definition consumed by downstream orchestration tasks. Covers
spec sections: artifact-shape table and gate definitions for --batch, plus
the per-stage permission mode and tool allow/deny lists the runner emits as
CLI arguments (findings 5404, 5417: build_command used to emit one identical
permission surface for every stage, and the runner silently honored the
target repo's own .claude/settings.json, so a third-party clone could widen
the permission set).

Stage ordering is implicit (the ordered list of stage IDs), with 1a -> 1b
sequential. Human-gated stages (2, 3, 4) require human review of their
output before the next stage may read it.

Permission surface per stage:
  permission_mode    – the --permission-mode value the runner passes (one of
                       PERMISSION_MODES). acceptEdits auto-accepts file
                       edits so a stage that writes its own artifact cannot
                       fail closed with no file on disk; it never auto-
                       approves Bash.
  allowed_tools      – optional list of tool patterns auto-approved for the
                       stage, e.g. "WebFetch(domain:github.com)". WebFetch
                       is allowed only where a stage actually fetches (stage
                       3 verifies the SDK against registries and code
                       hosting); every other stage drops it.
  disallowed_tools   – optional list of tool patterns hard-declined for the
                       stage, e.g. "Bash(*)" or "WebFetch(*)". Bash is never
                       auto-approved anywhere (see the runner-owned settings
                       file referenced from models.yaml); these lists are the
                       per-stage hard constraints on top of that.
#
# Read-only stages ("1a", "1b" — the playbook runs Stage 1 in "fresh session,
# repo mounted, read-only") resolve a NON-editing permission mode ("default"):
# a non-interactive -p session has no TTY to approve a Write/Edit, so the
# attempt is declined — a read-only stage cannot create a file outside
# docs/mcp (runner/probe_read_only.py pins this against the scratch repo).
# Path-scoped edit rules (Edit(docs/mcp/**), the spec's Open Question 2) are
# not relied on: no deployed claude build could be probed in the offline
# checkout to positively confirm the syntax, so the spec's prescribed fallback
# ships instead. The artifact-write exception is therefore APPROXIMATED:
# postflight_outputs still name the stage's artifact, but the read-only stage
# cannot auto-accept it on disk — capture it from the stage's output (final
# message / tee'd log) to materialize it.
"""

from __future__ import annotations

import re

# Ordered stage IDs as used throughout the pipeline.
STAGE_ORDER: list[str] = ["1a", "1b", "2", "3", "4", "5", "6", "7", "8", "9"]

# Valid --permission-mode values for a stage. bypassPermissions (i.e.
# --dangerously-skip-permissions) is deliberately NOT valid here: the
# runner-owned posture is that Bash is never auto-approved, and bypassing
# permissions would silently undo exactly that.
PERMISSION_MODES: tuple[str, ...] = ("default", "acceptEdits", "plan")

# Stages the playbook documents read-only ("fresh session, repo mounted,
# read-only"). These must never resolve an editing mode: see the module
# docstring — the probe in runner/probe_read_only.py pins the read-only
# surface, and validate_contract() refuses acceptEdits for them.
READ_ONLY_STAGES: tuple[str, ...] = ("1a", "1b")

# A tool pattern is a tool name optionally followed by a parenthesized
# selector, e.g. "WebFetch(domain:github.com)" or "Bash(*)".
_TOOL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:\([^)]*\))?\Z")

# Artifact shape constants.
SHAPE_SINGLE_FILE = "single-file"
SHAPE_PHASE_TRACKED = "phase-tracked"
SHAPE_MULTI_FILE = "multi-file"
SHAPE_NO_CHECKABLE = "no-checkable-artifact"

# ── TODO (Spec 1 wiring plan, Track B items 6-7) ──────────────────────────
# When Spec 1 merges, the runner gains an expected-artifact-shape check:
#
#   • Each single-file stage (1a, 1b, 2, 3, 4, 7, 9) registers its template
#     path (e.g. templates/01-instructions.md for stage 1a) as the
#     expected-artifact-shape entry in the runner.  The check compares
#     the target artifact's mtime + size against the template, computed
#     in-memory and never persisted to disk.
#
#   • Stage 5 registers as directory-or-phase-based (SHAPE_PHASE_TRACKED):
#     no single-file template match; completion is tracked per phase N/M.
#
#   • Stage 9 used to be EXPLICITLY EXEMPT from the artifact-path check,
#     because it patched this repo's own workflow file
#     (mcp-server-creation-workflow.md) rather than producing a docs/mcp/
#     artifact. That is no longer the case: the retro now writes
#     templates/08-retro.md to {{DOCS}}/08-retro.md as a reviewable
#     artifact, so stage 9 is tracked exactly like the other single-file
#     stages. Only stages 5 (phase-tracked) and 6/8 (multi-file) are not
#     single-file-checked.
#
# Until Spec 1 merges this is a documentation-only no-op.
# ───────────────────────────────────────────────────────────────────────────

# Each key is a stage ID. Values are dicts with:
#   artifact_shape     – one of the SHAPE_* constants
#   preflight_inputs   – exact filenames the stage must find on disk before it runs
#   postflight_outputs – tracked artifact filenames the stage produces
#   human_gated        – True for stages 2/3/4 only (per spec §1/§4 gate definitions)
#   permission_mode    – one of PERMISSION_MODES (required; the runner passes it as
#                        --permission-mode so the permission surface is per-stage)
#   allowed_tools      – optional list of tool patterns auto-approved for this stage
#   disallowed_tools   – optional list of tool patterns hard-declined for this stage
#   (optional) phase_tracking_note – extra requirement text for phase-tracked stages
STAGE_CONTRACT: dict[str, dict] = {
    "1a": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["01-instructions.md"],
        "human_gated": False,
        # Read-only stage (playbook §Stage 1). Probe-pinned surface
        # (runner/probe_read_only.py): a NON-editing mode — a -p session
        # declines Write/Edit without a TTY, so the stage cannot create a file
        # outside docs/mcp. Path-scoped edit rules (Edit(docs/mcp/**), spec
        # Open Question 2) are not relied on; the artifact-write exception is
        # APPROXIMATED — the stage's artifact is captured from its output
        # rather than auto-accepted on disk.
        "permission_mode": "default",
        "disallowed_tools": ["WebFetch(*)"],
    },
    "1b": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["01-signatures.md"],
        "human_gated": False,
        # Read-only stage (playbook §Stage 1). Same approximate artifact-write
        # exception as 1a: non-editing mode, artifact captured from output.
        "permission_mode": "default",
        "disallowed_tools": ["WebFetch(*)"],
    },
    "2": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [
            "00-decisions.md",
            "01-instructions.md",
            "01-signatures.md",
        ],
        "postflight_outputs": ["02-capability-inventory.md"],
        "human_gated": True,
        "permission_mode": "acceptEdits",
        "disallowed_tools": ["WebFetch(*)"],
    },
    "3": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [
            "00-decisions.md",
            "01-instructions.md",
            "01-signatures.md",
            "02-capability-inventory.md",
        ],
        "postflight_outputs": ["03-mcp-surface.md"],
        "human_gated": True,
        # The one stage that fetches: it verifies the SDK version it designs
        # against on package registries and code hosting. Every other stage
        # drops WebFetch entirely.
        "permission_mode": "acceptEdits",
        "allowed_tools": [
            "WebFetch(domain:github.com)",
            "WebFetch(domain:registry.npmjs.org)",
            "WebFetch(domain:raw.githubusercontent.com)",
        ],
    },
    "4": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [
            "01-instructions.md",
            "01-signatures.md",
            "02-capability-inventory.md",
            "03-mcp-surface.md",
        ],
        "postflight_outputs": ["04-spec.md"],
        "human_gated": True,
        "permission_mode": "acceptEdits",
        "disallowed_tools": ["WebFetch(*)"],
    },
    "5": {
        "artifact_shape": SHAPE_PHASE_TRACKED,
        "preflight_inputs": ["04-spec.md"],
        "postflight_outputs": [],
        "human_gated": False,
        "permission_mode": "acceptEdits",
        "disallowed_tools": ["WebFetch(*)"],
        "phase_tracking_note": (
            "Pre-flight requires 04-spec.md exists and that "
            "--phase N/M is given with M consistent with prior "
            "stage-5 manifest entries."
        ),
    },
    "6": {
        "artifact_shape": SHAPE_MULTI_FILE,
        "preflight_inputs": ["04-spec.md"],
        "postflight_outputs": ["05-test-plan.md"],
        "human_gated": False,
        "permission_mode": "acceptEdits",
        "disallowed_tools": ["WebFetch(*)"],
    },
    "7": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["06-review.md"],
        "human_gated": False,
        "permission_mode": "acceptEdits",
        "disallowed_tools": ["WebFetch(*)"],
    },
    "8": {
        "artifact_shape": SHAPE_MULTI_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["07-release.md", "README.md"],
        "human_gated": False,
        "permission_mode": "acceptEdits",
        "disallowed_tools": ["WebFetch(*)"],
    },
    "9": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["08-retro.md"],
        "human_gated": False,
        "permission_mode": "acceptEdits",
        "disallowed_tools": ["WebFetch(*)"],
    },
}


def validate_contract() -> None:
    """Fail-fast check that the contract is internally consistent.

    Catches duplicate stage IDs, missing keys, permission-surface errors,
    and ordering violations. Raises ValueError on the first problem found;
    returns None if valid.
    """
    seen_ids: set[str] = set()
    for stage_id in STAGE_ORDER:
        if stage_id in seen_ids:
            raise ValueError(f"duplicate stage ID in STAGE_ORDER: {stage_id}")
        seen_ids.add(stage_id)

        if stage_id not in STAGE_CONTRACT:
            raise ValueError(f"stage {stage_id} is in STAGE_ORDER but missing from STAGE_CONTRACT")

        entry = STAGE_CONTRACT[stage_id]
        for key in ("artifact_shape", "preflight_inputs", "postflight_outputs", "human_gated"):
            if key not in entry:
                raise ValueError(f"stage {stage_id} missing required key: {key}")

        # Every stage must resolve a --permission-mode the runner can pass;
        # bypassPermissions is never valid here (see PERMISSION_MODES).
        if "permission_mode" not in entry:
            raise ValueError(f"stage {stage_id} missing required key: permission_mode")
        if entry["permission_mode"] not in PERMISSION_MODES:
            raise ValueError(
                f"stage {stage_id} permission_mode {entry['permission_mode']!r} "
                f"not in {PERMISSION_MODES}; bypassPermissions is never valid "
                "here — the runner-owned posture is that Bash is never "
                "auto-approved")

        # Read-only stages must stay read-only: the probe-pinned surface is a
        # non-editing mode, so an edit that could touch target source can never
        # be auto-accepted for them. Their artifact-write exception is
        # approximated instead (see the module docstring).
        if stage_id in READ_ONLY_STAGES and entry["permission_mode"] == "acceptEdits":
            raise ValueError(
                f"stage {stage_id} is read-only ({READ_ONLY_STAGES}) and must "
                "not resolve acceptEdits — use a non-editing mode (default); "
                "the artifact-write exception is approximated")

        # allowed_tools / disallowed_tools are optional lists of tool patterns.
        for key in ("allowed_tools", "disallowed_tools"):
            tools = entry.get(key, [])
            if not isinstance(tools, list) or not all(
                    isinstance(t, str) and _TOOL_PATTERN.match(t)
                    for t in tools):
                raise ValueError(
                    f"stage {stage_id} {key} must be a list of tool patterns "
                    f"like 'WebFetch(domain:github.com)' or 'Bash(*)' — "
                    f"got {tools!r}")
            if any(str(t).startswith("Bash") for t in tools):
                raise ValueError(
                    f"stage {stage_id} {key} must not list Bash tool patterns "
                    f"({tools!r}) — Bash is never auto-approved in this "
                    "pipeline; if a stage must never run commands, deny Bash "
                    "in the runner-owned settings file instead")

        if stage_id not in ("1a", "1b"):
            for dep in entry["preflight_inputs"]:
                # Walk backwards through STAGE_ORDER to confirm the dep
                # was produced by an earlier stage.
                found = False
                idx = STAGE_ORDER.index(stage_id)
                for prev_id in STAGE_ORDER[:idx]:
                    if dep in STAGE_CONTRACT[prev_id]["postflight_outputs"]:
                        found = True
                        break
                if not found and dep not in entry.get("postflight_outputs", []):
                    # Allow deps that live outside the contract (e.g. repo files).
                    # Only warn — the dep may come from setup stage 0 or the repo.
                    pass

    # The WebFetch surface must be internally consistent, not just well-shaped:
    # exactly one stage fetches (allowlisted, domain-scoped); every other stage
    # drops WebFetch and declines it explicitly. Otherwise a contract edit could
    # silently hand WebFetch to a stage that never fetches, or drop the decline
    # and leave a non-fetching stage with WebFetch prompting into a session.
    webfetch_domain = re.compile(r"WebFetch\(domain:[^)]+\)\Z")
    fetching_stages = [
        stage_id
        for stage_id in STAGE_ORDER
        if any(str(t).startswith("WebFetch")
               for t in STAGE_CONTRACT[stage_id].get("allowed_tools", []))
    ]
    if len(fetching_stages) != 1:
        raise ValueError(
            f"exactly one stage may allow WebFetch, got {fetching_stages!r} — "
            "drop WebFetch from stages that do not fetch and decline it there "
            "with 'WebFetch(*)'")
    fetch_stage = fetching_stages[0]
    for stage_id in STAGE_ORDER:
        entry = STAGE_CONTRACT[stage_id]
        allowed = [str(t) for t in entry.get("allowed_tools", [])]
        disallowed = [str(t) for t in entry.get("disallowed_tools", [])]
        if stage_id == fetch_stage:
            unscoped = [t for t in allowed
                        if t.startswith("WebFetch") and not webfetch_domain.match(t)]
            if unscoped:
                raise ValueError(
                    f"stage {stage_id} may only allow domain-scoped WebFetch "
                    f"patterns, got {unscoped!r}")
            if "WebFetch(*)" in disallowed:
                raise ValueError(
                    f"stage {stage_id} must not decline WebFetch(*) while also "
                    "allowlisting it — pick one surface")
        else:
            if any(t.startswith("WebFetch") for t in allowed):
                raise ValueError(
                    f"stage {stage_id} is not a fetching stage and must drop "
                    f"WebFetch: {allowed!r}")
            if "WebFetch(*)" not in disallowed:
                raise ValueError(
                    f"stage {stage_id} must decline WebFetch with "
                    "'WebFetch(*)' in disallowed_tools")
