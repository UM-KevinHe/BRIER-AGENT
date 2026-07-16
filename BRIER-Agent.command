#!/usr/bin/env bash
# =============================================================================
# BRIER-Agent.command -- double-click launcher for the chat UI (macOS).
#
# Double-click this file in Finder to start BRIER-Agent. The FIRST run sets up a
# virtual environment and installs the Python dependencies (a few minutes); every
# run after that just starts the UI and opens it in your browser.
#
# Gatekeeper note: the first time, macOS may say it "cannot verify the developer".
# Right-click the file -> Open -> Open, once, and it will run normally after that.
#
# It does NOT install R or the BRIER R package -- those are a one-time setup (see
# DEPLOY.md). Without them the UI still opens, but a fit will report what is missing.
# =============================================================================
set -euo pipefail

# Run from the repository (this file sits at the repo root).
cd "$(dirname "$0")"

PY=".venv/bin/python"

echo "== BRIER-Agent =="

# 1. Virtual environment (created once).
if [ ! -x "$PY" ]; then
  echo "First-time setup: creating a virtual environment (.venv)..."
  python3 -m venv .venv --prompt brier-agent
fi

# 2. Python dependencies (installed once; re-checked cheaply each launch).
if ! "$PY" -c "import gradio" >/dev/null 2>&1; then
  echo "Installing dependencies (one time, a few minutes)..."
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt
fi

# 3. A quick environment check (R + BRIER). Informational: never blocks the launch.
echo "Checking R and the BRIER package..."
"$PY" -m brier_agent.check_env || {
  echo ""
  echo "NOTE: something the fitters need is missing above (usually R or the BRIER"
  echo "package). The UI will still open, but a fit will fail until it is installed."
  echo "See DEPLOY.md -> 'Installing R and BRIER'."
  echo ""
}

# 4. Open the browser once the server has had a moment to start.
( sleep 5; open "http://localhost:7860" >/dev/null 2>&1 || true ) &

# 5. Launch the UI. run_ui.sh loads .env.local and uses this venv's interpreter.
echo "Starting the UI at http://localhost:7860 ... (close this window to stop it)"
export VIRTUAL_ENV="$PWD/.venv"
export PATH="$PWD/.venv/bin:$PATH"
exec ./run_ui.sh
