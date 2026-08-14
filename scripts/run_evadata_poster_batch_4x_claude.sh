#!/usr/bin/env bash
set -euo pipefail

# Run the same EvaData 4x academic-poster batch as run_evadata_poster_batch_4x.sh,
# but force the external designer to Claude Code through AutoDesign's
# isolated Claude harness. The harness auth dir currently pins the company
# Anthropic gateway defaults to yuju-claude-opus-4.8-evaDaily.
#
# Usage:
#   scripts/run_evadata_poster_batch_4x_claude.sh
#   scripts/run_evadata_poster_batch_4x_claude.sh resume out/eva_poster_batches/<batch>
#   scripts/run_evadata_poster_batch_4x_claude.sh stop   out/eva_poster_batches/<batch>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"

export DESIGNER_AUTHOR_HARNESS=claude
export BATCH_SCRIPT_LABEL="scripts/run_evadata_poster_batch_4x_claude.sh"

if [[ -n "${CLAUDE_BIN:-}" ]]; then
  export AUTODESIGN_DESIGNER_AUTHOR_CLAUDE_BIN="$CLAUDE_BIN"
fi

# This script is deliberately Claude-only. Ignore a caller's stale Codex
# PLANNER_CMD so the base runner resolves the Claude harness command itself.
unset PLANNER_CMD

if [[ -z "${BATCH_DIR:-}" && -z "${1:-}" ]]; then
  export BATCH_DIR="$REPO/out/eva_poster_batches/$(date +%Y%m%d-%H%M%S)-evadata-all-cvpr-4x-claude-opus48"
fi

exec "$SCRIPT_DIR/run_evadata_poster_batch_4x.sh" "$@"
