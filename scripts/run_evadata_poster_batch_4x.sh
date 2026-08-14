#!/usr/bin/env bash
set -euo pipefail

# Run EvaData academic-poster generation with bounded concurrency.
#
# Usage:
#   scripts/run_evadata_poster_batch_4x.sh
#   scripts/run_evadata_poster_batch_4x.sh resume out/eva_poster_batches/<batch>
#   scripts/run_evadata_poster_batch_4x.sh stop   out/eva_poster_batches/<batch>
#
# Useful env knobs:
#   CONCURRENCY=4          number of simultaneous poster runs
#   LIMIT=0                run only the first N discovered papers; 0 means all
#   RETRY_FAILED=1         rerun failed tasks on resume; set 0 to skip failures
#   FORCE=0                set 1 to ignore .done markers and rerun everything
#   EVADATA_DIR=...        source corpus, defaults to AutoDesign/eval/EvaData
#   BATCH_DIR=...          output batch directory
#   DESIGNER_AUTHOR_HARNESS=codex  external designer harness passed to AutoDesign
#   CODEX_BIN=...          absolute Codex binary path; auto-detected by default
#   PLANNER_CMD=...        full external designer command; overrides CODEX_BIN
#   SKIP_PROMPT_ENHANCER=1 skip the pre-designer prompt enhancer; set 0 to enable

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"
AUTODESIGN_REPO="${AUTODESIGN_REPO:-${DESIGN_ANYTHING_REPO:-$REPO}}"
EVADATA_DIR="${EVADATA_DIR:-$AUTODESIGN_REPO/eval/EvaData}"
CONCURRENCY="${CONCURRENCY:-4}"
LIMIT="${LIMIT:-0}"
RETRY_FAILED="${RETRY_FAILED:-1}"
FORCE="${FORCE:-0}"
UV_CACHE_DIR="${UV_CACHE_DIR:-.uv-cache}"
DESIGNER_AUTHOR_HARNESS="${DESIGNER_AUTHOR_HARNESS:-codex}"
case "$DESIGNER_AUTHOR_HARNESS" in
  claude-code|cloud-code) DESIGNER_AUTHOR_HARNESS="claude" ;;
esac
CODEX_BIN="${CODEX_BIN:-}"
PLANNER_CMD="${PLANNER_CMD:-}"
BATCH_SCRIPT_LABEL="${BATCH_SCRIPT_LABEL:-scripts/run_evadata_poster_batch_4x.sh}"
TEMPLATE="${TEMPLATE:-cvpr-landscape}"
DESIGNER_AUTHOR_TIMEOUT="${DESIGNER_AUTHOR_TIMEOUT:-3600}"
DESIGNER_AUTHOR_MAX_ATTEMPTS="${DESIGNER_AUTHOR_MAX_ATTEMPTS:-12}"

usage() {
  sed -n '1,27p' "$0" >&2
}

MODE="run"
if [[ "${1:-}" == "resume" || "${1:-}" == "stop" ]]; then
  MODE="$1"
  shift
fi

if [[ -n "${1:-}" ]]; then
  BATCH_DIR="$1"
else
  BATCH_DIR="${BATCH_DIR:-$REPO/out/eva_poster_batches/$(date +%Y%m%d-%H%M%S)-evadata-all-cvpr-4x}"
fi

case "$MODE" in
  run|resume|stop) ;;
  *)
    usage
    exit 2
    ;;
esac

cd "$REPO"

if [[ -x "$REPO/.venv/bin/python3" ]]; then
  BATCH_PYTHON="$REPO/.venv/bin/python3"
else
  BATCH_PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$BATCH_PYTHON" ]]; then
  echo "Python interpreter not found. Install the repository environment with uv sync." >&2
  exit 2
fi

resolve_planner_cmd() {
  if [[ -n "$PLANNER_CMD" ]]; then
    return
  fi
  if [[ "$DESIGNER_AUTHOR_HARNESS" == "claude" ]]; then
    PLANNER_CMD="$(uv --cache-dir "$UV_CACHE_DIR" run python - <<'PY'
from autodesign.config import designer_author_command_for_harness

print(designer_author_command_for_harness("claude", None))
PY
)"
    return
  fi
  if [[ "$DESIGNER_AUTHOR_HARNESS" != "codex" ]]; then
    echo "Set PLANNER_CMD for DESIGNER_AUTHOR_HARNESS=$DESIGNER_AUTHOR_HARNESS; this script only auto-builds codex and claude commands." >&2
    exit 2
  fi
  if [[ -z "$CODEX_BIN" ]]; then
    CODEX_BIN="$(command -v codex || true)"
  fi
  if [[ -z "$CODEX_BIN" && -x "/Applications/ChatGPT.app/Contents/Resources/codex" ]]; then
    CODEX_BIN="/Applications/ChatGPT.app/Contents/Resources/codex"
  fi
  if [[ -z "$CODEX_BIN" && -x "/Applications/Codex.app/Contents/Resources/codex" ]]; then
    CODEX_BIN="/Applications/Codex.app/Contents/Resources/codex"
  fi
  if [[ -z "$CODEX_BIN" || ! -x "$CODEX_BIN" ]]; then
    echo "Codex binary not found. Set CODEX_BIN=/absolute/path/to/codex or PLANNER_CMD=..." >&2
    exit 2
  fi
  PLANNER_CMD="$CODEX_BIN --search exec --dangerously-bypass-approvals-and-sandbox -"
}

resolve_planner_cmd

if [[ "$MODE" == "stop" ]]; then
  mkdir -p "$BATCH_DIR"
  touch "$BATCH_DIR/STOP"
  echo "Stop requested: $BATCH_DIR/STOP"
  exit 0
fi

mkdir -p "$BATCH_DIR"/{commands,logs,status,links,tmp}
STOP_FILE="$BATCH_DIR/STOP"
RUNNER_LOCK="$BATCH_DIR/.runner.lock"
TASKS_TSV="$BATCH_DIR/tasks.tsv"
PROMPT_FILE="$BATCH_DIR/prompt.txt"
STATUS_JSONL="$BATCH_DIR/status.jsonl"
ACTIVE_FILE="$REPO/out/eva_poster_batches/ACTIVE_BATCH.txt"

if ! mkdir "$RUNNER_LOCK" 2>/dev/null; then
  echo "Another runner appears active for $BATCH_DIR"
  echo "If this is stale, remove: $RUNNER_LOCK"
  exit 1
fi

stop_children() {
  touch "$STOP_FILE"
  local pids
  pids="$(jobs -pr || true)"
  if [[ -n "$pids" ]]; then
    echo "Stopping child jobs: $pids" >&2
    kill $pids 2>/dev/null || true
  fi
}

cleanup() {
  rmdir "$RUNNER_LOCK" 2>/dev/null || true
}

trap 'echo "Interrupted; writing STOP and terminating running jobs." >&2; stop_children; cleanup; exit 130' INT TERM
trap cleanup EXIT

# Re-running the script is the resume path, so clear an old STOP marker.
rm -f "$STOP_FILE"
rm -f "$BATCH_DIR/status/"*.running 2>/dev/null || true
if [[ "$MODE" == "resume" && "$RETRY_FAILED" == "1" && "$FORCE" != "1" ]]; then
  failed_history_dir="$BATCH_DIR/tmp/failed_history/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$failed_history_dir"
  for failed_status in "$BATCH_DIR"/status/*.failed; do
    [[ -e "$failed_status" ]] || continue
    mv "$failed_status" "$failed_history_dir/$(basename "$failed_status")"
  done
fi
mkdir -p "$(dirname "$ACTIVE_FILE")"
printf '%s\n' "$BATCH_DIR" > "$ACTIVE_FILE"

cat > "$PROMPT_FILE" <<'PROMPT'
Generate a polished academic paper poster from the attached paper. The visible artifact must be the poster itself: a 3072 x 1536 px / 2:1 landscape academic conference poster, not a landing page, dashboard, marketing graphic, or slide collage. Use browser-native authored HTML/CSS, CSS grid/flex/normal document flow, and native editable text.

Header requirements:
- Use a full-width top header band.
- The header is limited to exactly these three visible paper-identity lines: paper title, author list, and school/institution/company names.
- Place those fields as three compact centered text rows only: title line, authors line, school/institution/company line.
- The school/institution/company line should contain only organization names grounded in the paper, rendered as plain text only. Do not invent missing organizations.
- Do not add a fourth header/meta/subtitle row or side identity rail. Do not put any other visible content in the header: no logos, image badges, icons, QR codes, venue/year text, conference names, arXiv/archive labels, citation/contact text, project/code/resource links, topic badges, method slogans, contribution bullets, benchmark claims, source figures/tables, or explanatory captions. If any of those fields are available, leave them out of the header; users can add them after export.

Poster structure and density:
- Use three balanced vertical columns below the header.
- Use a compact editorial hierarchy: usually 7-10 numbered sections across the three columns, with 2-4 sections per column.
- Prefer section titles such as Motivation, Method, Data/Benchmark, Results, Ablation/Analysis, Limitations, and Takeaways. Keep titles literal and academic; do not use marketing headlines.
- Make the poster dense like a real conference poster, but avoid the AI-dashboard look: no repeated large cards, no grid of generic metric tiles, no decorative badge rows, and no nested card-in-card panels.
- Do not add a horizontal metric/KPI/result band: a row of standalone big-number-plus-label tiles that highlights headline numbers (e.g. Inception Score, FID). State those numbers inside the running results prose or a distilled native table, never as a separate summary strip that duplicates figures already given elsewhere.
- Build visual evidence units in the DOM shape expected by validation. Each placed source figure/table/image must live in its own direct child of the section panel root: `<section class="source-flow-unit figure-flow-unit" data-source-id="ingest_fig_01" data-layer-id="ingest_fig_01"> ... </section>`.
- Inside that source-flow unit, keep the source crop and its explanation as direct siblings. Use a bound `<figure class="flow-asset source-asset float-right" data-source-id="..." data-layer-id="...">` with an `<img src="{{layer:...}}">`, plus direct sibling `p`, `ul`, `table`, or `div` readout content in the same source-flow unit.
- The `.source-flow-unit` / `.figure-flow-unit` element itself must be normal flow: `display: flow-root` or `display: block`, not grid or flex. Do not wrap source crops and readouts in `.media-row`, `.visual-shell`, `.source-grid`, `.evidence-grid`, or other grid/flex split wrappers. If side wrapping is useful, float the `.flow-asset` left or right with `shape-outside`; otherwise make the asset stacked/full-width with the readout directly below inside the same source-flow unit.
- Build visual evidence units deliberately. A paper figure/table crop may be stacked/full-width, or floated with a wrapped readout only when the local readout meaningfully fills the side flow. If the side flow would be empty, stack the crop and readout instead.
- Floated source-flow lists must reserve a marker gutter. If a direct sibling `ul`/`ol` wraps beside a floated source asset, style it with `display: flow-root`, `padding-inline-start: 1.25em` or more, `list-style-position: outside`, and small `li` padding; do not use `padding: 0`, negative text indents, or absolutely positioned pseudo bullets. If the side lane is too narrow for the marker and readable wrapped text, use a stacked/full-width source-flow instead.
- Every section should earn its space with paper-specific content. If a section has obvious blank space, first add concise paper facts, benchmark context, mechanism notes, ablation caveats, compact comparison table rows, source-grounded bullets, or concise visual interpretation. If content still does not fit, shorten prose, rebalance section heights, split a dense section, or remove low-value text.
- Do not enlarge figures, tables, cards, or body text just to fill space. Do not shrink text below readable poster scale to force in extra content.

Evidence and content requirements:
- Use real figures, tables, charts, and diagrams from the paper whenever they support the story.
- Paper source figures and paper source tables must appear as original PDF crops when used as source evidence. Do not replace a source paper table with a native reconstruction, selected-column subset, or re-rendered table image.
- Every source figure/table crop should appear near a concise local explanation of what it proves.
- Native authored tables are only for distilled poster summaries: compact benchmark readouts, method taxonomies, training-stage summaries, ablation summaries, or limitation evidence. They must summarize rather than duplicate a full paper source table, and must not degenerate into a horizontal strip of big-number metric tiles.
- Do not show the same paper table as both an original source crop and a full native reconstruction. If a source crop is placed, any nearby native table must be clearly smaller and more distilled.
- All claims, numbers, benchmarks, dataset details, method descriptions, limitations, and takeaways must be grounded in the attached paper.
- If a number or claim cannot be verified from the paper, omit it or rewrite it qualitatively.
- Results sections should include more than headline numbers. Add benchmark context: competing baselines, task families, secondary metrics, and the claim those results support.
- Training, method, and analysis sections should use paper-specific details: stages, datasets, losses, sequence lengths, modules, ablation results, assumptions, or future-work claims where available.
- Prefer concrete paper terms over generic filler. Avoid empty phrases such as "core thesis", "grounded poster interpretation", "forward path", "evidence target", "benchmark signal", "what it supports", or "key insight" as visible labels unless those exact terms are natural to the paper.

Visual and typography requirements:
- Use a formal academic poster visual system with a strong title header, clear section title bands, compact body text, thin dividers, native tables, charts, and disciplined spacing.
- Use an academic serif typography system such as Times New Roman / Times / Georgia for the poster text. Body, local readout, table-prose, caption, ordinary bullet, and table-cell parent text should stay at font-weight 400.
- Use short inline `<strong class="lead-key">...</strong>` emphasis as sentence-start lead phrases in motivation, method, results, analysis, limitations, takeaways, and source readouts, for example `<strong class="lead-key">Training signal:</strong> ...` or `<strong class="lead-key">Evidence:</strong> ...`. Keep each lead phrase 2-5 words when possible; one-word academic labels such as `Problem:` or `Risk:` are fine when natural.
- Avoid large blocks of bold body copy, bold whole bullets/readouts/table rows, scattered bold keywords, all-caps labels, negative letter spacing, and oversized display type inside dense sections. Use italics sparingly or not at all.
- Prefer concise bullets for explanatory text when they improve scanability.
- Prefer real paper visuals over decorative imagery. Do not use generated or stock imagery as scientific evidence.
- Preserve readable source figures and source table crops. Avoid oversized white wrappers around images. Crop only obvious external white margins; never crop into axes, legends, labels, captions, diagrams, table rules, or meaningful visual content.
- Use restrained academic color: neutral paper background, high-contrast dark ink, one disciplined muted accent, and at most one subtle secondary accent. Use light section bands and thin rules rather than saturated fills.
- Avoid one-note blue/purple dashboard palettes, decorative gradients, orbs, stock-like backgrounds, heavy drop shadows, and repeated pill/badge decoration.

Native academic table requirements:
- Poster-native summary/readout tables should use booktabs-style academic treatment: no full boxed grid, no heavy outer frame, no vertical rules by default, no double rules, and no saturated dark header bars.
- Use a top rule, header/mid rule, optional light row separators, and a bottom rule. Headers should usually be white or lightly tinted with dark text.
- Left-align every `th` and `td`, including numeric values. Do not right-align or decimal-align numeric columns in ordinary poster-native tables.
- Use all-center alignment only for short pure symbol/numeric matrices with no prose cells, no method/dataset row labels, no sentence fragments, and uniformly short values.
- Keep native tables poster-readable: usually 3-7 body rows and 3-6 columns. Move paragraph explanations into a nearby readout instead of stuffing long prose into cells.
- Use only one restrained emphasis mechanism per table: one focus row, one key column, or a small number of best cells. Avoid zebra striping unless the table is unusually long or wide.

Do not:
- Do not create a web landing page, dashboard, marketing graphic, promotional poster, or grid of large repeated cards.
- Do not show process labels such as "source-backed", "authored HTML", "paper poster", "no generated evidence imagery", "ingested", "retrieved", "grounded", "evidence pack", or "pipeline".
- Do not display source ids, layout notes, planning notes, or internal instructions.
- Do not bake final text into images; keep text native and editable.
- Do not invent logos, institutions, venues, links, benchmark numbers, dataset details, or paper limitations.

After rendering, visually inspect the full poster and iterate locally until it looks intentionally composed: no clipped figures, overlapped text, unreadable tables, padded-looking sections, oversized wrappers, decorative filler, repeated generic cards, or obvious blank panel bottoms.
PROMPT

sanitize_id() {
  printf '%s' "$1" \
    | tr '[:upper:]_' '[:lower:]-' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g' \
    | cut -c 1-180
}

init_tasks() {
  if [[ -s "$TASKS_TSV" ]]; then
    return
  fi
  if [[ ! -d "$EVADATA_DIR" ]]; then
    echo "EvaData directory not found: $EVADATA_DIR" >&2
    exit 1
  fi
  local tmp="$TASKS_TSV.tmp"
  : > "$tmp"
  local idx=0
  while IFS= read -r paper_pdf; do
    idx=$((idx + 1))
    if [[ "$LIMIT" != "0" && "$idx" -gt "$LIMIT" ]]; then
      break
    fi
    local rel category paper_slug task_id
    rel="${paper_pdf#$EVADATA_DIR/}"
    category="${rel%%/*}"
    paper_slug="${rel#*/}"
    paper_slug="${paper_slug%/paper.pdf}"
    task_id="$(printf '%03d-%s-%s' "$idx" "$(sanitize_id "$category")" "$(sanitize_id "$paper_slug")")"
    printf '%s\t%s\t%s\t%s\t%s\n' "$idx" "$task_id" "$category" "$paper_slug" "$paper_pdf" >> "$tmp"
  done < <(find "$EVADATA_DIR" -name paper.pdf -type f | sort)
  mv "$tmp" "$TASKS_TSV"
}

load_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0

  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ "$line" == export[[:space:]]* ]]; then
      line="${line#export}"
      line="${line#"${line%%[![:space:]]*}"}"
    fi
    [[ "$line" == *"="* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      if [[ "${#value}" -ge 2 ]]; then
        if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
          value="${value:1:${#value}-2}"
        elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
          value="${value:1:${#value}-2}"
        fi
      fi
      export "$key=$value"
    fi
  done < "$env_file"
}

configure_env() {
  load_env_file "$AUTODESIGN_REPO/.env"
  load_env_file "$REPO/.env"
  export SKIP_PROMPT_ENHANCER="${SKIP_PROMPT_ENHANCER:-1}"
  export NO_CLAIM_GRAPH=0
  export AUTODESIGN_DESIGNER_AUTHOR=external
  export AUTODESIGN_DESIGNER_AUTHOR_HARNESS="$DESIGNER_AUTHOR_HARNESS"
  export AUTODESIGN_DESIGNER_AUTHOR_CMD="$PLANNER_CMD"
  export AUTODESIGN_DESIGNER_AUTHOR_MAX_ATTEMPTS="$DESIGNER_AUTHOR_MAX_ATTEMPTS"
  export AUTODESIGN_DESIGNER_AUTHOR_TIMEOUT_SECONDS="$DESIGNER_AUTHOR_TIMEOUT"
  export AUTODESIGN_DESIGNER_AUTHOR_POSTER_STABLE_SECONDS="${AUTODESIGN_DESIGNER_AUTHOR_POSTER_STABLE_SECONDS:-${DESIGN_ANYTHING_DESIGNER_AUTHOR_POSTER_STABLE_SECONDS:-8}}"
  export AUTODESIGN_POSTER_HARNESS_MODE=dogfood
  export POSTER_CANVAS_TEMPLATE="$TEMPLATE"
  export PYTHONUNBUFFERED=1
  unset AUTODESIGN_DESIGNER_AUTHOR_MODEL
  unset DESIGN_ANYTHING_DESIGNER_AUTHOR_MODEL

}

shell_quote() {
  printf '%q' "$1"
}

command_line_for() {
  local paper_pdf="$1"
  printf 'uv --cache-dir %q run python -m autodesign.cli run --from-file %q --template %q --designer-author external --designer-author-harness %q --designer-author-cmd %q --designer-author-timeout %q --designer-author-max-attempts %q' \
    "$UV_CACHE_DIR" "$paper_pdf" "$TEMPLATE" "$DESIGNER_AUTHOR_HARNESS" "$PLANNER_CMD" "$DESIGNER_AUTHOR_TIMEOUT" "$DESIGNER_AUTHOR_MAX_ATTEMPTS"
  printf ' %q' "$(cat "$PROMPT_FILE")"
}

parse_run_dir() {
  local log_path="$1"
  local run_dir
  run_dir="$(awk '/Run dir:/ {sub(/^.*Run dir:[[:space:]]*/, ""); print}' "$log_path" | tail -1)"
  if [[ -z "$run_dir" ]]; then
    run_dir="$(grep -Eo '/[^[:space:]"'"'"']+/out/runs/[^[:space:]"'"'"']+' "$log_path" | tail -1 || true)"
  fi
  printf '%s' "$run_dir"
}

terminal_status_for() {
  local run_dir="$1"
  local summary="$run_dir/run_telemetry_summary.json"
  if [[ -f "$summary" ]]; then
    grep -Eo '"terminal_status"[[:space:]]*:[[:space:]]*"[^"]*"' "$summary" \
      | tail -1 \
      | sed -E 's/^.*"terminal_status"[[:space:]]*:[[:space:]]*"([^"]*)".*$/\1/' || true
  fi
}

write_status_json() {
  local path="$1"
  shift
  "$BATCH_PYTHON" - "$path" "$@" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
pairs = sys.argv[2:]
data = {}
for item in pairs:
    key, value = item.split("=", 1)
    if value.isdigit():
        data[key] = int(value)
    else:
        data[key] = value
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(path)
PY
}

append_jsonl() {
  local path="$1"
  shift
  "$BATCH_PYTHON" - "$path" "$@" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

path = Path(sys.argv[1])
data = {"ts": datetime.now().isoformat(timespec="seconds")}
for item in sys.argv[2:]:
    key, value = item.split("=", 1)
    data[key] = value
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(data, ensure_ascii=False) + "\n")
PY
}

is_done() {
  local task_id="$1"
  [[ "$FORCE" != "1" && -f "$BATCH_DIR/status/$task_id.done" ]]
}

should_skip_failed() {
  local task_id="$1"
  [[ "$FORCE" != "1" && "$RETRY_FAILED" != "1" && -f "$BATCH_DIR/status/$task_id.failed" ]]
}

link_artifacts() {
  local task_id="$1"
  local run_dir="$2"
  local final_dir="$run_dir/final"
  mkdir -p "$BATCH_DIR/links"
  ln -sfn "$run_dir" "$BATCH_DIR/links/$task_id"
  [[ -f "$final_dir/poster.html" ]] && ln -sfn "$final_dir/poster.html" "$BATCH_DIR/links/$task_id-poster.html"
  [[ -f "$final_dir/preview.png" ]] && ln -sfn "$final_dir/preview.png" "$BATCH_DIR/links/$task_id-preview.png"
  [[ -f "$final_dir/designer_author_best_candidate_fallback.json" ]] && \
    ln -sfn "$final_dir/designer_author_best_candidate_fallback.json" "$BATCH_DIR/links/$task_id-fallback.json"
  [[ -f "$final_dir/designer_author_best_available_artifact_fallback.json" ]] && {
    ln -sfn "$final_dir/designer_author_best_available_artifact_fallback.json" "$BATCH_DIR/links/$task_id-fallback.json"
    ln -sfn "$final_dir/designer_author_best_available_artifact_fallback.json" "$BATCH_DIR/links/$task_id-best-available-fallback.json"
  }
  return 0
}

latest_candidate_dir() {
  local run_dir="$1"
  "$BATCH_PYTHON" - "$run_dir" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1]) / "html_first" / "candidates"
if not root.is_dir():
    raise SystemExit(0)

def key(path: Path) -> tuple[int, str]:
    match = re.search(r"candidate_(\d+)$", path.name)
    return (int(match.group(1)) if match else -1, path.name)

candidates = [
    path for path in root.iterdir()
    if path.is_dir() and (path / "preview.png").is_file()
]
if candidates:
    print(max(candidates, key=key))
PY
}

link_failed_candidate_artifacts() {
  local task_id="$1"
  local run_dir="$2"
  local candidate_dir candidate_id
  [[ -n "$run_dir" && -d "$run_dir" ]] || return 0
  mkdir -p "$BATCH_DIR/links"
  ln -sfn "$run_dir" "$BATCH_DIR/links/$task_id"
  candidate_dir="$(latest_candidate_dir "$run_dir")"
  [[ -n "$candidate_dir" && -d "$candidate_dir" ]] || return 0
  candidate_id="$(basename "$candidate_dir")"
  [[ -f "$candidate_dir/preview.png" ]] && ln -sfn "$candidate_dir/preview.png" "$BATCH_DIR/links/$task_id-failed-$candidate_id-preview.png"
  [[ -f "$candidate_dir/body.html" ]] && ln -sfn "$candidate_dir/body.html" "$BATCH_DIR/links/$task_id-failed-$candidate_id-body.html"
  [[ -f "$candidate_dir/measure.html" ]] && ln -sfn "$candidate_dir/measure.html" "$BATCH_DIR/links/$task_id-failed-$candidate_id-measure.html"
  [[ -f "$candidate_dir/measurement.json" ]] && ln -sfn "$candidate_dir/measurement.json" "$BATCH_DIR/links/$task_id-failed-$candidate_id-measurement.json"
  [[ -f "$candidate_dir/validation_overlay.png" ]] && ln -sfn "$candidate_dir/validation_overlay.png" "$BATCH_DIR/links/$task_id-failed-$candidate_id-validation-overlay.png"
  ln -sfn "$candidate_dir" "$BATCH_DIR/links/$task_id-failed-$candidate_id"
  ln -sfn "$candidate_dir/preview.png" "$BATCH_DIR/links/$task_id-failed-candidate-preview.png"
  printf '%s' "$BATCH_DIR/links/$task_id-failed-candidate-preview.png"
  return 0
}

status_terminal_status() {
  local status_path="$1"
  [[ -f "$status_path" ]] || return 0
  "$BATCH_PYTHON" - "$status_path" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
value = str(data.get("terminal_status") or "").strip()
if value:
    print(value)
PY
}

run_one() {
  local idx="$1"
  local task_id="$2"
  local category="$3"
  local paper_slug="$4"
  local paper_pdf="$5"
  local log_path="$BATCH_DIR/logs/$task_id.log"
  local cmd_path="$BATCH_DIR/commands/$task_id.sh"
  local started epoch_started

  if [[ -z "$idx" || -z "$task_id" || -z "$category" || -z "$paper_slug" || -z "$paper_pdf" || ! -f "$paper_pdf" ]]; then
    append_jsonl "$STATUS_JSONL" event=skip_invalid_task task_id="$task_id" category="$category" paper_slug="$paper_slug" paper_pdf="$paper_pdf"
    return 0
  fi

  started="$(date '+%Y-%m-%dT%H:%M:%S')"
  epoch_started="$(date +%s)"
  append_jsonl "$STATUS_JSONL" event=start task_id="$task_id" category="$category" paper_slug="$paper_slug"
  write_status_json "$BATCH_DIR/status/$task_id.running" \
    index="$idx" task_id="$task_id" category="$category" paper_slug="$paper_slug" paper_pdf="$paper_pdf" started_at="$started"

  command_line_for "$paper_pdf" > "$cmd_path"
  chmod +x "$cmd_path"

  {
    echo "# task_id=$task_id"
    echo "# category=$category"
    echo "# paper_slug=$paper_slug"
    echo "# paper_pdf=$paper_pdf"
    echo "# started_at=$started"
    echo "# cwd=$REPO"
    echo "# template=$TEMPLATE"
    echo "# concurrency=$CONCURRENCY"
    if [[ "$SKIP_PROMPT_ENHANCER" == "1" || "$SKIP_PROMPT_ENHANCER" == "true" || "$SKIP_PROMPT_ENHANCER" == "True" || "$SKIP_PROMPT_ENHANCER" == "yes" ]]; then
      echo "# prompt_enhancer=disabled"
    else
      echo "# prompt_enhancer=enabled"
    fi
    echo "# claim_graph=enabled"
    echo "# AUTODESIGN_DESIGNER_AUTHOR_HARNESS=$DESIGNER_AUTHOR_HARNESS"
    echo "# AUTODESIGN_DESIGNER_AUTHOR_CMD=$PLANNER_CMD"
    echo "# AUTODESIGN_DESIGNER_AUTHOR_MODEL=(default)"
    echo
    echo "$ $(cat "$cmd_path")"
    echo
  } > "$log_path"

  set +e
  uv --cache-dir "$UV_CACHE_DIR" run python -m autodesign.cli run \
    --from-file "$paper_pdf" \
    --template "$TEMPLATE" \
    --designer-author external \
    --designer-author-harness "$DESIGNER_AUTHOR_HARNESS" \
    --designer-author-cmd "$PLANNER_CMD" \
    --designer-author-timeout "$DESIGNER_AUTHOR_TIMEOUT" \
    --designer-author-max-attempts "$DESIGNER_AUTHOR_MAX_ATTEMPTS" \
    "$(cat "$PROMPT_FILE")" >> "$log_path" 2>&1
  local rc=$?
  set -e

  local finished epoch_finished duration run_dir terminal_status state final_dir
  finished="$(date '+%Y-%m-%dT%H:%M:%S')"
  epoch_finished="$(date +%s)"
  duration=$((epoch_finished - epoch_started))
  run_dir="$(parse_run_dir "$log_path")"
  terminal_status=""
  final_dir=""
  if [[ -n "$run_dir" ]]; then
    terminal_status="$(terminal_status_for "$run_dir")"
    final_dir="$run_dir/final"
  fi

  # Lifecycle success means the requested artifact was delivered.  The CLI
  # exit code still records whether the quality loop reached a clean pass.
  if [[ -n "$run_dir" && -f "$final_dir/poster.html" && -f "$final_dir/preview.png" ]]; then
    state="succeeded"
    link_artifacts "$task_id" "$run_dir"
    rm -f "$BATCH_DIR/status/$task_id.failed" "$BATCH_DIR/status/$task_id.running"
    write_status_json "$BATCH_DIR/status/$task_id.done" \
      index="$idx" task_id="$task_id" category="$category" paper_slug="$paper_slug" paper_pdf="$paper_pdf" \
      state="$state" exit_code="$rc" process_exit_code="$rc" terminal_status="$terminal_status" started_at="$started" finished_at="$finished" \
      duration_seconds="$duration" log_path="$log_path" command_path="$cmd_path" run_dir="$run_dir" \
      poster_html="$final_dir/poster.html" preview_png="$final_dir/preview.png"
  else
    state="failed"
    local failed_candidate_preview
    failed_candidate_preview=""
    if [[ -n "$run_dir" ]]; then
      link_artifacts "$task_id" "$run_dir"
      failed_candidate_preview="$(link_failed_candidate_artifacts "$task_id" "$run_dir")"
    fi
    rm -f "$BATCH_DIR/status/$task_id.done" "$BATCH_DIR/status/$task_id.running"
    write_status_json "$BATCH_DIR/status/$task_id.failed" \
      index="$idx" task_id="$task_id" category="$category" paper_slug="$paper_slug" paper_pdf="$paper_pdf" \
      state="$state" exit_code="$rc" process_exit_code="$rc" terminal_status="$terminal_status" started_at="$started" finished_at="$finished" \
      duration_seconds="$duration" log_path="$log_path" command_path="$cmd_path" run_dir="$run_dir" \
      failed_candidate_preview="$failed_candidate_preview"
  fi
  append_jsonl "$STATUS_JSONL" event=finish task_id="$task_id" state="$state" exit_code="$rc" run_dir="$run_dir"
  rebuild_index
}

running_count() {
  jobs -rp | wc -l | tr -d ' '
}

count_files() {
  local pattern="$1"
  find "$BATCH_DIR/status" -maxdepth 1 -name "$pattern" -type f 2>/dev/null | wc -l | tr -d ' '
}

reconcile_status_records() {
  rm -f "$BATCH_DIR/status/.running" "$BATCH_DIR/status/.failed" "$BATCH_DIR/logs/.log" "$BATCH_DIR/commands/.sh" 2>/dev/null || true

  local running task_id poster preview run_link run_dir final_dir terminal_status failed_candidate_preview
  for running in "$BATCH_DIR"/status/*.running; do
    [[ -e "$running" ]] || continue
    task_id="$(basename "$running" .running)"
    [[ -n "$task_id" ]] || continue
    poster="$BATCH_DIR/links/$task_id-poster.html"
    preview="$BATCH_DIR/links/$task_id-preview.png"
    run_link="$BATCH_DIR/links/$task_id"
    if [[ -f "$poster" && -f "$preview" && -e "$run_link" ]]; then
      run_dir="$(readlink "$run_link" || true)"
      "$BATCH_PYTHON" - "$running" "$BATCH_DIR/status/$task_id.done" "$run_dir" "$poster" "$preview" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
run_dir = sys.argv[3]
poster = sys.argv[4]
preview = sys.argv[5]
try:
    data = json.loads(src.read_text(encoding="utf-8"))
except Exception:
    data = {"task_id": src.stem}
summary = Path(run_dir) / "run_telemetry_summary.json"
try:
    telemetry = json.loads(summary.read_text(encoding="utf-8"))
    terminal_status = str(telemetry.get("terminal_status") or "").strip()
except Exception:
    terminal_status = ""
recorded_terminal_status = str(data.get("terminal_status") or "").strip()
recorded_exit_code = data.get("process_exit_code")
if recorded_exit_code is None:
    recorded_exit_code = data.get("exit_code")
resolved_terminal_status = terminal_status or recorded_terminal_status or "unknown"
data.update({
    "state": "succeeded",
    "exit_code": recorded_exit_code,
    "process_exit_code": recorded_exit_code,
    "terminal_status": resolved_terminal_status,
    "finished_at": data.get("finished_at") or datetime.now().isoformat(timespec="seconds"),
    "run_dir": run_dir,
    "poster_html": poster,
    "preview_png": preview,
    "reconciled_from": src.name,
})
if resolved_terminal_status == "unknown":
    data["reconcile_error"] = "run telemetry and status marker did not provide terminal_status"
else:
    data.pop("reconcile_error", None)
dst.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
      rm -f "$running"
      append_jsonl "$STATUS_JSONL" event=reconciled_done task_id="$task_id" run_dir="$run_dir"
    elif [[ -e "$run_link" ]]; then
      run_dir="$(readlink "$run_link" || true)"
      if [[ -n "$run_dir" && -d "$run_dir" ]]; then
        final_dir="$run_dir/final"
        if [[ -f "$final_dir/poster.html" && -f "$final_dir/preview.png" ]]; then
          link_artifacts "$task_id" "$run_dir"
          "$BATCH_PYTHON" - "$running" "$BATCH_DIR/status/$task_id.done" "$run_dir" "$final_dir/poster.html" "$final_dir/preview.png" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
run_dir = sys.argv[3]
poster = sys.argv[4]
preview = sys.argv[5]
try:
    data = json.loads(src.read_text(encoding="utf-8"))
except Exception:
    data = {"task_id": src.stem}
summary = Path(run_dir) / "run_telemetry_summary.json"
try:
    telemetry = json.loads(summary.read_text(encoding="utf-8"))
    terminal_status = str(telemetry.get("terminal_status") or "").strip()
except Exception:
    terminal_status = ""
recorded_terminal_status = str(data.get("terminal_status") or "").strip()
recorded_exit_code = data.get("process_exit_code")
if recorded_exit_code is None:
    recorded_exit_code = data.get("exit_code")
resolved_terminal_status = terminal_status or recorded_terminal_status or "unknown"
data.update({
    "state": "succeeded",
    "exit_code": recorded_exit_code,
    "process_exit_code": recorded_exit_code,
    "terminal_status": resolved_terminal_status,
    "finished_at": data.get("finished_at") or datetime.now().isoformat(timespec="seconds"),
    "run_dir": run_dir,
    "poster_html": poster,
    "preview_png": preview,
    "reconciled_from": src.name,
})
if resolved_terminal_status == "unknown":
    data["reconcile_error"] = "run telemetry and status marker did not provide terminal_status"
else:
    data.pop("reconcile_error", None)
dst.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
          rm -f "$running"
          append_jsonl "$STATUS_JSONL" event=reconciled_done task_id="$task_id" run_dir="$run_dir"
        else
          terminal_status="$(terminal_status_for "$run_dir")"
          if [[ -n "$terminal_status" && "$terminal_status" != "pass" ]]; then
            failed_candidate_preview="$(link_failed_candidate_artifacts "$task_id" "$run_dir")"
            "$BATCH_PYTHON" - "$running" "$BATCH_DIR/status/$task_id.failed" "$run_dir" "$terminal_status" "$failed_candidate_preview" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
run_dir = sys.argv[3]
terminal_status = sys.argv[4]
failed_candidate_preview = sys.argv[5]
try:
    data = json.loads(src.read_text(encoding="utf-8"))
except Exception:
    data = {"task_id": src.stem}
data.update({
    "state": "failed",
    "exit_code": data.get("exit_code") or 1,
    "terminal_status": terminal_status,
    "finished_at": data.get("finished_at") or datetime.now().isoformat(timespec="seconds"),
    "run_dir": run_dir,
    "failed_candidate_preview": failed_candidate_preview,
    "reconciled_from": src.name,
})
dst.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
            rm -f "$running"
            append_jsonl "$STATUS_JSONL" event=reconciled_failed task_id="$task_id" run_dir="$run_dir" terminal_status="$terminal_status"
          fi
        fi
      fi
    fi
  done
}

rebuild_index() {
  local total done failed running pending
  reconcile_status_records
  total="$(awk -F '\t' 'NF >= 5 && $1 != "" && $2 != "" && $5 != "" { count++ } END { print count + 0 }' "$TASKS_TSV")"
  done="$(count_files '*.done')"
  failed="$(count_files '*.failed')"
  running="$(count_files '*.running')"
  pending=$((total - done - failed - running))
  {
    echo "# EvaData Poster Batch"
    echo
    echo "- Batch dir: \`$BATCH_DIR\`"
    echo "- Source: \`$EVADATA_DIR\`"
    echo "- Template: \`$TEMPLATE\`"
    echo "- Concurrency: \`$CONCURRENCY\`"
    echo "- Updated: \`$(date '+%Y-%m-%dT%H:%M:%S')\`"
    echo "- Counts: total=$total succeeded=$done failed=$failed running=$running pending=$pending"
    echo
    echo "Stop gracefully: \`$BATCH_SCRIPT_LABEL stop $BATCH_DIR\`"
    echo "Resume: \`$BATCH_SCRIPT_LABEL resume $BATCH_DIR\`"
    echo
    echo "| # | Category | Paper | State | Preview | Run |"
    echo "|---:|---|---|---|---|---|"
    while IFS=$'\t' read -r idx task_id category paper_slug paper_pdf; do
      [[ -n "${idx:-}" && -n "${task_id:-}" && -n "${paper_pdf:-}" ]] || continue
      local state preview run_link status_path terminal_status display_state
      state="pending"
      status_path=""
      [[ -f "$BATCH_DIR/status/$task_id.done" ]] && { state="succeeded"; status_path="$BATCH_DIR/status/$task_id.done"; }
      [[ -f "$BATCH_DIR/status/$task_id.failed" ]] && { state="failed"; status_path="$BATCH_DIR/status/$task_id.failed"; }
      [[ -f "$BATCH_DIR/status/$task_id.running" ]] && { state="running"; status_path="$BATCH_DIR/status/$task_id.running"; }
      preview="$BATCH_DIR/links/$task_id-preview.png"
      [[ -e "$preview" ]] || preview="$BATCH_DIR/links/$task_id-failed-candidate-preview.png"
      run_link="$BATCH_DIR/links/$task_id"
      [[ -e "$preview" ]] || preview=""
      [[ -e "$run_link" ]] || run_link=""
      display_state="$state"
      terminal_status="$(status_terminal_status "$status_path")"
      if [[ "$state" == "succeeded" && -n "$terminal_status" && "$terminal_status" != "pass" ]]; then
        display_state="succeeded ($terminal_status)"
      fi
      echo "| $idx | $category | $paper_slug | $display_state | $preview | $run_link |"
    done < "$TASKS_TSV"
  } > "$BATCH_DIR/index.md"
}

next_pending_task() {
  "$BATCH_PYTHON" - "$TASKS_TSV" "$BATCH_DIR/status" <<'PY'
import csv
import sys
from pathlib import Path

tasks = Path(sys.argv[1])
status_dir = Path(sys.argv[2])
with tasks.open("r", encoding="utf-8", newline="") as fh:
    for row in csv.reader(fh, delimiter="\t"):
        if len(row) < 5:
            continue
        idx, task_id, category, paper_slug, paper_pdf = row[:5]
        if not idx or not task_id or not category or not paper_slug or not paper_pdf:
            continue
        if not Path(paper_pdf).is_file():
            continue
        if (status_dir / f"{task_id}.done").exists():
            continue
        if (status_dir / f"{task_id}.running").exists():
            continue
        if (status_dir / f"{task_id}.failed").exists():
            continue
        print("\t".join([idx, task_id, category, paper_slug, paper_pdf]))
        break
PY
}

main() {
  configure_env
  init_tasks
  rebuild_index
  echo "Batch dir: $BATCH_DIR"
  echo "Tasks: $(wc -l < "$TASKS_TSV" | tr -d ' ')"
  echo "Concurrency: $CONCURRENCY"
  echo "Prompt: $PROMPT_FILE"
  echo "Index: $BATCH_DIR/index.md"
  echo

  while true; do
    if [[ -f "$STOP_FILE" ]]; then
      echo "STOP file present; no new tasks will be launched."
      break
    fi
    while [[ "$(running_count)" -ge "$CONCURRENCY" ]]; do
      sleep 10
      rebuild_index
      if [[ -f "$STOP_FILE" ]]; then
        echo "STOP file present; no new tasks will be launched."
        break
      fi
    done
    if [[ -f "$STOP_FILE" ]]; then
      break
    fi

    local next_task idx task_id category paper_slug paper_pdf
    next_task="$(next_pending_task)"
    if [[ -z "$next_task" ]]; then
      if [[ "$(running_count)" -eq 0 ]]; then
        break
      fi
      sleep 10
      rebuild_index
      continue
    fi

    IFS=$'\t' read -r idx task_id category paper_slug paper_pdf <<< "$next_task"
    # Keep planner/Codex subprocess stdin away from the task queue file.
    # Some external author commands read stdin, which can otherwise consume
    # subsequent TSV rows from this loop and skip papers.
    run_one "$idx" "$task_id" "$category" "$paper_slug" "$paper_pdf" < /dev/null &
    sleep 1
  done

  while [[ "$(running_count)" -gt 0 ]]; do
    sleep 10
    rebuild_index
  done
  rebuild_index
  echo "Batch complete or stopped. See: $BATCH_DIR/index.md"
}

main "$@"
