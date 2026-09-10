# Implementation spec — {{SERVER}}

Stage 4 output. A plan a competent developer executes without guessing.

## 1. Scope

### In scope

- 

### Out of scope (non-goals)

- 

## 2. Repo layout

Every file to be created, with its responsibility in one line.

| File | Responsibility |
|---|---|
| | |

## 3. Dependencies

| Package | Version | Why needed |
|---|---|---|
| | | |

## 4. Server bootstrap

Initialisation, capability declaration, transport wiring, graceful shutdown, signal handling.

## 5. Config

Every env var.

| Variable | Type | Required? | Default | Validation | Failure mode if missing |
|---|---|---|---|---|---|
| | | | | | |

## 6. Tools

### `verb_noun`

- **Input schema:** 
- **Validation rules:** 
- **Underlying call ({{MODE}}):** 
- **Output mapping:** 
- **Errors:** 
- **Confirmation required:** yes/no — mechanism, display, behaviour when client has no elicitation

## 7. Resources

| URI template | Resolution logic | MIME type | Listing behaviour |
|---|---|---|---|
| | | | |

## 8. Shared internals

Client/adapter to {{PROJECT}}, error taxonomy and mapping, logging, pagination helper,
any guard/policy layer.

## 9. Implementation phases

Ordered, independently runnable and testable. Phase 1 must end in a server that starts,
handshakes, and lists tools successfully.

### Phase 1

**Scope:**
**Acceptance criteria:**
- 

### Phase 2

**Scope:**
**Acceptance criteria:**
- 

### Phase 3

**Scope:**
**Acceptance criteria:**
- 

### Phase 4

**Scope:**
**Acceptance criteria:**
- 

## Open questions

Numbered, blocking ones first. Answer inline before Stage 5 reads this file.

1.
