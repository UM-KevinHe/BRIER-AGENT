#!/usr/bin/env bash
# run_ui.sh -- launch the BRIER-Agent chat UI (app.py) with the model env loaded.
#
#   ./run_ui.sh              # foreground; Ctrl-C to stop
#   ./run_ui.sh --detach     # background via nohup; survives closing the terminal
#
# It starts the Gradio UI on http://localhost:7860. An env file is OPTIONAL: it sources
# .env.local (or .env) if present for the model endpoint / name / key, otherwise it uses
# whatever is already exported, and if nothing is set the UI still starts (enter the
# connection in its "Model & connection" panel). app.py does NOT auto-load .env.local, which
# is why this wrapper sources it.
#
# One-time setup first (in the environment you run this from):
#   python3 -m pip install -r requirements.txt
#
# Override the interpreter with PYTHON=... (defaults to python3); the detached log path with
# BRIER_UI_LOG=... (defaults to /tmp/brier_ui.log).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Model settings (endpoint / model / key). An env file is OPTIONAL: source .env.local if it
# exists, else .env, else rely on whatever is already exported. Even with nothing set the UI
# starts fine, you just enter the endpoint / model / key in its "Model & connection" panel.
ENV_FILE=""
for f in .env.local .env; do
  if [ -f "$f" ]; then ENV_FILE="$f"; break; fi
done
if [ -n "$ENV_FILE" ]; then
  set -a; . "./$ENV_FILE"; set +a
  echo "loaded model settings from $ENV_FILE"
elif [ -n "${BRIER_MODEL_ENDPOINT:-}" ]; then
  echo "no env file found; using BRIER_MODEL_* already set in the environment."
else
  echo "no .env.local / .env and no BRIER_MODEL_* set: starting anyway; set the endpoint," >&2
  echo "model, and key in the UI's 'Model & connection' panel." >&2
fi

# Interpreter, in order of preference: an explicit PYTHON=..., then the ACTIVE virtualenv's
# python (so we never fall through to a system python3 that lacks the deps just because the
# venv exposed `python` but not `python3`), then python3.
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
  PY="$VIRTUAL_ENV/bin/python"
else
  PY="python3"
fi
command -v "$PY" >/dev/null 2>&1 || {
  echo "ERROR: '$PY' not found. Set PYTHON=<your interpreter> (e.g. PYTHON=python)." >&2
  exit 1
}

# UI dependencies present? app.py needs gradio (+ gradio_client). If the one-time install has
# not been run in this environment, fail with a clear hint instead of a cryptic
# ModuleNotFoundError from deep inside the import.
if ! "$PY" -c "import gradio" >/dev/null 2>&1; then
  echo "ERROR: the UI dependencies are not installed for '$PY'." >&2
  echo "Run once (in this environment):  $PY -m pip install -r requirements.txt" >&2
  exit 1
fi

if [ "${1:-}" = "--detach" ] || [ "${1:-}" = "-d" ]; then
  LOG="${BRIER_UI_LOG:-/tmp/brier_ui.log}"
  nohup "$PY" app.py > "$LOG" 2>&1 &
  echo "BRIER-Agent UI starting in the background (pid $!). Logs: $LOG"
  echo "Open http://localhost:7860 . Stop it with: pkill -f app.py"
else
  echo "BRIER-Agent UI starting. Open http://localhost:7860 . Ctrl-C to stop."
  exec "$PY" app.py
fi
