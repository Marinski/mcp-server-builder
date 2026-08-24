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

## 4. Protocol feature checklist

For every row: **applies / does not apply, because…** — and where it applies, the capability IDs
and what the server does if the client did not declare that capability.

| Feature | Methods | Applies? | Because… |
|---|---|---|---|
| Tasks (SEP-1686) | `tools/call` `task: true`, `tasks/get`, `tasks/result`, `tasks/list`, `tasks/cancel` | | |
| Elicitation | `elicitation/create` | | |
| Progress | `notifications/progress` | | |
| Resource subscriptions | `resources/subscribe`, `notifications/resources/updated` | | |
| Argument completions | completions on prompt/resource-template arguments | | |
| Sampling | `sampling/createMessage` | | |
| Roots | client-provided root list | | |
| Logging | `logging/setLevel` | | |

Wall-clock threshold that makes a tool a task, and where that number came from:

## 5. Tool catalog

### `verb_noun`
- **Description (for the model):** when to use, when NOT to, what it does not do
- **Input schema:** sketch, with per-field descriptions, enums, constraints
- **Output:** structured shape + what the text summary says
- **Annotations:** read-only / destructive / idempotent / open-world
- **Confirmation required:** yes/no — the mechanism (`elicitation/create`, or a
  server-side precondition), what it displays, and the behaviour when the client declared no
  elicitation capability
- **Errors:** recoverable tool results vs protocol errors
- **Source capability:** C_

## 6. Resource catalog

URI scheme, templates and parameters, MIME types, static or dynamic listing, subscriptions.

## 7. Prompt catalog

May be empty — say so explicitly if so.

## 8. Cross-cutting design

Transport and its consequences; auth & secrets; scoping; pagination and size caps;
rate limits, timeouts, retries; statefulness across reconnect; versioning; observability.
Anything that can outlast a client request timeout belongs in §4, not here.

## 9. Rejected alternatives

At least two, with why.

## 10. Risks and open questions

Numbered. Answer inline — this is the cheapest place to change your mind.

1.
