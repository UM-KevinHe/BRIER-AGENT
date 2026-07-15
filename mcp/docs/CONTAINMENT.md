# Containment profiles for agentic preprocessing over SSH

When you drive BRIER through an agentic coding tool (Claude Code or OpenAI
Codex) to preprocess data on a remote server, you want the agent fenced in:
free to do useful work, stopped before it can do damage. This directory ships
**template** containment profiles for both clients:

- `claude-containment-settings.template.json` for Claude Code
- `codex-containment-settings.template.toml` for OpenAI Codex

They express the same intent through each client's own mechanism. Read this
whole document before using either: the threat model and the honest limits are
the same for both, and the most important protection (server-side, below) is
the same for both.

## The setup this is for

Your data lives on a remote server (an HPC cluster, a lab server) that is
locked down: you can SSH in, but you cannot install software like the agent
there. So the agent runs **on your laptop**, where you can install it, and it
acts on the server's data **over SSH** (`ssh you@server '...'`). BRIER-MCP can
be registered alongside it for fitting and reporting once data is prepared.

The data never moves to your laptop; the agent reaches into the server to work
on it in place.

> These profiles are a **first, deliberately strict** draft. Expect to loosen
> specific rules as you learn what your preprocessing needs. Start inside the
> fence, widen gates intentionally.

## How containment actually works here (read this)

Be clear-eyed about what protects you. The two clients enforce containment
differently, but the conclusion is the same.

**Claude Code uses string-matched permission rules.** A rule like `Bash(rm:*)`
denies an `rm` command on your laptop. Reads, edits, and commands are checked
against allow/ask/deny lists.

**Codex uses an OS-enforced sandbox plus an approval policy.** The sandbox
(Seatbelt on macOS, bubblewrap on Linux) is a real technical boundary on what
the laptop process can touch; the approval policy decides when Codex must stop
and ask before crossing it. In `workspace-write` mode, writes are confined to
the workspace and network access is off by default.

The crucial shared limitation, true for BOTH:

- **Neither client can contain what runs inside `ssh you@server '...'`.** When
  the agent runs an ssh command, the laptop-side controls (Claude's rules,
  Codex's sandbox) govern the `ssh` process on the laptop. They cannot inspect
  or restrict the command string that executes on the server. So a laptop rule
  or sandbox that blocks `rm` does NOT block `ssh server 'rm ...'`. The remote
  command is opaque to the laptop side. That is why ssh-to-your-server is set to
  **ask/prompt** in both profiles: so a human sees every remote command.
- **The agent acts as you, on your data, under your account.** It cannot do
  anything on the server you could not do yourself. The risk is an honest
  *mistake* (an accidental delete, an overwrite), not a malicious breakout.
  Ask-before-remote plus an attentive eye handles mistakes.
- **The laptop-side controls protect your laptop**, not the server: no
  destructive ops, no reading secrets, no network egress from the laptop side.

## The enforced backstop you SHOULD add (server-side, both clients)

Because neither client can contain the remote side, add one enforced layer that
does not require installing anything on the locked-down server: make your
**raw-data directory read-only**, using your own file permissions:

```bash
# on the server, once
chmod -R a-w /path/to/your/raw-data
```

Now even an approved-by-mistake `ssh server 'rm ...'` against raw data fails at
the filesystem level. The agent reads raw data (still allowed) but writes all
intermediate and output files to a SEPARATE, writable output directory. This is
the "even if I misclick approve, the irreplaceable raw data survives" layer. It
works on a locked-down server because it is just your own `chmod`, and it
protects you identically whether you drive with Claude or Codex.

## Claude Code: what the template does and what you fill in

The template is `claude-containment-settings.template.json`.

What it already does:
- **Posture: ask-by-default.** Anything not explicitly allowed or denied
  prompts you. Reads of your laptop's project directory are free.
- **ssh-to-your-server is in `ask`.** Every remote command prompts you.
- **Laptop deny walls:** destructive ops (`rm`, `mv`, `chmod`, `sudo`, ...),
  laptop network egress (`curl`, `wget`, `scp`, `rsync`, `nc`, `WebFetch`),
  reading secrets (`~/.ssh`, `/etc`, `~/.aws`, `.env`, `credentials`, `token`,
  `secret`) via both the Read tool and `cat`/`head`/`tail`, and editing its own
  config or your shell startup files.
- **Pre-allowed laptop read-only commands:** `ls`, `cat`, `head`, `tail`, `wc`,
  `pwd`, `echo`, `which`.

What you fill in:
1. **Your server, in the ssh ask-rule.** Replace `YOUR_USER@YOUR_SERVER` with
   the exact string you SSH with, e.g.
   `"Bash(ssh zrayw@psoriasis.sph.umich.edu:*)"`.
2. **A laptop working directory** if you keep local notes/scripts; `Read(./**)`
   already covers the launch directory.
3. **BRIER-MCP as a tool** if you want fitting/reporting in the same session;
   add an MCP rule such as `"mcp__brier"` under `ask` to approve each tool.

Install: put the filled-in file at `.claude/settings.json` in the laptop
directory you launch the agent from. A user-level deny backstop at
`~/.claude/settings.json` is worth adding so no project file can re-enable the
dangerous laptop operations; a user-level deny cannot be overridden by a
project-level allow.

## Codex: what the template does and what you fill in

The template is `codex-containment-settings.template.toml`.

What it sets:
- **`sandbox_mode = "workspace-write"`** with **`network_access = false`**:
  writes confined to the workspace, no network from the sandbox. This is the
  OS-enforced equivalent of Claude's write/egress deny walls.
- **`approval_policy = "on-request"`, `approvals_reviewer = "user"`:** Codex
  asks you before leaving the sandbox, and you review each request. This is the
  ask-by-default posture.
- **`read_deny` globs** for secrets (`.ssh`, `.aws`, `/etc`, `.env`,
  `credentials`, `secret`, `token`): the equivalent of Claude's Read deny list.
- **`[[rules]]` forbidding** destructive / exfiltration command prefixes
  (`rm`, `sudo`, `chmod`, `chown`, `dd`, `curl`, `wget`, `git push`).
- **An ssh rule set to `prompt`** so every remote command pauses for approval.

What you fill in:
1. **Your server, in the ssh rule.** Replace `ssh YOUR_USER@YOUR_SERVER` with
   your real ssh target so remote commands prompt you.
2. **A writable output directory** in `writable_roots` if your preprocessing
   writes outside the launch directory. Do NOT add raw data here; keep it
   read-only via the server-side chmod backstop.
3. **Trust the project directory** if you use a project-scoped
   `.codex/config.toml`; Codex ignores project config in untrusted directories.

Install: merge the keys into `~/.codex/config.toml` (global) or a project-scoped
`.codex/config.toml` in a trusted directory.

> Verification note: the Codex template's exact key names and table layout
> (notably `read_deny` and the `[[rules]]` schema) follow Codex's documented
> sandbox/approval model, but should be confirmed against your installed Codex
> version. Codex's config reference is the source of truth; run `codex` and
> check the profile takes effect (see the test below) before trusting it.

## Test it before trusting it (both clients)

In a throwaway session in the project dir on your laptop:

1. Ask it to read `~/.ssh/id_ed25519` (a laptop secret). Should be **denied**.
2. Ask it to `rm` a junk laptop file. Should be **denied** (Claude) or
   **forbidden** (Codex).
3. Ask it to run `curl https://example.com`. Should be **denied/blocked**.
4. Ask it to run `ssh you@server 'ls'`. Should **prompt** you, then run on the
   server after you approve.
5. Ask it to run `ssh someotherhost 'ls'`. It should also prompt (there is no
   rule that auto-denies other hosts). **Reject it** if unintended; the prompt
   is your protection.

If 1-3 are not blocked, stop and fix; the laptop walls are not holding.

## What this does NOT contain (honest limits, both clients)

- **Remote commands are not contained.** Laptop-side controls cannot restrict
  what runs inside `ssh server '...'`. Your approval is the gate. The
  read-only-raw-data backstop is the only enforced server-side layer, and you
  must set it up.
- **No auto-deny of ssh to other hosts.** The ask/prompt rule names your
  server; a command to a different host falls through to ask-by-default (it
  prompts, it is not silently denied). Reading the prompt is what stops an
  unintended host.
- **R's own network access is not blocked.** If a remote command runs R and
  that R code opens a network connection, these controls do not see it.
- **Desktop/IDE may differ from CLI.** The profiles are most reliable from the
  command line. If you use a desktop or IDE surface, re-run the tests above to
  confirm the controls take effect.
