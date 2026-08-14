#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8000}"

cd "${REPO_ROOT}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    echo "Install it first, then rerun scripts/start_local_web.sh." >&2
    exit 1
  fi
}

need_cmd uv

if [ ! -f "web/dist/index.html" ]; then
  cat >&2 <<'EOF'
Built frontend not found at web/dist/index.html.

General users should use an AutoDesign release/source bundle that includes
web/dist, then rerun scripts/start_local_web.sh.

Developers can build it from source:

  cd web
  npm ci
  npm run build

or run the frontend separately with `npm run dev`.
EOF
  exit 1
fi

CODEX_CLI=""
for candidate in \
  "${AUTODESIGN_CODEX_BIN:-}" \
  "${DESIGN_ANYTHING_CODEX_BIN:-}" \
  "${AUTODESIGN_CODE_EDITOR_CODEX_BIN:-}" \
  "${DESIGN_ANYTHING_CODE_EDITOR_CODEX_BIN:-}" \
  "${AUTODESIGN_DESIGNER_AUTHOR_CODEX_BIN:-}" \
  "${DESIGN_ANYTHING_DESIGNER_AUTHOR_CODEX_BIN:-}" \
  "${DESIGN_ANYTHING_PLANNER_AUTHOR_CODEX_BIN:-}"; do
  if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
    CODEX_CLI="${candidate}"
    break
  fi
done
if [ -z "${CODEX_CLI}" ]; then
  CODEX_CLI="$(command -v codex || true)"
fi
if [ -z "${CODEX_CLI}" ]; then
  for candidate in \
    "/Applications/ChatGPT.app/Contents/Resources/codex" \
    "/Applications/Codex.app/Contents/Resources/codex"; do
    if [ -x "${candidate}" ]; then
      CODEX_CLI="${candidate}"
      break
    fi
  done
fi

if [ -z "${CODEX_CLI}" ]; then
  cat >&2 <<'EOF'
Warning: Codex CLI was not found by this AutoDesign runtime.
Paper-poster generation with the default external author needs Codex CLI.
Run `codex --version` in the same shell that starts AutoDesign. If it only
works elsewhere, add it to this runtime's PATH or set AUTODESIGN_CODEX_BIN.

EOF
elif ! "${CODEX_CLI}" login status >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Warning: Codex CLI is installed but not logged in.
Run `codex login` before generating paper posters.

EOF
fi

if [ ! -f ".env" ]; then
  cat >&2 <<'EOF'
No .env file found. The web UI will still open, and you can enter an OpenAI
API key in Settings. For a persistent local setup, create .env with:

OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1
OPENAI_COMPAT_API_KEY=<your OpenAI API key>
OPENAI_API_KEY=<same key, optional compatibility>
ENHANCER_PROVIDER=openai_compat
CLAIM_GRAPH_PROVIDER=openai_compat
DECK_OUTLINE_PROVIDER=openai_compat
PAPER_MEMORY_PROVIDER=openai_compat
CRITIC_PROVIDER=openai_compat
COMPOSER_PROVIDER=openai_compat
ENHANCER_MODEL=gpt-5.4-nano
CLAIM_GRAPH_MODEL=gpt-5.4-nano
DECK_OUTLINE_MODEL=gpt-5.4-nano
PAPER_MEMORY_MODEL=gpt-5.4-nano
CRITIC_MODEL=gpt-5.4-nano
COMPOSER_MODEL=gpt-5.4-nano
INGEST_MODEL=gpt-5.4-nano

EOF
fi

AUTODESIGN_SKIP_SETUP="${AUTODESIGN_SKIP_SETUP:-${DESIGN_ANYTHING_SKIP_SETUP:-${DESIGNANYTHING_SKIP_SETUP:-0}}}"
if [ "${AUTODESIGN_SKIP_SETUP}" != "1" ]; then
  echo "Installing Python dependencies with uv..."
  uv sync

  echo "Installing Playwright browsers..."
  uv run python scripts/install_playwright_browsers.py
fi

export AUTODESIGN_DESIGNER_AUTHOR="${AUTODESIGN_DESIGNER_AUTHOR:-${DESIGN_ANYTHING_DESIGNER_AUTHOR:-external}}"
export AUTODESIGN_DESIGNER_AUTHOR_HARNESS="${AUTODESIGN_DESIGNER_AUTHOR_HARNESS:-${DESIGN_ANYTHING_DESIGNER_AUTHOR_HARNESS:-codex}}"
export AUTODESIGN_DESIGNER_AUTHOR_TIMEOUT_SECONDS="${AUTODESIGN_DESIGNER_AUTHOR_TIMEOUT_SECONDS:-${DESIGN_ANYTHING_DESIGNER_AUTHOR_TIMEOUT_SECONDS:-3600}}"

URL="http://127.0.0.1:${BACKEND_PORT}"
HEALTH_URL="${URL}/api/health"

cleanup() {
  local pid
  for pid in $(jobs -p); do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

check_health() {
  if command -v curl >/dev/null 2>&1; then
    curl --fail --silent --show-error --max-time 1 "${HEALTH_URL}" >/dev/null 2>&1
  else
    uv run python - "${HEALTH_URL}" >/dev/null 2>&1 <<'PY'
from urllib.request import urlopen
import sys

with urlopen(sys.argv[1], timeout=1) as response:
    if not 200 <= response.status < 300:
        raise SystemExit(1)
PY
  fi
}

backend_is_alive() {
  local process_state

  if ! kill -0 "${backend_pid}" 2>/dev/null; then
    return 1
  fi
  process_state="$(ps -o stat= -p "${backend_pid}" 2>/dev/null)" || return 0
  case "${process_state}" in
    *Z*) return 1 ;;
    *) return 0 ;;
  esac
}

backend_exited_before_health() {
  local startup_status

  if wait "${backend_pid}"; then
    echo "error: Backend exited before becoming healthy at ${HEALTH_URL}." >&2
    return 1
  else
    startup_status=$?
    echo "error: Backend exited before becoming healthy at ${HEALTH_URL} (exit ${startup_status})." >&2
    return "${startup_status}"
  fi
}

wait_for_health() {
  local attempt=1

  while [ "${attempt}" -le 30 ]; do
    if ! backend_is_alive; then
      break
    fi
    if check_health; then
      if backend_is_alive; then
        return 0
      fi
      break
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  if ! backend_is_alive; then
    backend_exited_before_health
    return $?
  fi

  echo "error: Backend did not become healthy at ${HEALTH_URL} within 30 seconds." >&2
  return 1
}

echo "Starting FastAPI backend on http://127.0.0.1:${BACKEND_PORT}"
uv run uvicorn scripts.web_server:app --host 127.0.0.1 --port "${BACKEND_PORT}" &
backend_pid=$!

if wait_for_health; then
  :
else
  startup_status=$?
  exit "${startup_status}"
fi

AUTODESIGN_NO_OPEN="${AUTODESIGN_NO_OPEN:-${DESIGN_ANYTHING_NO_OPEN:-${DESIGNANYTHING_NO_OPEN:-0}}}"
if [ "${AUTODESIGN_NO_OPEN}" != "1" ]; then
  if command -v open >/dev/null 2>&1; then
    open "${URL}" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${URL}" >/dev/null 2>&1 || true
  fi
fi

echo
echo "AutoDesign is running at ${URL}"
echo "Press Ctrl-C to stop the backend."
if wait "${backend_pid}"; then
  :
else
  exit "$?"
fi
