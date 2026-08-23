# Hardening review — {{SERVER}}

Severity-ordered. Everything HIGH and above is fixed before release.

| # | Severity | Finding | file:line | Fix | Status |
|---|---|---|---|---|---|
| 1 | HIGH | | | | fixed / accepted-risk |

## Security & safety

- Tool argument reaching shell / SQL / filesystem path / URL without validation:
- Path traversal on resource URIs or file-taking tools:
- SSRF on caller-supplied URLs:
- Secrets, tokens, internal hostnames in results, errors, or logs:
- Destructive tools gated exactly as the ADR requires:
- Injected instructions echoed from {{PROJECT}} data — what is neutralised, and where:
- Result size caps:

## Correctness & operations

- Unbounded loops, missing timeouts, unhandled rejections, leaks:
- Behaviour when {{PROJECT}} is down, slow, or returns garbage:
- Concurrency and shared mutable state:
- Restart/reconnect:

## Model ergonomics

- Which two tools could be confused:
- Which descriptions fail to say when NOT to use them:

## Verification

Build, test, and lint output after fixes — pasted, not summarised.
