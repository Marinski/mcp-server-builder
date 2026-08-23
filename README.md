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

There is deliberately **no orchestrator that runs all nine stages in one process** —
that would reintroduce the shared context the design exists to avoid.

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

| | `claude` | `opencode` |
|---|---|---|
| API shape | Anthropic (`/v1/messages`) | **OpenAI-compatible** + many providers |
| skills / personas | native | via skill autodiscovery |
| fallback chain | native, same provider only | handled by this repo's runner |

Anything OpenAI-compatible works through `opencode` — LiteLLM, vLLM, Ollama,
OpenRouter, a vendor API. A gateway that speaks both shapes can serve either runner.

Endpoints and keys are read from **environment variables named in the config**, never
stored in it, so `models.yaml` never contains a URL or secret.

Inspect what a stage would run, without calling anything:

```bash
./runner/run_stage.py 2 --dry-run
```

### Where model quality actually matters

Learned from running this pipeline, not assumed:

- **Stages 2, 3 and 7 produce judgement** — the capability inventory, the MCP surface
  ADR, and the security review. A weaker model degrades these *quietly* rather than
  failing loudly, and every later stage inherits the damage. Spend your best model here.
- **Stages 1b and 8 are extraction and formatting.** Cheap or local models do fine.

## Requirements

- `claude` (Claude Code) and/or `opencode`
- Python 3.9+ with PyYAML, for the runner
- The plugins providing the skills the playbook invokes — see [NOTICE](NOTICE)

## Layout

```
mcp-server-creation-workflow.md   the playbook — the actual pipeline
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
