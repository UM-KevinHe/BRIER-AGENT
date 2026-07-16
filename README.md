# BRIER-Agent

**Transfer learning for risk prediction from genetic and genomics data, driven by
natural language.**

[BRIER](https://github.com/UM-KevinHe/BRIER) integrates a target cohort with external
information (a pretrained model, GWAS summary statistics, or another cohort) and adaptively
determines how much to borrow. Its predictors can be SNP genotypes, gene-expression levels,
or proteins; the canonical use is cross-ancestry polygenic risk scoring.

BRIER-Agent is an agent layer over the package, built to run with a small open model
(Qwen 2.5-7B) so an analysis can run locally, even fully offline, where sending sensitive
genomic data to a frontier API is not an option. The raw data never leave the machine: the
model only ever sees tool results and R expressions, not your matrices.

BRIER-Agent has two parts. The **agent** is a ReAct loop that carries an analysis through
its stages: it **identifies** the right BRIER module for your data, **preprocesses** and
aligns the inputs, **fits** the transfer model, **evaluates and decides** (the integrated
transfer fit versus a no-transfer baseline versus each external used alone, including when
several external models are combined), and **reports** the result with a reproducible R
script. The bundled **BRIER-MCP server** exposes the BRIER R functions as the tools behind
each stage. The agent talks to a language model to plan the stages, and to the MCP server
to do the work; the server runs every fit through R on the machine where your data lives,
so the data never moves.

You can use it two ways. Run the **full agent** (a chat UI or a CLI) and it drives the
whole analysis for you, with its model either on a local GPU or behind an external API. Or,
if you already use Claude Desktop or OpenAI Codex, drive the **BRIER tools directly** from
that client, with your own subscription as the model, for data on your machine or on a
remote server over SSH.

| Path | Setup | LLM | Data | Best for |
|---|---|---|---|---|
| **Agent, Docker self-host** | NVIDIA GPU + Docker | Qwen 2.5-7B (vLLM, in-container) | 100% local; no external call | PHI, air-gapped networks, paper experiments |
| **Agent, external API** | Docker (no GPU) + an API key | OpenAI / Together / any OpenAI-compatible | tool results transit the API; raw genotypes never leave | no GPU, or a stronger model |
| **MCP + Claude/Codex, local** | R + BRIER + one setup script | your Claude or Codex subscription | file paths + summaries only | day-to-day use on your own machine |
| **MCP + Claude/Codex, remote** | same, on the server (over SSH) | your Claude or Codex subscription | data and compute stay on the server | data on an HPC or lab server |

Both agent paths below use Docker. If `docker` is not installed (`command not found`),
install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS, Windows,
Linux) or a lighter alternative ([OrbStack](https://orbstack.dev), or colima via Homebrew:
`brew install colima docker docker-compose && colima start`), then start the engine before
running `docker compose`. Prefer not to use Docker at all? The agent also runs natively;
see [`DEPLOY.md`](DEPLOY.md).

## Run the agent with a local model (Docker self-host)

For PHI workflows, networks that block outbound LLM calls, or anyone who wants the whole
stack on hardware they control. A local Qwen 2.5-7B (via vLLM) runs alongside the R
package and the Gradio UI; nothing leaves the machine.

**Prerequisites:**

- An NVIDIA GPU (a 7B model needs roughly 16 GB VRAM at full precision, less quantized)
  and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).
- Docker with Compose.

**Quick start:**

```
cp .env.example .env          # the local block is the default; no edits needed
docker compose --profile local up
```

Then open <http://localhost:7860>. The first start downloads the model weights (cached on
the host so a restart does not re-fetch them).

## Run the agent with an external API (no GPU)

For a machine without a GPU, or when you want a stronger model than a local 7B. The agent
reaches a hosted OpenAI-compatible endpoint (OpenAI, Together, Groq, a remote vLLM). The
agent code is identical to the self-host path; only where the model runs differs.

**Prerequisites:** Docker with Compose, and an API key.

**Quick start:**

```
cp .env.example .env          # fill the external-API block: endpoint, model name, key
docker compose up agent
```

Then open <http://localhost:7860>. With an external API, the model sees tool results
(variant ids, sample counts, summary statistics, metrics) and those go to the provider;
the raw genotype matrices never leave the machine. For a cohort whose summary statistics
may not leave the premises, use the self-host path above.

Both agent paths are covered in full, with the CLI and troubleshooting, in
[`DEPLOY.md`](DEPLOY.md). Before the first run, verify the environment:

```
python -m brier_agent.check_env
```

The UI has the same check as an **Environment check** panel, with a button to install the
optional R packages, alongside a **Test connection** button under **Model & connection**.

## Switching between backends

The agent talks to the model over an OpenAI-compatible endpoint, so the two backends are
interchangeable at run time: point it at whichever you want in the UI's "Model & connection"
panel, or via the `BRIER_MODEL_ENDPOINT` / `BRIER_MODEL_NAME` / `BRIER_API_KEY` variables.
The two are not symmetric in what they require, though. An external API needs no local
deployment: a key and internet, and it is reachable immediately. The local 7B must be
deployed first (the vLLM service on an NVIDIA GPU); once it is running you can switch to it,
or back to an external API, freely in either direction. Where there is no GPU (for example a
Mac) the local 7B cannot be deployed at all, so only external endpoints are available there.
The "Test connection" button in that panel reports whether the current endpoint is reachable
and which model it serves.

The local 7B endpoint depends on where the agent runs relative to vLLM:

| How you run it | Local 7B endpoint |
|---|---|
| Docker Compose (`docker compose --profile local up`, both containers) | `http://vllm:8000/v1` |
| Agent natively, vLLM on the same host | `http://localhost:8000/v1` |
| vLLM on a remote host | `http://<host>:8000/v1` |

The model name is `Qwen/Qwen2.5-7B-Instruct`, and the key can be any value (vLLM ignores
it). Inside Compose the agent reaches the vLLM container by its service name (`vllm`); from
the host or a native run you use `localhost` because Compose publishes the port. The
endpoint field does **not** auto-detect a running vLLM: it shows whatever
`BRIER_MODEL_ENDPOINT` was set to when the app started. So with `docker compose --profile
local up` and no override in `.env`, it is pre-filled with the vLLM endpoint; but if `.env`
points at an external API, that value wins and you switch by editing the field (or setting
`BRIER_MODEL_ENDPOINT` before launch).

## Use the BRIER tools in Claude or Codex (local data)

If you already use Claude Desktop, Claude Code, or OpenAI Codex, you can drive the BRIER
tools directly from it, with your own subscription as the model. This is the `brier-local`
deployment shape: data, R, and BRIER on the machine you run the client on; only file paths
and analysis summaries transit the chat.

**Prerequisites:**

- R (>= 4.0) with the BRIER package installed (see [Installation](#installation)).
- Claude Desktop / Claude Code, or OpenAI Codex. Python and `uv` are handled by the setup
  script.

**Install:**

```
cd mcp
./install/setup.sh --client claude     # or: --client codex
```

The script verifies R and BRIER, then prints a ready-to-register config block with the
correct paths for your machine. Register it, reconnect the client, and the BRIER tools are
available. Full per-client configuration is in [`mcp/docs/SETUP.md`](mcp/docs/SETUP.md).

## Use the BRIER tools on a remote server (over SSH)

When your data, R, and BRIER live on a server (an HPC cluster, a lab machine) rather than
your laptop. This is the `brier-remote` shape: the client launches the server over an
SSH-wrapped stdio tunnel, it runs next to the data, and only summaries return. The SSH
mechanics (keepalives, absolute paths, preflight) are in
[`mcp/docs/REMOTE.md`](mcp/docs/REMOTE.md); registration is the same
[`mcp/docs/SETUP.md`](mcp/docs/SETUP.md) as above.

## Installation

BRIER-Agent drives the BRIER R package, which you install from GitHub (it is not on CRAN):

```r
# Development version from GitHub
remotes::install_github("UM-KevinHe/BRIER")
```

Requires R >= 4.0. The Docker self-host and external-API paths install BRIER for you
inside the image; you only need R and BRIER on the host for the MCP-with-Claude/Codex
paths.

## Repository layout

- `brier_agent/` - the agent: config, llm_client, mcp_client, tools, guardrails, the
  ReAct loop, the routing prompt, `check_env`.
- `app.py` - the Gradio chat UI. `python -m brier_agent` - the CLI.
- `mcp/` - the bundled BRIER-MCP server (self-contained; its own README): `server.py` +
  `r_scripts/*.R` + `install/` + `docs/`.
- `Dockerfile`, `docker-compose.yml`, `.env.example` - the self-host package.

## Key documents

- [DEPLOY.md](DEPLOY.md) - the two agent backends (Docker self-host and external API), the
  CLI, and the environment check.
- [mcp/README.md](mcp/README.md) - the bundled BRIER-MCP server: the tool set, install,
  and standalone use with Claude or Codex.
- [mcp/docs/SETUP.md](mcp/docs/SETUP.md) / [mcp/docs/REMOTE.md](mcp/docs/REMOTE.md) - MCP
  client configuration (local) and the remote-over-SSH deployment.
- [mcp/docs/AGENTS.md](mcp/docs/AGENTS.md) - the BRIER workflow guide the model reads.

## Status

The agent runs the full evaluation suite (individual, summary, and multi-source transfer
cases, plus preprocessing cases) end to end on the real 7B, with every awarded point
provable from the persisted object or a re-executed reproduce script rather than from
prose. Known limits, stated plainly: the evaluation is entirely gaussian (height), so the
binomial and poisson paths are implemented but unmeasured on a real model; and the
`BRIERfull` pooled fit on large cohorts is compute-heavy and deferred.

## Tests

```
./test.sh
```

Pure logic, no model and no network: the R helpers, the aligner, the prepared-object
contract, the guardrails, the loop hooks, and the environment check. Some R tests skip
cleanly when BRIER or a reference dataset is absent.

## Getting help

Please report issues or unexpected behavior on the GitHub repository, or contact:

- Ruiwen Zhou - <zrayw@umich.edu>
- Kevin He (Kevin He Lab, University of Michigan) - <kevinhe@umich.edu>

For the underlying BRIER method and R package, see
[UM-KevinHe/BRIER](https://github.com/UM-KevinHe/BRIER).
