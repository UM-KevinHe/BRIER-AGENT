# Running BRIER MCP on a remote server

This document covers the SSH mechanics of running the BRIER MCP server on a
remote machine where your data lives, reaching it from a client on your laptop.
It is client-agnostic: the SSH layer works the same whether you drive with
Claude or Codex. For registering the resulting server in a specific client
(the JSON or TOML config), see SETUP.md. For containment when an agent acts on
remote data, see CONTAINMENT.md.

## When you want this

Use a remote deployment when your data, R, and the BRIER package live on a
server (an HPC cluster, a lab server) rather than on your laptop. The server
runs there, next to the data; only analysis summaries return to your laptop.
This is the `brier-remote` deployment shape (the local shape, `brier-local`,
is for data on the same machine as the client).

## How it works

An MCP client launches MCP servers as local subprocesses and talks to them over
stdio (JSON-RPC on stdin/stdout). The trick: instead of launching the BRIER
server directly, the client launches `ssh`, and `ssh` runs the BRIER server on
the remote. The stdio stream flows through the SSH connection. The BRIER server
code is identical to the local case; it does not know or care that its
stdin/stdout are connected to an SSH tunnel rather than directly to the client.

```
[ Your laptop ]                          [ Remote server ]
+---------------------+                  +----------------------------+
|  MCP client         |                  |                            |
|        |            |                  |   BRIER MCP server         |
|     stdio           |   SSH tunnel     |   (uv run server.py)       |
|        |            | <==============> |        |                   |
|     ssh client      |                  |     Rscript -> BRIER pkg   |
+---------------------+                  |        |                   |
                                         |   /path/to/your/data       |
                                         +----------------------------+
```

Because the server runs on the remote:

- File paths are the **remote's** paths, not your laptop's. When you tell the
  agent to fit on `/path/on/server/data.rds`, that path is resolved on the
  server, where it actually exists.
- Rscript is the **remote's** Rscript, and the BRIER package is the one
  installed on the remote.
- No data is copied to your laptop. Only the analysis summaries (coefficients,
  metrics, plot file paths) flow back through the tunnel.

## Quick start with the setup scripts

Two scripts automate most of the manual work. The manual steps are documented
below if you prefer them or need to debug.

1. **On your laptop**, check that passwordless SSH to the server works:

   ```bash
   ./install/setup-ssh.sh HOST     # HOST is whatever makes `ssh HOST` work,
                                   # e.g. ./install/setup-ssh.sh user@server
   ```

   If it prints PASS, SSH is ready. If not, it walks you through generating an
   SSH key, installing it on the server with `ssh-copy-id`, and fixing the
   server-side permissions that commonly block key auth. It guides each step but
   never types your password, key passphrase, or 2FA code; those are yours to
   enter. Re-run it until it prints PASS.

2. **Get the BRIER MCP code onto the server** (clone or copy), then

3. **On the server**, run setup with the string your laptop uses to reach it
   (a full `user@host`, or a short SSH alias if you have one):

   ```bash
   ./install/setup.sh --client CLIENT --remote HOST
   ```

   This installs `uv` if missing, installs the Python dependencies, verifies R
   and the BRIER R package and a writable cache (via `--selfcheck`), and prints a
   ready-to-use `brier-remote` config block for your client. Because the script
   runs on the server, the paths in that block are the server's real paths; your
   laptop does not have to guess where `server.py` lives remotely. If the BRIER R
   package is missing, the script prints the exact install command rather than
   installing it for you (safer on a shared or cluster R library).

4. **Back on your laptop**, register the printed block in your client. The exact
   step is client-specific (paste into a JSON config for Claude, add to a TOML
   config or run `codex mcp add` for Codex); see SETUP.md. Then restart or
   reconnect the client.

The rest of this document explains the SSH layer manually and covers the
hardened SSH options, limitations, and troubleshooting.

## The SSH command, explained

However you register it, the remote launch is one SSH command. Both clients use
the same command; only the surrounding config syntax differs.

```
ssh -T \
  -o BatchMode=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=4 \
  USER@HOST \
  "cd /remote/path/to/BRIER-MCP && BRIER_RSCRIPT=/usr/bin/Rscript /remote/path/to/uv run server.py"
```

### What each SSH option does

- **`-T`**: disable pseudo-terminal allocation. Without this, SSH may allocate a
  TTY that injects control characters or echo into the stream, corrupting the
  JSON-RPC that MCP relies on. Required.
- **`-o BatchMode=yes`**: never prompt interactively. If key auth fails, fail
  fast instead of hanging on a prompt the client cannot answer.
- **`-o ServerAliveInterval=15` / `ServerAliveCountMax=4`**: send a keepalive
  every 15s; give up after 4 unanswered (60s). Prevents an idle tunnel from
  being silently dropped by a NAT/firewall timeout mid-analysis.
- **the remote command**: `cd` into the install dir, set `BRIER_RSCRIPT`
  explicitly (because the non-interactive shell may not have R on PATH), and run
  the server with the absolute path to `uv`.

## Prerequisites

- **Passwordless, non-interactive SSH** from laptop to server. This is the hard
  requirement: the client runs `ssh -o BatchMode=yes`, which cannot answer a
  password or 2FA prompt. `ssh -o BatchMode=yes HOST true` must succeed silently.
  `install/setup-ssh.sh` checks and guides this.
- **R (>= 4.0) and the BRIER R package on the server**, loadable by the Rscript
  the config points to. `--selfcheck` verifies this.
- **`uv` on the server** (the setup script installs it if missing).
- **The BRIER MCP code on the server** (clone or copy).

## Verifying the remote setup before wiring a client

Before registering anything in a client, confirm the whole chain works by hand
from your laptop. This isolates "does the SSH + server chain work" from "is the
client config right":

```bash
ssh -T -o BatchMode=yes USER@HOST \
  "cd /remote/path/to/BRIER-MCP && BRIER_RSCRIPT=/usr/bin/Rscript /remote/path/to/uv run server.py --selfcheck"
```

If that prints the selfcheck JSON with `"status": "ok"` and the expected BRIER
MCP version, the chain is sound and you only need the client to launch it. If it
hangs or errors, fix that first (it is an SSH-key or path issue, independent of
any client).

## Known limitations

- **Cold-start latency.** The first tool call after the client connects has to
  open the SSH tunnel, start `uv`, and load R and the BRIER package. This can
  take many seconds. Some clients impose a startup timeout (Codex defaults to
  10s); raise it for the remote case (see SETUP.md). Subsequent calls reuse the
  warm connection.
- **One connection per session.** The tunnel is a single SSH connection; if it
  drops (laptop sleep, network change), the client must relaunch it, which means
  another cold start.
- **Session persistence.** A long-running remote command tied to your SSH
  session can be killed if the session drops. For interactive shells doing heavy
  preprocessing, consider `tmux`/`screen`/`nohup`; the MCP server itself is
  relaunched per session and does not need this.

## Running inside a scheduler job (optional, advanced)

On clusters where login nodes are not meant for compute, you may want the server
to run inside a scheduler allocation (SLURM, etc.) rather than on the login node.
This is advanced and cluster-specific: the remote command would request or attach
to an allocation before launching `server.py`. Most users do not need this; the
server is lightweight (it shells out to Rscript per call), and the heavy compute
happens inside those Rscript calls.

## Troubleshooting

- **Connects but no tools / immediate disconnect.** Usually the remote command
  is wrong: wrong path to the repo or to `uv`, or `BRIER_RSCRIPT` not set. Run
  the by-hand `--selfcheck` command above to see the real error.
- **Hangs forever on connect.** Passwordless SSH is not actually working; a
  prompt is waiting that the client cannot answer. Test
  `ssh -o BatchMode=yes HOST true` and fix with `install/setup-ssh.sh`.
- **Worked, then stopped.** The tunnel was dropped (sleep, network change) or an
  idle timeout fired. The keepalive options reduce this; reconnect the client to
  relaunch.
- **`selfcheck` shows BRIER not loadable.** The package is installed but its
  compiled library will not load under that R (often a system C++ runtime / a
  GLIBCXX version). Resolve the runtime on the server (for example, preload a
  newer libstdc++) before it works.
- **Stale code on the server.** If the server runs an old version, `git pull` in
  the server's checkout and re-run `--selfcheck` to confirm the version.
