# Scaffolding checklist — {{SERVER}}

Stage 5 output. Tracks phase-by-phase implementation progress.
No single output path — the artifact is the phase-tracked working tree itself.

## How to use

1. Fill `M` from `04-spec.md` §9 (count the phases).
2. Run each phase with `--phase N/M` against the spec.
3. Between phases, run the verify-gate (`implement-workflow:ready-to-push`).
4. Mark gate status below. Do not proceed until the gate is green.

## Phase inventory

| # | Phase | Scope (from 04-spec.md §9) | Acceptance criteria (from 04-spec.md §9) | Gate status |
|---|---|---|---|---|
| 1 | | | | ⬜ pending |
| 2 | | | | ⬜ pending |
| 3 | | | | ⬜ pending |
| 4 | | | | ⬜ pending |

*Add or remove rows to match the actual phase count from 04-spec.md.*

## Gate log

Record the verify-gate result after each phase.

| After phase | `implement-workflow:ready-to-push` output | Pass/Fail | Notes |
|---|---|---|---|
| | | | |

## Rules

- **One phase per session.** Each `--phase N/M` runs in a fresh session that reads `04-spec.md`.
- **Gate between phases.** After phase N completes, run the verify-gate before starting phase N+1. If the gate is red, fix and re-run; do not stack phases on a broken tree.
- **Phase 1 must end in a server that starts, handshakes, and lists tools.** This is the spec's hard requirement — if Phase 1 fails this, do not proceed.
- **No future-phase code.** Each phase must not implement anything from later phases.
- **Conventions from 01-instructions.md §8** apply to every phase (error handling, logging, naming, testing style).
- **stdio constraint.** On `{{TRANSPORT}}=stdio`, nothing may write to stdout except protocol frames. Verify this before finishing each phase.
- **HTTP constraint.** On `{{TRANSPORT}}=streamable-http` or `both`, verify the server includes an HTTP-addressing scaffold: session handling, SSE framing, CORS policy, per-request auth. Verify this before finishing each phase.
- **Confirmation gates.** Any tool marked "confirmation required" in `03-mcp-surface.md` must not execute its side effect without the confirmation flow the spec defines.

## Runner invocation

```bash
# Single phase:
./runner/run_stage.py 5 --phase N/M --docs {{DOCS}} --prompt <prompt-file>

# Batch (all phases sequentially, verify-gate between each):
./runner/run_stage.py --batch --phases M --docs {{DOCS}}
```

The runner validates `--phase N/M` format, checks M consistency with prior manifest entries, and records each phase attempt to `run-manifest.jsonl`.
