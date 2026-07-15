#!/usr/bin/env bash
#
# BRIER MCP - SSH preflight (run on your LAPTOP, for remote deployments)
# ----------------------------------------------------------------------
# Goal: get `ssh -o BatchMode=yes HOST true` to succeed with NO prompt,
# which is the prerequisite for your MCP client (Claude or Codex) to launch the
# on a remote machine over SSH.
#
# This script CHECKS and GUIDES. It does not, and cannot, type your
# password, your key passphrase, or a 2FA code for you -- those are
# yours to enter. Anything that needs a secret, it tells you to run and
# explains why; it never stores or transmits secrets.
#
# Usage:
#   ./install/setup-ssh.sh HOST          # HOST is the alias or user@hostname
#   ./install/setup-ssh.sh psoriasis
#   ./install/setup-ssh.sh zrayw@psoriasis.sph.umich.edu
#
set -uo pipefail   # note: not -e; we want to handle check failures ourselves

say()  { printf '%s\n' "$*"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n>>> %s\n' "$*"; }

HOST="${1:-}"
[ -n "$HOST" ] || die "usage: ./install/setup-ssh.sh HOST   (e.g. ./install/setup-ssh.sh psoriasis)"

say "BRIER MCP - SSH preflight for: $HOST"
hr

# --------------------------------------------------------------------------
# Test 1: does passwordless, non-interactive SSH already work?
# This is exactly how the MCP client (Claude or Codex) will connect.
# --------------------------------------------------------------------------
step "Checking whether passwordless SSH to '$HOST' already works..."
if ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" 'echo OK' 2>/dev/null | grep -q '^OK$'; then
    say "PASS: passwordless SSH to '$HOST' works."
    say ""
    say "You are ready for remote BRIER. Next steps:"
    say "  1. Get the BRIER MCP code onto '$HOST': copy the self-contained"
    say "     mcp/ folder there (scp), or clone the BRIER-Agent repo on '$HOST'."
    say "  2. On '$HOST', in that directory, run:"
    say "       ./install/setup.sh --client CLIENT --remote $HOST"
    say "     (CLIENT is claude or codex)"
    say "  3. Register the printed brier-remote block in your client on this"
    say "     laptop (see docs/SETUP.md), then restart or reconnect the client."
    exit 0
fi

say "Passwordless SSH does not work yet. Walking through what to fix."
say "(The banner/policy text some servers print is harmless; we check the"
say " actual auth result, not the banner.)"

# --------------------------------------------------------------------------
# Diagnose: is it a missing key, an uninstalled key, or a policy block?
# --------------------------------------------------------------------------

# Pick a default key to use/look for.
KEY="$HOME/.ssh/id_ed25519"

step "Step 1 of 4: do you have an SSH key on this laptop?"
if [ -f "$KEY" ]; then
    say "Found a key: $KEY"
else
    # Maybe a differently-named key exists.
    OTHER_KEYS="$(ls "$HOME"/.ssh/id_* 2>/dev/null | grep -v '\.pub$' || true)"
    if [ -n "$OTHER_KEYS" ]; then
        say "No id_ed25519, but found other key(s):"
        printf '%s\n' "$OTHER_KEYS"
        say ""
        say "You can use one of those instead of generating a new one. If so,"
        say "substitute its path for $KEY in the commands below."
    else
        say "No SSH key found. Generate one (you'll choose a passphrase; a"
        say "passphrase is recommended, and macOS can remember it for you):"
        say ""
        say "    ssh-keygen -t ed25519 -C \"$HOST\" -f $KEY"
        say ""
        say "Then re-run this script."
        exit 0
    fi
fi

step "Step 2 of 4: is the key loaded in your ssh-agent?"
if ssh-add -l 2>/dev/null | grep -q .; then
    say "Agent has at least one identity loaded."
else
    say "No identities in the agent. Load your key (macOS: also store the"
    say "passphrase in Keychain so you never type it again):"
    say ""
    if [ "$(uname -s)" = "Darwin" ]; then
        say "    ssh-add --apple-use-keychain $KEY"
    else
        say "    ssh-add $KEY"
    fi
    say ""
    say "You'll enter the key's passphrase once. Then re-run this script."
    # Don't exit; the key might still be offered via IdentityFile even if not
    # in the agent, so continue to the install step.
fi

step "Step 3 of 4: is your PUBLIC key installed on '$HOST'?"
say "Install it with ssh-copy-id. This prompts for your '$HOST' password"
say "(and 2FA if the server uses it) exactly ONCE -- that's expected and"
say "necessary; the script can't and shouldn't do it for you:"
say ""
say "    ssh-copy-id -i ${KEY}.pub $HOST"
say ""
say "If you already ran this and it still fails, the cause is usually"
say "server-side PERMISSIONS (next step)."

step "Step 4 of 4: check server-side permissions (a common silent blocker)"
say "SSH refuses key auth if your home or ~/.ssh on the server are writable"
say "by group/other. After ssh-copy-id, log in once with your password and run"
say "these on '$HOST':"
say ""
say "    chmod go-w ~            # remove group/other WRITE on home"
say "    chmod 700 ~/.ssh"
say "    chmod 600 ~/.ssh/authorized_keys"
say ""

# --------------------------------------------------------------------------
# Offer to add a config alias if HOST looks like a bare alias (no @, no dot)
# --------------------------------------------------------------------------
case "$HOST" in
    *@*|*.*)
        : # looks like user@host or a FQDN; no alias suggestion needed
        ;;
    *)
        step "Optional: SSH config alias"
        say "Since '$HOST' is a short name, make sure ~/.ssh/config maps it to the"
        say "real host and your key, e.g.:"
        say ""
        say "    Host $HOST"
        say "        HostName <real.hostname.here>"
        say "        User <your-username>"
        say "        IdentityFile $KEY"
        say "        IdentitiesOnly yes"
        ;;
esac

hr
say "After completing the steps above, re-run:  ./install/setup-ssh.sh $HOST"
say "When it prints PASS, you're ready to deploy the server with install/setup.sh on '$HOST'."
