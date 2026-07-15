#!/usr/bin/env bash
#
# BRIER MCP - Claude config printer
# ---------------------------------
# Called by install/setup.sh after the shared environment checks. Prints a
# ready-to-paste Claude config block (brier-local or brier-remote) with the
# real absolute paths discovered on this machine, and, for the local case,
# registers the deployment with the Claude switcher's stash.
#
# Not usually run directly; setup.sh invokes it as:
#   install/claude/print-config.sh UV SCRIPT_DIR RSCRIPT_ENV REMOTE_HOST
#
set -euo pipefail

say()  { printf '%s\n' "$*"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

UV="${1:?usage: print-config.sh UV SCRIPT_DIR RSCRIPT_ENV REMOTE_HOST}"
SCRIPT_DIR="${2:?missing SCRIPT_DIR}"
RSCRIPT_ENV="${3:?missing RSCRIPT_ENV}"
REMOTE_HOST="${4:-}"

if [ -n "$REMOTE_HOST" ]; then
    # We are running ON the remote server. Emit the block the LAPTOP uses.
    # $SCRIPT_DIR and $UV are this machine's (the remote's) real paths.
    say "REMOTE config block (Claude)."
    say "You ran this ON the remote server '$REMOTE_HOST'. The paths below are"
    say "this machine's real paths. Paste this into the mcpServers section of"
    say "claude_desktop_config.json ON YOUR LAPTOP (not here)."
    say ""
    say "Prerequisite on the laptop: passwordless SSH to '$REMOTE_HOST' must"
    say "already work (run install/setup-ssh.sh on the laptop first if it does not)."
    say ""
    cat <<EOF
    "brier-remote": {
      "command": "ssh",
      "args": [
        "-T",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "$REMOTE_HOST",
        "cd $SCRIPT_DIR && BRIER_RSCRIPT=$RSCRIPT_ENV $UV run server.py"
      ]
    }
EOF
else
    # Local deployment: server runs on THIS machine, launched directly by uv.
    say "LOCAL config block (Claude)."
    say "Paste this into the mcpServers section of claude_desktop_config.json"
    say "on THIS machine (the server runs here, where your data is)."
    say ""
    cat <<EOF
    "brier-local": {
      "command": "$UV",
      "args": [
        "run",
        "--directory",
        "$SCRIPT_DIR",
        "server.py"
      ],
      "env": {
        "BRIER_RSCRIPT": "$RSCRIPT_ENV"
      }
    }
EOF
fi

hr
# --------------------------------------------------------------------------
# register this deployment with the switcher's stash
# --------------------------------------------------------------------------
# Local case: setup runs on the same machine as the laptop config/stash, so we
# can write the registry directly. Remote case: setup runs on the server, which
# is NOT where the laptop's stash lives, so we print the command for the user to
# register it on their laptop instead.
STASH_DIR="$HOME/.brier-mcp"
STASH="$STASH_DIR/servers.json"

if [ -z "$REMOTE_HOST" ]; then
    # LOCAL: auto-register "local" in the stash on this machine.
    if command -v python3 >/dev/null 2>&1; then
        mkdir -p "$STASH_DIR"
        [ -f "$STASH" ] || printf '{}\n' > "$STASH"
        UV="$UV" SCRIPT_DIR="$SCRIPT_DIR" RSCRIPT_ENV="$RSCRIPT_ENV" \
        STASH="$STASH" python3 - <<'PY' || warn "could not update the switcher registry (non-fatal)."
import json, os
stash = os.environ["STASH"]
entry = {"brier-local": {
    "command": os.environ["UV"],
    "args": ["run", "--directory", os.environ["SCRIPT_DIR"], "server.py"],
    "env": {"BRIER_RSCRIPT": os.environ["RSCRIPT_ENV"]},
}}
try:
    data = json.load(open(stash))
except Exception:
    data = {}
data["local"] = entry
json.dump(data, open(stash, "w"), indent=2)
print(f"Registered 'local' in the switcher registry ({stash}).")
PY
    fi
else
    # REMOTE: the stash lives on the laptop, not here. Tell the user how to
    # register this deployment there using the block printed above.
    say "To register this remote deployment with the switcher on your LAPTOP,"
    say "run there (pasting the block above when prompted). The NAME is a short"
    say "nickname of your choosing for this deployment, not the host; it is only"
    say "a label you type with the switcher (the ssh target stays the full"
    say "user@host inside the pasted block):"
    say ""
    say "    ./install/claude/brier-switch.sh add NAME      # e.g. ./install/claude/brier-switch.sh add cubio"
    say ""
    say "Then activate it with:  ./install/claude/brier-switch.sh use NAME --write"
fi
