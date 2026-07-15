#!/usr/bin/env bash
#
# BRIER MCP setup / bootstrap
# ---------------------------
# Run this AFTER you have the BRIER MCP code on the machine (bundled in
# BRIER-Agent, cloned, or copied). It can be run from anywhere; it locates
# the repo root itself. It handles the parts of the install that are tedious
# to do by hand:
#
#   1. ensures `uv` is installed (installs it if missing) and finds its
#      absolute path (so the non-interactive PATH gotcha never bites)
#   2. runs `uv sync` to install the Python dependencies
#   3. runs `server.py --selfcheck` to verify R, the BRIER R package,
#      and a writable cache directory
#   4. if the BRIER R package is missing, prints the exact command to
#      install it (it does NOT auto-install: on a shared/cluster R
#      library a silent install can go wrong, so we leave that to you)
#   5. prints a ready-to-use config block for YOUR CLIENT (Claude or Codex),
#      with the real absolute paths discovered on THIS machine
#
# What this script deliberately does NOT do:
#   * get the code onto the machine (that has to happen before this runs;
#     for a remote server, copy the self-contained mcp/ folder there, or
#     clone the BRIER-Agent repo on the server)
#   * install R or the BRIER R package (detect + instruct only)
#   * edit your client config (it prints a block; you register it)
#
# Usage (this script lives in install/ ; run it from anywhere):
#   cd /path/to/BRIER-MCP            # the mcp/ folder, or a standalone checkout
#   ./install/setup.sh --client claude            # local, print a Claude block
#   ./install/setup.sh --client codex             # local, print a Codex block
#   ./install/setup.sh --client claude --remote HOST   # RUN THIS ON THE REMOTE
#                              # server. HOST is the SSH alias your laptop uses
#                              # to reach this machine. The script discovers the
#                              # repo's own absolute path + uv path HERE (on the
#                              # remote) and prints a brier-remote block, already
#                              # filled in with the correct remote paths, that you
#                              # register on your LAPTOP.
#   --install-brier            # optional, combinable with the above: attempt to
#                              # install the BRIER R package if it is missing,
#                              # then RE-VERIFY it actually loads (not just that
#                              # it installed). Off by default, because a silent
#                              # install on a shared/cluster R library can fail
#                              # or compile unexpectedly.
#
# --client is REQUIRED: there is no default, so you always choose claude or codex.
#
# The remote-path question answers itself: because the script runs on the
# remote machine, the repo root it finds and the uv path it finds ARE the
# remote paths. Your laptop never has to guess where server.py lives.
#
set -euo pipefail

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
say()  { printf '%s\n' "$*"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# This script lives in <repo-root>/install/. The BRIER MCP ROOT (where
# server.py lives) is the PARENT of this script's directory. Resolve both
# absolutely, then cd to the root so 'uv run server.py' works.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$SELF_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

CLIENT=""
REMOTE_HOST=""
INSTALL_BRIER=0
while [ $# -gt 0 ]; do
    case "$1" in
        --client)
            CLIENT="${2:-}"
            [ -n "$CLIENT" ] || die "--client requires a value: claude or codex"
            shift 2
            ;;
        --remote)
            REMOTE_HOST="${2:-}"
            [ -n "$REMOTE_HOST" ] || die "--remote requires a host alias, e.g. ./install/setup.sh --client claude --remote psoriasis"
            shift 2
            ;;
        --install-brier)
            INSTALL_BRIER=1
            shift
            ;;
        -h|--help)
            sed -n '2,44p' "$0"
            exit 0
            ;;
        *)
            die "unknown argument: $1 (use --client claude|codex, --remote HOST, and/or --install-brier)"
            ;;
    esac
done

# --client is required, and must be one of the supported clients.
case "$CLIENT" in
    claude|codex) ;;
    "") die "missing required --client. Choose one: --client claude  or  --client codex" ;;
    *)  die "unknown --client '$CLIENT'. Supported: claude, codex" ;;
esac

say "BRIER MCP setup"
say "Install directory (repo root): $SCRIPT_DIR"
say "Client: $CLIENT"
hr

# --------------------------------------------------------------------------
# 1. ensure uv is present, find its absolute path
# --------------------------------------------------------------------------
find_uv() {
    # Prefer an already-resolvable uv, else check the usual install spots.
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /usr/local/bin/uv; do
        if [ -x "$cand" ]; then
            printf '%s\n' "$cand"
            return 0
        fi
    done
    return 1
}

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
    say "uv not found; installing it (no admin rights needed, installs into your home)..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        die "neither curl nor wget is available; cannot install uv automatically. Install uv manually: https://docs.astral.sh/uv/getting-started/installation/"
    fi
    UV="$(find_uv || true)"
    [ -n "$UV" ] || die "uv installed but still not found on PATH or in ~/.local/bin. Open a new shell or check the installer output above."
fi
say "uv: $UV"

# --------------------------------------------------------------------------
# 2. install Python dependencies
# --------------------------------------------------------------------------
hr
say "Installing Python dependencies (uv sync)..."
"$UV" sync
say "Dependencies installed."

# --------------------------------------------------------------------------
# 3. verify environment via --selfcheck
# --------------------------------------------------------------------------
hr
say "Verifying environment (R, BRIER package, cache)..."
SELFCHECK_JSON="$("$UV" run server.py --selfcheck 2>/dev/null || true)"
if [ -z "$SELFCHECK_JSON" ]; then
    die "server.py --selfcheck produced no output. Try running it directly: $UV run server.py --selfcheck"
fi
printf '%s\n' "$SELFCHECK_JSON"

# Extract fields with python (always available since uv sync just ran a venv,
# but we use system python3 here for parsing only).
get_field() {
    printf '%s' "$SELFCHECK_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null || true
}
STATUS="$(get_field status)"
RSCRIPT="$(get_field rscript)"
RSCRIPT_FOUND="$(get_field rscript_found)"
BRIER_INSTALLED="$(get_field brier_package_installed)"
CACHE_DIR="$(get_field cache_dir)"

# --------------------------------------------------------------------------
# 4. detect + instruct for missing pieces (no auto-install)
# --------------------------------------------------------------------------
hr
NEED_ACTION=0

if [ "$RSCRIPT_FOUND" != "True" ]; then
    NEED_ACTION=1
    warn "Rscript was not found."
    say  "  Install R (>= 4.0) from https://cran.r-project.org/ , or if R is"
    say  "  installed but not on PATH, set BRIER_RSCRIPT to its absolute path"
    say  "  (find it with: which Rscript) in your client config env."
fi

if [ "$BRIER_INSTALLED" != "True" ]; then
    if [ "$INSTALL_BRIER" = "1" ] && [ "$RSCRIPT_FOUND" = "True" ]; then
        # Opt-in auto-install (--install-brier). We attempt the install, then
        # RE-VERIFY with --selfcheck, because "installed" and "loadable" are
        # not the same thing: a package can install yet fail to load (e.g. a
        # missing system C++ runtime). We only call it a success if the
        # hardened selfcheck confirms a real load afterwards.
        say "Attempting to install the BRIER R package (--install-brier)..."
        say "  Using: remotes::install_github(\"UM-KevinHe/BRIER\")"
        say "  (this can take several minutes and may compile from source)"
        say ""
        INSTALL_LOG="$("$RSCRIPT" --no-save --no-restore -e \
            'if (!requireNamespace("remotes", quietly=TRUE)) install.packages("remotes", repos="https://cloud.r-project.org"); remotes::install_github("UM-KevinHe/BRIER", upgrade="never")' \
            2>&1 || true)"
        printf '%s\n' "$INSTALL_LOG" | tail -15
        say ""
        say "Re-verifying with --selfcheck (does it actually LOAD?)..."
        RECHECK_JSON="$("$UV" run server.py --selfcheck 2>/dev/null || true)"
        RECHECK_STATUS="$(printf '%s' "$RECHECK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)"
        RECHECK_BRIER="$(printf '%s' "$RECHECK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('brier_package_installed',''))" 2>/dev/null || true)"
        if [ "$RECHECK_BRIER" = "True" ]; then
            say "BRIER R package now installs AND loads. Good."
            BRIER_INSTALLED="True"
            STATUS="$RECHECK_STATUS"
        else
            NEED_ACTION=1
            RECHECK_ERR="$(printf '%s' "$RECHECK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('brier_load_error',''))" 2>/dev/null || true)"
            warn "The install ran, but BRIER still does not LOAD cleanly."
            say  "  selfcheck load error: $RECHECK_ERR"
            say  "  This is the install-but-cannot-load case (often a missing or"
            say  "  too-old system C++ runtime, e.g. a GLIBCXX version). The"
            say  "  package files are present but its compiled library will not"
            say  "  dlopen under this R. You will need to resolve the runtime"
            say  "  (for example, preload a newer libstdc++) before it works."
        fi
    else
        NEED_ACTION=1
        warn "The BRIER R package is not installed (or not loadable) under that R."
        say  "  Install it by running this in R (the same R that Rscript points to):"
        say  ""
        say  '      install.packages("remotes")'
        say  '      remotes::install_github("UM-KevinHe/BRIER")'
        say  '      library(BRIER)   # should load with no error'
        say  ""
        say  "  Or re-run this script with --install-brier to attempt it"
        say  "  automatically (it will verify the package actually LOADS, not"
        say  "  just that it installed)."
        say  ""
        say  "  By default this script does NOT install it: on a shared or"
        say  "  cluster R library, a silent install can fail or compile"
        say  "  unexpectedly, so it is safer for you to run it and watch."
    fi
fi

if [ "$STATUS" = "ok" ]; then
    say "Environment check: OK (R found, BRIER loads, cache writable)."
else
    warn "Environment check reported status=$STATUS. Address the items above, then re-run ./install/setup.sh"
fi

# --------------------------------------------------------------------------
# 5. hand off to the per-client config printer
# --------------------------------------------------------------------------
# The discovery work above (uv path, repo root, Rscript, remote host) is
# client-agnostic. Only the printed config block differs by client, so we
# hand the discovered values to the client-specific printer. Each printer
# lives in install/<client>/print-config.sh and receives the same arguments.
hr
RSCRIPT_ENV="${RSCRIPT:-/usr/local/bin/Rscript}"
PRINTER="$SELF_DIR/$CLIENT/print-config.sh"
[ -f "$PRINTER" ] || die "client printer not found: $PRINTER (expected install/$CLIENT/print-config.sh)"
chmod +x "$PRINTER" 2>/dev/null || true

# Args, positional: UV, SCRIPT_DIR (repo root), RSCRIPT_ENV, REMOTE_HOST (may be empty)
"$PRINTER" "$UV" "$SCRIPT_DIR" "$RSCRIPT_ENV" "$REMOTE_HOST"

hr
if [ "$NEED_ACTION" = "1" ]; then
    say "Setup finished WITH ACTION ITEMS above. Resolve them, then re-run ./install/setup.sh --client $CLIENT to confirm."
    exit 0
else
    say "Setup complete. Register the printed config block in $CLIENT (see docs/SETUP.md),"
    say "then restart or reconnect the client; the BRIER tools will be available."
fi
