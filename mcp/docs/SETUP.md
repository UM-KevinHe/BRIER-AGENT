# Setting up BRIER MCP (Claude and Codex)

BRIER MCP speaks standard MCP over stdio. The exact same server runs unchanged
across clients: Claude Desktop, Claude Code, and OpenAI Codex all launch it as a
local stdio subprocess. Only the configuration file and its syntax differ
(Claude uses JSON, Codex uses TOML). This document covers installing the server
and registering it in both clients.

This document covers the two supported clients, Claude and Codex, as equals.
For containment when running an agent against data, see CONTAINMENT.md. For the
SSH mechanics behind a remote launch, see REMOTE.md.

## The two deployment shapes, and the naming convention

There are two deployment shapes, by where your data lives:

- **`brier-local`**: data, R, and BRIER are on the same machine you run the
  client on. The server runs locally; nothing leaves the machine.
- **`brier-remote`**: data lives on a server you reach over SSH. The server runs
  there, launched over an SSH-wrapped stdio tunnel; only summaries return.

Use these exact names for the registered server in both clients: `brier-local`
for the local deployment and `brier-remote` for the remote one. The convention
encodes a principle: use one deployment at a time, keyed to where the data is.

If you genuinely need more than one remote at once (for example two different
servers), suffix per host: `brier-psoriasis`, `brier-cubio`. The default and
recommended pattern is the strict `brier-local` / `brier-remote` pair, pointing
`brier-remote` at whichever server holds the data you are currently analyzing.

## Install the server (shared, client-agnostic)

Installing the BRIER MCP server is the same regardless of which client will
drive it: the server is identical; only registration differs. The install
scripts live in `install/`:

- `install/install.sh`: clone or locate the code, then hand off to setup.
- `install/setup.sh`: ensure `uv`, run `uv sync`, verify R + the BRIER R
  package + a writable cache via `--selfcheck`, then print the config block for
  your chosen client.
- `install/setup-ssh.sh`: SSH preflight for the remote case (run on the laptop).

Client-specific helpers live in subfolders:

- `install/claude/`: the Claude JSON config printer and `brier-switch.sh` (a
  Claude-Desktop deployment switcher; see below).
- `install/codex/`: the Codex registration helper (TOML block or
  `codex mcp add` command).

> Note on structure: the shared scripts at `install/` root hold the install
> logic that does not depend on the client (uv, selfcheck, R/BRIER detection,
> SSH preflight). Only the genuinely client-specific parts (config-block format,
> the Claude switcher) live in the per-client subfolders, so the shared logic is
> not duplicated.

Core steps, the same for everyone:

```bash
# from the mcp/ directory (or a standalone BRIER-MCP checkout)
./install/setup.sh --client claude     # local, print a Claude block
./install/setup.sh --client codex      # local, print a Codex block / command
./install/setup.sh --client claude --remote HOST   # run ON the remote server
```

`setup.sh` discovers this machine's real absolute paths (uv, the repo root,
Rscript) and fills them into the printed config, so you never guess where
`server.py` lives. For the remote case you run `setup.sh --remote HOST` ON the
server, and it prints a block with the server's real paths for you to register
on your laptop.

## Claude

Claude reads MCP configuration from `claude_desktop_config.json` (Claude
Desktop) under an `mcpServers` object. Claude Code uses `claude mcp add` or a
project `.mcp.json`.

### Local (brier-local)

`setup.sh --client claude` prints a block like:

```json
"brier-local": {
  "command": "/absolute/path/to/uv",
  "args": ["run", "--directory", "/absolute/path/to/BRIER-MCP", "server.py"],
  "env": { "BRIER_RSCRIPT": "/absolute/path/to/Rscript" }
}
```

Paste it under `mcpServers` in `claude_desktop_config.json`, then fully quit and
reopen Claude Desktop.

### Remote (brier-remote)

Run `setup.sh --client claude --remote HOST` ON the server; it prints:

```json
"brier-remote": {
  "command": "ssh",
  "args": [
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    "USER@HOST",
    "cd /remote/path/to/BRIER-MCP && BRIER_RSCRIPT=/usr/bin/Rscript /remote/path/to/uv run server.py"
  ]
}
```

Paste it into your laptop's `claude_desktop_config.json`. Passwordless SSH to
the host must already work; run `install/setup-ssh.sh HOST` on the laptop first
if it does not. See REMOTE.md for the SSH details.

### Managing multiple deployments (Claude switcher)

Claude Desktop has no built-in way to manage multiple MCP deployments; you edit
one JSON file by hand, and corrupting it breaks all your MCP servers. The
switcher `install/claude/brier-switch.sh` is a safety wrapper that keeps a
registry of your deployments and swaps them into the config with backup and
validation:

```bash
./install/claude/brier-switch.sh list            # show known + active
./install/claude/brier-switch.sh add NAME        # register a deployment
./install/claude/brier-switch.sh use NAME        # print the block to activate
./install/claude/brier-switch.sh use NAME --write  # edit the config (backup+validate)
```

The switcher is Claude-specific. Codex does not need it (see below).

## Codex

Codex reads MCP configuration from a TOML file: `~/.codex/config.toml` (global)
or a project-scoped `.codex/config.toml` (that project only, and the project
directory must be trusted). Codex also has built-in CLI commands to manage
servers, so you do not hand-edit a fragile single file the way Claude Desktop
requires.

### Local (brier-local)

`setup.sh --client codex` prints either a TOML block or a `codex mcp add`
command. The TOML form, for `~/.codex/config.toml`:

```toml
[mcp_servers.brier-local]
command = "/absolute/path/to/uv"
args = ["run", "--directory", "/absolute/path/to/BRIER-MCP", "server.py"]
startup_timeout_sec = 30

[mcp_servers.brier-local.env]
BRIER_RSCRIPT = "/absolute/path/to/Rscript"
```

Or add it from the command line:

```bash
codex mcp add brier-local \
  --env BRIER_RSCRIPT=/absolute/path/to/Rscript \
  -- /absolute/path/to/uv run --directory /absolute/path/to/BRIER-MCP server.py
```

### Remote (brier-remote)

The remote launch is the same SSH-wrapped stdio command, expressed in TOML:

```toml
[mcp_servers.brier-remote]
command = "ssh"
args = [
  "-T",
  "-o", "BatchMode=yes",
  "-o", "ServerAliveInterval=15",
  "-o", "ServerAliveCountMax=4",
  "USER@HOST",
  "cd /remote/path/to/BRIER-MCP && BRIER_RSCRIPT=/usr/bin/Rscript /remote/path/to/uv run server.py"
]
startup_timeout_sec = 60
```

Or via the CLI:

```bash
codex mcp add brier-remote -- ssh -T -o BatchMode=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 USER@HOST \
  "cd /remote/path/to/BRIER-MCP && BRIER_RSCRIPT=/usr/bin/Rscript /remote/path/to/uv run server.py"
```

Verify with `codex mcp list` and, in a session, `/mcp` to confirm `brier-remote`
is connected and listing its tools.

### Managing multiple deployments (Codex)

Codex does not need a switcher. It manages servers natively
(`codex mcp add` / `codex mcp list` / `codex mcp remove`), and multiple servers
coexist: register `brier-local` and `brier-remote` and both appear in `/mcp` at
once. To pick a deployment, invoke the one you want, or scope it per project via
`.codex/config.toml`. The same naming convention applies (`brier-local`,
`brier-remote`, with per-host suffixes only if you truly need simultaneous
remotes).

### Codex gotchas (most common connection failures)

1. **Raise `startup_timeout_sec`.** Codex's default startup timeout is 10
   seconds. An SSH-launched server has to open the tunnel, start `uv`, and load
   R and the BRIER package before it answers, which easily exceeds 10s on a cold
   start. If the server "won't connect" but works when run by hand, this is
   almost always the cause. The remote examples use 60s; raise it further if
   your cluster is slow to log in.
2. **Use absolute paths for `command`.** Codex does not inherit your full shell
   PATH, so a bare `uv` or even `ssh` may not be found. Use `which ssh` /
   `which uv` and put the absolute path in `command`.
3. **Trust the project directory** if you use a project-scoped
   `.codex/config.toml`; an untrusted project silently ignores the entry. The
   global `~/.codex/config.toml` does not have this constraint.
4. **`env` is a separate table for stdio servers.** Environment variables go
   under `[mcp_servers.<name>.env]`, not inline. For the remote launch the env
   is set inside the remote command string (`BRIER_RSCRIPT=...`), so no separate
   `env` table is needed there.

## Server-side routing guidance (automatic, both clients)

The server advertises a top-level `instructions` field in its MCP initialization
response. Clients that read it (Codex uses it directly, prioritizing the first
512 characters) get the core routing logic without configuration: inspect data
first, then route by what the data is (individual-level target with phenotype to
`brier_i`, pooled cohorts to `brier_full`, summary-statistics target to
`brier_s`), and that the absence of an individual-level phenotype rules out the
individual-level modules. This ships with the server; you do not configure it.

It is complementary to `CLAUDE.md.example`, the fuller copy-into-your-project
working guide for the staged preprocessing workflow when driving the server from
an agent.

## A note on data location

Pointing a different client at the server does not move the data. Computation
runs in the R installation on the machine where the data lives (local or the
SSH-reached server), and only summaries return to the client. The choice of
client changes which application drives the same local or SSH-wrapped stdio
server, nothing about where computation or data sit.

## What is out of scope

The ChatGPT web app is not supported. Its custom MCP support connects to remote
servers over HTTPS (SSE / streamable HTTP) and cannot launch a local stdio
subprocess or reach a localhost server. BRIER MCP is a stdio server by design,
so patient-level data stays on the machine where R runs. Supporting the ChatGPT
web app would require standing up a separate HTTPS endpoint, which is out of
scope. The supported clients (Claude and Codex) run on your machine and launch
the stdio server locally.
