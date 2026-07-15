# BRIER MCP

BRIER MCP exposes the BRIER R package, transfer-learning genetic risk
prediction (PRS), as a set of MCP tools that an AI client can call. You describe
your data and question; the agent inspects the data, picks the right BRIER
variant, prepares fit-ready inputs, runs the fit through your local R, and
explains the result. Computation happens on the machine where your data lives;
only summaries return to the client.

This folder is a self-contained component. It bundles the BRIER MCP server
inside BRIER-Agent, and it is also the same server you can run standalone with
Claude or Codex. Everything needed to install, configure, and run it is here.

## What is in here

- `server.py`: the MCP server entry point (stdio transport).
- `r_scripts/`: the BRIER tool implementations (one R script per tool, plus
  `_common.R`).
- `install/`: install and setup scripts (shared core plus per-client helpers).
- `tests/`: the server's test suite.
- `manifest.json`, `pyproject.toml`, `uv.lock`: packaging and dependencies.
- `docs/`: the documentation below.

## What the tools cover

The server exposes 31 tools spanning the full workflow. The main groups:

- **Guidance:** `get_workflow_guide` returns the complete phase-by-phase
  analysis guide (the same content as `docs/AGENTS.md`), so a connected client
  has the workflow without any file being copied into a project. A compact
  version of this guidance is also served automatically in the server's
  initialization instructions.
- **Inspection:** `inspect_data` (R objects: `.rds`/`.rda`/`.RData`),
  `inspect_user_data` (tabular and text, including `.csv`/`.tsv`/`.txt` and
  their gzipped `.gz` forms, plus genotype binaries via companion files), and
  `list_data_directory`.
- **Preprocessing:** `prep_auto` assembles fit-ready inputs in one call. It aligns
  the external to the target's variant panel itself (matching, allele-flip and
  strand-ambiguity handling, `corr` derivation, impute-0 alignment), merges
  multiple externals via `mergeExternals`, and adds the numeric steps around them
  (subset to surviving variants, conditional standardization, the BRIERi intercept
  row, LD construction from a reference panel, validation/test alignment). Its
  aligner is verified bitwise against BRIER's own `preprocessI` / `preprocessS`,
  which the standalone `preprocess_i` / `preprocess_s` tools still expose directly;
  `prep_data` offers composable, leakage-aware operations for custom wrangling.
- **Fitting:** `brier_i` (individual-level target + pretrained external
  coefficients), `brier_full` (pooled individual-level cohorts), and `brier_s`
  (summary-statistics target + an LD matrix), plus `cal_ld` / `get_ldb` for LD
  construction.
- **Tuning, prediction, evaluation:** the `*_selection` tools,
  `brier_auto_tune_eta`, `brier_predict`, and `brier_evaluate`.
- **Reporting and plots:** `summarize_fit`, `brier_plot_*`.
- **Wizard and config:** `start_analysis`, `set_output_directory` /
  `get_output_directory`.

The full list with one-line descriptions is in the header of `server.py`.

## Supported clients

Two clients are supported as equals, both driving the same stdio server:

- **Claude** (Claude Desktop, Claude Code)
- **OpenAI Codex** (CLI and IDE extension)

The server is identical for both; only the configuration format differs (Claude
uses JSON, Codex uses TOML). The ChatGPT web app is not supported (it needs a
remote HTTPS server; this is a local stdio server by design).

Both clients receive the workflow guidance automatically: the compact form in
the server's initialization instructions, and the full guide on demand via
`get_workflow_guide`. For Claude Code specifically, a `docs/CLAUDE.md` shim
(importing `AGENTS.md`) also auto-loads the full guide at session start. Copying
`AGENTS.md` into a project is therefore optional, only needed to customize the
per-project notes at the bottom of that file.

## Quick start

You need R (>= 4.0) with the BRIER R package installed, plus `uv` (the setup
script installs `uv` if it is missing). Then:

```bash
# install dependencies and verify the environment
./install/setup.sh --client claude     # or: --client codex
```

`setup.sh` ensures `uv`, runs `uv sync`, verifies R and the BRIER package via
`--selfcheck`, and prints a ready-to-use config block for your client with the
correct absolute paths for this machine. Register that block (see SETUP.md),
restart or reconnect the client, and the BRIER tools are available.

For the deployment shapes (`brier-local` for data on this machine,
`brier-remote` for data on an SSH-reached server) and the full per-client
configuration, see `docs/SETUP.md`.

## Documentation

- `docs/SETUP.md`: install and per-client configuration (Claude and Codex),
  the `brier-local` / `brier-remote` naming convention, and the Codex gotchas.
- `docs/REMOTE.md`: running the server on a remote server over SSH (the SSH
  mechanics, keepalives, absolute paths).
- `docs/CONTAINMENT.md`: containment profiles for safely running an agent
  against your data, for both Claude and Codex, plus the templates
  `claude-containment-settings.template.json` and
  `codex-containment-settings.template.toml`.
- `docs/AGENTS.md`: the full working guide for the staged analysis workflow
  (inspect, preprocess, fit, decide, report) with a baseline-comparison
  decision layer. This is the content the `get_workflow_guide` tool serves; it
  is also the cross-tool convention file that Codex reads automatically. Copy it
  into a project only to customize its per-project notes.
- `docs/CLAUDE.md`: a one-line shim that imports `AGENTS.md`, so Claude Code
  auto-loads the same guide at session start.

## Self-check

To verify an install at any time:

```bash
uv run server.py --selfcheck
```

It reports the BRIER MCP version, whether Rscript and the BRIER package are
found and loadable, and whether the cache directory is writable.

## Note for readers inside BRIER-Agent

This component is bundled into BRIER-Agent and carries its own version (the MCP
server version), which is distinct from the BRIER-Agent product version.
BRIER-Agent's own documentation (the access paths, the agent layer, and
deployment) lives at the top level of the BRIER-Agent repository, not here.
