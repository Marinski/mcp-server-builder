# MCP Server Creation Workflow — Sequential Prompt Playbook

A reusable, agent-driven pipeline that turns an existing codebase into a working MCP server.
Each stage runs in a **fresh agent session** and communicates only through files on disk.

Agents are the vendored copies in `agents/` (from the agency-agents marketplace). Skills come
from four plugins — `code` and `implement-workflow` (Bitovi), `engineering` (Anthropic), and
`pskoett-ai-skills` — installed from three marketplaces; see [NOTICE](NOTICE) for provenance
and `README.md` § "Installing the skills the playbook names" for the commands.

**Every named skill is an optional accelerator, not a dependency.** A stage session sees only
the plugins installed where it actually runs — and plugins installed into the desktop app are
invisible to the `claude -p` sessions the runner spawns. A model whose registry lacks a named
skill will quietly follow the prompt's structure instead, producing well-formed output while
hiding that the skill never ran. So every stage prompt below is a complete spec on its own,
and every skill invocation tells the agent what to do when the skill is missing: say so in one
line, then follow the prompt.

---

## 0. Setup (do this once, by hand)

### 0.1 Variables

Fill these in and reuse them across every prompt below. `./new-project.sh` writes them into a
run config so you are not hand-substituting nine times.

| Variable | Meaning | Example |
|---|---|---|
| `{{REPO}}` | Absolute path to the source project | `/path/to/your-project` |
| `{{PROJECT}}` | Human name of the source project | `Your Project` |
| `{{SERVER}}` | MCP server package name | `your-project-mcp-server` |
| `{{LANG}}` | Implementation language / SDK | `TypeScript (@modelcontextprotocol/sdk)` |
| `{{TRANSPORT}}` | `stdio`, `streamable-http`, or both | `stdio` |
| `{{MODE}}` | `wrap` (call the project's HTTP/CLI) or `embed` (import its code) | `wrap` |
| `{{DOCS}}` | Output dir for pipeline artifacts | `{{REPO}}/docs/mcp` |

### 0.2 The `wrap` vs `embed` decision — settle it before Stage 2

- **wrap** — the MCP server is a separate process that calls the project's existing HTTP API / CLI.
  Inventory the *external* interface. Best when the project already runs as a service, is in another
  language, or you want the server independently deployable.
- **embed** — the MCP server imports the project's modules directly.
  Inventory *internal* functions. Best for libraries, or when you need in-process state/performance.

This single choice changes what Stage 2 inventories. Write it into `{{DOCS}}/00-decisions.md` now:

```
MODE: wrap | embed
Rationale: <2–3 sentences>
Consequence: Stage 2 inventories <the HTTP/CLI surface | the internal module surface>
```

### 0.3 Artifact contract

Every stage writes exactly these files. Later stages read them instead of re-crawling the repo.

```
{{DOCS}}/
  00-decisions.md            # 0.2 — wrap/embed + any later ADR-lite calls
  01-instructions.md         # 1a — codebase onboarding for agents
  01-signatures.md           # 1b — public surface + dependency graph
  02-capability-inventory.md # 2  — capabilities in MCP terms  ← the key artifact
  03-mcp-surface.md          # 3  — ADR: tools/resources/prompts design
  04-spec.md                 # 4  — implementation spec
  05-test-plan.md            # 6  — test strategy
  06-review.md               # 7  — hardening findings
  07-release.md              # 8  — install/config instructions
```

---

## Stage 1 — Codebase onboarding

**Run in:** fresh session, repo mounted, read-only.
**Agent:** `agency-engineering:Codebase Onboarding Engineer` (`agents/engineering-codebase-onboarding-engineer.md`)
**Skills:** `code:instruction-generation`, then `code:signatures`.

> **Skills that write into the target repo.** `code:instruction-generation` emits several
> intermediate artifacts and targets `.github/copilot-instructions.md`. That collides with
> this stage being read-only, with the artifact contract, and — if the project already has
> a `copilot-instructions.md` — with a tracked file it will overwrite. Run the skill's
> analysis inline and emit only the stage's own output document. Verify with `git status`
> before you finish.

> **Absolute claims decay.** Any "zero X" or "always Y" claim written into `01-instructions.md` —
> zero runtime dependencies, always validates before writing, etc. — is a snapshot of the source
> at write time, not a permanent fact. A later stage that trusts it without re-checking can run on
> a stale claim for stages before anyone notices: on one run, a "zero runtime dependencies" claim
> here was wrong by Stage 4b, three stages after it was written. Any stage about to make a design
> or spec decision on a claim from an earlier artifact re-verifies it against current source
> first — it does not inherit it as fact.

The agent matters here: it is briefed to state only facts grounded in source and to trace code
paths rather than summarise the README. That is exactly the failure mode Stage 2 inherits if
this document is wrong.

### 1a — Instructions file

```
Use the code:instruction-generation skill on the codebase at {{REPO}}. If that skill is not
registered in this session, say so in one line and follow this prompt's structure instead.

Goal: produce an onboarding document that a *different* AI agent with no prior context can
read in one pass and then work productively in this codebase.

Write the result to {{DOCS}}/01-instructions.md.

Cover, in this order:
1. What this project does, in 5 sentences, for someone who has never seen it.
2. Tech stack with versions, package manager, build/test/lint commands that actually work.
3. Directory map — for each top-level dir, one line on what lives there.
4. Architecture: entry points, request/data flow, key abstractions and where they are defined.
5. Domain vocabulary — the 10–20 nouns this codebase uses and what each one means here
   (include any term whose meaning differs from its common industry usage).
6. State and side effects: databases, caches, external services, filesystem writes, background jobs.
7. Auth model: how a caller is identified and authorised, where that is enforced.
8. Conventions: error handling, logging, config/env vars, naming, testing style.
9. Gotchas: anything that surprised you while reading, or that would mislead a new agent.

Rules:
- Cite file paths (and line numbers where useful) for every non-obvious claim.
- Do not speculate. If something is unclear from the code, list it under "Open questions".
- No marketing language. No restating the README verbatim.
```

### 1b — Signatures + dependency graph

```
Use the code:signatures skill on {{REPO}}. If that skill is not registered in this session,
say so in one line and follow this prompt's structure instead.

Write the result to {{DOCS}}/01-signatures.md.

Scope: the {{MODE}} surface only —
  - if MODE=wrap: every externally reachable HTTP route / CLI command / message handler.
  - if MODE=embed: every exported function, class, and type from the package's public entry points.

For each entry include: full signature with types, one-line purpose, source location,
and whether it reads state, mutates state, or performs I/O.

End with a mermaid dependency graph of the modules involved.
Flag any entry that is exported but appears to be internal-only (unused externally, or marked
@internal / private-by-convention).

Do not trust this brief's own assumptions, or any existing doc's, about which script is
library-only, which function is an internal helper, or which entry point is unreachable —
verify every such claim by grepping for actual call sites and process-spawn invocations across
the whole repo. Two real cases on one run were wrong this way: a script assumed "library-only"
was also spawned as a subprocess elsewhere, and a function assumed to be an "internal helper"
had zero callers and was really an independent third entry point.
```

---

## Stage 2 — Technical writer: capability inventory

**Run in:** fresh session. **Reads:** `01-instructions.md`, `01-signatures.md`, existing docs, repo.
**Writes:** `02-capability-inventory.md`.
**Agent:** `agency-engineering:Technical Writer` (`agents/engineering-technical-writer.md`)

This is deliberately **not** API documentation. It is the input the MCP design needs.

```
You are a technical writer preparing input for an MCP server design.

Read:
- {{DOCS}}/00-decisions.md
- {{DOCS}}/01-instructions.md
- {{DOCS}}/01-signatures.md
- any existing docs in {{REPO}}/docs
- the source at {{REPO}} as needed to verify claims

Produce {{DOCS}}/02-capability-inventory.md. Do NOT write a full API reference.

Part A — Capability table. One row per thing the system can DO (not per endpoint or function;
merge trivial variants, split anything that does two unrelated things). Columns:
  | ID | Capability | One-line description | Inputs (name: type, required?) | Output shape |
  | Read/Write | Side effects | Cost & latency | Auth/context required | Implemented at (file:symbol) |

For "Side effects" be specific: writes to DB table X, sends email, spends money, places a live
order, deletes files, is irreversible. Say "none" only when you have verified none.

Part B — Danger list. Capabilities that must NEVER be exposed to an autonomous caller, or that
must require explicit human confirmation. One line of justification each.

Part C — Deceptive granularity. Operations that look atomic but are actually multi-step, and
operations that look separate but must always be called together (ordering constraints, required
setup calls, session/handle lifecycles).

Part D — Data the caller would want to READ but that is not an action: config, catalogs, status,
recent records, schemas. List these separately — they are resource candidates, not tool candidates.

Part E — Identity & scoping. How does a call know which user/account/workspace it acts on?
Is that a parameter, ambient config, or session state?

Part F — Open questions for the maintainer. Numbered, answerable, blocking-first.

Part G — Structured-output check (wrap mode only; skip if MODE=embed). For every capability
whose result a caller needs back in machine-readable form — an id to reference in a later call,
a status enum, a count — confirm there is an actual machine-readable channel for it TODAY, not
just a human-readable success message. A CLI that prints "Backtest submitted" with the id only
in prose, or an HTTP response that 200s with no body, means the capability cannot be composed
into a second tool call without a separate lookup. Flag every such gap here; this is the
cheapest stage to catch it — a real one surfaced three stages late, at Stage 4a, because Stage 2
did not check for it.

Rules:
- Every row cites a source location. If you cannot find the implementation, mark it UNVERIFIED.
- Target 2–5 pages. Density over completeness of prose.
- Use the project's own domain vocabulary from 01-instructions.md §5.
```

**Human gate:** answer Part F inline before Stage 3 reads this file.

---

## Stage 3 — Architect: MCP surface design

**Run in:** fresh session **with web access**. **Reads:** `00`, `01-*`, `02`.
**Writes:** `03-mcp-surface.md`.
**Agents:** `agency-specialized:MCP Builder` (`agents/specialized-mcp-builder.md`) as lead,
with `agency-engineering:Software Architect` (`agents/engineering-software-architect.md`)
for the ADR discipline.
**Skill:** `engineering:architecture` (ADR format).

The web-fetch step matters: the MCP specification is versioned and revises regularly. Do not let
the agent design against whatever it remembers.

```
You are designing the MCP surface for {{PROJECT}}. Use the engineering:architecture skill;
if it is not registered in this session, say so in one line and follow this prompt's
structure instead.

STEP 1 — Ground yourself in the current spec. Fetch and read:
- https://modelcontextprotocol.io/specification (find the LATEST revision, note its date)
- the tools, resources, and prompts pages of that revision
- the {{LANG}} SDK README/docs for the version you will use
- confirm the {{LANG}} SDK package is actually published and installable — check its registry
  page, not just a README mention. A real v1-vs-v2 name ambiguity has surfaced this way before.
- for any SDK convenience behaviour the design will lean on (pagination, batching, retries),
  verify it against the SDK's actual source or installed package, not the protocol spec alone.
  The protocol spec describes what a compliant server may do; it does not describe what a
  specific SDK's helper actually implements. One run's ADR assumed cursor-based pagination that
  the installed SDK did not support.
Record in your output: spec revision date, SDK name and version.
If anything below contradicts the current spec, the spec wins — flag the conflict explicitly.

STEP 2 — Read {{DOCS}}/00-decisions.md, 01-instructions.md, 01-signatures.md,
02-capability-inventory.md. Design decisions must trace back to capability IDs from Part A.

STEP 3 — Write {{DOCS}}/03-mcp-surface.md as an ADR containing:

1. Context — what {{PROJECT}} is, who will call this server, through which client(s).

2. Primitive allocation. For every capability ID in the inventory, decide:
   TOOL / RESOURCE / PROMPT / EXCLUDED — with a one-line reason.
   Heuristics: model-invoked actions are tools; addressable read-only data the client can attach
   as context is a resource; user-invoked reusable templates are prompts. Everything on the
   inventory's Danger list is EXCLUDED unless there is a written reason to include it.
   Nothing from Part A may be left unallocated.

3. Granularity decision. Argue explicitly for one of:
   (a) many narrow tools, (b) fewer coarse tools with a mode/action enum, (c) a hybrid.
   State the trade-off in terms of model accuracy vs tool-list token cost. Target a count you
   name and justify. If >20 tools, justify why the model will still choose correctly.

4. Tool catalog. For each tool:
   - name (verb_noun, snake_case, prefixed if collision-prone)
   - description written FOR THE MODEL: when to use it, when NOT to, what it does not do
   - input JSON Schema sketch, with per-field descriptions, enums, and constraints
   - output: structured content shape, plus what the text summary says
   - annotations/hints: read-only, destructive, idempotent, open-world. Check the field
     names against the revision you fetched, and note that clients treat annotations as
     UNTRUSTED — they are a hint to the host, never an enforcement mechanism.
   - confirmation required? (yes/no) and what the confirmation must display. Confirmation
     is **not a server-side protocol capability** in any revision published so far: it is
     an obligation on the client. Say what the server does to make the side effect
     impossible without confirmation, because declaring an annotation does not.
   - errors: which failures are tool-level results the model can recover from vs protocol errors
   - source capability ID(s)

5. Resource catalog. URI scheme, templates and their parameters, MIME types, whether the list is
   static or dynamic, subscription/update behaviour if any.

6. Prompt catalog (may be empty — say so if so).

7. Cross-cutting design:
   - transport(s): {{TRANSPORT}}, and the consequences (e.g. on stdio, nothing may be written to
     stdout except protocol frames — all logging goes to stderr or the logging capability)
   - auth & secrets: where credentials come from, what is never accepted as a tool argument
   - scoping/multi-account: how a call selects the target account/workspace
   - pagination for anything unbounded; hard caps on result size
   - rate limiting, timeouts, retries
   - statefulness: what the server holds between calls, and what happens on reconnect
   - versioning: how the tool surface evolves without breaking clients
   - observability: what gets logged, and how logs stay out of the transport

8. Rejected alternatives — at least two, with why.

9. Risks and open questions, numbered.

Rules:
- No implementation code. Schemas as sketches, not final source.
- Every tool must be justifiable to a sceptic asking "why would a model ever call this?"
- Prefer excluding a capability over exposing one that is dangerous or hard to describe.
```

**Human gate:** read this document yourself before continuing. It is the cheapest place to change
your mind. Answer §9 inline in the file.

> **Expect to find defects in the wrapped product.** Deciding what an autonomous caller may
> do forces a harder look at the authorization model than feature work ever does, so stages
> 2 and 3 routinely surface real vulnerabilities — missing object-level authorization is the
> common one, because a UI that only ever passes a user their own ids hides it completely.
> When it happens: fork the findings to a separate security track rather than absorbing them
> into this pipeline, and verify each one in source yourself before it drives a design
> decision. Then decide explicitly whether to design against the product as it is, or as it
> will be once fixed — that answer changes the tool surface substantially, and it belongs in
> `00-decisions.md`.

> **Cite capability IDs, not counts, in `00-decisions.md`.** A decision that names "3 tools" or
> "the read-only subset" is easy to miscount against the ADR's actual tool catalog. One run's own
> decision said 3 when the ADR it was describing actually allocated 5. Any decision restricting
> or naming a subset of tools must list the canonical capability IDs (`C4`, `C7`, ...) inline —
> never just a count or a category label.

---

## Stage 4 — PM: implementation spec

**Run in:** fresh session. **Reads:** `01-*`, `02`, `03`. **Writes:** `04-spec.md`.
**Agent:** `agency-product:Product Manager` (`agents/product-manager.md`)
**Skills:** `code:spec`, then `code:spec-answered-questions`, then `code:spec-check`.

### 4a — Draft

```
Use the code:spec skill to produce {{DOCS}}/04-spec.md — an implementation spec for {{SERVER}},
an MCP server for {{PROJECT}} in {{LANG}} using transport {{TRANSPORT}}. If that skill is not
registered in this session, say so in one line and follow this prompt's structure instead.

Authoritative inputs (read all, in this order):
- {{DOCS}}/03-mcp-surface.md   ← the design. Do not redesign it. If you disagree, log it as a question.
- {{DOCS}}/02-capability-inventory.md
- {{DOCS}}/01-instructions.md and 01-signatures.md
- {{REPO}} for anything you must verify

The spec must contain:
1. Scope — in and explicitly out. Non-goals.
2. Repo layout: every file to be created, with its responsibility in one line.
3. Dependencies with versions, and why each is needed.
4. Server bootstrap: initialisation, capability declaration, transport wiring, graceful shutdown,
   signal handling.
5. Config: every env var — name, type, required?, default, validation, failure mode if missing.
6. A section per tool: exact name, final input schema, validation rules, the underlying
   {{MODE}} call it makes, output mapping, every error path and its user-visible message,
   confirmation flow if required.
7. A section per resource: URI template, resolution logic, MIME type, listing behaviour.
8. Shared internals: the client/adapter to {{PROJECT}}, error taxonomy and mapping,
   logging, pagination helper, any guard/policy layer.
9. Implementation phases — 4 to 7 ordered phases, each independently runnable and testable,
   each with explicit acceptance criteria. Phase 1 must end in a server that starts, handshakes,
   and lists tools successfully.
10. Numbered open questions, blocking ones first.

Rules:
- Concrete over abstract: real names, real types, real env vars, real error strings.
- Do not write the implementation. This is a plan a competent developer executes without guessing.
- Flag any place where 03-mcp-surface.md is ambiguous rather than silently choosing.
```

### 4b — Resolve and validate

```
I have answered the open questions inline in {{DOCS}}/04-spec.md.
Use the code:spec-answered-questions skill to fold the answers into the body of the spec,
remove the resolved questions, and keep the document internally consistent. If that skill
is not registered in this session, say so in one line and do exactly that yourself.
```

```
Use the code:spec-check skill on {{DOCS}}/04-spec.md, validated against {{REPO}} and
{{DOCS}}/03-mcp-surface.md. If that skill is not registered in this session, say so in one
line and perform the check this prompt describes yourself.

Report: contradictions, redundancy, gaps, and any spec claim about {{PROJECT}} that the code
does not support. Explicitly cross-check every numeric or enum constraint (valid ranges, limits,
allowed values) against every other place the spec states or exercises it — including its own
acceptance-criteria test values. One run's contradiction-detection step missed exactly this: a
config's own valid-range declaration contradicted the value its own acceptance-criteria test
used, because nothing forced a cross-section check on constraints specifically. Then apply the
fixes. List what you changed.
```

**Human gate:** skim the tool sections. If a tool's description would confuse *you*, it will
confuse the model.

---

## Stage 5 — Implementation

**Run in:** fresh session per phase, with write access. **Reads:** `04-spec.md`.
**Agent:** `agency-specialized:MCP Builder` (`agents/specialized-mcp-builder.md`)
**Skill:** `code:spec-implement`.

```
Use the code:spec-implement skill to implement {{DOCS}}/04-spec.md, PHASE {{N}} ONLY.
If that skill is not registered in this session, say so in one line and implement the
phase directly under the constraints below.

Before writing code, restate: this phase's scope, its acceptance criteria, and the files you
will touch. Then implement.

Constraints:
- Do not implement future phases. Do not refactor outside this phase's files.
- Follow the conventions in {{DOCS}}/01-instructions.md §8.
- On {{TRANSPORT}}=stdio: nothing may write to stdout except protocol frames. Route all logging
  to stderr. Verify this before you finish.
- Every tool input is validated at the boundary; never trust the model's arguments.
- Secrets come from env/config only, never from tool arguments, and never appear in logs,
  error messages, or tool output.
- Any tool marked "confirmation required" in 03-mcp-surface.md must not execute its side effect
  without the explicit confirmation flow the spec defines.
- Do not assume POSIX filesystem/process semantics. If the target platform includes Windows, or
  you cannot rule it out, verify timestamp, file-copy, and process-spawn behaviour directly
  against that platform rather than by analogy. A backup-freshness check on one run broke on
  Windows' file-copy timestamp behaviour, which differs from POSIX's in exactly the way that
  check relied on.
- When this phase implements a capability that is structurally similar to one built in an
  earlier phase (e.g. two different ways of running the same underlying job), re-verify that any
  reliability property the earlier phase established still holds for this one — it can silently
  not, because the two are built differently under the hood. On one run, a locally-run job's
  status silently lost the "survives restart" property its pipeline-run sibling had, because the
  two were implemented on different mechanisms and nobody re-checked the property transferred.

When done: run the project's build, tests, and lint. Paste the actual output. Then state which
acceptance criteria pass and which do not. Do not claim success you have not observed.
```

Between phases, run the verify gate:

```
Run the implement-workflow:ready-to-push skill on the current working tree.
It runs tests → lint → format/type check → build in order, fixing and re-running each until it
passes before moving to the next. If that skill is not registered in this session, say so in
one line and run that sequence yourself. Paste the actual output. Do not proceed to the next
phase until it is green.
```

---

## Stage 6 — Test the protocol surface, not just the code

**Run in:** fresh session. **Writes:** `05-test-plan.md` + test files.
**Agent:** `agency-testing:Test Automation Engineer` (`agents/testing-test-automation-engineer.md`)
**Skill:** `engineering:testing-strategy`.

```
Use the engineering:testing-strategy skill to produce {{DOCS}}/05-test-plan.md and then
implement the tests for {{SERVER}} (spec: {{DOCS}}/04-spec.md). If that skill is not
registered in this session, say so in one line and follow this prompt's structure instead.

Four layers, all required:

L1 Unit — schema validation (valid, invalid, missing, wrong type, boundary), error mapping,
   pagination helper, and the {{PROJECT}} adapter against mocked responses.

L2 Protocol — start the server over {{TRANSPORT}} and assert:
   - initialize handshake succeeds and declares the intended capabilities
   - tools/list returns exactly the catalog in 03-mcp-surface.md (names, count, schemas valid)
   - resources/list and resource reads resolve
   - a tool error returns a recoverable tool result, not a transport crash
   - unknown tool name, malformed args, and oversized results all degrade gracefully
   - on stdio: stdout carries protocol frames ONLY (assert this explicitly)

L3 Integration — real calls against a test instance of {{PROJECT}}, read-only tools freely,
   mutating tools against disposable data only.

L4 Manual smoke — a checklist for MCP Inspector: launch command, each tool exercised once with
   sample arguments, expected result. Then a real client config and one end-to-end task.

Also add: a test that every tool's description is non-empty and mentions when NOT to use it,
and a snapshot test on the tool list so surface changes are never silent.

Any test that polls on a wall-clock timeout (waiting for a job to finish, a file to appear, a
process to exit) must justify that number against real observed contention — paste the
measurement that produced it. A timeout picked in isolation, without measuring actual latency
under load, is usually wrong in one direction or the other and becomes a flaky test either way.

Report actual pass/fail output. Do not mark a layer complete without running it.
```

---

## Stage 7 — Harden

**Run in:** fresh session. **Writes:** `06-review.md`.
**Agents:** `agency-engineering:Code Reviewer` (`agents/engineering-code-reviewer.md`) and
`agency-security:Application Security Engineer` (`agents/security-appsec-engineer.md`) for the
review; `agency-engineering:Minimal Change Engineer` (`agents/engineering-minimal-change-engineer.md`)
for the simplify pass — it is briefed to refuse scope creep, which is what keeps a hardening
pass from turning into a refactor.
**Skills:** `engineering:code-review`, then `pskoett-ai-skills:simplify-and-harden`.

```
Review {{SERVER}} for release. Write findings to {{DOCS}}/06-review.md, severity-ordered,
each with file:line and a concrete fix. Then apply everything at HIGH and above.

Security & safety:
- Can any tool argument reach a shell, SQL query, filesystem path, or URL without validation?
- Path traversal on any resource URI or file-taking tool?
- SSRF on anything that fetches a caller-supplied URL?
- Do secrets, tokens, or internal hostnames appear in any tool result, error, or log line?
- Is every destructive tool gated exactly as 03-mcp-surface.md requires?
- Can a crafted tool result influence the calling model in a way it should not
  (injected instructions echoed from {{PROJECT}} data)? What is neutralised, and where?
- Are results size-capped so one call cannot blow the client's context?

Correctness & operations:
- Unbounded loops, missing timeouts, unhandled promise rejections, resource leaks
- Behaviour when {{PROJECT}} is down, slow, or returns garbage
- Concurrency: two tool calls in flight — any shared mutable state?
- Restart/reconnect behaviour

Model ergonomics:
- Read every tool description as if you were the model. Which two tools could be confused?
  Which description fails to say when NOT to use it? Fix the descriptions.

Then run the pskoett-ai-skills:simplify-and-harden skill: remove dead abstraction, collapse
needless indirection, delete speculative generality. If that skill is not registered in this
session, say so in one line and do that pass directly. Do not change behaviour. Re-run build,
tests, and lint; paste the output.

That skill's harden pass overlaps the security review above. Treat the review as
authoritative: anything harden raises that the review missed is a finding to add to
06-review.md, not a licence to re-open decisions the review already settled.
```

> **Verify the simplify pass's own completion — do not trust its report.** It has been observed to
> spawn several nested background review agents, then return as though finished before they
> actually completed, with zero edits applied despite reporting done. Their findings were not
> lost — they could be recovered and applied manually — but the skill's own "done" was false.
> Check the actual diff after it runs, not just its final message.

> **Never run two fix-applying review agents concurrently against the same tree.** Both this
> stage's review pass and its simplify-and-harden pass write to the same files. If you parallelise review
> work, only concurrent *read-only* agents or ones scoped to disjoint, isolated trees are safe —
> two agents both allowed to apply fixes against the same working tree at once is a real
> near-miss waiting to happen (one run caught this only by manually stopping an agent mid-run).

---

## Stage 8 — Package and document

**Run in:** fresh session. **Writes:** `07-release.md`, `README.md`.
**Agent:** `agency-engineering:Technical Writer` (`agents/engineering-technical-writer.md`)
**Skill:** `engineering:documentation`.

```
Use the engineering:documentation skill to write the README for {{SERVER}} and
{{DOCS}}/07-release.md. If that skill is not registered in this session, say so in one
line and follow this prompt's structure instead.

README (for a user who has never seen {{PROJECT}}):
1. What this server lets an AI assistant do — 3 sentences, concrete.
2. Requirements and installation.
3. Configuration: every env var in a table — name, required?, default, what it does, how to obtain it.
4. Client setup: a copy-pasteable config block for the target client(s), for {{TRANSPORT}}.
5. Tool reference: a table of name + one-line purpose. Full schemas link to the code.
6. Resources: URI patterns with an example.
7. Safety notes: which tools mutate state, which require confirmation, what the server will
   never do. Be direct about anything that spends money or is irreversible.
8. Troubleshooting: the 5 failures a new user will actually hit, with the fix for each.
9. Development: build, test, run under MCP Inspector.

07-release.md (for you, the maintainer):
- version, spec revision and SDK version targeted
- the tool surface as of this release (this is the compatibility contract)
- known limitations and deliberate exclusions, with reasons
- what to check before publishing, and the rollback story

Derive everything from the code as built, not from the spec as written. Where they differ,
the code wins and you note the drift.
```

---

## Stage 9 — Capture what the pipeline got wrong

Run once at the end, while it is fresh.

```
Use the pskoett-ai-skills:self-improvement skill. If that skill is not registered in this
session, say so in one line and follow this prompt's structure instead.

Review this MCP server build end to end. Capture, as durable learnings:
- Which stage produced output the next stage could not use, and what was missing.
- Every place I had to correct an agent, and the correction.
- Anything about MCP or {{LANG}} SDK behaviour that contradicted an agent's assumptions.
- Which prompts in this playbook should change for the next project — quote the exact edit.

Then write the revised prompt text back into
<path-to>/mcp-server-builder/mcp-server-creation-workflow.md.
```

---

## Quick reference — the whole run

| # | Stage | Agent | Skill | Produces | Gate |
|---|---|---|---|---|---|
| 0 | Decide wrap/embed | — | — | `00-decisions.md` | human |
| 1a | Onboarding | Codebase Onboarding Engineer | `code:instruction-generation` | `01-instructions.md` | — |
| 1b | Signatures | Codebase Onboarding Engineer | `code:signatures` | `01-signatures.md` | — |
| 2 | Capability inventory | Technical Writer | — | `02-capability-inventory.md` | human answers Part F |
| 3 | MCP surface ADR | MCP Builder + Software Architect | `engineering:architecture` | `03-mcp-surface.md` | **human — cheapest place to change your mind** |
| 4 | Spec + check | Product Manager | `code:spec`, `code:spec-answered-questions`, `code:spec-check` | `04-spec.md` | human answers questions |
| 5 | Implement, phase by phase | MCP Builder | `code:spec-implement`, `implement-workflow:ready-to-push` | code | gate green per phase |
| 6 | Test L1–L4 | Test Automation Engineer | `engineering:testing-strategy` | `05-test-plan.md`, tests | Inspector smoke passes |
| 7 | Harden | Code Reviewer + AppSec Engineer + Minimal Change Engineer | `engineering:code-review`, `pskoett-ai-skills:simplify-and-harden` | `06-review.md` | HIGH+ findings fixed |
| 8 | Package | Technical Writer | `engineering:documentation` | `README.md`, `07-release.md` | real client, real task |
| 9 | Retro | — | `pskoett-ai-skills:self-improvement` | updated playbook | — |

### Rules that make this work

1. **Fresh session per stage.** Pass files, not conversation history. A PM inheriting the writer's
   half-formed opinions is the main failure mode.
2. **Never skip Stage 3.** Going inventory → spec produces a server that mirrors your internal API
   instead of one a model can use.
3. **Design against the fetched spec**, not the agent's memory of it.
4. **Fewer, better-described tools beat complete coverage.** A capability you exclude costs you
   nothing; a tool the model misuses costs you trust.
5. **Demand pasted command output** at every verification point. "Tests pass" without output is
   not evidence.
6. **Never run two fix-applying agents concurrently against the same working tree**, in any
   stage. Concurrent agents are only safe when every one of them is read-only, or each is scoped
   to a disjoint, isolated tree. Two agents both able to write against the same tree at once is
   how a run loses track of what actually changed.
