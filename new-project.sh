#!/usr/bin/env bash
# Scaffold a run of the MCP server creation pipeline.
#
# Stage 0 of the playbook is "do this once, by hand" — but hand-substituting seven
# variables across nine prompts is where the run goes wrong. This writes the run
# config once, creates the artifact skeletons the stages expect, and prints the
# variable block to paste into each fresh session.
#
# Everything after this is the playbook. There is no runner: each stage is a fresh
# agent session by design (see playbook rule 1), so automating the sequence would
# defeat the thing that makes it work.
set -euo pipefail

PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'USAGE'
usage: new-project.sh --repo PATH --project NAME --server NAME [options]

required:
  --repo PATH        absolute path to the source project
  --project NAME     human name of the source project
  --server NAME      MCP server package name

optional:
  --lang LANG        default: TypeScript (@modelcontextprotocol/sdk)
  --transport T      stdio | streamable-http | both     default: stdio
  --mode MODE        wrap | embed                       default: wrap
  --docs DIR         default: <repo>/docs/mcp
USAGE
}

REPO=""; PROJECT=""; SERVER=""
LANG_SDK="TypeScript (@modelcontextprotocol/sdk)"
TRANSPORT="stdio"; MODE="wrap"; DOCS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)      REPO="$2"; shift 2 ;;
        --project)   PROJECT="$2"; shift 2 ;;
        --server)    SERVER="$2"; shift 2 ;;
        --lang)      LANG_SDK="$2"; shift 2 ;;
        --transport) TRANSPORT="$2"; shift 2 ;;
        --mode)      MODE="$2"; shift 2 ;;
        --docs)      DOCS="$2"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ -n "$REPO" && -n "$PROJECT" && -n "$SERVER" ]] || { usage >&2; exit 2; }
[[ -d "$REPO" ]] || { echo "repo not found: $REPO" >&2; exit 1; }
[[ "$MODE" == "wrap" || "$MODE" == "embed" ]] || { echo "mode must be wrap or embed" >&2; exit 2; }
case "$TRANSPORT" in stdio|streamable-http|both) ;; *)
    echo "transport must be stdio, streamable-http, or both" >&2; exit 2 ;;
esac

DOCS="${DOCS:-$REPO/docs/mcp}"
mkdir -p "$DOCS"

if [[ "$MODE" == "wrap" ]]; then
    CONSEQUENCE="the HTTP/CLI surface"
else
    CONSEQUENCE="the internal module surface"
fi

# The run config. Stages read this instead of you retyping variables.
cat > "$DOCS/run-config.env" <<EOF
REPO=$REPO
PROJECT=$PROJECT
SERVER=$SERVER
LANG=$LANG_SDK
TRANSPORT=$TRANSPORT
MODE=$MODE
DOCS=$DOCS
EOF

# 00-decisions.md is the one artifact Stage 0 owns; the rest are created by their
# stage. Never clobber a decision already written.
if [[ -f "$DOCS/00-decisions.md" ]]; then
    echo "note: $DOCS/00-decisions.md exists, left untouched"
else
    cat > "$DOCS/00-decisions.md" <<EOF
# Decisions — $PROJECT MCP server

MODE: $MODE
Rationale: <2-3 sentences — why $MODE and not the alternative>
Consequence: Stage 2 inventories $CONSEQUENCE

## Later decisions (ADR-lite)

<append as the run makes calls worth remembering>
EOF
fi

echo "scaffolded $DOCS"
echo
echo "Paste this variable block into each fresh stage session:"
echo
sed 's/^/  /' "$DOCS/run-config.env"
echo
echo "Next: fill in the Rationale in $DOCS/00-decisions.md (playbook § 0.2),"
echo "then run Stage 1a from $PIPELINE_DIR/mcp-server-creation-workflow.md"
