#!/usr/bin/env bash
#
# BRIER MCP - Codex config printer
# --------------------------------
# Called by install/setup.sh after the shared environment checks. Prints a
# ready-to-use Codex configuration (brier-local or brier-remote) with the real
# absolute paths discovered on this machine: both the TOML block for
# ~/.codex/config.toml and the equivalent `codex mcp add` command line.
#
# Codex manages MCP servers natively (codex mcp add/list/remove), so unlike
# Claude there is no switcher and no config-file surgery: you either paste the
# TOML or run the add command.
#
# Not usually run directly; setup.sh invokes it as:
#   install/codex/print-config.sh UV SCRIPT_DIR RSCRIPT_ENV REMOTE_HOST
#
set -euo pipefail

say()  { printf '%s\n' "$*"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }

UV="${1:?usage: print-config.sh UV SCRIPT_DIR RSCRIPT_ENV REMOTE_HOST}"
SCRIPT_DIR="${2:?missing SCRIPT_DIR}"
RSCRIPT_ENV="${3:?missing RSCRIPT_ENV}"
REMOTE_HOST="${4:-}"

if [ -n "$REMOTE_HOST" ]; then
    # We are running ON the remote server. Emit what the LAPTOP uses.
    say "REMOTE config (Codex)."
    say "You ran this ON the remote server '$REMOTE_HOST'. The paths below are"
    say "this machine's real paths. Use this ON YOUR LAPTOP (not here)."
    say ""
    say "Prerequisite on the laptop: passwordless SSH to '$REMOTE_HOST' must"
    say "already work (run install/setup-ssh.sh on the laptop first if it does not)."
    say ""
    say "Option A: TOML block for ~/.codex/config.toml"
    say ""
    cat <<EOF
[mcp_servers.brier-remote]
command = "ssh"
args = [
  "-T",
  "-o", "BatchMode=yes",
  "-o", "ServerAliveInterval=15",
  "-o", "ServerAliveCountMax=4",
  "$REMOTE_HOST",
  "cd $SCRIPT_DIR && BRIER_RSCRIPT=$RSCRIPT_ENV $UV run server.py"
]
startup_timeout_sec = 60
EOF
    say ""
    say "Option B: equivalent CLI command (run on your laptop)"
    say ""
    cat <<EOF
codex mcp add brier-remote -- ssh -T -o BatchMode=yes \\
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 $REMOTE_HOST \\
  "cd $SCRIPT_DIR && BRIER_RSCRIPT=$RSCRIPT_ENV $UV run server.py"
EOF
    say ""
    say "After adding, raise the startup timeout if needed (SSH cold start can"
    say "exceed Codex's 10s default; the TOML above uses 60s). Verify with"
    say "'codex mcp list' and, in a session, '/mcp'."
else
    # Local deployment: server runs on THIS machine, launched directly by uv.
    say "LOCAL config (Codex)."
    say "Use this on THIS machine (the server runs here, where your data is)."
    say ""
    say "Option A: TOML block for ~/.codex/config.toml"
    say ""
    cat <<EOF
[mcp_servers.brier-local]
command = "$UV"
args = ["run", "--directory", "$SCRIPT_DIR", "server.py"]
startup_timeout_sec = 30

[mcp_servers.brier-local.env]
BRIER_RSCRIPT = "$RSCRIPT_ENV"
EOF
    say ""
    say "Option B: equivalent CLI command"
    say ""
    cat <<EOF
codex mcp add brier-local \\
  --env BRIER_RSCRIPT=$RSCRIPT_ENV \\
  -- $UV run --directory $SCRIPT_DIR server.py
EOF
    say ""
    say "Verify with 'codex mcp list' and, in a session, '/mcp'."
fi
