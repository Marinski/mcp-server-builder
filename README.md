# mcp-server-builder

Turn an existing codebase into a working MCP server, through a sequential,
agent-driven pipeline.

Nine stages. Each runs in a **fresh agent session** and communicates only through
files on disk. You can route every stage to a different model — including
OpenAI-compatible and local endpoints — with automatic fallback when a provider is
unavailable or you are out of credits.

The pipeline itself is [`mcp-server-creation-workflow.md`](mcp-server-creation-workflow.md).
Everything else here exists to run it.

## Why staged, and why fresh sessions

The failure mode this is built against is a single long session that designs an MCP
server which mirrors your internal API instead of one a model can actually use. Two
rules prevent it:

- **Fresh session per stage.** Later stages read artifacts, not conversation history.
  A spec author inheriting the inventory author's half-formed opinions is the main
  way this goes wrong.
- **Never skip the design stage.** Going straight from capability inventory to
  implementation spec reliably produces one tool per endpoint.

There is deliberately **no single process that runs all nine stages in one context** —
that would reintroduce the shared history the design exists to avoid. You can still
orchestrate the run: see [ORCHESTRATION.md](ORCHESTRATION.md) for the two execution
models, and for the one discipline rule that keeps an orchestrated run honest.

## Quick start

```bash
git clone https://github.com/Marinski/mcp-server-builder
cd mcp-server-builder
cp models.example.yaml models.yaml     # edit to taste; gitignored

./new-project.sh \
  --repo /path/to/your-project \
  --project "Your Project" \
  --server your-project-mcp-server \
  --mode wrap \
  --transport stdio
```

That scaffolds `<your-project>/docs/mcp/` and prints the variable block to paste
into each stage. Then work through the playbook, one fresh session per stage,
stopping at each human gate.

Artifacts land in **the project being wrapped**, never in this repo.

## Using it as a submodule

```bash
git submodule add https://github.com/Marinski/mcp-server-builder .mcp-builder
./.mcp-builder/new-project.sh --repo "$PWD" --project "..." --server "..."
```

The split that keeps this clean:

| Lives in the submodule | Lives in your project |
|---|---|
| playbook, agents, config schema, runner | `docs/mcp/*` artifacts |
| improvements, upstreamed by PR | the generated `mcp-server/` |

Stage 9 (the retro) edits the **playbook**, so its output is a commit in the
submodule. That is intended — improvements flow upstream and every consuming repo
picks them up on the next bump. Never commit a client's capability inventory here.

## Model routing

Configure a provider and model per stage in `models.yaml`, with a fallback chain:

```yaml
defaults:
  provider: anthropic
  model: claude-sonnet-5
  fallback:
    - { provider: anthropic, model: claude-opus-5 }

stages:
  "2": { model: claude-opus-5 }          # capability inventory
  "1b":                                   # cheap model for mechanical extraction
    provider: gateway
    model: openai/qwen3.6
    fallback:
      - { provider: anthropic, model: claude-sonnet-5 }
```

Two runners, because they reach different things:

| | `claude` | `opencode` | `pi` |
|---|---|---|---|
| API shape | Anthropic (`/v1/messages`) | OpenAI-compatible | OpenAI / Anthropic / Google |
| provider selection | none — endpoint only | folded into `provider/model` | **separate `--provider` flag** |
| credential | env var | env var | env var via the provider's `api_key_env` |
| skills / personas | native | via skill autodiscovery | its own extension system |
| fallback chain | native, same provider only | handled by this repo's runner | handled by this repo's runner |

Anything OpenAI-compatible works through `opencode` or `pi` — LiteLLM, vLLM, Ollama,
OpenRouter, a vendor API.

[`pi`](https://github.com/earendil-works/pi) is the cleanest fit of the three: it takes
provider and model as separate flags, and `--mode json` is genuinely non-interactive.
The credential is **not** one of those flags: the runner supplies it through the
provider's `api_key_env` (see `models.example.yaml`), exactly like the other two
runners. Never put a key on a command line — argv is exposed via `ps` and shell
history. If a runner build genuinely requires a key flag, treat it as unsupported
here and route the credential through the config's env-var indirection instead.
Install either way:

```bash
npm i -g @earendil-works/pi-coding-agent        # simplest; provides `pi`
git submodule update --init vendor/pi           # pinned source, if you'd rather build it
```

`vendor/pi` is a submodule pinned to a reviewed commit. It is **not required** to run the
pipeline — the npm package provides the same binary. Pin it when you want the source under
review (their own supply-chain posture is worth matching), or when you want their
`pi-ai` and `pi-evals` packages for work beyond the runner.

Endpoints and keys are read from **environment variables named in the config**, never
stored in it, so `models.yaml` never contains a URL or secret.

The stage CLI does not inherit the invoking shell's whole environment. The runner builds
a minimal child environment — `PATH`, `HOME`, locale (`LANG`/`LC_*`), plus `SYSTEMROOT`,
`TEMP` and `USERPROFILE` on Windows — and overlays only the resolved provider's key and
base URL. An unrelated secret you happen to have exported (an `AWS_SECRET_ACCESS_KEY`,
a `GITHUB_TOKEN`) is not handed to the stage agent.

Inspect what a stage would run, without calling anything:

```bash
./runner/run_stage.py 2 --dry-run
```

Every real run appends to `docs/mcp/run-manifest.jsonl` in the target project, recording
which provider and model produced each artifact and whether the chain fell back:

```json
{"ts":"...","stage":"2","provider":"anthropic","model":"claude-opus-5","attempt":"1/2","ok":true}
```

An artifact does not otherwise say which model wrote it, so a stage that quietly fell back
to a weaker model is indistinguishable from one that did not — which matters most on
exactly the stages where weak models degrade quietly.

### Non-interactive sessions and permissions

A `-p` stage session has no TTY to answer a permission prompt, so anything that would
prompt is **declined by default**. Three consequences, each found the hard way:

- **Working directory.** Claude Code confines file access to the directory it is launched
  from. The runner therefore launches every stage with the *target repo* as its working
  directory (derived from `--docs`, or set explicitly with `--cwd`) — otherwise a stage
  invoked from this repo is silently sandboxed out of the repo it is meant to read and write.
- **File writes.** Every stage writes its own artifact; without `--permission-mode
  acceptEdits` the declined Write leaves the stage exiting 0 with no file on disk and
  the document unrecoverable. The runner passes `--permission-mode` per stage from the stage
  contract (`runner/stage_contract.py`): stages that write project artifacts (2-9) resolve
  `acceptEdits`, while the onboarding stages 1a/1b are pinned **read-only** and resolve the
  non-editing mode `default` (see `mcp-server-creation-workflow.md` §Stage 1). The read-only
  enforcement is probed by `runner/probe_read_only.py`, whose transcript is committed under
  `scratch-repo/probe-read-only-transcript.md`: the read-only stage cannot create a file
  outside `docs/mcp`, the write stages can. Because a read-only stage cannot auto-accept even
  its own artifact write, that write-exception is approximated — `01-instructions.md` /
  `01-signatures.md` are captured from the stage's output. Deliberately not
  `--dangerously-skip-permissions`, which would also unfence Bash (see Sandboxing below).
- **Artifact writes never follow symlinks.** The runner's own writes — the tee'd log
  under `docs/mcp/logs` and the `run-manifest.jsonl` append — go through `safe_open`
  (`runner/run_stage.py`), which refuses a symlink target or a symlinked component under
  `--docs`, refuses when the resolved path escapes the docs root, and opens with
  `O_NOFOLLOW` where the OS supports it. Git stores symlinks as the link itself, so
  without this a third-party clone shipping `docs/mcp/logs` (or `run-manifest.jsonl`) as
  a symlink would turn the runner into an arbitrary-file write/append primitive; the
  same containment is enforced by `new-project.sh` before it writes `run-config.env` /
  `00-decisions.md`.
- **Permissions are runner-owned, per stage.** `build_command` used to emit one identical
  argv for every stage and silently honor the target repo's own `.claude/settings.json` —
  so a third-party clone could widen the permission set. That is gone: every stage gets
  `--settings` pointing at a settings file **shipped under this checkout**
  (`runner/settings/claude-settings.json`, see `models.yaml`). Claude Code merges
  permission rules across settings scopes, so the shipped file pairs the WebFetch
  allowlist Stage 3 needs with hard **deny** rules — `Bash(*)` and `mcp__*` — and a deny
  from any scope beats an allow from a lower one: the target repo's
  `.claude/settings.json` can add allow rules, but it can never override the deny to
  widen the permission set. Stage-specific tool allow/deny lists come from the stage
  contract (only Stage 3 allows WebFetch; every other stage declines it with
  `--disallowedTools WebFetch(*)`), and the claude runner pairs `--strict-mcp-config`
  with an explicit empty `--mcp-config {}` so the session loads no MCP servers at all — a
  target repo's `.mcp.json` is ignored. Override the posture by editing `models.yaml`
  (the runner args, `settings_file`, or `isolate_config`), never by editing the target
  repo. The shipped default denies `Bash(*)` — and a deny cannot be overridden by any
  `--allowedTools` — so if your stage personas run build/test commands (e.g. the 5b/6
  implement personas), add a per-runner variant whose `settings_file` allows the specific
  commands you accept. If your `claude` build does not let `--settings` outrank the
  project file, set `isolate_config: true` on the claude runner to run each stage against
  a fresh empty `CLAUDE_CONFIG_DIR`. The deny-beats-allow claim is pinned by a unit probe
  of the emitted surface (`runner/test_run_stage.py`); verify it once against your
  deployed `claude` build (scratch repo whose `.claude/settings.json` allows `Bash(*)`;
  confirm the stage still declines) before relying on it in production.

### Validation status

The runner is exercised end to end, not just dry-run: a stage executes a real model
call, and a failing primary falls through to a different provider **and** a different
runner, with both attempts recorded:

```
  1/2  dead-gateway/some-local-model   rc=1  FAILED
  2/2  anthropic/claude-sonnet-5       rc=0  OK
```

Templates now exist for all 9 stages (see [`templates/`](templates/)), providing
skeleton artifacts the agents fill in. This does **not** substitute for actually running
any of stages 1a, 1b, 4, 5, 6, 8, or 9 — templates are outlines, not validations.

What is **not** yet validated: a full nine-stage run start to finish, and the `pi` and
`opencode` runners against live endpoints (their argument construction is verified, their
model calls are not). Treat those as untested paths.

### Where model quality actually matters

Learned from running this pipeline, not assumed:

- **Stages 2, 3 and 7 produce judgement** — the capability inventory, the MCP surface
  ADR, and the security review. A weaker model degrades these *quietly* rather than
  failing loudly, and every later stage inherits the damage. Spend your best model here.
- **Stages 1b and 8 are extraction and formatting.** Cheap or local models do fine.

## Sandboxing

The runner ships a per-stage permission **contract** — permission mode, and tool
allow/deny lists, enforced through runner-owned `--settings` and per-stage
`--allowedTools`/`--disallowedTools` — but that is a policy for the agent's own tools, not
a sandbox for the code it executes. From Stage 5 onward the agents have write access to the
target repository, and they run whatever build, test and lint commands that project
defines. Treat a pipeline run as executing untrusted code.

The runner-owned settings file fences the permission *rules* (deny beats a merged allow),
but Claude Code itself still reads `.claude/settings.json` and `.claude/settings.local.json`
from the stage's working directory, and a clone can use those to grant things the runner
cannot override — e.g. `permissions.additionalDirectories` (subject to Claude Code's
workspace-trust handling, not to this repo). The hardening above closes the permission-rule
surface; it does not replace the deployment boundary.

Run it in a container, VM, or an agent sandbox with a policy you control, against a clone
rather than your only copy of the repo. The reference run used a fresh clone on a
throwaway path for exactly this reason.

## Requirements

- `claude` (Claude Code) and/or `opencode`
- Python 3.9+ with PyYAML, for the runner
- Optionally, the plugins providing the skills the playbook names — see below. Every
  stage prompt is a complete spec without them; a session that lacks a named skill is
  told to say so and follow the prompt's own structure.

## Installing the skills the playbook names

Four plugins, from three marketplaces. Provenance and licensing are in [NOTICE](NOTICE).

```bash
claude plugin marketplace add bitovi/ai-enablement-prompts
claude plugin install code@bitovi-ai-enablement
claude plugin install implement-workflow@bitovi-ai-enablement

claude plugin marketplace add anthropics/knowledge-work-plugins
claude plugin install engineering@knowledge-work-plugins

claude plugin marketplace add pskoett/pskoett-skills
claude plugin install pskoett-ai-skills@pskoett-skills
```

Note the marketplace ids differ from the repo names — Bitovi's registers as
`bitovi-ai-enablement`, not `ai-enablement-prompts`.

**Install them with the CLI, not the desktop app.** A plugin installed into Claude
Cowork or an app session lives under `~/.claude/remote/plugins/` and is invisible to
`claude -p` — which is exactly what the runner spawns. A stage whose skill is missing
does not fail: the model quietly follows the prompt's own structure and produces
well-formed output, so the gap hides for an entire run. Verify against the CLI rather
than trusting a skill list you saw in the app:

```bash
echo 'Is engineering:architecture a registered skill available to you right now? Answer YES or NO only.' | claude -p
```

## Prior art

[`earendil-works/pi`](https://github.com/earendil-works/pi) is an agent harness with a
unified multi-provider LLM API. Three of its choices influenced this repo: being explicit
that there is no built-in permission system and that isolation belongs to the deployment
(the Sandboxing section above), treating provider selection as an abstraction rather than
a hardcoded vendor, and pinning what you can so a run is reproducible — which is what the
run manifest is for here. It has no subagent or delegation mechanism, so the orchestration
model in [ORCHESTRATION.md](ORCHESTRATION.md) is not derived from it.

## Layout

```
mcp-server-creation-workflow.md   the playbook — the actual pipeline
ORCHESTRATION.md                  execution models, and the anti-leakage rule
new-project.sh                    scaffolds a run (Stage 0)
models.example.yaml               model routing; copy to models.yaml
runner/run_stage.py               resolves stage -> provider -> CLI, with fallback
runner/stage_contract.py          per-stage artifact shapes, gates, permission surface
runner/settings/claude-settings.json  runner-owned permission posture for claude stages
agents/                           vendored agent personas (see NOTICE)
templates/                        artifact skeletons for the structured stages
```

## Contributing

The playbook is the product. If a stage produced output the next stage could not
use, that is a bug in the prompt — open an issue or a PR with the exact edit. Stage 9
exists to capture exactly this, so retro output is welcome as a PR.

## License

MIT — see [LICENSE](LICENSE). Vendored agent personas are MIT from
[agency-agents](https://github.com/msitarzewski/agency-agents); see [NOTICE](NOTICE).
