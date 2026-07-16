#!/usr/bin/env bash
# run_ui.sh -- launch the BRIER-Agent chat UI (app.py) with the model env loaded.
#
#   ./run_ui.sh              # foreground; Ctrl-C to stop
#   ./run_ui.sh --detach     # background via nohup; survives closing the terminal
#
# It sources .env.local (the model endpoint / name / key) if present, then starts the
# Gradio UI on http://localhost:7860. app.py reads the model settings from the environment
# (it does NOT auto-load .env.local), so this wrapper is just "source the env, then launch".
#
# One-time setup first (in the environment you run this from):
#   python3 -m pip install -r requirements.txt
#
# Override the interpreter with PYTHON=... (defaults to python3); the detached log path with
# BRIER_UI_LOG=... (defaults to /tmp/brier_ui.log).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Model env: endpoint / model / key. Sourced so app.py's AgentConfig.from_env() sees them.
if [ -f .env.local ]; then
  set -a; . ./.env.local; set +a
else
  echo "note: no .env.local found; the model endpoint/key are unset (the UI still starts, but" >&2
  echo "      it will not reach a model until you set BRIER_MODEL_ENDPOINT / _NAME / _API_KEY)." >&2
fi

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || {
  echo "ERROR: '$PY' not found. Set PYTHON=<your interpreter> (e.g. PYTHON=python)." >&2
  exit 1
}

if [ "${1:-}" = "--detach" ] || [ "${1:-}" = "-d" ]; then
  LOG="${BRIER_UI_LOG:-/tmp/brier_ui.log}"
  nohup "$PY" app.py > "$LOG" 2>&1 &
  echo "BRIER-Agent UI starting in the background (pid $!). Logs: $LOG"
  echo "Open http://localhost:7860 . Stop it with: pkill -f app.py"
else
  echo "BRIER-Agent UI starting. Open http://localhost:7860 . Ctrl-C to stop."
  exec "$PY" app.py
fi
