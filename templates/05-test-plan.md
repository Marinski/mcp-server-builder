# Test plan — {{SERVER}}

Stage 6 output. Four required layers covering schema validation, protocol conformance,
integration, and manual smoke.

## L1 — Unit

Schema validation (valid, invalid, missing, wrong type, boundary), error mapping,
pagination helper, and the {{PROJECT}} adapter against mocked responses.

| Test | What it covers | Pass criterion |
|---|---|---|
| | | |

## L2 — Protocol

Server started over {{TRANSPORT}}. Assert every item below.

| Check | Assertion |
|---|---|
| initialize handshake | succeeds and declares the intended capabilities |
| tools/list | returns exactly the catalog from 03-mcp-surface.md (names, count, schemas valid) |
| resources/list + reads | resolve correctly |
| tool error | returns a recoverable tool result, not a transport crash |
| unknown tool / malformed args / oversized result | degrade gracefully |
| stdout (stdio only) | carries protocol frames ONLY |
| feature "applies" items from 03-mcp-surface.md §4 | capability appears in handshake and methods work |
| feature "does not apply" items from 03-mcp-surface.md §4 | capability is NOT declared in the handshake |
| tool descriptions | every description is non-empty and mentions when NOT to use it |
| tool list snapshot | surface changes are never silent |

## L3 — Integration

Real calls against a test instance of {{PROJECT}}. Read-only tools used freely;
mutating tools against disposable data only.

| Test | Tool / endpoint | Expected result |
|---|---|---|
| | | |

## L4 — Manual smoke

A checklist for MCP Inspector.

1. Launch command:
2. Each tool exercised once with sample arguments:
3. Expected result for each:
4. Real client config and one end-to-end task:

## Downstream proxy tests (if applicable)

If {{SERVER}} proxies, aggregates, or gateways other MCP servers, use the reference
"everything" server (`@modelcontextprotocol/server-everything`) as a synthetic downstream
in L2 and L3. Assert progress notifications, resource-update notifications, task lifecycles,
elicitation, sampling, and argument completions survive the hop.

## Timeout justification

Any test that polls on a wall-clock timeout must justify that number against real observed
contention. Paste the measurement that produced it.
