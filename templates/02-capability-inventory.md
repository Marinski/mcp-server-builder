# Capability inventory — {{PROJECT}}

Source: {{MODE}} surface. Every row cites `file:symbol`; unverified rows are marked UNVERIFIED.

## Part A — Capabilities

| ID | Capability | Description | Inputs (name: type, required?) | Output shape | Read/Write | Side effects | Cost & latency | Auth/context | Implemented at |
|---|---|---|---|---|---|---|---|---|---|
| C1 | | | | | | | | | |

## Part B — Danger list

Capabilities that must never be exposed to an autonomous caller, or that require explicit
human confirmation. One line of justification each.

| ID | Why it is dangerous | Never expose / confirm |
|---|---|---|

## Part C — Deceptive granularity

Operations that look atomic but are multi-step, and operations that must always be called
together (ordering constraints, required setup calls, session/handle lifecycles).

## Part D — Resource candidates

Data a caller would want to READ but which is not an action: config, catalogs, status,
recent records, schemas.

| ID | Data | Shape | Changes how often? | Implemented at |
|---|---|---|---|---|

## Part E — Identity & scoping

How does a call know which user/account/workspace it acts on — parameter, ambient config,
or session state?

## Part F — Open questions for the maintainer

Numbered, answerable, blocking-first. Answer inline before Stage 3 reads this file.

1.
