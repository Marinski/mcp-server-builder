# Signatures — {{PROJECT}}

Stage 1b output. The public surface of the {{MODE}} interface plus a module dependency
graph.

## Scope

Scope depends on MODE:
- **wrap** — every externally reachable HTTP route / CLI command / message handler.
- **embed** — every exported function, class, and type from the package's public entry
  points.

## Signatures

| Entry | Full signature (with types) | Purpose | Source location | Reads state | Mutates state | I/O | Internal-only? |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

## Dependency graph

```mermaid
graph TD
    %% Module dependency graph — replace with actual modules
```

## Flags

Entries that are exported but appear to be internal-only (unused externally, or marked
`@internal` / private-by-convention). Each claim must be verified by grepping for actual
call sites across the whole repo.
