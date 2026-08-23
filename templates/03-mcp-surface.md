# ADR: MCP surface for {{PROJECT}}

- **Spec revision:** <date, from the fetched specification>
- **SDK:** <name and version>
- **Status:** proposed | accepted
- **Conflicts with current spec:** <none, or flag them explicitly>

## 1. Context

What {{PROJECT}} is, who calls this server, through which client(s).

## 2. Primitive allocation

Every capability ID from inventory Part A. Nothing may be left unallocated.

| Capability ID | TOOL / RESOURCE / PROMPT / EXCLUDED | Reason |
|---|---|---|

## 3. Granularity decision

(a) many narrow tools, (b) fewer coarse tools with an action enum, or (c) hybrid.
Trade-off in model accuracy vs tool-list token cost. Target count, named and justified.

## 4. Tool catalog

### `verb_noun`
- **Description (for the model):** when to use, when NOT to, what it does not do
- **Input schema:** sketch, with per-field descriptions, enums, constraints
- **Output:** structured shape + what the text summary says
- **Annotations:** read-only / destructive / idempotent / open-world
- **Confirmation required:** yes/no — and what the confirmation displays
- **Errors:** recoverable tool results vs protocol errors
- **Source capability:** C_

## 5. Resource catalog

URI scheme, templates and parameters, MIME types, static or dynamic listing, subscriptions.

## 6. Prompt catalog

May be empty — say so explicitly if so.

## 7. Cross-cutting design

Transport and its consequences; auth & secrets; scoping; pagination and size caps;
rate limits, timeouts, retries; statefulness across reconnect; versioning; observability.

## 8. Rejected alternatives

At least two, with why.

## 9. Risks and open questions

Numbered. Answer inline — this is the cheapest place to change your mind.

1.
