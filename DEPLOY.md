# Deploying BRIER-Agent

BRIER-Agent is the agent plus the bundled BRIER-MCP server, R, and the BRIER package. The
model is separate: you choose where it lives (a hosted API, or a local GPU), and that choice,
together with whether you run in Docker or directly on the host, is the whole difference
between the options below. The tools, the analysis, and the correctness guarantees are
identical across all of them.

- [Option A: external API (no GPU)](#option-a-external-api-no-gpu) - Docker, a hosted model.
- [Option B: local model (privacy / offline)](#option-b-local-model-privacy--offline-needs-a-gpu) - Docker + an NVIDIA GPU, model in-container.
- [Option C: run directly, without Docker](#option-c-run-directly-without-docker) - the Quick
  start path (`run_ui.sh`), a hosted model, R and BRIER on the host.

## Prerequisites

- For Options A and B (Docker): Docker with Compose. Option B also needs an NVIDIA GPU and
  the NVIDIA Container Toolkit.
- For Option C (run directly): Python 3.10+, and R with BRIER on the host (below). Docker is
  not used.
- Your data in a directory the agent can read (mounted read-only at `/data` in the Docker
  options). Genotype/text/`.gz` inputs; see the case docs for the expected roles.

## Installing R and BRIER

The Docker options (A and B) install R and BRIER for you inside the image; you only need
them on the host for Option C (run directly) and for the MCP-with-Claude/Codex paths. BRIER
is on GitHub, not CRAN:

```r
# in R (>= 4.0)
install.packages("remotes")
remotes::install_github("UM-KevinHe/BRIER")
```

Then confirm everything the tools need is present:

```
python3 -m brier_agent.check_env
```

## Check the environment

The image build runs a preflight and fails if anything required is missing, so a broken
image never ships. To run the same check yourself (inside the container, or on a bare-metal
install where you provide Python and R directly):

```
python3 -m brier_agent.check_env
```

It verifies the Python interpreter and packages, `Rscript`, the R packages the tools load
(BRIER, Matrix, jsonlite, survival), and the bundled server file. It exits non-zero only
when a REQUIRED item is missing; a missing recommended package (`data.table`, `ggplot2`) or
optional one (xlsx / PLINK readers) is a warning, not a failure. It does not contact the
model, so it passes with no endpoint configured.

## Option A: external API (no GPU)

Use a hosted OpenAI-compatible endpoint (OpenAI, Together, Groq, a remote vLLM). This is
the path for a machine without a GPU, or when you want a stronger model than a local 7B.

```
cp .env.example .env
# in .env, fill the (B) EXTERNAL API block: endpoint, model name, API key
docker compose up agent
```

Open http://localhost:7860.

**Which provider.** Any OpenAI-compatible endpoint works; it is three env vars
(`BRIER_MODEL_ENDPOINT`, `BRIER_MODEL_NAME`, `BRIER_API_KEY`), no code change:

- **OpenAI** -- `https://api.openai.com/v1`, e.g. `gpt-4o-mini`.
- **Together / Groq / a remote vLLM** -- their base URL + an open-weight model id.
- **Anthropic (Claude)** -- Anthropic ships an OpenAI-SDK compatibility layer, so Claude
  works too: `BRIER_MODEL_ENDPOINT=https://api.anthropic.com/v1/`,
  `BRIER_MODEL_NAME=claude-opus-4-8` (or `claude-sonnet-5`, ...), `BRIER_API_KEY=<anthropic key>`.
  Tool calling is supported, so the agent's loop runs unchanged.
- **OpenRouter** -- one key + endpoint (`https://openrouter.ai/api/v1`) proxies all of the
  above; select with the slug (`openai/gpt-4o-mini`, `anthropic/claude-opus-4.8`,
  `qwen/qwen-2.5-72b-instruct`, ...).

A stronger driver (Claude, GPT-4o, a 70B+ open model) makes routing and the decision workflow
more reliable than the local 7B; the trade-off is that tool results leave the machine (below).

What leaves the machine: the model sees tool RESULTS (variant ids, sample counts, summary
statistics, metrics) and R expressions, and those go to the API provider. The raw genotype
matrices never leave: the agent only ever sees tool outputs, not your data. For most users
that is fine. For a cohort whose summary statistics may not leave the premises, use Option
B.

## Option B: local model (privacy / offline, needs a GPU)

A vLLM service serves the model on an OpenAI-compatible endpoint inside the compose
network, and the agent points at it. Nothing leaves the machine. The default is a
one-click **Qwen 2.5-7B**, the model BRIER-Agent was developed and validated on and small
enough for a single modest GPU; with more GPU you can serve a larger open-weight model
instead (see "Serving a different open-weight model" below).

```
cp .env.example .env          # the (A) LOCAL block is the default; no edits needed
docker compose --profile local up
```

The first start downloads the model weights (cached on the host at `HF_CACHE_DIR` so a
restart does not re-fetch them), then the UI comes up at http://localhost:7860.

**Pointing the agent at the local 7B.** With this profile the agent's endpoint defaults to
`http://vllm:8000/v1` (the agent container reaches the vLLM container by its Compose service
name `vllm`), model `Qwen/Qwen2.5-7B-Instruct`, and any API key (vLLM ignores it). The app
does not auto-detect vLLM; it reads `BRIER_MODEL_ENDPOINT` at startup. So this is pre-filled
only when you do not override that variable in `.env`. If your `.env` points at an external
API, that value wins even with vLLM running: switch by editing the endpoint in the UI's
"Model & connection" panel (and clicking "Test connection"), or set
`BRIER_MODEL_ENDPOINT=http://vllm:8000/v1` before launch. If you run the agent directly
(no Docker, `python3 app.py`) rather than in Compose, use `http://localhost:8000/v1` instead
(Compose publishes port 8000 to the host).

**Serving a different open-weight model.** `BRIER_MODEL_NAME` sets both the model vLLM
serves (its `--model`) and the model the agent requests, so switching is one variable: set
`BRIER_MODEL_NAME=<hf-org/model-id>` in `.env` to any open-weight model your GPU can hold
(for example a larger Qwen or Llama instruct model). The weights download on first start and
are cached. The only requirement is native tool (function) calling, since the agent loop
dispatches tool calls; a model without it will not drive the tools. Qwen 2.5-7B is the
default because it is the smallest model that runs the workflow well and is the one the agent
was validated on.

## Option C: run directly, without Docker

If you do not have Docker, or already have R and the BRIER package on the machine, run the
agent directly. This uses an external API for the model (a local 7B still needs a GPU and
vLLM), so it suits a laptop without a GPU.

Prerequisites: Python 3.10+, and R (>= 4.0) with the BRIER package installed
(`remotes::install_github("UM-KevinHe/BRIER")`).

Install into a virtual environment or a conda env, not the system Python. On macOS a
Homebrew Python is "externally managed" (PEP 668) and refuses a direct `pip install`; a venv
avoids that and gives you plain `python` / `pip`:

```
python3 -m venv .venv --prompt brier-agent   # names the env (brier-agent)
source .venv/bin/activate                    # or: conda activate <your-env>
```

```
python3 -m pip install -r requirements.txt   # ONE TIME, in this environment
python3 -m brier_agent.check_env             # confirm Python, R, BRIER, and deps are present

export BRIER_MODEL_ENDPOINT=https://api.openai.com/v1   # or Together, Groq, a remote vLLM
export BRIER_MODEL_NAME=gpt-4o-mini
export BRIER_API_KEY=sk-...your-key...
export BRIER_MCP_SERVER=$PWD/mcp/server.py

python3 app.py                            # the UI at http://localhost:7860
# or a one-shot query:
python3 -m brier_agent "your request here"
```

**Launcher: `./run_ui.sh`.** Instead of exporting the model variables each time, put them in
`.env.local` (gitignored) once and use the wrapper, which sources it and starts the UI. The
env file is OPTIONAL: `run_ui.sh` falls back to `.env`, then to whatever is already exported,
and if nothing is set the UI still starts, you just enter the endpoint, model, and key in its
"Model & connection" panel.

```
python3 -m pip install -r requirements.txt   # ONE TIME per environment
./run_ui.sh                                  # foreground; Ctrl-C to stop
./run_ui.sh --detach                         # background; survives closing the terminal
```

The install is one time per environment; `run_ui.sh` only STARTS the UI (it stays up on
<http://localhost:7860> until you stop it, so you do not rerun it while it is running).
`app.py` does not auto-load `.env.local`, so the wrapper sources it for you. Detached mode
logs to `/tmp/brier_ui.log` (override with `BRIER_UI_LOG`); stop it with `pkill -f app.py`.
Override the interpreter with `PYTHON=...` if `python3` is not your env's name.

**Double-click launcher (macOS): `BRIER-Agent.command`.** For a non-technical user, this is
the whole of Option C in one step: double-click it in Finder and it creates the virtual
environment, installs the Python dependencies on the first run, checks R + BRIER, starts the
UI, and opens the browser. Put the model settings in `.env.local` first, and install R +
BRIER once (above). The first time, macOS may say it "cannot verify the developer":
right-click -> Open -> Open, once. It does not install R, Docker, or a GPU model: it only
removes the terminal step.

## The CLI instead of the UI

The same image runs a one-shot command-line query:

```
docker compose run --rm agent python3 -m brier_agent "your request here"
```

## Notes

- BRIER is pulled from GitHub at build time (it is not on CRAN). For a reproducible image,
  pin a commit: `BRIER_REF=<sha> docker compose build`.
- `.env` holds your API key. It is gitignored; never commit it.
- The agent and the model are decoupled by design: switching from a local 7B to an
  external frontier model is three environment variables, not a rebuild.
- Tool surface: the UI exposes a validated core set of ~11 tools by default. Tool schemas are
  re-sent every turn, and the full surface (~31 tools) overflows a small model's context
  before the analysis starts. With a large-context model you can expose everything:
  `BRIER_INCLUDE_TOOLS=all` (or a comma-separated list of tool names).
- The HTML report and `reproduce.R` are written by the harness after a fit, from the recorded
  selection, rather than by the model: a small model builds that call unreliably. They land in
  `~/.cache/brier-mcp/reports/` and the UI shows the paths.
