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
| credential | env var | env var | **`--api-key` flag** |
| skills / personas | native | via skill autodiscovery | its own extension system |
| fallback chain | native, same provider only | handled by this repo's runner | handled by this repo's runner |

Anything OpenAI-compatible works through `opencode` or `pi` — LiteLLM, vLLM, Ollama,
OpenRouter, a vendor API.

[`pi`](https://github.com/earendil-works/pi) is the cleanest fit of the three: it takes
provider, model and key as separate flags rather than requiring env-var juggling, and
`--mode json` is genuinely non-interactive. Install it either way:

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

### Where model quality actually matters

Learned from running this pipeline, not assumed:

- **Stages 2, 3 and 7 produce judgement** — the capability inventory, the MCP surface
  ADR, and the security review. A weaker model degrades these *quietly* rather than
  failing loudly, and every later stage inherits the damage. Spend your best model here.
- **Stages 1b and 8 are extraction and formatting.** Cheap or local models do fine.

## Sandboxing

This repo ships **no permission system**. From Stage 5 onward the agents have write access
to the target repository, and they run whatever build, test and lint commands that project
defines. Treat a pipeline run as executing untrusted code.

Run it in a container, VM, or an agent sandbox with a policy you control, against a clone
rather than your only copy of the repo. The reference run used a fresh clone on a
throwaway path for exactly this reason.

## Requirements

- `claude` (Claude Code) and/or `opencode`
- Python 3.9+ with PyYAML, for the runner
- The plugins providing the skills the playbook invokes — see [NOTICE](NOTICE)

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
