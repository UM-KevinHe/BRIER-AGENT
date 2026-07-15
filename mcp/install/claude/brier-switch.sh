#!/usr/bin/env bash
#
# BRIER MCP - deployment switcher (run on your LAPTOP)
# ----------------------------------------------------
# When you have more than one BRIER deployment (a local one, plus one or
# more remote servers like psoriasis and cubio), this picks which one is
# active in Claude Desktop. It keeps a registry of known deployments and
# can show you which is active, print the block to activate one, or (with
# --write) edit claude_desktop_config.json for you behind safety rails.
#
# SAFETY: by default this script NEVER edits your config. It prints what to
# paste. Editing the config is opt-in via --write, and even then it backs
# the file up first, validates the result is valid JSON, and restores the
# backup if anything is wrong. The config file is the one whose corruption
# breaks ALL your MCP servers, so we treat it with care.
#
# Registry (stash) file:  ~/.brier-mcp/servers.json
#   A JSON object: { "name": { ...the mcpServers entry value... }, ... }
#   where each value is exactly what goes under the server's key in
#   claude_desktop_config.json -> mcpServers.
#
# Commands (this script lives in install/ ; run it from anywhere):
#   ./install/claude/brier-switch.sh list                 # show known deployments + which is active
#   ./install/claude/brier-switch.sh add NAME             # register a deployment by pasting its block
#   ./install/claude/brier-switch.sh use NAME             # PRINT the block to activate NAME (safe default)
#   ./install/claude/brier-switch.sh use NAME --write     # actually edit the config (with backup+validate)
#   ./install/claude/brier-switch.sh remove NAME          # drop NAME from the registry (not the config)
#
# NAME is a short nickname YOU choose for a deployment, e.g. "local",
# "psoriasis", "cubio". It is just a label for this registry; it is NOT an SSH
# alias and need not match the host. The real ssh target stays the full
# user@host inside the stored block. Activating a deployment means it becomes
# the BRIER entry under mcpServers; others are left out (the principle: use one
# deployment at a time, keyed to where the data lives). After any change you
# must fully quit and reopen Claude Desktop for it to reload.
#
set -uo pipefail

say()  { printf '%s\n' "$*"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------
STASH_DIR="$HOME/.brier-mcp"
STASH="$STASH_DIR/servers.json"

# Claude Desktop config location by OS.
detect_config() {
    case "$(uname -s)" in
        Darwin) printf '%s\n' "$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
        Linux)  printf '%s\n' "$HOME/.config/Claude/claude_desktop_config.json" ;;
        *)      printf '%s\n' "$HOME/.config/Claude/claude_desktop_config.json" ;;
    esac
}
CONFIG="${BRIER_CLAUDE_CONFIG:-$(detect_config)}"

need_python() {
    command -v python3 >/dev/null 2>&1 || die "python3 is required (used for safe JSON handling)."
}

ensure_stash() {
    mkdir -p "$STASH_DIR"
    if [ ! -f "$STASH" ]; then
        printf '{}\n' > "$STASH"
    fi
}

# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
cmd_list() {
    need_python; ensure_stash
    say "Registry: $STASH"
    say "Config:   $CONFIG"
    hr
    python3 - "$STASH" "$CONFIG" <<'PY'
import json, sys
stash_path, config_path = sys.argv[1], sys.argv[2]
try:
    stash = json.load(open(stash_path))
except Exception:
    stash = {}
active = set()
try:
    cfg = json.load(open(config_path))
    active = set((cfg.get("mcpServers") or {}).keys())
except Exception:
    pass
if not stash:
    print("No deployments registered yet. Use:  ./install/claude/brier-switch.sh add NAME")
else:
    print("Known deployments (in the registry):")
    for name, block in stash.items():
        keys = block.keys() if isinstance(block, dict) else []
        mark = ""
        skey = next(iter(keys), None) if keys else None
        if skey and skey in active:
            mark = "  [ACTIVE]"
        print(f"  - {name}{mark}")
    print()
    print("Active BRIER-ish servers currently in the config:")
    brierish = [k for k in active if "brier" in k.lower()]
    print("  " + (", ".join(brierish) if brierish else "(none)"))
PY
}

cmd_add() {
    local name="${1:-}"
    [ -n "$name" ] || die "usage: ./install/claude/brier-switch.sh add NAME"
    need_python; ensure_stash
    say "Registering deployment '$name'."
    say "Paste the BRIER config block for it (the object including its"
    say "\"brier-...\": { ... } key, exactly as setup.sh printed it), then press"
    say "Ctrl-D on a new line:"
    say ""
    local pasted
    pasted="$(cat)"
    [ -n "$pasted" ] || die "nothing pasted; aborting."
    STASH="$STASH" BRIER_PASTED="$pasted" python3 - "$name" <<'PY'
import json, os, sys
name = sys.argv[1]
stash_path = os.environ["STASH"]
raw = os.environ.get("BRIER_PASTED", "").strip()
if raw.endswith(","):
    raw = raw[:-1]
try:
    obj = json.loads(raw)
except Exception as e:
    try:
        obj = json.loads("{" + raw + "}")
    except Exception:
        sys.exit(f"ERROR: could not parse the pasted block as JSON: {e}")
if isinstance(obj, dict) and "command" in obj:
    entry = {f"brier-{name}": obj}
elif isinstance(obj, dict) and len(obj) == 1:
    entry = obj
else:
    sys.exit("ERROR: expected a single server block like "
             '{"brier-NAME": {"command": ...}} or its inner object.')
try:
    stash = json.load(open(stash_path))
except Exception:
    stash = {}
stash[name] = entry
json.dump(stash, open(stash_path, "w"), indent=2)
skey = next(iter(entry))
print(f"Registered '{name}' (server key: {skey}).")
PY
}

cmd_remove() {
    local name="${1:-}"
    [ -n "$name" ] || die "usage: ./install/claude/brier-switch.sh remove NAME"
    need_python; ensure_stash
    STASH="$STASH" python3 - "$name" <<'PY'
import json, os, sys
name = sys.argv[1]; stash_path = os.environ["STASH"]
try:
    stash = json.load(open(stash_path))
except Exception:
    stash = {}
if name in stash:
    del stash[name]
    json.dump(stash, open(stash_path, "w"), indent=2)
    print(f"Removed '{name}' from the registry (the config was not touched).")
else:
    print(f"'{name}' is not in the registry; nothing to do.")
PY
}

cmd_use() {
    local name="${1:-}"
    local write=0
    [ -n "$name" ] || die "usage: ./install/claude/brier-switch.sh use NAME [--write]"
    shift || true
    while [ $# -gt 0 ]; do
        case "$1" in
            --write) write=1; shift ;;
            *) die "unknown option to use: $1" ;;
        esac
    done
    need_python; ensure_stash

    local entry_json
    entry_json="$(STASH="$STASH" python3 - "$name" <<'PY'
import json, os, sys
name = sys.argv[1]; stash_path = os.environ["STASH"]
try:
    stash = json.load(open(stash_path))
except Exception:
    stash = {}
if name not in stash:
    sys.exit(f"'{name}' not in registry. Run './install/claude/brier-switch.sh list' to see known names, or 'add' it first.")
print(json.dumps(stash[name]))
PY
)" || exit 1

    if [ "$write" = "0" ]; then
        say "To activate '$name', put this under \"mcpServers\" in:"
        say "  $CONFIG"
        say "(remove any other brier-* entries so only one deployment is active)"
        hr
        printf '%s\n' "$entry_json" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), indent=2))"
        hr
        say "Then fully quit and reopen Claude Desktop. To do this edit"
        say "automatically (with backup + validation), re-run with --write."
        return 0
    fi

    [ -f "$CONFIG" ] || die "config not found at $CONFIG (set BRIER_CLAUDE_CONFIG or create it first)."
    local backup="$CONFIG.bak.$(date +%Y%m%d_%H%M%S)"
    cp "$CONFIG" "$backup" || die "could not create backup $backup"
    say "Backed up config to: $backup"

    if BRIER_ENTRY="$entry_json" BRIER_CONFIG="$CONFIG" python3 - "$name" <<'PY'
import json, os, sys
name = sys.argv[1]
config_path = os.environ["BRIER_CONFIG"]
entry = json.loads(os.environ["BRIER_ENTRY"])   # {serverKey: value}
cfg = json.load(open(config_path))
servers = cfg.get("mcpServers")
if servers is None:
    servers = {}
    cfg["mcpServers"] = servers
for k in [k for k in list(servers.keys()) if "brier" in k.lower()]:
    del servers[k]
for k, v in entry.items():
    servers[k] = v
out = json.dumps(cfg, indent=2)
json.loads(out)
open(config_path, "w").write(out + "\n")
print("OK")
PY
    then
        if python3 -c "import json,sys; json.load(open('$CONFIG'))" 2>/dev/null; then
            say "Config updated: '$name' is now the active BRIER deployment."
            say "Fully quit and reopen Claude Desktop to load it."
            say "(backup kept at $backup)"
        else
            warn "Post-edit validation failed; restoring backup."
            cp "$backup" "$CONFIG"
            die "config restored from backup; no changes applied."
        fi
    else
        warn "Edit step failed; restoring backup."
        cp "$backup" "$CONFIG"
        die "config restored from backup; no changes applied."
    fi
}

# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
CMD="${1:-}"
shift || true
case "$CMD" in
    list)   cmd_list "$@" ;;
    add)    cmd_add "$@" ;;
    use)    cmd_use "$@" ;;
    remove) cmd_remove "$@" ;;
    ""|-h|--help)
        sed -n '2,46p' "$0"
        ;;
    *)
        die "unknown command: $CMD (use: list | add NAME | use NAME [--write] | remove NAME)"
        ;;
esac
