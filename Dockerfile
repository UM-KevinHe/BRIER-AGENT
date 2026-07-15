# =============================================================================
# BRIER-Agent: the agent + the bundled BRIER-MCP server + R + BRIER, in ONE image.
#
# What is NOT here: the model. The agent reaches the LLM over an OpenAI-compatible
# endpoint, so the model runs EITHER as a separate local vLLM service (the privacy /
# air-gapped path, needs a GPU) OR as an external API (OpenAI, Together, ...). This
# image is identical for both; only BRIER_MODEL_ENDPOINT / BRIER_API_KEY /
# BRIER_MODEL_NAME change. See docker-compose.yml and DEPLOY.md.
#
# Why R is in the agent image: the agent spawns the MCP server as a stdio subprocess
# in-process, and the server shells out to Rscript. Agent, server, and R are one unit.
#
# Build:   docker build -t brier-agent .
# The BRIER package is pulled from GitHub at build time (it is not on CRAN). Pin a
# commit for a reproducible image:  --build-arg BRIER_REF=<sha>
# =============================================================================

# Pinned R on Debian. The model image is separate, so no CUDA here.
FROM rocker/r-ver:4.4.1

# --- OS + Python. The agent is Python; R is already in the base image. ------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        git \
        # BRIER and its Imports (data.table, Matrix, survival) compile against these
        libcurl4-openssl-dev libssl-dev libxml2-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# --- BRIER (from GitHub; not on CRAN). dependencies=TRUE pulls Matrix / data.table /
#     survival / jsonlite automatically. Override BRIER_REF to pin a commit. ---------
ARG BRIER_REF=main
RUN R -e "install.packages('remotes', repos='https://cloud.r-project.org')" \
    && R -e "remotes::install_github('UM-KevinHe/BRIER', ref='${BRIER_REF}', dependencies=TRUE, upgrade='never')" \
    && R -e "stopifnot('BRIER' %in% rownames(installed.packages()))"

# --- Python deps in an isolated venv (avoids the Debian 'externally-managed' block). -
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# --- The application. --------------------------------------------------------------
WORKDIR /app
COPY . /app

# Fail the build if the interpreter, R, or any REQUIRED package did not land. This runs
# the same preflight a user runs after a bare-metal install, so a broken image never
# ships. It exits 0 when only optional/recommended items are missing (no model needed).
RUN PYTHONPATH=/app python -m brier_agent.check_env

# The agent finds the MCP server and R here; the UI must bind outside localhost so the
# published port is reachable. Model settings are supplied at RUN time (compose / .env).
ENV BRIER_MCP_SERVER=/app/mcp/server.py \
    BRIER_RSCRIPT=Rscript \
    PYTHONPATH=/app \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860

EXPOSE 7860

# Default: the chat UI. For the CLI instead, override the command, e.g.
#   docker run --rm brier-agent python -m brier_agent "your request"
CMD ["python", "app.py"]
