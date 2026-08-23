# Execution models

The playbook's first rule is *fresh session per stage, pass files not conversation
history*. There are two honest ways to satisfy it. Both are supported; they differ in
what can go wrong.

## Model A — orchestrated subagents (recommended)

One long-lived **orchestrator** session spawns a cold subagent per stage. The subagent
cannot see the orchestrator's conversation; it reads only the brief it is given plus
files on disk.

```
orchestrator ──spawn──> stage 1a agent ──writes──> 01-instructions.md
             ──spawn──> stage 1b agent ──writes──> 01-signatures.md
             ──spawn──> stage 2  agent ──reads 00,01──> 02-capability-inventory.md
```

This satisfies rule 1 exactly — no stage inherits another stage's reasoning — and adds
four things manual sessions cannot do:

- **Parallelism.** Stages 1a and 1b are independent and read-only; run them together.
- **Verification between stages.** The orchestrator can check a subagent's claims against
  source before passing them on. In the reference run this caught security findings that
  needed confirming before they could drive a design decision.
- **Fallback.** A failed stage can be retried on another model (see `models.yaml`).
- **Contract enforcement.** The orchestrator ensures each stage writes the artifact the
  next one expects, in the place it expects it.

### The failure mode it introduces: orchestrator leakage

The orchestrator's brief is a context channel. Left undisciplined, it becomes exactly the
shared-context problem rule 1 exists to prevent — except now it is invisible, because it
lives in a prompt nobody archived.

**The rule that makes Model A safe:**

> The orchestrator may pass only:
>   (a) pointers to artifacts,
>   (b) decisions a human made at a gate,
>   (c) facts it verified in source itself.
>
> Never its own opinions, designs, or predictions. Anything that shapes the design must be
> written into an artifact **first**, then referenced — so the next stage reads it from
> disk and a human can see and override it.

The test: if a subagent's output depends on something you said in a brief that is not in
any artifact, you have leaked, and the human has lost the ability to audit that input.

Two corollaries:

- **Resolve contradictions in the artifact, not the brief.** When gate answers conflict,
  write the resolution into `00-decisions.md` and let the stage read it there.
- **Keep the orchestrator thin.** It should hold summaries and file paths, not artifact
  content. It is the one session that lives for the whole run; if it accumulates the full
  text of every artifact it will run out of context before the retro.

## Model B — separate sessions by hand

Open a fresh session per stage yourself, paste the variable block, run the stage prompt.

Slower and unparallelisable, and you become the verification step. But it is the honest
fallback when you have no orchestration available, and it makes leakage structurally
impossible — the only channel between stages is the filesystem.

Use it when a stage matters enough that you want to read every input yourself, or when
debugging a stage whose output keeps coming out wrong.

## Which to use

Model A for a real run. Model B for the stages where you want to be the one deciding what
goes in — in practice Stage 3, which is where the design is actually made.

Nothing about the playbook changes between them. The prompts are the same; only who opens
the session differs.
