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

| Path | Setup | LLM | Data | Best for | Docs |
|---|---|---|---|---|---|
| **Agent, external API** | no GPU + an API key (run directly, or Docker) | OpenAI / Together / any OpenAI-compatible | tool results transit the API; raw genotypes never leave | no GPU, or a stronger model | [Quick start](#quick-start), [DEPLOY.md](DEPLOY.md) |
| **Agent, Docker self-host** | NVIDIA GPU + Docker | Qwen 2.5-7B (vLLM, in-container) | 100% local; no external call | PHI, offline or restricted networks, paper experiments | [DEPLOY.md](DEPLOY.md) |
| **MCP + Claude/Codex, local** | R + BRIER + one setup script | your Claude or Codex subscription | file paths + summaries only | day-to-day use on your own machine | [mcp/docs/SETUP.md](mcp/docs/SETUP.md) |
| **MCP + Claude/Codex, remote** | same, on the server (over SSH) | your Claude or Codex subscription | data and compute stay on the server | data on an HPC or lab server | [mcp/docs/REMOTE.md](mcp/docs/REMOTE.md) |

## Quick start

The simplest way to run the full agent: no GPU, no Docker, a hosted OpenAI-compatible API
(OpenAI, Together, Groq, or any compatible endpoint).

**Prerequisites:** Python 3.10+, an API key, and R (>= 4.0) with the BRIER package installed.
Installing R and BRIER is a one-liner, covered in [`DEPLOY.md`](DEPLOY.md#installing-r-and-brier).

Install the Python dependencies into a virtual environment (on macOS a Homebrew Python is
"externally managed" and refuses a direct `pip install`; a venv or conda env avoids that, and
gives you plain `python` / `pip`):

```
python3 -m venv .venv --prompt brier-agent   # creates .venv, names the env (brier-agent)
source .venv/bin/activate                    # or: conda activate <your-env>
pip install -r requirements.txt              # one time
```

Reactivate the environment (`source .venv/bin/activate`) in each new terminal before
`./run_ui.sh`; your prompt shows `(brier-agent)` when it is active.

Put your external-API settings in a `.env.local` file (gitignored):

```
export BRIER_MODEL_ENDPOINT=https://api.openai.com/v1   # or Together, Groq, ...
export BRIER_MODEL_NAME=gpt-4o-mini
export BRIER_API_KEY=sk-...your-key...
```

Then start the chat UI:

```
./run_ui.sh                              # serves the chat UI on port 7860
```

Open <http://localhost:7860> in your browser and chat with the agent: describe your data and
what you want, and it drives the whole analysis and reports back.

`run_ui.sh` loads `.env.local` and starts the UI. `.env.local` is optional: you can instead
type the endpoint, model, and key into the UI's "Model & connection" panel, where a **Test
connection** button checks it. `./run_ui.sh --detach` runs it in the background. Verify the
environment before the first run with `python3 -m brier_agent.check_env` (also an **Environment
check** panel in the UI). Prefer the command line? `python3 -m brier_agent "your request"` runs
a one-off query with no UI.

With an external API the model sees tool results (variant ids, sample counts, summary
statistics, metrics), which go to the provider; the raw genotype matrices never leave the
machine. For a cohort whose summary statistics may not leave the premises, use the local-GPU
option below.

Full setup, the environment check, and troubleshooting are in [`DEPLOY.md`](DEPLOY.md).

## Try it with example data

Small synthetic demo datasets and ready-to-run prompts ship under [`examples/`](examples/),
derived from BRIER's own bundled example data. They cover the three common shapes: an
individual-level target with one external model, a summary-statistics target, and the
multi-source decision with several external models. Paste a prompt into the chat UI, or run
one from the command line:

```
python3 -m brier_agent "<paste a prompt from examples/README.md>"
```

The datasets, what each file is, and the three prompts are in
[`examples/README.md`](examples/README.md). The predictors are non-genetic, so the prompts
also show that path: no variant map is needed, and the agent derives the panel from the data.

## Repository layout

- `brier_agent/` - the agent: config, llm_client, mcp_client, tools, guardrails, the
  ReAct loop, the routing prompt, `check_env`.
- `app.py` - the Gradio chat UI. `python3 -m brier_agent` - the CLI. `run_ui.sh` - the UI
  launcher (loads `.env.local`).
- `mcp/` - the bundled BRIER-MCP server (self-contained; its own README): `server.py` +
  `r_scripts/*.R` + `install/` + `docs/`.
- `examples/` - synthetic demo data and example prompts (`make_demo_data.R` regenerates it).
- `Dockerfile`, `docker-compose.yml`, `.env.example` - the self-host package.

## Key documents

- [DEPLOY.md](DEPLOY.md) - installing R and BRIER, and every way to run the agent (Quick
  start / run directly, Docker external API, Docker local GPU), with the environment check.
- [examples/README.md](examples/README.md) - the bundled demo data and example prompts.
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
