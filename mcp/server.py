"""
BRIER MCP server.

FastMCP server that bridges Claude Desktop (or any MCP client) to the
BRIER R package. Each tool shells out to Rscript via a temp JSON file;
no statistical logic lives in this module.

Current tool surface (v2.4.0):
  * start_analysis         - guided data-first wizard.
  * get_workflow_guide     - return the full phase-by-phase BRIER workflow guide (AGENTS.md).
  * inspect_data           - peek at the structure of a local .rda/.RData/.rds file.
  * inspect_user_data      - heuristic inspection; R objects, tabular
                             (.csv/.tsv/.txt/.xlsx), and genotype binaries
                             (.pgen/.bed/.bgen via companion files).
  * list_data_directory    - list R-object, tabular, and genotype-binary files.
  * brier_i                - fit BRIERi() (pretrained external + individual target).
  * brier_i_cv             - cross-validation tuning for BRIERi.
  * brier_i_selection      - IC- or validation-set selection on a cached brier_i fit.
  * brier_full             - fit BRIERfull() (pooled-cohort integration, raw external data).
  * brier_full_selection   - validation-set selection on a cached brier_full fit.
  * brier_s                - fit BRIERs() (summary-statistics target).
  * brier_s_selection      - IC- or validation-set selection on a cached brier_s fit.
  * brier_auto_tune_eta    - auto-escalate eta-ceiling on boundary, or de-escalate on low optimum.
  * get_ldb                - return Berisa-Pickrell LD block coordinates.
  * cal_ld                 - build an LD matrix from a reference panel.
  * brier_predict          - predict from any cached BRIER fit or selection.
  * brier_evaluate         - score any cached BRIER fit on a new (X, y) pair.
  * score_external_prs      - score a raw external coef vector directly on (X, y);
                             the external-only comparator, no fitting.
  * brier_plot_eta         - validation criterion vs eta; auto-heatmap for M=2.
  * brier_plot_box         - bootstrap performance comparison (target / external / integrated).
  * brier_plot_importance  - bootstrap variable importance bar plot.
  * brier_plot_selection   - SELECTION criterion vs eta from cached selection (no test data needed).
  * summarize_fit          - comprehensive HTML report + reproduce.R script.
  * generate_reproduce_script - emit a runnable R script replaying a recorded
                             tool sequence (threads ids/paths) to reproduce numbers.
  * preprocess_i           - align target SNP info with external coefficient tables.
  * preprocess_s           - align target sumstats + LD + external coefs.
  * prep_auto              - one-call fit-ready assembly: delegates alignment to
                             preprocessI/S/mergeExternals, adds subset, conditional
                             standardization, intercept row, and val/test handling.
  * prep_data              - v0.13: composable, leakage-aware data prep (9 operations).
  * prep_data_log          - v0.13: read the audit log for a prep session.
  * set_output_directory   - configure where prediction CSVs / outputs land.
  * get_output_directory   - retrieve the current output directory setting.

Architecture invariants - DO NOT change without re-verifying in Claude
Desktop:

  1. subprocess.run(..., stdin=subprocess.DEVNULL) on every Rscript
     call. Without this, Rscript inherits Claude Desktop's never-writing
     stdin (which is bound to the MCP stdio channel under the desktop
     app) and can stall on TTY probes (readline) during startup.

  2. Rscript flags must be `--no-save --no-restore --no-init-file`.
     `--vanilla` would be the natural choice but also implies
     `--no-environ`, which suppresses R_LIBS_USER. When required packages
     (jsonlite, BRIER) are installed only in the user library (the
     default on Windows for install.packages() without admin rights),
     `--vanilla` makes them undiscoverable.

  3. Rscript discovery falls back to well-known install locations
     because Claude Desktop subprocesses do not inherit the user's
     interactive shell PATH on macOS. An Rscript that "works in
     Terminal" may not be found by which() inside Claude Desktop.

Run for local debugging (outside Claude Desktop):
    uv run server.py
"""

from __future__ import annotations

import base64
import datetime
import html
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Server version. Keep in sync with manifest.json and pyproject.toml.
__version__ = "2.4.0"

# --------------------------------------------------------------------------
# Paths and Rscript discovery
# --------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
R_SCRIPTS = SCRIPT_DIR / "r_scripts"


def _find_rscript() -> str:
    """Locate an Rscript executable.

    Resolution order:
      1. $BRIER_RSCRIPT (explicit override from manifest user_config)
      2. "Rscript" / "Rscript.exe" on PATH
      3. Windows: highest-versioned R-x.y.z under C:\\Program Files\\R\\
         (and the x86 sibling)
      4. macOS / Linux: well-known install locations (CRAN framework,
         /usr/local/bin, Homebrew, distro defaults)
    """
    override = os.environ.get("BRIER_RSCRIPT")
    if override and Path(override).exists():
        return override

    on_path = shutil.which("Rscript") or shutil.which("Rscript.exe")
    if on_path:
        return on_path

    for base in (Path(r"C:\Program Files\R"), Path(r"C:\Program Files (x86)\R")):
        if not base.exists():
            continue
        candidates = sorted(
            (d for d in base.iterdir() if d.is_dir() and d.name.startswith("R-")),
            key=lambda d: d.name,
            reverse=True,
        )
        for d in candidates:
            exe = d / "bin" / "Rscript.exe"
            if exe.exists():
                return str(exe)

    for cand in (
        Path("/Library/Frameworks/R.framework/Resources/bin/Rscript"),
        Path("/usr/local/bin/Rscript"),
        Path("/opt/homebrew/bin/Rscript"),
        Path("/usr/bin/Rscript"),
    ):
        if cand.exists():
            return str(cand)

    raise FileNotFoundError(
        "Rscript not found. Install R (https://cran.r-project.org/), or set "
        "the BRIER_RSCRIPT environment variable to the full path of Rscript "
        "(or Rscript.exe on Windows)."
    )


# --------------------------------------------------------------------------
# Expression-string deny-list (soft sandbox)
# --------------------------------------------------------------------------

# Pre-flight check before any payload that contains an R expression string
# (e.g. `X_expr`, `y_expr`) gets written to the JSON handshake.
#
# This is NOT real security - the user is running this on their own machine
# against their own data, with the same privileges as if they had typed
# the expression into the R console themselves. The deny-list is a smoke
# detector: it catches obviously-malicious patterns and obviously-weird
# composition an AI model might emit in error.
#
# Legitimate patterns that PASS this filter:
#   X                              (bare name)
#   data$X                         ($ access)
#   external_models[[1]]$beta      ([[ ]] + $)
#   target.info[, c("CHR", "BP")]  (subsetting)
#   as.matrix(data$X)              (coercion - parens are allowed; only
#                                   specific *function names* are blocked)
#
# Blocked patterns:
#   system("rm -rf /")
#   eval(parse(text = ...))
#   source("/etc/passwd")
#   `evil_call`(args)
#   foo(); bar()                   (multiple statements)
DENY_PATTERNS = (
    "system(",
    "system2(",
    "shell(",
    "shell.exec(",
    "unlink(",
    "file.remove(",
    "file.rename(",
    "file.create(",
    "file.copy(",
    "eval(",
    "parse(",
    "source(",
    "Sys.setenv(",
    "Sys.unsetenv(",
    "do.call(",
    "::",
    ":::",
    "`",
    ";",
)


# Safe namespace prefixes that are allowed even though `::` is otherwise
# blocked. These map to packages whose exported functions are documented
# and benign for the kinds of expressions we run (data shaping, model
# fitting, basic math). Anything outside this list still gets blocked.
SAFE_NAMESPACE_PREFIXES = (
    "BRIER::",
    "base::",
    "stats::",
    "utils::",
    "Matrix::",
)


def _expr_uses_only_safe_namespaces(expr_str: str) -> bool:
    """True iff every occurrence of '::' in expr_str is preceded by a
    safe namespace prefix.

    Triple-colons ':::' (access to non-exported functions) are NEVER safe
    and always blocked.
    """
    if ":::" in expr_str:
        return False
    if "::" not in expr_str:
        return True
    # Walk through every occurrence of '::' and confirm the preceding
    # token is in the safe list.
    remaining = expr_str
    while "::" in remaining:
        idx = remaining.index("::")
        # Find the start of the namespace identifier (alpha/underscore/.)
        i = idx - 1
        while i >= 0 and (remaining[i].isalnum() or remaining[i] in "._"):
            i -= 1
        ns = remaining[i + 1:idx + 2]  # includes the "::"
        if ns not in SAFE_NAMESPACE_PREFIXES:
            return False
        remaining = remaining[idx + 2:]
    return True


def _validate_expr(expr_str: Optional[str], param_name: str) -> Optional[str]:
    """Return None if expr_str is safe (or absent), else an error message.

    Validation is by substring deny-list, not parsing. Tradeoff: simple to
    audit, can produce false positives if a legitimate column name happens
    to contain a deny-listed substring (e.g. a column literally named
    'system_id'). Such names should be accessed via [["system_id"]] which
    contains '"' before 'system' and so passes; or renamed.

    Special case for '::' (namespace access): the v0.8.0 allow-list lets
    expressions use a small set of safe namespaces (BRIER::, base::,
    stats::, utils::, Matrix::). Triple-colons ':::' are always blocked.
    """
    if expr_str is None or not expr_str.strip():
        return None
    for pat in DENY_PATTERNS:
        if pat in expr_str:
            # Special case: '::' is conditionally allowed for safe namespaces
            if pat == "::" and _expr_uses_only_safe_namespaces(expr_str):
                continue
            return (
                f"Refusing to evaluate {param_name!r}: contains disallowed "
                f"pattern {pat!r}. Expression strings must be simple "
                f"accessor expressions like `X`, `data$X`, or "
                f"`external[[1]]$beta`. Function calls to system, "
                f"file-manipulation, multi-statement expressions, and "
                f"namespace access outside {SAFE_NAMESPACE_PREFIXES} "
                f"are blocked."
            )
    return None


# --------------------------------------------------------------------------
# Rscript bridge
# --------------------------------------------------------------------------

# prep_auto's cap is raised well above the generic 600s because prep_auto may FIT
# a model, not just reshape data: a RAW external (Bucket B) is fit inside the same
# call, and a penalized fit on a 20k x 10k genotype matrix takes ~7 min (a
# two-external case fits two of them, plus an LD build). Env-overridable; a caller
# can also pass timeout_s explicitly.
#
# This MUST stay BELOW the client's per-call transport cap
# (mcp_client._DEFAULT_CALL_TIMEOUT_S, 2100s), so an over-long R step fails HERE
# with a clean "Rscript timed out after Ns" message instead of the transport
# dying first and surfacing a masked TaskGroup/ExceptionGroup error.
# Nesting: Rscript (1800) < MCP transport (2100) < benchmark per-case (2700).
#
# Deliberately NOT a tool parameter. It was briefly exposed as prep_auto(timeout_s=),
# and the 7B promptly set it to nonsense -- 3600 in one run, 0 in the next, which
# made every prep_auto die instantly with "Rscript timed out after 0s". A wall-clock
# cap is an OPERATOR knob with no modelling meaning, so the model must not be able to
# reach it; operators override it with BRIER_MCP_PREP_TIMEOUT.
_PREP_AUTO_TIMEOUT_S = int(os.environ.get("BRIER_MCP_PREP_TIMEOUT", "1800"))


def _run_r(script_name: str, payload: dict,
            timeout_s: Optional[int] = 600) -> dict:
    """Invoke an R script via a temp-file JSON handshake.

    Writes `payload` to a temp input.json, runs
        Rscript --no-save --no-restore --no-init-file <script> <in.json> <out.json>
    and returns the parsed contents of output.json. If the R script fails
    to produce output, returns a structured {status:"error",...} dict so
    the MCP tool never throws.

    timeout_s: wall-clock cap in seconds. Pass None to disable the cap
    entirely (the subprocess will run as long as it needs). The default
    600s is a safe upper bound for most fits; long-running bootstrap
    plots on large p may want None.

    Two non-obvious flag choices:

      * --no-save --no-restore --no-init-file (NOT --vanilla). `--vanilla`
        also implies `--no-environ`, which suppresses R_LIBS_USER. When
        required packages (jsonlite, BRIER) are installed only in the
        user library (default on Windows via install.packages()), R
        started with --vanilla cannot find them.

      * stdin=subprocess.DEVNULL. Without this, the Rscript child can
        inherit a parent stdin bound to the MCP stdio channel under
        Claude Desktop. R's startup may then stall waiting for input.
    """
    script_path = R_SCRIPTS / script_name
    if not script_path.exists():
        return {
            "status": "error",
            "message": f"R script not found: {script_path}",
            "class": "FileNotFoundError",
            "where": "server.py:_run_r",
        }

    try:
        rscript = _find_rscript()
    except FileNotFoundError as e:
        return {
            "status": "error",
            "message": str(e),
            "class": "FileNotFoundError",
            "where": "server.py:_find_rscript",
        }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".in.json", delete=False, encoding="utf-8"
    ) as fin:
        json.dump(payload, fin, ensure_ascii=False)
        in_path = fin.name
    out_path = in_path.replace(".in.json", ".out.json")

    try:
        cmd = [rscript, "--no-save", "--no-restore", "--no-init-file",
               str(script_path), in_path, out_path]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )

        if Path(out_path).exists():
            try:
                with open(out_path, encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError as e:
                return {
                    "status": "error",
                    "message": f"R output was not valid JSON: {e}",
                    "class": "JSONDecodeError",
                    "where": script_name,
                    "stderr": proc.stderr.strip()[:2000],
                }

        return {
            "status": "error",
            "message": (proc.stderr.strip() or
                        "Rscript exited without producing output"),
            "class": "RscriptCrash",
            "where": script_name,
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip()[:2000],
        }

    except subprocess.TimeoutExpired:
        msg = (f"Rscript timed out after {timeout_s}s"
                if timeout_s is not None
                else "Rscript timed out")
        return {
            "status": "error",
            "message": msg,
            "class": "TimeoutExpired",
            "where": script_name,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"{type(e).__name__}: {e}",
            "class": type(e).__name__,
            "where": f"server.py:_run_r -> {script_name}",
        }
    finally:
        for p in (in_path, out_path):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Post-call hint helpers (placeholder - will be filled in as model tools
# are added in subsequent versions)
#
# Pattern: inject `_notice_*` / `_followup_*` fields into successful tool
# returns so reminders land at reply-composition time rather than
# depending on docstrings alone. Particularly useful for surfacing
# BRIER's silent-failure traps (family default, BRIERs standardization,
# multi.method=ind slowdown, etc.).
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------

# Server-level instructions. Exposed in the MCP initialization response and
# read by clients (notably OpenAI Codex, which uses it as server-wide guidance
# and prioritizes the first 512 characters) to route tool selection. Kept
# self-contained so a client deciding which tool to call has the essentials
# (inspect first; module routing; no-phenotype implies summary-statistics)
# without needing the per-tool docstrings.
_SERVER_INSTRUCTIONS = (
    "Before starting any multi-stage BRIER analysis, FIRST call "
    "get_workflow_guide to load the full phase-by-phase workflow (checkpoints, "
    "the decision/evaluation layer, metrics, reporting), then follow it. "
    "BRIER fits transfer-learning genetic risk prediction models by "
    "borrowing strength from external cohorts, pretrained coefficients, or "
    "summary statistics. Always inspect data first (inspect_user_data / "
    "inspect_data / list_data_directory) before fitting, then route by what "
    "the data is: individual-level target WITH phenotype + pretrained external "
    "coefficients -> brier_i; pooled individual-level target AND external "
    "cohorts -> brier_full; summary-statistics target (+ an LD matrix from a "
    "reference panel) -> brier_s. If there is no individual-level phenotype, "
    "the individual-level modules are off the table; use brier_s. Genotype "
    "binaries (.pgen/.bed/.bgen) are inspected via their companion files only. "
    "Use prep_auto to assemble fit-ready inputs (alignment, standardization, "
    "intercept row, val/test) in one call rather than hand-writing "
    "preprocessing. Tune eta with the *_selection tools (use an information "
    "criterion when no validation set exists); brier_auto_tune_eta widens the "
    "grid if the optimum sits at the boundary. Evaluate with brier_evaluate "
    "and report with summarize_fit. "
    # --- workflow spine ---
    "Work one stage at a time (inspect -> preprocess -> fit -> decide -> "
    "report); never chain stages into one run. After each stage, save its "
    "output, show a short sanity summary, and pause for the user. Compute "
    "explicit baselines (local-alone, external-alone, integrated) and PROPOSE "
    "which externals to keep rather than deciding unilaterally. Judge "
    "performance with complementary metrics matched to the outcome family, "
    "never a single number. Computation runs in the user's R; only summaries "
    "return to the client. Ask the user when data roles are ambiguous. "
    "The full workflow is available anytime via get_workflow_guide."
)

# Piggyback reminder appended to the responses of tools an agent reliably calls
# early (start_analysis, inspect_user_data), so the workflow-guide pointer lands
# in front of the agent at the moment it starts working, even if it skipped the
# up-front get_workflow_guide call.
_WORKFLOW_GUIDE_REMINDER = (
    "For the full phase-by-phase BRIER workflow (stage checkpoints, the "
    "baseline/decision-evaluation layer, metrics, and reporting), call "
    "get_workflow_guide before proceeding if you have not already."
)

mcp = FastMCP("brier", instructions=_SERVER_INSTRUCTIONS)


# --------------------------------------------------------------------------
# Wizard: route new users from "I want to use BRIER" to a concrete tool call
# --------------------------------------------------------------------------
#
# Design notes (don't strip without thinking):
#
# Stateless. start_analysis is called once at the start of a conversation
# and returns a structured document. The AI walks the user through the
# steps in dialogue; answers live in conversation history, not in this
# module. v0.7 will add stateful tracking via a separate
# record_wizard_answers tool; until then the AI relies on its own context.
#
# Structured (not Markdown). The wizard payload is a JSON-friendly dict
# with separate fields for each section. This lets the AI quote specific
# sections without parsing prose, and lets us add new fields without
# breaking existing call sites.
#
# Size logic in code, not prose. The only branch in the wizard that
# requires logic (not just lookup) is "if the size profile is X, prefer
# tool Y." That lives in _recommend_for_sizes() and is invoked when the
# AI calls start_analysis(n_target=..., n_external=...) with sample
# sizes. Without sizes, the wizard returns a generic decision tree.
#
# Style: no em-dashes. Lowercase brier for technical identifiers, capital
# BRIER for prose. No version pinning.

_FAMILIARITY_PRIMERS = {
    "genetic_risk_prediction": (
        "Genetic risk prediction estimates an individual's risk for a "
        "trait (continuous like LDL cholesterol, binary like type 2 "
        "diabetes, count like number of cardiovascular events) using "
        "high-dimensional molecular predictors. Common predictor types "
        "include SNP genotypes (polygenic risk scores, PRS), gene "
        "expression levels, and protein abundances. Accurate genetic "
        "risk prediction remains challenging in underrepresented "
        "populations and small target cohorts, where limited sample "
        "size and heterogeneous genetic architectures reduce model "
        "portability from well-powered cohorts. "
        "Link: https://www.nature.com/articles/s41596-020-0353-1 "
        "(Choi et al. 2020, Nat Protocols, PRS tutorial)."
    ),
    "transfer_learning": (
        "Transfer learning improves prediction in a target dataset by "
        "borrowing structural information from a larger, related "
        "external dataset. For genetic risk prediction, borrowing can "
        "mean using GWAS summary statistics from a different ancestry, "
        "coefficients from a prior study, or pooling individual-level "
        "data across cohorts. The risk is that borrowing the wrong "
        "information hurts prediction (negative transfer). BRIER "
        "controls this via a tunable integration weight, eta. "
        "Link: https://ieeexplore.ieee.org/document/5288526 "
        "(Pan and Yang 2010, IEEE TKDE, transfer learning survey)."
    ),
    "BRIER": (
        "BRIER is a regularized regression framework (LASSO, MCP, "
        "SCAD) with an extra Bregman-divergence term that pulls "
        "coefficients toward external information. It has three "
        "variants depending on what external data you have: BRIERi "
        "(pretrained external coefficients), BRIERfull (raw external "
        "individual-level data), BRIERs (summary statistics target). "
        "Link: https://um-kevinhe.github.io/BRIER/."
    ),
}


_PROBLEM_DESCRIPTION_QUESTIONS = [
    {
        "id": "outcome_type",
        "ask": (
            "What is the outcome you are trying to predict, and what "
            "type is it: continuous (like a measured trait), binary "
            "(like case/control), count (like event counts), or "
            "time-to-event?"
        ),
        "options": ["continuous", "binary", "count", "time-to-event"],
        "downstream_effect": (
            "Maps to the family argument: continuous -> 'gaussian', "
            "binary -> 'binomial', count -> 'poisson'. time-to-event "
            "is NOT supported by BRIER; flag this and suggest the user "
            "look elsewhere (e.g. dedicated survival-prediction tools)."
        ),
    },
    {
        "id": "predictor_type",
        "ask": (
            "What are the predictors? SNP genotypes (the typical PRS "
            "case), gene expression levels, protein abundances, or a "
            "mix of these plus demographic variables?"
        ),
        "options": ["SNP genotypes", "gene expression", "protein levels",
                    "mixed / other"],
        "downstream_effect": (
            "Informational only; BRIER fits the same way regardless. "
            "Use the answer to tailor downstream examples (e.g. recommend "
            "preprocessI for SNP genotype alignment across cohorts)."
        ),
    },
    {
        "id": "include_demographics",
        "ask": (
            "Do you want to include demographic or clinical covariates "
            "(age, sex, principal components, batch indicators, etc.) "
            "alongside the molecular predictors?"
        ),
        "options": ["yes", "no"],
        "downstream_effect": (
            "If yes, recommend using penalty_factor_expr in the brier_i "
            "or brier_full call to keep the demographic columns "
            "unpenalized (penalty_factor = 0 for those columns, 1 for "
            "the molecular predictors)."
        ),
    },
    {
        "id": "sample_sizes",
        "ask": (
            "Roughly what are the sample sizes? How many individuals "
            "in your target cohort (n_target), how many total across "
            "all external cohorts you plan to use (n_external_total), "
            "and how many predictors (p, e.g. number of SNPs or genes)?"
        ),
        "options": ["numeric"],
        "downstream_effect": (
            "Drives the size-based recommendation: if "
            "n_target + n_external_total > 10000, try BRIERi (and "
            "BRIERs if applicable) before BRIERfull, because BRIERfull "
            "scales with total stacked n and becomes slow. Always offer "
            "BRIERfull as an alternative if the user prefers it."
        ),
    },
    {
        "id": "ancestry_context",
        "ask": (
            "What ancestry or population is your target cohort? And "
            "the external cohorts: same ancestry, different ancestry, "
            "or mixed? (Optional; helpful for LD-block reference choice "
            "if you go down the BRIERs path.)"
        ),
        "options": ["free text"],
        "downstream_effect": (
            "If the user reaches the BRIERs path AND mentions an "
            "ancestry, suggest the right get_ldb(ancestry, build) call "
            "to fetch the matching LD block coordinates (AFR / EAS / "
            "EUR for hg19 / hg38)."
        ),
    },
]


_ROUTING_QUESTIONS = [
    {
        "id": "target_data_shape",
        "ask": (
            "What does your TARGET-cohort data look like? Do you have "
            "individual-level data (a matrix with one row per person, "
            "plus the outcome for each person), or only per-predictor "
            "summary statistics from a published GWAS (one row per SNP "
            "or gene, with effect size, p-value, sample size)?"
        ),
        "branches": {
            "individual-level": "proceed to external_data_shape question",
            "summary statistics only": "route to BRIERs path",
        },
    },
    {
        "id": "external_data_shape",
        "ask": (
            "What EXTERNAL information do you have? Four common cases: "
            "(a) pretrained coefficient vectors from a prior study (one "
            "or more PRS weights you can plug in directly), (b) raw "
            "individual-level data from external cohorts (X and y per "
            "cohort), (c) external GWAS summary statistics (per-SNP "
            "marginal effect sizes and p-values, no individual data), "
            "or (d) none, in which case you just want a target-only "
            "baseline model."
        ),
        "branches": {
            "pretrained coefficients": (
                "route to BRIERi (if target is individual-level) or "
                "BRIERs (if target is sumstats). No prep step needed; "
                "the user's coefficient vectors plug straight into "
                "beta.external."
            ),
            "raw individual-level external data": (
                "If target is individual-level: route to BRIERfull "
                "directly (or BRIERi if size_recommendation prefers "
                "it). If target is sumstats: route to BRIERs, but the "
                "user MUST fit a per-cohort model on each external "
                "first to produce coefficient vectors. See "
                "paths.BRIERi.prep_external_individual or "
                "paths.BRIERs.prep_external_individual for the recipe."
            ),
            "external GWAS summary statistics": (
                "Route to BRIERi (if target is individual-level) or "
                "BRIERs (if target is sumstats). The user MUST run an "
                "upstream PRS method (clumping + thresholding, "
                "lassosum, PRS-CS, or glmnet against sumstats) FIRST "
                "to convert the external sumstats into a coefficient "
                "vector per external source. BRIERfull is NOT "
                "available when external is sumstats-only because "
                "BRIERfull requires individual-level external data. "
                "See paths.BRIERi.prep_external_sumstats or "
                "paths.BRIERs.prep_external_sumstats for guidance."
            ),
            "none": (
                "route to BRIERi-baseline path (target-only LASSO via "
                "eta=0). The user has no external information and "
                "wants a baseline."
            ),
        },
    },
    {
        "id": "validation_set_available",
        "ask": (
            "Do you have a held-out validation set, separate from your "
            "training and testing data, that you can use to tune the "
            "model's hyperparameters?"
        ),
        "branches": {
            "yes": (
                "selection criterion: gaussian.mspe, binomial.dev, "
                "binomial.auc, etc. (family-specific). Use "
                "X_val_expr / y_val_expr in the *_selection tool."
            ),
            "no": (
                "BRIERi: use BIC, Cp, or GCV. BRIERs: use Cp, GIC, or "
                "pseu.val (all require TN). BRIERfull: validation set "
                "is required, cannot proceed."
            ),
        },
    },
]


_PATHS = {
    "BRIERi": {
        "summary": (
            "Use BRIERi when the target cohort has individual-level "
            "X and y, and the external information is one or more "
            "pretrained coefficient vectors (PRS weights, prior-study "
            "coefficients)."
        ),
        "tool_sequence": [
            "brier_i", "brier_i_selection", "brier_predict", "brier_evaluate",
        ],
        "canonical_call": (
            "brier_i(\n"
            "  data_path = '/path/to/data.rds',\n"
            "  X_expr = 'data$target$train$X',\n"
            "  y_expr = 'data$target$train$y',\n"
            "  beta_external_expr = 'data$beta.external',  # (p+1) x M, INCLUDING intercept row\n"
            "  family = 'gaussian',     # or 'binomial', 'poisson'\n"
            "  multi_method = 'stacking'  # default; good for M >= 2\n"
            ")"
        ),
        "pitfalls": [
            (
                "beta.external for BRIERi MUST be (p+1) x M with an "
                "intercept row at the top. If no external intercept "
                "exists, set the first row to zero. Forgetting the "
                "intercept row is the most common BRIERi mistake."
            ),
            (
                "If using demographic covariates, pass "
                "penalty_factor_expr to keep them unpenalized "
                "(penalty.factor = 0 for those columns)."
            ),
        ],
        "selection_options": {
            "with_validation": (
                "brier_i_selection(criteria='gaussian.mspe', "
                "X_val_expr=..., y_val_expr=..., data_path=...)"
            ),
            "without_validation": (
                "brier_i_selection(criteria='BIC')  # or 'Cp', 'GCV'"
            ),
        },
        "prep_external_individual": (
            "When external information is raw individual-level data but "
            "the user wants BRIERi (not BRIERfull, often for speed at "
            "large n), fit a per-cohort model on each external cohort "
            "first to produce a coefficient vector, then stack into "
            "beta.external. Canonical R recipe:\n\n"
            "  library(glmnet)\n"
            "  # For each external cohort, fit a regularized model:\n"
            "  fit1 <- cv.glmnet(ext1$X, ext1$y, alpha = 1, family = 'gaussian')\n"
            "  fit2 <- cv.glmnet(ext2$X, ext2$y, alpha = 1, family = 'gaussian')\n"
            "  # Extract coefficients (length p+1, includes intercept at top):\n"
            "  beta1 <- as.numeric(coef(fit1, s = 'lambda.min'))\n"
            "  beta2 <- as.numeric(coef(fit2, s = 'lambda.min'))\n"
            "  # Stack column-wise; BRIERi expects (p+1) x M:\n"
            "  beta.external <- cbind(beta1, beta2)\n"
            "  saveRDS(list(X = target$X, y = target$y,\n"
            "               beta.external = beta.external),\n"
            "          'data.rds')\n\n"
            "BRIERi needs the intercept row at the TOP of each column. "
            "glmnet's coef() returns (intercept, beta_1, ..., beta_p) "
            "which matches BRIERi's required shape."
        ),
        "prep_external_sumstats": (
            "When external information is GWAS sumstats (no individual "
            "data), the user must run an upstream PRS method to convert "
            "sumstats into a coefficient vector first. Options:\n\n"
            "  * Clumping + thresholding (PLINK): simplest, widely used.\n"
            "  * lassosum (R package): LASSO on sumstats + a reference LD panel.\n"
            "  * PRS-CS (Python/command line): Bayesian shrinkage with continuous priors.\n"
            "  * glmnet with manually constructed pseudo-y: advanced; use only if you understand it.\n\n"
            "Each external sumstats source becomes one column of "
            "beta.external. After conversion, follow the same stack-"
            "and-prepend-intercept pattern as for individual-level "
            "external data:\n\n"
            "  beta1 <- as.numeric(c(0, lassosum_coefs_external_1))   # intercept = 0\n"
            "  beta2 <- as.numeric(c(0, lassosum_coefs_external_2))\n"
            "  beta.external <- cbind(beta1, beta2)\n\n"
            "PRS-method selection is beyond BRIER's scope; ask your "
            "collaborators or see the PRS-CS paper for guidance."
        ),
    },
    "BRIERi-baseline": {
        "summary": (
            "Use this when the user has individual-level target data "
            "but no external information. Fits a target-only LASSO "
            "(via BRIERi with eta=0), useful as a baseline to quantify "
            "the gain from borrowing in a later full BRIER fit."
        ),
        "tool_sequence": [
            "brier_i", "brier_i_selection", "brier_predict", "brier_evaluate",
        ],
        "canonical_call": (
            "# First in R: create a zero-vector beta_zero of length ncol(X)+1\n"
            "# and save it to your .rds file alongside X and y.\n"
            "brier_i(\n"
            "  data_path = '/path/to/data.rds',\n"
            "  X_expr = 'data$X',\n"
            "  y_expr = 'data$y',\n"
            "  beta_external_expr = 'data$beta_zero',\n"
            "  family = 'gaussian',\n"
            "  eta_list = [0]   # forces target-only\n"
            ")"
        ),
        "pitfalls": [
            (
                "Even with eta=0, beta_external_expr is required by "
                "BRIERi. Have the user create a length-(p+1) zero "
                "vector in R first: beta_zero <- rep(0, ncol(X) + 1)."
            ),
        ],
        "selection_options": {
            "with_validation": (
                "brier_i_selection(criteria='gaussian.mspe', ...)"
            ),
            "without_validation": "brier_i_selection(criteria='BIC')",
        },
    },
    "BRIERfull": {
        "summary": (
            "Use BRIERfull when raw individual-level data is available "
            "for both the target and all external cohorts. The fit "
            "jointly optimizes coefficients across the pooled data, "
            "regularized toward target-cohort prediction via eta."
        ),
        "tool_sequence": [
            "brier_full", "brier_full_selection",
            "brier_predict", "brier_evaluate",
        ],
        "canonical_call": (
            "# First in R, stack the data:\n"
            "# X.full <- rbind(target$X, ext1$X, ext2$X)\n"
            "# y.full <- c(target$y, ext1$y, ext2$y)\n"
            "# cohort.full <- c(rep(0L, nrow(target$X)),\n"
            "#                  rep(1L, nrow(ext1$X)),\n"
            "#                  rep(2L, nrow(ext2$X)))\n"
            "brier_full(\n"
            "  data_path = '/path/to/stacked.rds',\n"
            "  X_expr = 'data$X.full',\n"
            "  y_expr = 'data$y.full',\n"
            "  cohort_expr = 'data$cohort.full',\n"
            "  family = 'gaussian'\n"
            ")"
        ),
        "pitfalls": [
            (
                "cohort = 0 for target, positive integers (1, 2, ...) "
                "for external cohorts. At least one 0 and at least one "
                "positive value are required."
            ),
            (
                "Validation set is REQUIRED for brier_full_selection. "
                "IC criteria (BIC, Cp, GCV) are not supported for "
                "BRIERfull."
            ),
            (
                "BRIERfull's wall time scales with total stacked n. "
                "For very large external cohorts, expect minutes to "
                "tens of minutes; BRIERi is usually 10x to 100x faster."
            ),
        ],
        "selection_options": {
            "with_validation": (
                "brier_full_selection(criteria='gaussian.mspe', "
                "X_val_expr=..., y_val_expr=..., data_path=...)"
            ),
            "without_validation": (
                "Not supported. BRIERfull.selection requires X.val and y.val."
            ),
        },
    },
    "BRIERs": {
        "summary": (
            "Use BRIERs when the target is represented by summary "
            "statistics (one row per predictor with marginal effect "
            "size, p-value, n) plus an LD matrix, rather than "
            "individual-level X and y."
        ),
        "tool_sequence": [
            "cal_ld", "brier_s", "brier_s_selection",
            "brier_predict", "brier_evaluate",
        ],
        "canonical_call": (
            "# First in R, augment sumstats with a corr column:\n"
            "# sumstats$corr <- p2cor(sumstats$pval, sumstats$n,\n"
            "#                        sign = sign(sumstats$stats))\n"
            "# Save sumstats, beta.external, and a reference-panel X to a .rds.\n"
            "\n"
            "# Step 1: build the LD matrix\n"
            "cal_ld(data_path='...', X_expr='data$X')\n"
            "# returns ld_id\n"
            "\n"
            "# Step 2: fit with ld_id (auto-subsets sumstats and beta.external by $nz)\n"
            "brier_s(\n"
            "  data_path = '...',\n"
            "  sumstats_expr = 'data$sumstats',\n"
            "  beta_external_expr = 'data$beta.external',  # p x M, NO intercept row\n"
            "  family = 'gaussian',\n"
            "  ld_id = '<ld_id from step 1>',\n"
            "  multi_method = 'stacking'\n"
            ")"
        ),
        "pitfalls": [
            (
                "beta.external for BRIERs is p x M (NO intercept row). "
                "This is asymmetric with BRIERi which requires (p+1) x M."
            ),
            (
                "BRIERs returns coefficients on the STANDARDIZED scale. "
                "Before passing X.val and y.val (gaussian only) to "
                "brier_s_selection, standardize them: X.val.std <- "
                "standardize_X(X.val)$standardized."
            ),
            (
                "sumstats must have a corr column. If only p-values "
                "are present, build it first: "
                "sumstats$corr <- p2cor(pval, n, sign = sign(stats))."
            ),
            (
                "IC criteria (Cp, GIC, pseu.val) all require TN "
                "(training sample size, integer) in brier_s_selection."
            ),
        ],
        "selection_options": {
            "with_validation": (
                "brier_s_selection(criteria='gaussian.mspe', "
                "X_val_expr=..., y_val_expr=..., data_path=...) "
                "# X.val and y.val MUST be standardized."
            ),
            "without_validation": (
                "brier_s_selection(criteria='Cp', TN=<n_train>) "
                "# or 'GIC', 'pseu.val', all require TN."
            ),
        },
        "prep_external_individual": (
            "When external information is raw individual-level data "
            "and the user is going down the BRIERs path (target is "
            "sumstats), fit a per-cohort model on each external first "
            "to produce coefficient vectors, then stack into "
            "beta.external. CRITICAL: BRIERs expects coefficients on "
            "the STANDARDIZED scale, so the per-cohort prep models "
            "must be fit on standardized X. Canonical R recipe:\n\n"
            "  library(glmnet); library(BRIER)\n"
            "  # Standardize each external cohort's X BEFORE fitting:\n"
            "  ext1_X_std <- standardize_X(ext1$X)$standardized\n"
            "  ext2_X_std <- standardize_X(ext2$X)$standardized\n"
            "  # Fit a regularized model per cohort:\n"
            "  fit1 <- cv.glmnet(ext1_X_std, ext1$y, alpha = 1)\n"
            "  fit2 <- cv.glmnet(ext2_X_std, ext2$y, alpha = 1)\n"
            "  # Extract coefficients WITHOUT the intercept (BRIERs is p x M):\n"
            "  beta1 <- as.numeric(coef(fit1, s = 'lambda.min'))[-1]\n"
            "  beta2 <- as.numeric(coef(fit2, s = 'lambda.min'))[-1]\n"
            "  beta.external <- cbind(beta1, beta2)\n\n"
            "Note the [-1] to drop the intercept row: BRIERs beta.external "
            "is p x M (no intercept), unlike BRIERi which is (p+1) x M. "
            "Also: forgetting to standardize the external X before "
            "fitting produces coefficients on the wrong scale, which "
            "BRIERs cannot detect and which will silently degrade "
            "predictions."
        ),
        "prep_external_sumstats": (
            "When external information is GWAS sumstats, run an upstream "
            "PRS method to convert sumstats into coefficient vectors "
            "first. Same options as BRIERi (clumping + thresholding, "
            "lassosum, PRS-CS, glmnet). For BRIERs the resulting "
            "coefficients should be on the STANDARDIZED scale to be "
            "consistent with BRIERs internals; most PRS methods that "
            "operate on GWAS sumstats already return standardized "
            "coefficients (per-allele effect on the standardized "
            "outcome). Stack column-wise WITHOUT a leading intercept:\n\n"
            "  beta1 <- as.numeric(lassosum_coefs_external_1)   # NO intercept row\n"
            "  beta2 <- as.numeric(lassosum_coefs_external_2)\n"
            "  beta.external <- cbind(beta1, beta2)\n\n"
            "PRS-method selection is beyond BRIER's scope; consult the "
            "method's documentation for whether its output is on the "
            "standardized or raw scale."
        ),
        "non_snp_predictors": (
            "If predictors are gene expression, protein abundances, or "
            "any non-SNP feature, the BRIERs path is still valid: "
            "cal_ld computes a generic column cross-product (X'X / n), "
            "which captures predictor covariance regardless of feature "
            "type. The differences are: (1) skip get_ldb entirely, "
            "since LD blocks are a SNP-specific concept; (2) ancestry "
            "context is irrelevant; (3) calLD's optional LDB argument "
            "should not be passed. Just call cal_ld(data_path=..., "
            "X_expr='data$X') with the reference-panel X and proceed."
        ),
        "cross_family_comparison": (
            "BRIERs predictions are inherently on the STANDARDIZED "
            "scale because GWAS summary statistics contain only "
            "standardized marginal correlations and the LD matrix is "
            "a correlation matrix. Output predictions are in units of "
            "sd(y) from mean(y); to recover raw-scale predictions you "
            "need mean(y) and sd(y) as scalars.\n\n"
            "THREE SOURCES for y_center and y_scale, in order of "
            "reliability:\n\n"
            "  (1) Training y (UNBIASED). If the user happens to also "
            "      have the individual-level training outcome vector "
            "      (simulation, methods comparison, or the user is "
            "      also the GWAS author), compute:\n"
            "        y_center <- mean(Data$target$train$y)\n"
            "        y_scale  <- sd(Data$target$train$y)\n"
            "      Pass to brier_predict as y_center=..., y_scale=....\n\n"
            "  (2) Test y as approximation (SLIGHTLY BIASED). If the "
            "      user only has the test set outcome:\n"
            "        y_center <- mean(Data$target$testing$y)\n"
            "        y_scale  <- sd(Data$target$testing$y)\n"
            "      This is biased because test set mean and sd differ "
            "      from train's by sampling. Bias is usually small; "
            "      acceptable for many purposes.\n\n"
            "  (3) External scalars (VARIABLE BIAS). If no y is "
            "      available, use literature values for the trait's "
            "      population mean and sd. Quality depends entirely "
            "      on the source.\n\n"
            "  (4) No source available -> leave predictions STANDARDIZED. "
            "      Validation MSPE on standardized y means fraction of "
            "      standardized-y variance unexplained (variance is 1.0, "
            "      so MSPE = 0.85 means 15% of variance explained).\n\n"
            "CROSS-FAMILY COMPARISON. If the user wants to compare "
            "BRIERi / BRIERfull / BRIERs side by side, an alternative "
            "to un-standardization is to fit BRIERi and BRIERfull on "
            "STANDARDIZED inputs as well. This puts all three families "
            "on the same scale and avoids the sourcing question:\n\n"
            "  # Step 1: standardize once.\n"
            "  X_std_obj <- BRIER::standardize_X(Data$target$train$X)\n"
            "  X_train_std <- X_std_obj$standardized\n"
            "  y_train_std <- as.numeric(scale(Data$target$train$y))\n"
            "  X_val_std <- scale(Data$target$validation$X,\n"
            "                     center = X_std_obj$center,\n"
            "                     scale  = X_std_obj$scale)\n"
            "  y_val_std <- as.numeric(scale(Data$target$validation$y))\n"
            "  # (Save standardized versions back into the .rds.)\n\n"
            "  # Step 2: fit each family on the standardized data.\n"
            "  # BRIERi:    brier_i(X_expr='data$X_train_std', y_expr='data$y_train_std', ...)\n"
            "  # BRIERfull: brier_full(X_expr='data$X.full_std', y_expr='data$y.full_std', ...)\n"
            "  # BRIERs:    brier_s as usual (already standardized inputs)\n\n"
            "  # Step 3: select against the same standardized validation set.\n"
            "  # All three families' selected MSPE values are now on the\n"
            "  # standardized y scale and directly comparable; coefficient\n"
            "  # magnitudes are also on the same scale.\n\n"
            "Both approaches are valid; the choice depends on which "
            "scale the user wants the comparison on."
        ),
    },
}


_PREPROCESSING_HINTS = {
    "preprocessI": (
        "If the user's target SNP info and external coefficient tables "
        "use different SNP identifiers, coordinates, or allele codings, "
        "they should run BRIER's preprocessI in R BEFORE calling the "
        "MCP fitting tools. preprocessI aligns by CHR/BP/REF/ALT and "
        "drops strand-ambiguous SNPs. Canonical R-side call:\n\n"
        "  library(BRIER)\n"
        "  aligned <- preprocessI(\n"
        "    target.info = target_snp_info_dataframe,\n"
        "    external.ss = list(ext1_coef, ext2_coef),\n"
        "    drop.ambiguous = TRUE\n"
        "  )\n"
        "  # then save aligned$target.info and aligned$external.coefs to .rds\n\n"
        "MCP wrappers for preprocessI/preprocessS are planned for v0.7."
    ),
    "preprocessS": (
        "For the BRIERs path, if the target sumstats and external "
        "sumstats use different alleles or coordinates, run preprocessS "
        "in R first to align them. Same aligned-output pattern as "
        "preprocessI. See "
        "https://um-kevinhe.github.io/BRIER/ for the preprocessS "
        "vignette. MCP wrapper planned for v0.7."
    ),
}


_BASELINE_OFFER = (
    "Before fitting the full BRIER model, offer to fit a target-only "
    "LASSO baseline first. This quantifies how much the external "
    "information actually helps. Without the baseline, the user cannot "
    "say 'BRIER improved prediction by X percent' in their writeup. "
    "Implementation: same brier_i call but with eta_list=[0] (a zero "
    "beta_external is fine; the target-only LASSO is what comes out)."
)


_FAMILY_CAVEATS = {
    "binomial": (
        "If the binary outcome is extremely imbalanced (e.g. 1 percent "
        "cases, 99 percent controls), family='binomial' still works "
        "but the user should think about class-weighted loss, "
        "case-control sampling adjustments, or oversampling. BRIER "
        "itself does not handle class imbalance."
    ),
    "poisson": (
        "For count outcomes, family='poisson' assumes the dispersion "
        "matches a Poisson distribution. If the counts are "
        "over-dispersed (variance much larger than mean), the "
        "predictions will be miscalibrated; consider transforming the "
        "outcome or using a different framework."
    ),
    "time-to-event": (
        "BRIER does NOT support time-to-event (survival) outcomes. "
        "Flag this clearly and suggest the user look at dedicated "
        "survival-prediction methods (Cox-based models with regularization "
        "such as glmnet's cv.glmnet with family='cox'). Do not proceed "
        "with any BRIER tool call for a survival problem."
    ),
}


_MISSINGNESS_NOTE = (
    "BRIER does not impute missing values. If the user's X has missing "
    "genotypes or the y has missing outcomes, suggest they handle this "
    "upstream: PLINK 2's --mind for individual missingness, mean "
    "imputation per SNP, or exclusion of rows with missing y. Do not "
    "fit on data containing NA values."
)


def _recommend_for_sizes(
    n_target,
    n_external_total,
    has_individual_external,
    M=None,
):
    """Return the primary tool recommendation given size profile.

    Called when the user has individual-level data for BOTH target AND
    external cohorts. When external is summary-only or coefficients-only,
    the routing is unambiguous and this function is not invoked.

    Rules, in priority order:
      1. n_target + n_external_total > 10000 -> primary BRIERi
         (BRIERfull's joint optimization is slow at large total n).
      2. M >= 3 external cohorts -> primary BRIERi
         (BRIERfull's wall time scales with M; live test showed M=3
         on n~1050 already hits the 4-min MCP transport timeout).
      3. otherwise -> primary BRIERfull.

    Adds a `time_expectation` field for BRIERfull recommendations so the
    AI can warn the user about expected wall time before committing.
    """
    if not has_individual_external:
        return None  # caller doesn't need size-based logic

    # Many externals: M-based rule takes priority over size-only branches.
    if M is not None and M >= 3:
        return {
            "primary": "BRIERi",
            "alternatives": ["BRIERfull"],
            "reason": (
                f"M = {M} external cohorts. BRIERfull's joint "
                f"optimization scales multiplicatively with M, and "
                f"with M >= 3 the (eta x lambda x M) grid is large "
                f"enough that fits routinely exceed the MCP transport "
                f"timeout (4 minutes). BRIERi with "
                f"multi_method='stacking' first collapses the M "
                f"externals into a single combined direction, then "
                f"fits a much smaller grid. Recommended."
            ),
            "time_expectation": (
                f"BRIERi with M={M}: typically under 30 seconds. "
                f"BRIERfull with M={M}: typically several minutes "
                f"to tens of minutes; may exceed MCP timeout."
            ),
        }

    if n_target is None or n_external_total is None:
        return {
            "primary": "BRIERfull",
            "alternatives": ["BRIERi"],
            "reason": (
                "Without sample sizes, default to BRIERfull when "
                "individual-level external data is available. Ask the "
                "user for n_target and n_external_total to refine."
            ),
            "time_expectation": (
                "Cannot estimate wall time without sample sizes."
            ),
        }
    total = int(n_target) + int(n_external_total)
    if total > 10000:
        return {
            "primary": "BRIERi",
            "alternatives": ["BRIERfull"],
            "reason": (
                f"Combined sample size (n_target + n_external = "
                f"{total}) is large enough that BRIERfull's joint "
                f"optimization will be slow. BRIERi (collapse externals "
                f"to a coefficient vector via OLS/ridge first) is "
                f"typically 10x to 100x faster at this scale. "
                f"Offer BRIERfull as an alternative if the user wants "
                f"the full joint fit anyway."
            ),
            "time_expectation": (
                "BRIERi at this scale: roughly 1 minute. BRIERfull: "
                "very slow, may exceed MCP timeout."
            ),
        }
    # M = 1 or 2 with modest total n: BRIERfull is feasible.
    M_str = f", M = {M}" if M is not None else ""
    return {
        "primary": "BRIERfull",
        "alternatives": ["BRIERi"],
        "reason": (
            f"Combined sample size (n_target + n_external = "
            f"{total}{M_str}) is small enough that BRIERfull's joint "
            f"optimization is feasible and gives the most flexible fit."
        ),
        "time_expectation": (
            "BRIERfull at this scale: typically 1 to 5 minutes with "
            "the default eta grid of 7 values."
        ),
    }


@mcp.tool()
def start_analysis(
    inspection_id: Optional[str] = None,
    overrides: Optional[dict] = None,
    n_target: Optional[int] = None,
    n_external_total: Optional[int] = None,
    has_individual_external: Optional[bool] = None,
    M: Optional[int] = None,
) -> dict:
    """Guided wizard for new BRIER users: data-first routing.

    The v0.7 wizard is DATA-FIRST. The recommended call pattern is:

        1. AI calls start_analysis()   # returns welcome + familiarity check
        2. AI asks the user for data path(s)
        3. AI calls inspect_user_data(paths)  # returns inspection_id
        4. AI calls start_analysis(inspection_id=...)  # returns
           inspection summary + tentative recommendation
        5. AI presents to user, who confirms or sends free-text
           corrections
        6. If corrections: AI calls start_analysis(inspection_id=...,
           overrides={...}) and gets an updated recommendation
        7. AI proceeds to the recommended fit

    Two argument modes:
        * NO args: returns the welcome line and familiarity_check.
          The AI uses this to set up the conversation, then collects
          data paths and runs inspect_user_data.
        * inspection_id: returns inspection summary + a tentative
          recommendation grounded in what was found in the data.
          The AI presents to the user.
        * inspection_id + overrides: same as above but with user
          corrections applied. Overrides are TRANSIENT (not stored
          server-side); each call must pass them again if still
          relevant.

    Override keys (any subset):
        outcome_family: "gaussian" | "binomial" | "poisson"
        predictor_type: "SNP" | "gene_expression" | "protein" | "mixed"
        target_shape: "individual" | "sumstats"
        external_shape: "coefficients" | "individual" | "sumstats" | "none"
        has_validation_set: bool
        include_demographics: bool
        ancestry: str

    Legacy args (kept for backward compat with v0.6 callers):
        n_target, n_external_total, has_individual_external: pass these
        directly to drive the size-based recommendation if you haven't
        run inspect_user_data yet.

    Returns:
        Always: status, welcome, familiarity_check, paths,
            preprocessing_hints, baseline_offer, family_caveats,
            missingness_note, inspect_first_reminder, ai_instructions.

        Without inspection_id: also problem_description_questions,
            routing_questions, phase_gates (the v0.6 interview-style
            decision tree, as a fallback).

        With inspection_id: also inspection_summary (what was found),
            inferred_assessment (combined heuristics with overrides
            applied), recommendation (primary tool + alternatives +
            reasons + canonical call filled in with the user's
            actual R expression paths), confidence_notes (which
            heuristics were low-confidence and should be explicitly
            confirmed).
    """
    base = {
        "status": "ok",
        "welcome": (
            "Welcome to BRIER. BRIER is a transfer-learning framework for "
            "genetic risk prediction. It improves prediction in a target "
            "cohort by borrowing information from external cohorts, "
            "pretrained models, or summary statistics. Tell me where your "
            "data is and I'll figure out which BRIER tool fits."
        ),
        "background": (
            "Genetic risk prediction estimates a person's risk for a "
            "trait using high-dimensional molecular predictors. The "
            "outcome can be continuous (like LDL cholesterol), binary "
            "(like type 2 diabetes yes/no), or a count (like number of "
            "cardiovascular events). The predictors are usually SNP "
            "genotypes (this is the classic 'polygenic risk score' / "
            "PRS setup), but they can also be gene expression or "
            "protein levels. The hard part: these models tend to "
            "travel poorly to small cohorts and to populations that "
            "were underrepresented in the original study. "
            "PRS tutorial: "
            "https://www.nature.com/articles/s41596-020-0353-1\n\n"
            "Transfer learning is the idea of improving prediction in "
            "your target dataset by borrowing structure from a "
            "larger, related external dataset - for example, GWAS "
            "summary statistics from another ancestry, coefficients "
            "from a prior study, or pooled individual-level data. "
            "The danger is 'negative transfer': borrow the wrong "
            "thing and you make prediction worse. BRIER controls "
            "this with a tunable knob called eta, which sets how "
            "strongly the model leans on the external information.\n\n"
            "BRIER specifically is a regularized regression framework "
            "(LASSO/MCP/SCAD penalties) with an extra term that pulls "
            "coefficients toward your external information. It comes "
            "in three flavors depending on what external data you have:\n"
            "  - BRIERi: pretrained external coefficients (PRS "
            "weights, prior-study betas)\n"
            "  - BRIERfull: raw individual-level data for the external "
            "cohorts\n"
            "  - BRIERs: target represented by summary statistics "
            "rather than individual-level data\n\n"
            "Docs: https://um-kevinhe.github.io/BRIER/\n"
            "Source: https://github.com/UM-KevinHe/BRIER"
        ),
        "_display_instructions": (
            "On the FIRST turn, render `welcome` verbatim and then "
            "render `familiarity_check` so the user can tell you which "
            "concepts they're new to.\n\n"
            "Do NOT append URLs, references, 'See also' lists, "
            "GitHub links, or citation lines to the welcome message. "
            "The welcome is intentionally URL-free. URLs belong in "
            "`background` (which is gated, see below) or in the "
            "per-concept primers (which fire on familiarity_check "
            "answers). Pasting URLs into the greeting because they "
            "feel like helpful references is the specific anti-pattern "
            "to avoid.\n\n"
            "Render `background` (verbatim, including every URL "
            "inline) ONLY when (a) the familiarity_check response "
            "indicates the user is new to one or more of the listed "
            "concepts, OR (b) the user explicitly asks for "
            "background / context / a primer. When you do render "
            "background, do not paraphrase it - the URLs and the "
            "three-flavor list are calibrated; rewriting them risks "
            "dropping links or distorting which BRIER variant fits "
            "which data."
        ),
        "familiarity_check": {
            "_render_hint": "multi_select_buttons",
            "question": "Which of these are you new to?",
            "instructions": (
                "If your chat interface supports interactive multi-select "
                "buttons, render the options below as checkboxes/chips so "
                "the user can tap to select without typing. Otherwise, "
                "present as a numbered list and accept comma-separated "
                "numbers or option labels as input."
            ),
            "multi_select": True,
            "options": [
                {"id": 1, "label": "Genetic risk prediction",
                 "primer_key": "genetic_risk_prediction"},
                {"id": 2, "label": "Transfer learning",
                 "primer_key": "transfer_learning"},
                {"id": 3, "label": "BRIER specifically",
                 "primer_key": "BRIER"},
                {"id": 4, "label": "Skip - I'm comfortable with all of these",
                 "primer_key": None},
            ],
            "prompt_fallback": (
                "Which of these are you new to? Pick any that apply:\n\n"
                "  1. Genetic risk prediction\n"
                "  2. Transfer learning\n"
                "  3. BRIER specifically\n"
                "  4. Skip - I'm comfortable with all of these\n\n"
                "Reply with the number(s), e.g. '1, 2' or just '4'."
            ),
            "format": (
                "Optional. If interactive UI is available, render as "
                "checkboxes/chips using the structured options. Otherwise, "
                "use prompt_fallback. Deliver primers only for the items "
                "the user selects (matched via primer_key). Option 4 "
                "('Skip') means no primers."
            ),
            "primers": _FAMILIARITY_PRIMERS,
        },
        "path_question": {
            "_render_hint": "text_input",
            "question": "What is the full path to your data?",
            "prompt_fallback": (
                "What is the full path to your data?\n\n"
                "Examples:\n"
                "  /Users/you/Desktop/data.rds                       "
                " (single file with target + external)\n"
                "  /Users/you/study/target.rds, /Users/you/study/ext.rds   "
                "(multiple files, target first)\n"
                "  /Users/you/Desktop/study_data/                    "
                " (directory - I'll list contents)\n\n"
                "Supported file formats: .rds, .rda, .RData, .csv, "
                ".tsv, .txt. Paste your path below."
            ),
            "accepts": ["file_path", "comma_separated_file_paths",
                         "directory_path"],
            "format": (
                "If the chat interface supports text input UI, render "
                "with an input field. Otherwise use prompt_fallback. "
                "The user may paste: (a) a single file path, (b) "
                "comma-separated paths with the FIRST path treated as "
                "target, or (c) a directory path - in case (c), call "
                "list_data_directory first and present the contents "
                "as a numbered list for the user to pick from."
            ),
        },
        "paths": _PATHS,
        "preprocessing_hints": _PREPROCESSING_HINTS,
        "baseline_offer": _BASELINE_OFFER,
        "family_caveats": _FAMILY_CAVEATS,
        "missingness_note": _MISSINGNESS_NOTE,
        "inspect_first_reminder": (
            "Before any model fit, the wizard already calls "
            "inspect_user_data on the user's path. The inspection "
            "result drives the recommendation. If anything in the "
            "inspection summary looks wrong to the user, accept the "
            "correction as an override and re-call start_analysis "
            "with the new override."
        ),
        "workflow_guide_reminder": _WORKFLOW_GUIDE_REMINDER,
    }

    if inspection_id is None:
        # Mode 1: no inspection yet. Return the v0.6-style interview
        # fallback alongside instructions to do the data-first flow.
        base.update({
            "problem_description_questions": _PROBLEM_DESCRIPTION_QUESTIONS,
            "routing_questions": _ROUTING_QUESTIONS,
            "phase_gates": {
                "before_recommendation": [
                    "outcome_type",
                    "sample_sizes",
                    "target_data_shape",
                    "external_data_shape",
                    "validation_set_available",
                ],
                "ask_one_at_a_time": True,
                "do_not_batch_questions": True,
                "do_not_skip_ahead": True,
            },
            "size_recommendation": _recommend_for_sizes(
                n_target, n_external_total, has_individual_external, M=M
            ),
            "ai_instructions": (
                "PREFERRED FLOW (data-first, v0.7+, with v0.10.1 UX):\n"
                "(1) Deliver the welcome line.\n"
                "(2) Offer the familiarity check using "
                "    familiarity_check.options as a multi-select UI "
                "    (checkboxes/chips/buttons) if your interface "
                "    supports it; otherwise use prompt_fallback. "
                "    Match the user's selections against primer_key "
                "    and deliver primers only for the items they "
                "    selected. Option 4 ('Skip') means no primers.\n"
                "(3) Ask for the data path using path_question. If "
                "    the chat interface has a text input UI, use it; "
                "    otherwise use prompt_fallback. Accept three "
                "    input shapes:\n"
                "    (a) A single file path - proceed to step 4.\n"
                "    (b) Comma-separated file paths - first is the "
                "        target.\n"
                "    (c) A DIRECTORY path - call list_data_directory "
                "        first, present the contents as a numbered "
                "        list, let the user pick, then proceed to "
                "        step 4 with the chosen file(s).\n"
                "(4) Call inspect_user_data with the resolved "
                "    path(s). The FIRST path is treated as the "
                "    target file.\n"
                "(5) Call start_analysis again with the returned "
                "    inspection_id. The response will include the "
                "    inspection summary and a tentative "
                "    recommendation.\n"
                "(6) Present BOTH the structured summary and a short "
                "    prose explanation to the user. Show what was "
                "    detected, with appropriate hedge for low-"
                "    confidence fields. Ask: 'Does anything in this "
                "    summary look wrong? If so, tell me what to "
                "    change in plain English.'\n"
                "(7) If the user corrects anything, parse the "
                "    correction into the overrides dict and re-call "
                "    start_analysis(inspection_id=..., "
                "    overrides={...}).\n"
                "(8) Once the user confirms, deliver the canonical "
                "    call shape (already filled in with the user's "
                "    expression paths from the inspection) and "
                "    proceed to the fit.\n\n"
                "SPLIT-FILE DATA (IMPORTANT): If the target data and the "
                "external data live in SEPARATE files (for example, "
                "target sumstats + LD in one file and external "
                "beta.external in another, as in a cross-ancestry PRS), "
                "do NOT write or suggest an R merge script and do NOT "
                "tell the user a fit tool can only read one file. Every "
                "fit tool (brier_i, brier_full, brier_s) and the "
                "predict / evaluate / plot / summarize tools accept a "
                "`data_paths` LIST. Pass all the files in data_paths and "
                "reference each by its basename (filename without "
                "extension) in the expressions. Example for the "
                "split sumstats / external case:\n"
                "    brier_s(\n"
                "        data_paths=['/path/height_AFR.RData',\n"
                "                     '/path/height_EUR.RData'],\n"
                "        sumstats_expr='height_AFR$sumstats',\n"
                "        XtX_expr='height_AFR$XtX',\n"
                "        beta_external_expr='matrix(height_EUR$beta.external, ncol=1)',\n"
                "        family='gaussian')\n"
                "Only fall back to preprocess_s / preprocess_i (SNP "
                "alignment) or a prep step when the predictor sets are "
                "genuinely misaligned (different SNP IDs, different "
                "order, allele flips), NOT merely because the data spans "
                "two files.\n\n"
                "WHERE OUTPUTS GO (IMPORTANT): Before calling "
                "summarize_fit, brier_predict, or the plot tools, ASK "
                "the user where they want the artifacts saved (HTML "
                "report, reproduce.R, plot PNGs/CSVs, prediction CSVs). "
                "Suggest a project folder near their data, for example "
                "'~/Desktop/<project>/brier_outputs/'. Pass it as "
                "output_dir to whichever tool produces files. Do NOT "
                "silently let outputs land in the MCP cache directory "
                "(~/.cache/brier-mcp/...) - users routinely cannot find "
                "files there. If the user has previously named an output "
                "directory in this session, reuse it without asking "
                "again. If they say 'wherever / default / I don't care', "
                "use the configured output_directory if set, else fall "
                "back to a clearly-named folder under the data file's "
                "parent directory.\n\n"
                "ETA GRID - DO NOT HAND-WRITE (CRITICAL): When calling "
                "brier_i / brier_full / brier_s, DO NOT supply your own "
                "eta_list parameter. The tools have a principled "
                "log-spaced default starting at the floor (0.1) and "
                "topping at 10 - this is the right starting grid for "
                "the vast majority of cases. Hand-writing a grid like "
                "[0, 1, 5, 10, 25, 50, 100] from memory is WRONG; it "
                "is neither log-spaced nor based on the data, and it "
                "bypasses every diagnostic and escalation tool in the "
                "MCP. If the user explicitly says 'use this eta list' "
                "or 'try these specific values', then pass eta_list. "
                "Otherwise, omit eta_list entirely. If the boundary "
                "diagnostic _notice_eta_boundary fires (selected eta "
                "at the top of the grid), do NOT just rerun with a "
                "wider hand-spec grid - call brier_auto_tune_eta "
                "instead, which walks the ceiling ladder "
                "[30, 50, 100] automatically and stops at the first "
                "interior optimum. brier_auto_tune_eta also rejects "
                "explicit eta_list by design, forcing the principled "
                "path. The same rule applies to eta_floor / "
                "eta_ceiling / eta_n: only override when the user has "
                "named specific values, not on your own initiative.\n\n"
                "EXPRESSION VALIDATOR - DO NOT MISDESCRIBE: The "
                "expression validator ALLOWS namespace-qualified calls "
                "from this whitelist: BRIER::, base::, stats::, "
                "utils::, Matrix::. So BRIER::standardize_X(X), "
                "base::scale(X), stats::cor(x, y), etc. all pass. The "
                "validator denies :::  (three colons, non-exported "
                "access) and unknown namespaces. Do NOT tell the user "
                "the validator 'blocks ::' or that you have to avoid "
                "BRIER:: - this is FALSE and routinely causes you to "
                "pick a worse workaround (e.g. swapping BRIER::"
                "standardize_X for plain scale()). The deny-list "
                "blocks system(), system2(), eval(parse()), source(), "
                "file.*, do.call(), and a few literal characters, not "
                "namespace access.\n\n"
                "FALLBACK FLOW (if the user refuses to share a data "
                "path): use problem_description_questions and "
                "routing_questions as a v0.6-style interview, asking "
                "one at a time and respecting phase_gates."
            ),
        })
        return base

    # Mode 2: inspection_id supplied. Load the cache and build a
    # grounded recommendation.
    inspection = _load_inspection(inspection_id)
    if inspection is None:
        return {
            "status": "error",
            "message": (
                f"Inspection cache not found for inspection_id="
                f"{inspection_id!r}. Re-run inspect_user_data to "
                f"regenerate."
            ),
            "class": "CacheMiss",
            "where": "server.py:start_analysis",
        }

    combined = inspection.get("combined") or inspection.get("combined_assessment") or {}
    files = inspection.get("files") or []
    primary = files[0] if files else {}
    suggested_exprs = primary.get("suggested_exprs", {}) if primary else {}

    overrides = overrides or {}

    # Apply overrides on top of the inspection results.
    final_outcome_family = overrides.get(
        "outcome_family", combined.get("outcome_family", "unknown")
    )
    final_predictor_type = overrides.get(
        "predictor_type", combined.get("predictor_type", "unknown")
    )
    final_target_shape = overrides.get(
        "target_shape", combined.get("target_shape", "unknown")
    )
    final_external_shape = overrides.get(
        "external_shape", combined.get("external_shape", "unknown")
    )
    final_has_validation_set = overrides.get(
        "has_validation_set", combined.get("has_validation_set")
    )
    final_n_target = overrides.get("n_target", combined.get("n_target"))
    final_n_external_total = overrides.get(
        "n_external_total", combined.get("n_external_total")
    )
    final_p = overrides.get("p", combined.get("p"))
    final_M = overrides.get("M", combined.get("M"))

    has_individual_external_eff = (final_external_shape == "individual")
    size_rec = _recommend_for_sizes(
        final_n_target, final_n_external_total,
        has_individual_external_eff, M=final_M,
    )

    # Build the path recommendation.
    recommendation = _recommend_path(
        target_shape=final_target_shape,
        external_shape=final_external_shape,
        outcome_family=final_outcome_family,
        predictor_type=final_predictor_type,
        has_validation_set=final_has_validation_set,
        n_target=final_n_target,
        n_external_total=final_n_external_total,
        primary_path=primary.get("path"),
        suggested_exprs=suggested_exprs,
        size_recommendation=size_rec,
    )

    # Collect confidence notes for fields the AI should explicitly
    # confirm with the user.
    confidence_notes = []
    if primary and "heuristics" in primary:
        h = primary["heuristics"]
        for field_name, override_key in [
            ("outcome_family", "outcome_family"),
            ("predictor_type", "predictor_type"),
            ("data_shape", "target_shape"),
        ]:
            field = h.get(field_name) or {}
            conf = field.get("confidence", "unknown")
            applied_override = override_key in overrides
            if conf in ("low", "medium") and not applied_override:
                confidence_notes.append({
                    "field": field_name,
                    "value": field.get("value"),
                    "confidence": conf,
                    "evidence": field.get("evidence"),
                    "alternatives": field.get("alternatives", []),
                    "suggested_question": (
                        f"I'm only {conf}ly confident that {field_name} "
                        f"is {field.get('value')!r}. Could you confirm "
                        f"or correct?"
                    ),
                })

    if combined.get("has_time_to_event"):
        confidence_notes.append({
            "field": "time_to_event",
            "value": True,
            "confidence": "medium",
            "evidence": "data has time + event columns",
            "alternatives": [],
            "suggested_question": (
                "Your data looks like time-to-event (survival). BRIER "
                "does not support survival outcomes. Do you have a "
                "different non-survival outcome we could use instead?"
            ),
        })

    base.update({
        "inspection_summary": {
            "inspection_id": inspection_id,
            "files": [
                {
                    "path": f.get("path"),
                    "format": f.get("format"),
                    "n": f.get("derived", {}).get("n"),
                    "p": f.get("derived", {}).get("p"),
                    "structure_summary": _summarize_structure(
                        f.get("structure")
                    ),
                    "heuristics": f.get("heuristics"),
                }
                for f in files
            ],
            "combined_assessment_raw": combined,
        },
        "inferred_assessment": {
            "target_shape": final_target_shape,
            "external_shape": final_external_shape,
            "outcome_family": final_outcome_family,
            "predictor_type": final_predictor_type,
            "has_validation_set": final_has_validation_set,
            "n_target": final_n_target,
            "n_external_total": final_n_external_total,
            "p": final_p,
            "M": final_M,
            "applied_overrides": overrides,
        },
        "recommendation": recommendation,
        "confidence_notes": confidence_notes,
        "size_recommendation": size_rec,
        "ai_instructions": (
            "DATA-FIRST FLOW (continuing from step 5):\n"
            "(6) Present BOTH the structured inspection_summary "
            "    AND a short prose explanation to the user. Use the "
            "    structured table for clarity (n, p, M, family guess, "
            "    predictor type guess, etc.) and prose for context "
            "    (what this means for the model recommendation). For "
            "    each item in confidence_notes, hedge appropriately "
            "    and ask the user to confirm (do not state low-"
            "    confidence values as fact).\n"
            "(7) Present the recommendation: primary tool, "
            "    alternatives, reasons, AND the canonical_call (which "
            "    is already filled in with the user's actual R "
            "    expression paths from the inspection -- e.g. "
            "    'mydata$target$train$X' instead of generic "
            "    placeholders).\n"
            "(8) Ask: 'Does anything in this summary or recommendation "
            "    look wrong? If so, tell me what to change in plain "
            "    English (e.g. \"outcome is actually binomial\", "
            "    \"predictor type is gene expression\").' Parse the "
            "    user's free-text correction into the overrides dict.\n"
            "(9) If the user corrects anything, call start_analysis "
            "    again with the same inspection_id and the new "
            "    overrides. Re-present the updated summary and "
            "    recommendation.\n"
            "(10) Once confirmed, surface any applicable "
            "     preprocessing_hints, family_caveats, or path-"
            "     specific notes (e.g. paths.BRIERs.non_snp_predictors "
            "     if predictor_type != SNP and on BRIERs path).\n"
            "(11) Offer the baseline-first fit (baseline_offer).\n"
            "(12) Proceed to the fit using the canonical_call.\n\n"
            "Do NOT paste the structured dict verbatim. Thread it "
            "into natural dialogue, but show enough structure that "
            "the user can scan and spot errors quickly."
        ),
    })
    return base


def _load_inspection(inspection_id: str) -> Optional[dict]:
    """Load a cached inspection result by id.

    Returns the loaded dict, or None if the cache is missing or the
    id format is invalid.
    """
    if not inspection_id or not isinstance(inspection_id, str):
        return None
    # Sanitize: only allow expected characters in the id
    import re
    if not re.match(r"^insp_[a-zA-Z0-9_]+$", inspection_id):
        return None
    cache_dir = _inspection_cache_dir()
    cache_path = os.path.join(cache_dir, f"{inspection_id}.rds")
    if not os.path.exists(cache_path):
        return None

    # Round-trip through R to read the .rds and emit JSON.
    # Use the BRIER_CACHE_PATH env var to pass the path (more reliable
    # than --args after -e, which has parsing quirks).
    rscript = _find_rscript()
    r_code = (
        'cache_path <- Sys.getenv("BRIER_CACHE_PATH");'
        'cache <- readRDS(cache_path);'
        'cache$timestamp <- as.character(cache$timestamp);'
        'cat(jsonlite::toJSON(cache, auto_unbox = TRUE, force = TRUE, '
        '                     na = "null", null = "null"))'
    )
    env = os.environ.copy()
    env["BRIER_CACHE_PATH"] = cache_path
    try:
        proc = subprocess.run(
            [rscript, "--no-save", "--no-restore", "--no-init-file",
             "-e", r_code],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _inspection_cache_dir() -> str:
    """Return the directory where inspection caches live."""
    base = os.environ.get("XDG_CACHE_HOME")
    if not base:
        if os.name == "nt":
            base = os.environ.get(
                "LOCALAPPDATA",
                os.path.join(os.path.expanduser("~"), "AppData", "Local"),
            )
        else:
            base = os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "brier-mcp", "inspections")
    os.makedirs(d, exist_ok=True)
    return d


def _summarize_structure(structure: Optional[dict]) -> str:
    """Boil a structural description down to one short line."""
    if not isinstance(structure, dict):
        return "unknown"
    t = structure.get("type", "unknown")
    if t == "matrix":
        dims = structure.get("dim", [])
        return f"matrix {'x'.join(str(d) for d in dims)}"
    if t == "data.frame":
        dims = structure.get("dim", [])
        ncol = dims[1] if len(dims) > 1 else 0
        return f"data.frame {'x'.join(str(d) for d in dims)} ({ncol} columns)"
    if t == "list":
        nm = structure.get("names", [])
        return f"list with {len(nm)} entries: {', '.join(nm[:6])}"
    return t


def _recommend_path(
    target_shape: str,
    external_shape: str,
    outcome_family: str,
    predictor_type: str,
    has_validation_set: Optional[bool],
    n_target: Optional[int],
    n_external_total: Optional[int],
    primary_path: Optional[str],
    suggested_exprs: dict,
    size_recommendation: Optional[dict],
) -> dict:
    """Compute the recommended BRIER path given inferred assessment.

    Returns a dict with primary tool, alternatives, reasons, and a
    canonical_call filled in with the user's actual expression paths
    where possible.
    """
    # Determine the primary path
    if target_shape == "sumstats":
        primary = "BRIERs"
        reason = "Target is summary statistics (sumstats with corr column)."
        alternatives = []
    elif target_shape == "individual":
        if external_shape == "coefficients":
            primary = "BRIERi"
            reason = "Target is individual-level and external is pretrained coefficients."
            alternatives = []
        elif external_shape == "sumstats":
            primary = "BRIERi"
            reason = (
                "Target is individual-level and external is sumstats. "
                "BRIERi after running an upstream PRS method to convert "
                "the external sumstats into a coefficient vector."
            )
            alternatives = []
        elif external_shape == "individual":
            # Use size_recommendation here
            if size_recommendation:
                primary = size_recommendation["primary"]
                alternatives = size_recommendation.get("alternatives", [])
                reason = size_recommendation["reason"]
            else:
                primary = "BRIERfull"
                reason = (
                    "Target and external are both individual-level. "
                    "Default to BRIERfull; revisit if it's too slow."
                )
                alternatives = ["BRIERi"]
        elif external_shape == "none":
            primary = "BRIERi-baseline"
            reason = (
                "Target is individual-level and no external "
                "information is available. Fit a target-only LASSO "
                "as a baseline."
            )
            alternatives = []
        else:
            primary = "BRIERi-baseline"
            reason = (
                "External data shape is unclear; default to a "
                "target-only baseline. Confirm external data with "
                "the user to refine."
            )
            alternatives = ["BRIERi", "BRIERfull"]
    else:
        primary = "BRIERi-baseline"
        reason = (
            "Target data shape is unclear from inspection; cannot "
            "confidently route. Default to target-only baseline; "
            "confirm target shape with the user."
        )
        alternatives = ["BRIERi", "BRIERs", "BRIERfull"]

    # Build the canonical call with the user's actual expression paths
    canonical_call = _build_canonical_call(
        primary, primary_path, suggested_exprs, outcome_family
    )

    return {
        "primary": primary,
        "alternatives": alternatives,
        "reason": reason,
        "outcome_family": outcome_family,
        "predictor_type": predictor_type,
        "canonical_call": canonical_call,
        "has_validation_set": has_validation_set,
        "selection_criterion_suggestion": _suggest_selection_criterion(
            primary, outcome_family, has_validation_set
        ),
    }


def _build_canonical_call(
    primary: str,
    primary_path: Optional[str],
    suggested_exprs: dict,
    outcome_family: str,
) -> str:
    """Build a concrete tool-call snippet with the user's actual paths."""
    path_str = (
        f"'{primary_path}'" if primary_path else "'<your-data-path>'"
    )
    family_str = outcome_family if outcome_family != "unknown" else "gaussian"

    X_expr = suggested_exprs.get("target_X_expr", "<X_expr>")
    y_expr = suggested_exprs.get("target_y_expr", "<y_expr>")
    sumstats_expr = suggested_exprs.get("sumstats_expr", "<sumstats_expr>")
    beta_external_expr = suggested_exprs.get(
        "external_beta_expr", "<beta_external_expr>"
    )

    if primary == "BRIERi":
        return (
            f"brier_i(\n"
            f"  data_path = {path_str},\n"
            f"  X_expr = '{X_expr}',\n"
            f"  y_expr = '{y_expr}',\n"
            f"  beta_external_expr = '{beta_external_expr}',\n"
            f"  family = '{family_str}',\n"
            f"  multi_method = 'stacking'\n"
            f")"
        )
    if primary == "BRIERi-baseline":
        return (
            f"# First in R: beta_zero <- rep(0, ncol(X) + 1)\n"
            f"# Then save beta_zero into your .rds file alongside X and y.\n"
            f"brier_i(\n"
            f"  data_path = {path_str},\n"
            f"  X_expr = '{X_expr}',\n"
            f"  y_expr = '{y_expr}',\n"
            f"  beta_external_expr = '<your-zero-vector>',\n"
            f"  family = '{family_str}',\n"
            f"  eta_list = [0]   # target-only LASSO\n"
            f")"
        )
    if primary == "BRIERfull":
        return (
            f"# Prep step in R: stack target + externals into X.full, y.full,\n"
            f"# cohort.full (cohort=0 for target, 1..M for externals).\n"
            f"brier_full(\n"
            f"  data_path = {path_str},\n"
            f"  X_expr = '<X.full>',\n"
            f"  y_expr = '<y.full>',\n"
            f"  cohort_expr = '<cohort.full>',\n"
            f"  family = '{family_str}'\n"
            f")"
        )
    if primary == "BRIERs":
        return (
            f"# Step 1: build the LD matrix\n"
            f"cal_ld(data_path = {path_str}, X_expr = '{X_expr}')\n"
            f"# returns ld_id\n"
            f"\n"
            f"# Step 2: fit (ld_id from step 1)\n"
            f"brier_s(\n"
            f"  data_path = {path_str},\n"
            f"  sumstats_expr = '{sumstats_expr}',\n"
            f"  beta_external_expr = '{beta_external_expr}',\n"
            f"  family = '{family_str}',\n"
            f"  ld_id = '<ld_id from step 1>',\n"
            f"  multi_method = 'stacking'\n"
            f")"
        )
    return "# Could not build canonical call - confirm path with the user."


def _suggest_selection_criterion(
    primary: str,
    outcome_family: str,
    has_validation_set: Optional[bool],
) -> str:
    """Suggest a selection criterion given the primary tool + family + val set."""
    if primary == "BRIERfull":
        if not has_validation_set:
            return (
                "BRIERfull requires a validation set; no IC criteria are "
                "supported. If no validation set is available, BRIERfull "
                "is not feasible -- consider BRIERi instead."
            )
        fam = outcome_family if outcome_family != "unknown" else "gaussian"
        return f"brier_full_selection(criteria='{fam}.mspe', ...)"
    if has_validation_set:
        fam = outcome_family if outcome_family != "unknown" else "gaussian"
        crit = f"{fam}.mspe" if fam == "gaussian" else f"{fam}.dev"
        return (
            f"With validation set: {primary}_selection(criteria='{crit}', "
            f"X_val_expr=..., y_val_expr=..., data_path=...)."
        )
    # No validation set
    if primary == "BRIERi" or primary == "BRIERi-baseline":
        return "Without validation: brier_i_selection(criteria='BIC')."
    if primary == "BRIERs":
        return (
            "Without validation: brier_s_selection(criteria='Cp', "
            "TN=<n_target>). All IC criteria require TN."
        )
    return "Selection criterion depends on validation set availability."



@mcp.tool()
def get_workflow_guide() -> dict:
    """Return the full phase-by-phase BRIER analysis workflow guide.

    The compact server instructions cover routing and the workflow spine; this
    returns the COMPLETE guide (the same content as docs/AGENTS.md):
    the five phases (inspect, preprocess, fit, decide/evaluate, report), the
    STOP checkpoints, the baseline/decision layer (per-external evaluation,
    negative-transfer detection, propose-not-decide), and the performance-metric
    panel by outcome type. Call this when you want the detailed workflow rather
    than just tool routing, so you do not need a hand-copied AGENTS.md in the
    project.

    Returns:
        {status: "ok", guide: "<full markdown text>"} on success, or
        {status: "error", message: ...} if the guide file is not found.
    """
    # Prefer the shipped example; fall back to a project-local AGENTS.md if the
    # user placed a customized one next to the server.
    candidates = [
        SCRIPT_DIR / "docs" / "AGENTS.md",
        SCRIPT_DIR / "docs" / "AGENTS.md.example",
    ]
    for path in candidates:
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except Exception as err:  # noqa: BLE001
                return {"status": "error",
                        "message": f"could not read {path.name}: {err}",
                        "where": "server.py:get_workflow_guide"}
            return {"status": "ok", "source": path.name, "guide": text}
    return {"status": "error",
            "message": "workflow guide not found (expected docs/AGENTS.md)",
            "where": "server.py:get_workflow_guide"}




@mcp.tool()
def inspect_data(data_path: str) -> dict:
    """Describe the structure of a local R data file (.rda / .RData / .rds).

    Reads only metadata (object names, classes, dimensions, lengths) - it
    never materialises the actual data values, so it is safe to run on
    very large files (genotype matrices, LD matrices, etc.).

    Use this BEFORE calling any BRIER fitting tool to discover what objects
    a file contains and how to reference them. Typical workflow:

        1. User says "I have data at /Users/me/study.rda".
        2. Call inspect_data -> returns
           {top_level_names: ["X", "y", "cohort", "beta_external"]}.
        3. AI now knows the object names to pass as expression strings to
           the appropriate brier_* fitting tool.

    Args:
        data_path: Absolute path to a local .rda, .RData, or .rds file.
            On Windows, backslashes in JSON must be escaped as "\\\\".

    Returns:
        On success: {status: "ok", data_path, top_level_names, structure}
            where `structure` is a per-object description: matrix dims,
            data.frame columns + classes, list contents, factor levels,
            vector lengths.
        On error:   {status: "error", message, class, where}.
    """
    return _run_r("inspect_data.R", {"data_path": data_path})


@mcp.tool()
def inspect_user_data(
    data_paths: list,
    csv_options: Optional[dict] = None,
) -> dict:
    """Heuristic inspection of one or more user data files.

    Like inspect_data but also applies HEURISTICS to derive likely
    outcome family, predictor type, data shape, and other properties
    needed for routing in the start_analysis wizard.

    Supports .rds, .rda, .RData, .csv, .tsv, .txt, .xlsx/.xls, and the
    genotype binary formats .pgen (PLINK2), .bed (PLINK1), and .bgen.
    Genotype binaries are inspected via their companion files only
    (.pvar/.psam, .bim/.fam, .sample); the binary genotype data itself is
    never loaded, so inspection is safe on very large panels. xlsx needs
    the 'readxl' R package; if it is absent the file is reported with a
    clear message rather than failing. VCF is not yet supported.

    The result is cached on disk and an inspection_id is returned. Pass
    that inspection_id to start_analysis() to get a routing
    recommendation grounded in the inspection.

    Heuristics applied:
        * outcome_family: gaussian / binomial / poisson / time-to-event
          (from y values: cardinality and integer-ness)
        * predictor_type: SNP / gene_expression / protein / mixed
          (from column names: rsID, ensembl, gene symbol, uniprot patterns)
        * data_shape: individual / sumstats / coefficients
          (from object structure and column names)
        * splits: detects train/val/test field names
        * time_to_event: detects Surv objects or time+event/status columns
        * missingness: counts NAs in X and y

    Each heuristic returns {value, confidence, evidence, alternatives}.
    The AI should present results to the user with appropriate hedge
    based on confidence: high -> state it, medium -> "looks like X",
    low -> "best guess is X; please confirm".

    File-ordering convention: the FIRST path is treated as the TARGET
    file. Subsequent paths are treated as external sources (one per
    additional file).

    Args:
        data_paths: One or more file paths. First is target, rest are external.
        csv_options: Optional CSV/TSV parsing options:
            {header: bool=True, row_labels: bool=False,
             sep: str="auto" or ",", "\\t", " "}.

    Returns:
        On success: {status: "ok", inspection_id, inspection_path,
            files: [...per-file results...], combined_assessment: {...}}.
        On error:   {status: "error", message, class, where}.
    """
    if not data_paths or len(data_paths) == 0:
        return {
            "status": "error",
            "message": "data_paths must be a non-empty list",
            "class": "InvalidInput",
            "where": "server.py:inspect_user_data",
        }

    result = _run_r("inspect_user_data.R", {
        "data_paths": data_paths,
        "csv_options": csv_options,
    })
    # Piggyback the workflow-guide pointer onto a call the agent reliably makes
    # early, without overwriting anything the R script returned.
    if isinstance(result, dict) and result.get("status") == "ok":
        result.setdefault("workflow_guide_reminder", _WORKFLOW_GUIDE_REMINDER)
    return result


@mcp.tool()
def list_data_directory(dir_path: str, recursive: bool = False) -> dict:
    """List data files in a local directory.

    Lists R objects (.rda / .RData / .rds), tabular files (.csv / .tsv /
    .txt / .xlsx / .xls), and genotype binaries (.pgen / .bed / .bgen).

    Use this when the user mentions a data folder and you need to know
    what files are available before calling inspect_data on a specific
    file. Returns paths, basenames, sizes, and modification times - never
    file contents.

    Args:
        dir_path: Absolute path to a local directory.
        recursive: If true, descend into subdirectories. Default false.

    Returns:
        On success: {status: "ok", dir_path, recursive, n_files, files}
            where `files` is a list of {path, name, size_bytes, modified}
            entries.
        On error:   {status: "error", message, class, where}.
    """
    return _run_r("list_data_directory.R", {
        "dir_path": dir_path,
        "recursive": recursive,
    })


# --------------------------------------------------------------------------
# brier_i family: pretrained external + individual-level target data
# --------------------------------------------------------------------------

# Helper: pre-flight every R expression string with the deny-list. Returns
# the first error encountered, or None if all clear.
def _validate_exprs(**named_exprs) -> Optional[str]:
    for name, value in named_exprs.items():
        if value is None:
            continue
        err = _validate_expr(value, name)
        if err:
            return err
    return None


def _normalize_data_paths(
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> tuple[Optional[str], Optional[list]]:
    """v0.11: normalize either-or input to (data_path, data_paths).

    Tools accept either:
        data_path:  a single string path (legacy, v0.10.1 and earlier)
        data_paths: a list of string paths (preferred, v0.11+)

    Returns (data_path_legacy, data_paths_normalized) where
    data_paths_normalized is always either None or a non-empty list.
    Both fields are forwarded to R; R's resolve_data_paths_input
    prefers data_paths when both are set.
    """
    if data_paths is None and data_path is None:
        return None, None
    if data_paths is not None:
        if not isinstance(data_paths, list):
            data_paths = [str(data_paths)]
        if len(data_paths) == 0:
            return data_path, None
        return data_path, [str(p) for p in data_paths]
    # Only data_path given -> wrap into a one-element list
    return data_path, [data_path]


# --------------------------------------------------------------------------
# prep_data (v0.13): cache primitives + session lifecycle
# --------------------------------------------------------------------------

def _prep_cache_root() -> str:
    """Return the root directory for prep_data session caches."""
    cache_root = os.environ.get(
        "XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    d = os.path.join(cache_root, "brier-mcp", "prep_sessions")
    os.makedirs(d, exist_ok=True)
    return d


def _prep_session_dir(session_id: str) -> str:
    """Return the directory for a single prep session (created if missing)."""
    d = os.path.join(_prep_cache_root(), session_id)
    os.makedirs(d, exist_ok=True)
    return d


def _generate_prep_session_id() -> str:
    """Generate a fresh prep session id, format prep_<ts>_<rand>."""
    import random
    import string
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"prep_{ts}_{suffix}"


def _prep_log_append(session_id: str, op: str, args: dict,
                      result_summary: dict) -> None:
    """Append a JSONL record to the session's audit log."""
    log_path = os.path.join(_prep_session_dir(session_id), "log.jsonl")
    record = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "operation": op,
        # Strip very long values to keep the log compact and useful.
        "args": {k: (v if not isinstance(v, (list, dict))
                       or len(str(v)) < 500 else f"<{type(v).__name__} omitted>")
                   for k, v in args.items() if v is not None},
        "result_summary": result_summary,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _prep_log_read(session_id: str) -> list:
    """Read the audit log for a session as a list of dicts."""
    log_path = os.path.join(_prep_session_dir(session_id), "log.jsonl")
    if not os.path.exists(log_path):
        return []
    records = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


# State-file convention for a prep session: state.rds holds an R list
# named `state` with:
#   - aliases: named list of loaded data objects (the working bench)
#   - meta: list with paths, options
# The R operations all read state.rds, mutate `state`, and write it back.


# Default eta-grid parameters (v0.10.3). The grid is a literal 0 (the
# target-only / no-borrow anchor) followed by eta_n log-spaced points
# whose endpoints land exactly on eta_floor and eta_ceiling.
_DEFAULT_ETA_FLOOR = 0.1
_DEFAULT_ETA_CEILING = 10.0
_DEFAULT_ETA_N = 10


def _build_eta_grid(
    eta_floor: float = _DEFAULT_ETA_FLOOR,
    eta_ceiling: float = _DEFAULT_ETA_CEILING,
    eta_n: int = _DEFAULT_ETA_N,
) -> list:
    """Construct the principled default eta grid (v0.10.3).

    Returns c(0, exp(seq(log(floor), log(ceiling), length.out=n))) as a
    Python list of floats. The leading 0 is the target-only anchor; the
    remaining eta_n points are log-spaced with endpoints exactly at
    eta_floor and eta_ceiling. Total length is eta_n + 1.

    This grid is built in Python (not R) so the exact numbers are stored
    in the fit metadata and appear verbatim in the reproduce.R script.
    """
    if eta_floor <= 0:
        raise ValueError("eta_floor must be positive (the 0 anchor is "
                         "added automatically)")
    if eta_ceiling <= eta_floor:
        raise ValueError("eta_ceiling must be greater than eta_floor")
    if eta_n < 2:
        raise ValueError("eta_n must be at least 2")
    log_lo, log_hi = math.log(eta_floor), math.log(eta_ceiling)
    pts = [0.0]
    for i in range(eta_n):
        frac = i / (eta_n - 1)
        pts.append(math.exp(log_lo + (log_hi - log_lo) * frac))
    return pts


def _resolve_eta_list(
    eta_list: Optional[list],
    eta_floor: float = _DEFAULT_ETA_FLOOR,
    eta_ceiling: float = _DEFAULT_ETA_CEILING,
    eta_n: int = _DEFAULT_ETA_N,
) -> list:
    """Return the eta grid to use for a fit.

    If the caller supplied an explicit eta_list, it is returned unchanged
    (the floor/ceiling/n knobs are ignored). Otherwise the principled
    default grid is constructed from the knobs.
    """
    if eta_list is not None:
        return eta_list
    return _build_eta_grid(eta_floor, eta_ceiling, eta_n)


def _eta_grid_max(eta_list: list):
    """Return the maximum scalar eta in a (possibly nested) eta grid.

    eta_list may be a flat list (M=1) or a list of lists (M>=2). Returns
    None if it can't be interpreted numerically.
    """
    try:
        flat = []
        for x in eta_list:
            if isinstance(x, (list, tuple)):
                flat.extend(float(v) for v in x)
            else:
                flat.append(float(x))
        return max(flat) if flat else None
    except (TypeError, ValueError):
        return None


def _eta_axes(eta_list, n_comp: int):
    """The PER-SOURCE eta axes, when the grid carries them; else None.

    With M > 1 externals and multi_method="ind", eta is a VECTOR (one component per
    source) and each component has its OWN axis: the selection scripts emit one grid
    per source (a list of M vectors). With M = 1, or an older flattened grid, there
    is a single shared axis and this returns None.
    """
    if (n_comp > 1
            and isinstance(eta_list, (list, tuple))
            and len(eta_list) == n_comp
            and all(isinstance(a, (list, tuple)) and a for a in eta_list)):
        try:
            return [[float(v) for v in axis] for axis in eta_list]
        except (TypeError, ValueError):
            return None
    return None


def _boundary_optimum_notice(eta_min, eta_list, where: str = "selection"):
    """Return a diagnostic notice string if the selected eta sits at the
    top of the grid, else None.

    eta_min may be a scalar (M=1) or a list (M>=2). The check fires if ANY
    component of eta_min is at the top of ITS OWN axis. ANY, not ALL: each eta_k is a
    separate transfer strength with its own axis, so if source 1 pins while source 2
    sits interior, the fit is still truncated in the source-1 direction, and every
    coefficient is estimated conditional on that truncated value. Requiring ALL to pin
    would silently pass exactly the case where one source wants unbounded transfer and
    another wants a little.

    PER-AXIS, not against a global maximum. The grids can legitimately differ per
    source (BRIER accepts an `eta.list` of M vectors), and comparing every component
    against the largest value anywhere in the grid MISSES a source that pins at the top
    of its own, shorter axis: a false negative, which is the failure this diagnostic
    exists to prevent.
    """
    if eta_min is None:
        return None
    # Normalize eta_min to a list of floats
    try:
        if isinstance(eta_min, (list, tuple)):
            comps = [float(v) for v in eta_min]
        else:
            comps = [float(eta_min)]
    except (TypeError, ValueError):
        return None

    axes = _eta_axes(eta_list, len(comps))
    if axes is not None:
        tops = [max(a) for a in axes]
        pinned = [k for k, c in enumerate(comps)
                  if abs(c - tops[k]) < 1e-9]
        if not pinned:
            return None
        top = max(tops[k] for k in pinned)
        which = ", ".join(f"external {k + 1} (eta={comps[k]:g}, its grid ends at "
                          f"{tops[k]:g})" for k in pinned)
        detail = f"Selected eta is at the top of the {where} grid for {which}. "
    else:
        grid_max = _eta_grid_max(eta_list)
        if grid_max is None:
            return None
        if not any(abs(c - grid_max) < 1e-9 for c in comps):
            return None
        top = grid_max
        detail = (f"Selected eta ({grid_max:g}) is at the top of the {where} "
                  f"grid. ")
    suggested = top * 5
    return (
        detail
        + f"The optimum may lie beyond it; consider refitting with a higher "
        f"eta_ceiling (e.g. {suggested:g}). If the eta curve has already "
        f"flattened or peaked before the boundary, the current ceiling is "
        f"fine and no refit is needed."
    )


def _annotate_selection_boundary(result: dict) -> dict:
    """If a selection result's chosen eta sits at the top of the eta grid,
    attach a boundary-optimum diagnostic notice. No-op on errors or when
    the optimum is interior. Operates on the dict returned by the R
    selection scripts (which now include eta_grid_values)."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return result
    eta_min = result.get("selected_eta")
    grid_vals = result.get("eta_grid_values")
    if grid_vals is None:
        return result
    notice = _boundary_optimum_notice(eta_min, grid_vals, where="selection")
    if notice:
        result["_notice_eta_boundary"] = notice
    return result


def _is_near_boundary(eta_min, eta_list, top_fraction: float = 0.20) -> bool:
    """Return True if any component of eta_min lies in the top
    `top_fraction` of the log-spaced eta grid.

    For the default grid [0, 0.1, 0.167, ..., 5.995, 10] with non-zero
    log range [log(0.1), log(10)], top_fraction=0.20 means the threshold
    is exp(log(0.1) + 0.8 * (log(10) - log(0.1))) ~= 3.98 - so any
    selected eta >= ~3.98 in the default grid counts as near-boundary.

    This is the trigger for v0.11 auto-escalation, which is more
    aggressive than the strict-equality boundary diagnostic used in
    v0.10.3's _boundary_optimum_notice (that one stays diagnostic-only).
    """
    if eta_min is None:
        return False
    # Flatten the grid to scalars, drop the 0 anchor (we want the
    # log-spaced range, not the zero baseline).
    try:
        flat = []
        for x in eta_list:
            if isinstance(x, (list, tuple)):
                flat.extend(float(v) for v in x)
            else:
                flat.append(float(x))
        nonzero = [v for v in flat if v > 0]
        if not nonzero:
            return False
        lo, hi = min(nonzero), max(nonzero)
        if hi <= lo:
            return False
        log_lo, log_hi = math.log(lo), math.log(hi)
        threshold = math.exp(log_lo + (1.0 - top_fraction) * (log_hi - log_lo))
    except (TypeError, ValueError):
        return False
    # Normalize eta_min to components
    try:
        if isinstance(eta_min, (list, tuple)):
            comps = [float(v) for v in eta_min]
        else:
            comps = [float(eta_min)]
    except (TypeError, ValueError):
        return False
    return any(c >= threshold - 1e-9 for c in comps)


def _ladder_above(initial_ceiling: float,
                   ladder: list) -> list:
    """Return the rungs of `ladder` strictly greater than `initial_ceiling`,
    in ascending order. Used to skip rungs already covered by the
    initial fit (e.g. if the user starts at ceiling=50, only rungs >50
    are walked).
    """
    try:
        ic = float(initial_ceiling)
        return sorted(float(r) for r in ladder if float(r) > ic)
    except (TypeError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Penalty knobs (alpha / penalty / gamma), shared across the fit tools.
# These flow through BRIER's `...` into the per-eta worker (BRIERi.eta /
# BRIERs.eta) where they are consumed. All are OPTIONAL: omit to use the BRIER
# default (LASSO, alpha=1). penalty.factor is handled per-tool via its own
# *_expr argument. See BRIERi.eta: alpha in (0, 1], penalty in {LASSO,SCAD,MCP}
# (case-sensitive), gamma > 2 for SCAD and > 1 for MCP.
# ---------------------------------------------------------------------------
_PENALTY_CHOICES = ("LASSO", "SCAD", "MCP")


def _validate_penalty(alpha, penalty, gamma):
    """Return an error string if any penalty knob is out of range, else None."""
    if alpha is not None:
        try:
            a = float(alpha)
        except (TypeError, ValueError):
            return "alpha must be a number in (0, 1]"
        if not (0.0 < a <= 1.0):
            return (
                "OMIT `alpha` entirely to use the BRIER default (LASSO, alpha=1); it is an OPTIONAL knob and you should not set it unless the user asked for elastic net. If you do set it, alpha must be in (0, 1]. BRIER rejects alpha <= 0.")
    if penalty is not None and penalty != "":
        if str(penalty).upper() not in _PENALTY_CHOICES:
            return (f"penalty must be one of LASSO, SCAD, MCP (got "
                    f"'{penalty}')")
    if gamma is not None:
        try:
            float(gamma)
        except (TypeError, ValueError):
            return "gamma must be a number"
    return None


def _penalty_payload(alpha, penalty, gamma):
    """Build the JSON payload fragment for the penalty knobs (normalizing the
    penalty name to upper case so BRIER's case-sensitive match.arg accepts it)."""
    return {
        "alpha": alpha,
        "penalty": (str(penalty).upper()
                    if isinstance(penalty, str) and penalty else penalty),
        "gamma": gamma,
    }


@mcp.tool()
def brier_i(
    X_expr: str,
    y_expr: str,
    beta_external_expr: str,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
    family: str = "gaussian",
    eta_list: Optional[list] = None,
    eta_floor: float = 0.1,
    eta_ceiling: float = 10.0,
    eta_n: int = 10,
    multi_method: str = "auto",
    penalty_factor_expr: Optional[str] = None,
    alpha: Optional[float] = None,
    penalty: Optional[str] = None,
    gamma: Optional[float] = None,
    trace: bool = False,
) -> dict:
    """Fit BRIERi(): integrate pretrained external coefficients with individual-level target data.

    BRIERi is the right BRIER function when (a) you have individual-level
    X / y for the target cohort, and (b) external information is one or
    more pretrained coefficient vectors (e.g. PRS weights, penalized
    regression coefficients from a prior study).

    SPLIT ACROSS FILES? USE data_paths. If the target X / y and the
    external coefficients live in separate files, do NOT merge them with
    a script. Pass every file in `data_paths` and reference each by its
    basename (filename without extension), e.g. data_paths=["target.rds",
    "external.rds"] with X_expr="target$X" and
    beta_external_expr="external$beta". Single-file inputs can still use
    data_path (singular).

    IMPORTANT shape conventions:
        - X is (n_target x p), an individual-level predictor matrix.
        - y is length n_target.
        - beta_external is (p + 1) x M: the FIRST ROW is the intercept
          slot (set to 0 if no external intercept exists), followed by
          one column per external model. This is asymmetric with BRIERs
          (which does NOT take an intercept row). Forgetting the
          intercept row is a common silent-failure trap.

    The fit object is cached on disk in ~/.cache/brier-mcp/fits/ (or
    XDG_CACHE_HOME equivalent) and returned by fit_id; pass that fit_id
    to brier_i_selection to choose hyperparameters. The cache persists
    across Rscript invocations until you delete the cache directory.

    Args:
        data_path: Absolute path to a local .rda / .RData / .rds file
            containing the target data and external coefficients.
        X_expr: R expression evaluated inside the loaded data env that
            returns the target predictor matrix. Simple accessors only
            (e.g. "X", "data$X", "out$X.training"). No function calls
            other than coercions; see deny-list.
        y_expr: R expression that returns the target outcome vector.
        beta_external_expr: R expression that returns the external
            coefficient matrix of shape (p+1) x M.
        family: One of "gaussian" (default), "binomial", "poisson".
            ALWAYS pass explicitly when the outcome is binary or count;
            the default is a silent-failure trap.
        eta_list: Optional numeric vector of integration weights to
            evaluate. Default is BRIER's built-in grid.
        multi_method: "auto" (default), "stacking", "PCA" or "ind". Controls how
            MULTIPLE externals (M > 1) are combined. "ind" tunes one eta PER SOURCE
            (eta becomes a vector and the grid a product), so it can lean on a strong
            external and ignore a weak one. "stacking" collapses the sources into ONE
            combined predictor BEFORE transfer, so a single scalar eta must cover them
            all. Measured on a 2-source case (one useful external, one nearly empty),
            "ind" beat "stacking" on validation AND test -- but its grid is n^M, so at
            M = 3 it is ~9000 fits.
            "auto" resolves to "ind" up to M = 2 and "stacking" from M = 3: the better
            method where it is affordable, the affordable one where it is not. An
            EXPLICIT value ALWAYS WINS, so pass "stacking" (or "ind") to override the
            rule. The value actually used is echoed back as multi_method_used.
            "ind" for combining multiple external models. "ind" is
            multiplicative in M and impractical for M >= 5.
        penalty_factor_expr: Optional R expression returning a numeric
            vector of length p, with 0 for unpenalized predictors and 1
            for penalized predictors. Useful for keeping demographic
            covariates unpenalized (adjusted-for but not shrunk). Default
            is all-ones (every predictor penalized).
        alpha: Optional elastic-net mixing in (0, 1]. 1 = lasso (default);
            a small positive value approaches ridge. BRIER rejects
            alpha <= 0. Omit to use the default (1).
        penalty: Optional penalty type, one of "LASSO" (default), "SCAD",
            "MCP". Omit for LASSO. Only set when the user asks for a
            non-convex penalty.
        gamma: Optional concavity parameter for SCAD/MCP (default 3.7 for
            SCAD, 3 for MCP; must be > 2 for SCAD, > 1 for MCP). Ignored
            under LASSO.
        trace: If true, BRIERi prints progress messages to the R
            subprocess stderr.

    Returns:
        On success: {status: "ok", fit_id, fit_path, family, n_target, p,
            M_external, eta_list_used, multi_method_used, timing,
            _notice_*, _followup_*}. The fit_id is required to call
            brier_i_selection.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(
        X_expr=X_expr, y_expr=y_expr,
        beta_external_expr=beta_external_expr,
        penalty_factor_expr=penalty_factor_expr,
    )
    if err:
        return {"status": "error", "message": err, "class": "DenylistViolation",
                "where": "server.py:brier_i"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:brier_i"}

    try:
        resolved_eta = _resolve_eta_list(eta_list, eta_floor, eta_ceiling,
                                          eta_n)
    except ValueError as e:
        return {"status": "error", "message": str(e),
                "class": "InvalidEtaGrid", "where": "server.py:brier_i"}

    perr = _validate_penalty(alpha, penalty, gamma)
    if perr:
        return {"status": "error", "message": perr,
                "class": "InvalidPenalty", "where": "server.py:brier_i"}

    return _run_r("brier_i.R", {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "X_expr": X_expr,
        "y_expr": y_expr,
        "beta_external_expr": beta_external_expr,
        "family": family,
        "eta_list": resolved_eta,
        "multi_method": multi_method,
        "penalty_factor_expr": penalty_factor_expr,
        "trace": trace,
        **_penalty_payload(alpha, penalty, gamma),
    })


@mcp.tool()
def brier_i_cv(
    X_expr: str,
    y_expr: str,
    beta_external_expr: str,
    family: str = "gaussian",
    eta_list: Optional[list] = None,
    eta_floor: float = 0.1,
    eta_ceiling: float = 10.0,
    eta_n: int = 10,
    multi_method: str = "auto",
    penalty_factor_expr: Optional[str] = None,
    alpha: Optional[float] = None,
    penalty: Optional[str] = None,
    gamma: Optional[float] = None,
    nfolds: int = 5,
    seed: int = 1,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Cross-validation tuning for BRIERi.

    BRIERi.cv does NOT standardize X or y internally. If your inputs are
    pre-standardized (column-scaled X, residualized y), CV estimates
    leak across folds and become optimistic. Pass raw X / y, or
    perform preprocessing inside each fold yourself. The tool emits a
    _notice_brier_i_cv_leakage warning on every successful call to
    remind the user.

    Args:
        Same as brier_i (including alpha / penalty / gamma /
        penalty_factor_expr), with two extras:
        nfolds: Number of CV folds. Default 5.
        seed: Random seed for fold assignment. Default 1.

    Returns:
        On success: {status: "ok", selected_eta, selected_lambda,
            cv_metric, nfolds_used, seed_used, timing,
            _notice_brier_i_cv_leakage, _notice_*}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(
        X_expr=X_expr, y_expr=y_expr,
        beta_external_expr=beta_external_expr,
        penalty_factor_expr=penalty_factor_expr,
    )
    if err:
        return {"status": "error", "message": err, "class": "DenylistViolation",
                "where": "server.py:brier_i_cv"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:brier_i_cv"}

    try:
        resolved_eta = _resolve_eta_list(eta_list, eta_floor, eta_ceiling,
                                          eta_n)
    except ValueError as e:
        return {"status": "error", "message": str(e),
                "class": "InvalidEtaGrid", "where": "server.py:brier_i_cv"}

    perr = _validate_penalty(alpha, penalty, gamma)
    if perr:
        return {"status": "error", "message": perr,
                "class": "InvalidPenalty", "where": "server.py:brier_i_cv"}

    return _run_r("brier_i_cv.R", {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "X_expr": X_expr,
        "y_expr": y_expr,
        "beta_external_expr": beta_external_expr,
        "family": family,
        "eta_list": resolved_eta,
        "multi_method": multi_method,
        "penalty_factor_expr": penalty_factor_expr,
        "nfolds": nfolds,
        "seed": seed,
        **_penalty_payload(alpha, penalty, gamma),
    })


@mcp.tool()
def brier_i_selection(
    fit_id: str,
    criteria: str,
    X_val_expr: Optional[str] = None,
    y_val_expr: Optional[str] = None,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Select optimal (eta, lambda) from a cached BRIERi fit.

    Two modes:
      * IC-based selection: criteria in {"BIC", "Cp", "GCV"}.
        Does not require validation data.
      * Validation-set selection: criteria like "gaussian.mspe",
        "binomial.dev", "binomial.AUC" etc. Requires X_val_expr,
        y_val_expr, and data_path.

    Recommended default from BRIER simulations: criteria="BIC" when no
    held-out validation set exists.

    Args:
        fit_id: The fit_id returned by a prior brier_i call. The fit
            object is loaded from the session cache.
        criteria: One of the IC names or one of the family-specific
            validation metrics. See llms.txt for the full list.
        X_val_expr: R expression returning a validation predictor matrix.
            Required for validation-set criteria.
        y_val_expr: R expression returning a validation outcome vector.
            Required for validation-set criteria.
        data_path: Path to the .rda/.RData/.rds containing X_val and
            y_val. Required for validation-set criteria.

    Returns:
        On success: {status: "ok", fit_id, criteria, selected_eta,
            selected_lambda, selected_metric, _notice_*}.
        On error:   {status: "error", message, class, where}.
            Common error: "Fitted object not found" if the cache was
            cleared. The cache lives in ~/.cache/brier-mcp/fits/ (or
            XDG_CACHE_HOME equivalent).
    """
    err = _validate_exprs(X_val_expr=X_val_expr, y_val_expr=y_val_expr)
    if err:
        return {"status": "error", "message": err, "class": "DenylistViolation",
                "where": "server.py:brier_i_selection"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    return _annotate_selection_boundary(_run_r("brier_i_selection.R", {
        "fit_id": fit_id,
        "criteria": criteria,
        "X_val_expr": X_val_expr,
        "y_val_expr": y_val_expr,
        "data_path": dp_legacy,
        "data_paths": dp_list,
    }))


# --------------------------------------------------------------------------
# brier_full family: pooled-cohort integration with raw external data
# --------------------------------------------------------------------------

@mcp.tool()
def brier_full(
    X_expr: str,
    y_expr: str,
    cohort_expr: str,
    family: str = "gaussian",
    eta_list: Optional[list] = None,
    eta_floor: float = 0.1,
    eta_ceiling: float = 10.0,
    eta_n: int = 10,
    penalty_factor_expr: Optional[str] = None,
    alpha: Optional[float] = None,
    penalty: Optional[str] = None,
    gamma: Optional[float] = None,
    trace: bool = False,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Fit BRIERfull(): pool target + external cohorts with raw individual-level data.

    Use BRIERfull when you have INDIVIDUAL-LEVEL data for BOTH the target
    cohort AND the external cohort(s) (rather than pretrained external
    coefficients). The function jointly fits a coefficient vector for
    target prediction while borrowing from external cohorts via a
    Bregman-divergence integration term.

    DATA SHAPE REQUIREMENTS:
        - X is the STACKED predictor matrix: rows from target FIRST,
          then external cohort 1, then external cohort 2, etc.
          Shape: (n_target + sum(n_external_k)) x p.
        - y is the stacked outcome vector, same row order as X.
        - cohort is an integer vector of the same length as y, with:
              0  = target sample
              1  = external cohort 1
              2  = external cohort 2
              ...
          At least one 0 (target) AND at least one positive value
          (external) are required. For a target-only baseline, use
          brier_i with eta=0 instead.

    CANONICAL CONSTRUCTION PATTERN (in R, before calling this tool):
        X.full <- rbind(target$X, ext1$X, ext2$X)
        y.full <- c(target$y, ext1$y, ext2$y)
        cohort.full <- c(rep(0L, nrow(target$X)),
                         rep(1L, nrow(ext1$X)),
                         rep(2L, nrow(ext2$X)))

    MULTI-FILE: if target and external cohorts live in separate files,
    pass them all in data_paths and reference each by basename in the
    rbind/c() expressions, e.g. data_paths=["target.rds", "ext1.rds"]
    with X_expr="rbind(target$X, ext1$X)". Each file loads under its
    basename. (Heavy cohort reshaping is better handled by prep_data
    once available.)

    The fit is cached on disk in ~/.cache/brier-mcp/fits/ and returned
    via fit_id; pass that fit_id to brier_full_selection to choose
    hyperparameters via validation-set criteria (BRIERfull.selection
    does NOT support BIC, Cp, or GCV; held-out validation data is
    required).

    Args:
        data_path: Absolute path to a local .rda/.RData/.rds file
            containing the stacked X, y, and cohort objects.
        X_expr: R expression that evaluates to the stacked predictor
            matrix.
        y_expr: R expression that evaluates to the stacked outcome
            vector.
        cohort_expr: R expression that evaluates to the integer cohort
            indicator vector (0=target, 1+=external).
        family: One of "gaussian" (default), "binomial", "poisson".
        eta_list: Optional numeric vector of integration weights.
            Default is the recommended log-spaced grid:
            c(0, exp(seq(log(0.1), log(10), length.out = 20))).
        penalty_factor_expr: Optional R expression for a length-p vector
            with 0 for unpenalized predictors and 1 for penalized
            (e.g. demographic covariates to adjust for). Default all-ones.
        alpha: Optional elastic-net mixing in (0, 1]. 1 = lasso (default).
        penalty: Optional "LASSO" (default), "SCAD", or "MCP".
        gamma: Optional SCAD/MCP concavity (> 2 SCAD, > 1 MCP).
        trace: If true, BRIERfull prints progress messages.

    Returns:
        On success: {status: "ok", fit_id, fit_path, family, n_target,
            n_external_per_cohort, n_total, p, M_external,
            eta_list_used, timing, _notice_*, _followup_*}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(
        X_expr=X_expr, y_expr=y_expr, cohort_expr=cohort_expr,
        penalty_factor_expr=penalty_factor_expr,
    )
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:brier_full"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:brier_full"}

    try:
        resolved_eta = _resolve_eta_list(eta_list, eta_floor, eta_ceiling,
                                          eta_n)
    except ValueError as e:
        return {"status": "error", "message": str(e),
                "class": "InvalidEtaGrid", "where": "server.py:brier_full"}

    perr = _validate_penalty(alpha, penalty, gamma)
    if perr:
        return {"status": "error", "message": perr,
                "class": "InvalidPenalty", "where": "server.py:brier_full"}

    return _run_r("brier_full.R", {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "X_expr": X_expr,
        "y_expr": y_expr,
        "cohort_expr": cohort_expr,
        "family": family,
        "eta_list": resolved_eta,
        "penalty_factor_expr": penalty_factor_expr,
        "trace": trace,
        **_penalty_payload(alpha, penalty, gamma),
    })


@mcp.tool()
def brier_full_selection(
    fit_id: str,
    criteria: str,
    X_val_expr: str,
    y_val_expr: str,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Select optimal (eta, lambda) from a cached BRIERfull fit using a validation set.

    UNLIKE brier_i_selection, BRIERfull.selection ONLY accepts
    validation-set criteria. IC criteria (BIC, Cp, GCV) are not
    supported. A held-out X.val and y.val are always required.

    Valid criteria (family-specific):
        gaussian:  "gaussian.mspe", "gaussian.rsq"
        binomial:  "binomial.dev", "binomial.mcfrsq",
                   "binomial.tjursq", "binomial.auc"
        poisson:   "poisson.dev"

    The selection result is cached and returned as selection_id; pass
    that selection_id to brier_predict or brier_evaluate to predict on
    new data without restating eta/lambda.

    Args:
        fit_id: The fit_id returned by a prior brier_full call.
        criteria: One of the validation-set metrics above.
        X_val_expr: R expression returning the validation predictor
            matrix (typically target-only, not stacked).
        y_val_expr: R expression returning the validation outcome
            vector.
        data_path: Path to the .rda/.RData/.rds file containing X_val
            and y_val.

    Returns:
        On success: {status: "ok", fit_id, selection_id, selection_path,
            criteria, selected_eta, selected_lambda, selected_metric,
            _notice_*}.
        On error:   {status: "error", message, class, where}.
            If criteria is one of BIC/Cp/GCV, the error message
            specifically directs the user to brier_i_selection instead.
    """
    err = _validate_exprs(X_val_expr=X_val_expr, y_val_expr=y_val_expr)
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:brier_full_selection"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:brier_full_selection"}

    return _annotate_selection_boundary(_run_r("brier_full_selection.R", {
        "fit_id": fit_id,
        "criteria": criteria,
        "X_val_expr": X_val_expr,
        "y_val_expr": y_val_expr,
        "data_path": dp_legacy,
        "data_paths": dp_list,
    }))


# --------------------------------------------------------------------------
# brier_s family + LD utilities: summary-statistics target workflow
# --------------------------------------------------------------------------

@mcp.tool()
def get_ldb(ancestry: str, build: str) -> dict:
    """Return Berisa-Pickrell LD block coordinates for a given ancestry + build.

    BRIER ships pre-computed approximately-independent LD block coordinates
    from Berisa and Pickrell (2016) for three ancestries (AFR, EAS, EUR)
    and two genome builds (hg19, hg38). The blocks are used by cal_ld()
    to construct sparse LD matrices.

    Returns a path to the bundled BED file plus summary metadata
    (number of blocks, chromosome format). The actual block coordinates
    are loaded by cal_ld(); you don't typically read this BED file
    yourself.

    Args:
        ancestry: One of "AFR", "EAS", "EUR".
        build:    One of "hg19", "hg38".

    Returns:
        On success: {status: "ok", ancestry, build, bed_path, n_blocks,
            n_chromosomes, chr_format, _notice_chr_prefix_mismatch}.
            The notice fires when the BED file uses "chr1"-style labels
            (always, for the bundled files); it reminds the user that
            numeric-CHR sumstats need conversion before joining.
        On error:   {status: "error", message, class, where}.
    """
    return _run_r("get_ldb.R", {
        "ancestry": ancestry,
        "build": build,
    })


@mcp.tool()
def cal_ld(
    X_expr: str,
    tau: Optional[float] = None,
    ldb_expr: Optional[str] = None,
    ldb_path: Optional[str] = None,
    snp_info_expr: Optional[str] = None,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Build an LD matrix from a genotype reference panel.

    Wraps BRIER::calLD(). Computes the column-wise cross-product
    (essentially X'X / n) from the reference-panel genotype matrix,
    optionally restricted to within-LD-block correlations when LDB
    coordinates are supplied. Drops constant-genotype columns; the
    retained indices come back as $nz inside the cached LD object.

    The result is cached to disk and returned by `ld_id`; pass that
    `ld_id` to brier_s and the sumstats / beta.external arguments will
    be automatically subset by the retained-variant indices.

    Args:
        data_path: Path to a file holding the reference panel. Accepted via
            data_path / data_paths: R binaries (.rda/.RData/.rds); tabular
            matrices (.csv/.tsv/.txt/.tab/.dat, including .gz/.bgz); and genotype
            binaries (PLINK1 .bed + .bim/.fam, PLINK2 .pgen + .pvar/.psam). For a
            .bed/.pgen pass ONLY the .bed/.pgen path (companions are auto-found),
            and note that reading them needs an R package (genio or BEDMatrix for
            .bed, pgenlibr for .pgen), else a clear error names what to install.
        X_expr: R expression for the genotype reference panel matrix (shape
            n_ref x p), e.g. "panel" for a genotype binary, or
            "as.matrix(panel[, -1])" for a tabular file with a leading id column.
        tau: Optional shrinkage parameter. Default is 0 (no shrinkage).
        ldb_expr: R expression for an in-memory LD block data frame
            (CHR, start, stop columns). Mutually exclusive with ldb_path.
        ldb_path: Filesystem path to a BED file. Typically the output of
            get_ldb(). Mutually exclusive with ldb_expr.
        snp_info_expr: R expression for a SNP info table with CHR and BP
            columns. Required when ldb_expr or ldb_path is provided.

    Returns:
        On success: {status: "ok", ld_id, ld_path, p_input, p_retained,
            n_dropped, sparsity, block_count, _notice_subset_required}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(
        X_expr=X_expr,
        ldb_expr=ldb_expr, snp_info_expr=snp_info_expr,
    )
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:cal_ld"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:cal_ld"}

    return _run_r("cal_ld.R", {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "X_expr": X_expr,
        "tau": tau,
        "ldb_expr": ldb_expr,
        "ldb_path": ldb_path,
        "snp_info_expr": snp_info_expr,
    })


@mcp.tool()
def brier_s(
    sumstats_expr: str,
    beta_external_expr: str,
    family: str = "gaussian",
    ld_id: Optional[str] = None,
    XtX_expr: Optional[str] = None,
    multi_method: str = "auto",
    eta_list: Optional[list] = None,
    eta_floor: float = 0.1,
    eta_ceiling: float = 10.0,
    eta_n: int = 10,
    penalty_factor_expr: Optional[str] = None,
    alpha: Optional[float] = None,
    penalty: Optional[str] = None,
    gamma: Optional[float] = None,
    trace: bool = False,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Fit BRIERs() with a summary-statistics target.

    Use BRIERs when the target cohort is represented by SUMMARY STATISTICS
    (per-variant marginal correlations + an LD matrix) rather than
    individual-level X / y. The external information is one or more
    pretrained coefficient vectors, similar to BRIERi.

    SPLIT ACROSS FILES? USE data_paths. A very common layout has the
    target sumstats + LD in one file and the external coefficients in a
    SEPARATE file (e.g. a cross-ancestry PRS: target=AFR sumstats,
    external=EUR betas). You do NOT need to merge them into one file and
    you should NOT write a merge script. Pass every file in `data_paths`
    and reference each by its basename in the expressions. Each file's
    contents are loaded under a variable named after the file (without
    extension), so:
        brier_s(
            data_paths=["/path/height_AFR.RData",
                         "/path/height_EUR.RData"],
            sumstats_expr="height_AFR$sumstats",
            XtX_expr="height_AFR$XtX",
            beta_external_expr="matrix(height_EUR$beta.external, ncol=1)",
            family="gaussian",
        )
    For a single file containing everything, use data_path (singular) or
    pass a one-element data_paths list. data_paths is preferred going
    forward.

    DATA SHAPE - critical differences from BRIERi:
        * sumstats is a data.frame with columns: variable, corr, stats,
          df, pval, n. The `corr` column is required.
        * beta.external is p x M (NO intercept row). This is asymmetric
          with BRIERi which requires (p+1) x M with the intercept row.
        * XtX is the LD matrix (typically sparse), shape p x p.

    TWO WAYS TO PROVIDE LD:
        * PREFERRED: pass `ld_id` from a prior cal_ld() call. sumstats and
          beta.external will be automatically subset by the LD object's
          $nz indices (constant-genotype columns dropped). This sidesteps
          the silent-failure trap where sumstats and the LD matrix end up
          with mismatched row counts.
        * MANUAL: pass `XtX_expr` directly. You are responsible for
          subsetting sumstats and beta.external yourself.

    SCALE: BRIERs operates on the STANDARDIZED scale throughout.
    GWAS summary statistics contain standardized marginal correlations
    and the LD matrix is a correlation matrix; there is no information
    in the BRIERs pipeline to recover per-genotype means or scales, so
    fitted coefficients and resulting predictions are inherently on the
    standardized scale. For validation-set selection criteria, X.val
    must be column-standardized (via standardize_X) and y.val must be
    standardized for family='gaussian' (mean=0, sd=1).

    For cross-family comparisons across BRIERi / BRIERfull / BRIERs:
    fit BRIERi and BRIERfull on standardized X and standardized y as
    well. This puts all three families on the same scale; coefficient
    magnitudes and validation MSPE values become directly comparable.
    Do NOT try to un-standardize BRIERs predictions; doing so without
    per-genotype scale information is methodologically unsound.

    Args:
        data_path: Path to .rda/.RData/.rds with sumstats and beta.external.
        sumstats_expr: R expression for the sumstats data frame.
        beta_external_expr: R expression for the external coefficients
            matrix (p x M, NO intercept row).
        family: One of "gaussian" (default), "binomial", "poisson".
        ld_id: ID of a cached LD object from cal_ld. Preferred.
        XtX_expr: Alternative to ld_id; R expression for the LD matrix
            directly.
        multi_method: One of "stacking" (default), "PCA", "ind".
        eta_list: Optional eta grid. Default is the recommended log-spaced
            grid: c(0, exp(seq(log(0.1), log(10), length.out = 20))).
        penalty_factor_expr: Optional R expression for a length-p vector
            (p = the post-LD-subset variant count) with 0 for unpenalized
            variants and 1 for penalized. Default all-ones.
        alpha: Optional elastic-net mixing in (0, 1]. 1 = lasso (default).
        penalty: Optional "LASSO" (default), "SCAD", or "MCP".
        gamma: Optional SCAD/MCP concavity (> 2 SCAD, > 1 MCP).
        trace: If true, BRIERs prints progress messages.

    Returns:
        On success: {status: "ok", fit_id, fit_path, family, p,
            M_external, eta_list_used, multi_method_used, ld_id_used,
            timing, _notice_*, _followup_*}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(
        sumstats_expr=sumstats_expr,
        beta_external_expr=beta_external_expr,
        XtX_expr=XtX_expr,
        penalty_factor_expr=penalty_factor_expr,
    )
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:brier_s"}

    # ld_id is an alternative source for XtX; if used, no data file is
    # strictly needed for the LD matrix path. But we still expect either
    # data_path or data_paths since sumstats_expr / beta_external_expr
    # are resolved from the loaded data.
    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:brier_s"}

    try:
        resolved_eta = _resolve_eta_list(eta_list, eta_floor, eta_ceiling,
                                          eta_n)
    except ValueError as e:
        return {"status": "error", "message": str(e),
                "class": "InvalidEtaGrid", "where": "server.py:brier_s"}

    perr = _validate_penalty(alpha, penalty, gamma)
    if perr:
        return {"status": "error", "message": perr,
                "class": "InvalidPenalty", "where": "server.py:brier_s"}

    return _run_r("brier_s.R", {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "sumstats_expr": sumstats_expr,
        "beta_external_expr": beta_external_expr,
        "family": family,
        "ld_id": ld_id,
        "XtX_expr": XtX_expr,
        "multi_method": multi_method,
        "eta_list": resolved_eta,
        "penalty_factor_expr": penalty_factor_expr,
        "trace": trace,
        **_penalty_payload(alpha, penalty, gamma),
    })


@mcp.tool()
def brier_s_selection(
    fit_id: str,
    criteria: str,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
    X_val_expr: Optional[str] = None,
    y_val_expr: Optional[str] = None,
    XtX_val_expr: Optional[str] = None,
    sumstats_val_expr: Optional[str] = None,
    TN: Optional[int] = None,
    h2: Optional[float] = None,
) -> dict:
    """Select optimal (eta, lambda) from a cached BRIERs fit.

    Two modes:
      * IC-based:        criteria in {"Cp", "GIC", "pseu.val"}.
                         Requires `TN` (training sample size, an integer).
                         No validation data required.
      * Validation-set:  criteria like "gaussian.mspe", "binomial.dev".
                         Requires EITHER individual-level
                         (X_val_expr + y_val_expr) OR summary-level
                         (XtX_val_expr + sumstats_val_expr + TN + h2)
                         validation data. data_path is required for both.

    The selection result is cached and returned as selection_id; pass
    that to brier_predict or brier_evaluate.

    CRITICAL: For validation-set criteria, X.val must be column-
    standardized BEFORE passing in. BRIERs returned coefficients on the
    standardized scale; an un-standardized X.val produces garbage
    predictions. A heuristic check emits _notice_x_val_not_standardized
    if X.val doesn't look standardized.

    Args:
        fit_id: The fit_id from a prior brier_s call.
        criteria: One of the IC criteria or family-specific validation
            metrics.
        data_path: Required for any validation-set criterion.
        X_val_expr: Individual-level validation predictor matrix (must
            be standardized).
        y_val_expr: Individual-level validation outcome (standardized
            for gaussian family only).
        XtX_val_expr: Summary-level validation LD matrix.
        sumstats_val_expr: Summary-level validation sumstats.
        TN: Total N at validation (for summary-level path).
        h2: Heritability estimate at validation (for summary-level path).

    Returns:
        On success: {status: "ok", fit_id, selection_id, selection_path,
            criteria, criteria_mode, selected_eta, selected_lambda,
            selected_metric, _notice_*}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(
        X_val_expr=X_val_expr, y_val_expr=y_val_expr,
        XtX_val_expr=XtX_val_expr,
        sumstats_val_expr=sumstats_val_expr,
    )
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:brier_s_selection"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    return _annotate_selection_boundary(_run_r("brier_s_selection.R", {
        "fit_id": fit_id,
        "criteria": criteria,
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "X_val_expr": X_val_expr,
        "y_val_expr": y_val_expr,
        "XtX_val_expr": XtX_val_expr,
        "sumstats_val_expr": sumstats_val_expr,
        "TN": TN,
        "h2": h2,
    }))


@mcp.tool()
def brier_auto_tune_eta(
    family: str,
    fit_kwargs: dict,
    selection_kwargs: dict,
    escalation_ceilings: Optional[list] = None,
    near_boundary_top_fraction: float = 0.0,
    de_escalation_threshold: float = 1.0,
    de_escalation_ceiling: float = 2.0,
) -> dict:
    """Auto-escalating eta-grid tuning: refit with a wider ceiling until the
    optimum is interior (or the ladder is exhausted).

    First shipped in v0.11; v0.12 tightened the escalation trigger to
    strict grid_max equality and added single-shot de-escalation for
    fits that land in the low end of the grid.

    This wraps the standard fit + selection pair with an automatic
    eta-ceiling adjustment loop. Use it when you want the tool itself to
    widen (escalation) or narrow (de-escalation) the eta search rather
    than relying on you to read the boundary diagnostic and manually
    rerun.

    The plain selection tools (brier_i_selection, brier_full_selection,
    brier_s_selection) are unchanged - they still emit
    `_notice_eta_boundary` as a diagnostic. This tool is strictly
    opt-in by name.

    Algorithm:
      1. Fit with the initial eta_ceiling (from fit_kwargs, or default 10).
      2. Run selection.
      3. ESCALATION PATH: if the chosen eta sits exactly at the grid
         maximum (strict equality), refit with the next ceiling from
         `escalation_ceilings` (default [30, 50, 100]). Re-select.
         Repeat. Stop the first time the optimum is interior or when the
         ladder is exhausted.
      4. DE-ESCALATION PATH (mutually exclusive with escalation): if
         escalation did NOT trigger on the INITIAL fit AND the chosen
         eta is below `de_escalation_threshold` (default 1.0), refit
         ONCE with `eta_ceiling = de_escalation_ceiling` (default 2.0).
         This gives finer log-spaced resolution in the low-eta region.
         Single-shot, never iterates.
      5. Else: return the initial fit + selection.

    Args:
        family: One of "brier_i", "brier_full", "brier_s". Picks which
            fit + selection pair to use.
        fit_kwargs: Dict of keyword args for the fit tool. Must include
            data_path/data_paths and the family-specific expressions
            (X_expr/y_expr/beta_external_expr for brier_i;
            X_expr/y_expr/cohort_expr for brier_full;
            sumstats_expr/beta_external_expr/XtX_expr or ld_id for
            brier_s). May include eta_floor/eta_ceiling/eta_n to set the
            INITIAL grid; if omitted, the v0.10.3 defaults apply
            (floor=0.1, ceiling=10, n=10). An explicit eta_list in
            fit_kwargs is rejected (the tool varies the ceiling so the
            grid must be buildable from knobs).
        selection_kwargs: Dict of keyword args for the selection tool
            (criteria, X_val_expr/y_val_expr, data_path/data_paths, etc).
            fit_id is filled in automatically per rung.
        escalation_ceilings: Ladder of ceiling values to try after the
            initial fit if it lands at grid_max. Default [30, 50, 100].
            Only rungs strictly greater than the initial eta_ceiling are
            walked. Pass an empty list to disable escalation entirely.
        near_boundary_top_fraction: Trigger threshold for escalation,
            as a fraction of the log-spaced grid measured from the top.
            DEFAULT 0.0 (v0.12 strict equality - escalate only when
            eta_min == grid_max). v0.11 used 0.20; raise it back if you
            want the looser top-20% behavior.
        de_escalation_threshold: If the initial fit lands with
            eta_min < this value (default 1.0) AND escalation did not
            trigger, do a single-shot refit. Set to 0 to disable
            de-escalation.
        de_escalation_ceiling: The ceiling used for the de-escalation
            refit (default 2.0). The refit grid runs from eta_floor to
            this value with the same eta_n.

    Returns:
        On success:
          {status: "ok",
           family,
           final_fit_id, final_selection_id,
           final_eta_min, final_eta_ceiling,
           final_hit_near_boundary,   # True if last rung still at boundary
           de_escalated,              # True if a de-escalation refit ran
           escalation_history: [{rung, ceiling, fit_id, selection_id,
                                  eta_min, hit_near_boundary,
                                  time_seconds, de_escalation?}, ...],
           _notice (string) when the ladder is exhausted at boundary}
        On error:
          {status: "error", message, where}

    Cost warning: each rung is a full refit + selection. On high-p data
    (e.g. 10,000 SNPs) this can be many minutes per rung. The default
    3-rung ladder can therefore mean tens of minutes for a single
    auto-tune call. Pass a shorter ladder for heavy data, or run the
    plain fit + selection first and only escalate if the diagnostic
    fires.
    """
    if family not in ("brier_i", "brier_full", "brier_s"):
        return {"status": "error",
                "message": ("family must be one of 'brier_i', "
                            "'brier_full', 'brier_s'"),
                "where": "server.py:brier_auto_tune_eta"}

    if "eta_list" in fit_kwargs and fit_kwargs["eta_list"] is not None:
        return {"status": "error",
                "message": ("eta_list is not allowed in auto-tune (the "
                            "tool varies the ceiling across rungs and "
                            "rebuilds the grid each time). Pass "
                            "eta_floor / eta_ceiling / eta_n instead."),
                "where": "server.py:brier_auto_tune_eta"}

    if escalation_ceilings is None:
        escalation_ceilings = [30.0, 50.0, 100.0]

    initial_ceiling = float(fit_kwargs.get("eta_ceiling",
                                            _DEFAULT_ETA_CEILING))
    ladder = _ladder_above(initial_ceiling, escalation_ceilings)
    # The full sequence of ceilings we may try, in order:
    ceilings_to_try = [initial_ceiling] + ladder

    fit_tools = {"brier_i": brier_i,
                  "brier_full": brier_full,
                  "brier_s": brier_s}
    selection_tools = {"brier_i": brier_i_selection,
                        "brier_full": brier_full_selection,
                        "brier_s": brier_s_selection}
    fit_fn = fit_tools[family]
    sel_fn = selection_tools[family]

    history = []
    final = None
    final_near_boundary = False

    for rung_idx, ceiling in enumerate(ceilings_to_try):
        rung_kwargs = dict(fit_kwargs)
        rung_kwargs["eta_ceiling"] = float(ceiling)
        # Always strip eta_list (we built the grid via knobs)
        rung_kwargs.pop("eta_list", None)

        t0 = datetime.datetime.now()
        fit_result = fit_fn(**rung_kwargs)
        if fit_result.get("status") != "ok":
            return {
                "status": "error",
                "message": (f"fit failed at ceiling={ceiling}: "
                            f"{fit_result.get('message')}"),
                "where": "server.py:brier_auto_tune_eta",
                "rung": rung_idx,
                "ceiling": ceiling,
                "escalation_history": history,
            }

        sel_call = dict(selection_kwargs)
        sel_call["fit_id"] = fit_result["fit_id"]
        sel_result = sel_fn(**sel_call)
        elapsed = (datetime.datetime.now() - t0).total_seconds()
        if sel_result.get("status") != "ok":
            return {
                "status": "error",
                "message": (f"selection failed at ceiling={ceiling}: "
                            f"{sel_result.get('message')}"),
                "where": "server.py:brier_auto_tune_eta",
                "rung": rung_idx,
                "ceiling": ceiling,
                "escalation_history": history,
            }

        grid_vals = sel_result.get("eta_grid_values")
        eta_min = sel_result.get("selected_eta")
        hit_nb = bool(_is_near_boundary(eta_min, grid_vals,
                                         near_boundary_top_fraction))

        history.append({
            "rung": rung_idx,
            "ceiling": float(ceiling),
            "fit_id": fit_result["fit_id"],
            "selection_id": sel_result["selection_id"],
            "eta_min": eta_min,
            "hit_near_boundary": hit_nb,
            "time_seconds": round(elapsed, 2),
        })
        final = (fit_result, sel_result, float(ceiling))
        final_near_boundary = hit_nb

        if not hit_nb:
            break  # interior optimum -> done

    # ----- De-escalation (single-shot, mutually exclusive with escalation) -----
    # Only applies when the INITIAL rung returned an interior optimum
    # (escalation never fired) AND eta_min is in the low end of the grid.
    de_escalated = False
    if (
        len(history) == 1
        and not history[0]["hit_near_boundary"]
        and de_escalation_threshold > 0
    ):
        eta_min_val = history[0]["eta_min"]
        # Get the scalar (smallest component for M>=2) for the threshold check
        try:
            if isinstance(eta_min_val, (list, tuple)):
                eta_for_check = min(float(v) for v in eta_min_val)
            else:
                eta_for_check = float(eta_min_val)
        except (TypeError, ValueError):
            eta_for_check = None

        if (eta_for_check is not None
                and eta_for_check > 0
                and eta_for_check < de_escalation_threshold
                and de_escalation_ceiling > eta_for_check):
            de_kwargs = dict(fit_kwargs)
            de_kwargs["eta_ceiling"] = float(de_escalation_ceiling)
            de_kwargs.pop("eta_list", None)
            t0 = datetime.datetime.now()
            de_fit = fit_fn(**de_kwargs)
            if de_fit.get("status") == "ok":
                de_sel_call = dict(selection_kwargs)
                de_sel_call["fit_id"] = de_fit["fit_id"]
                de_sel = sel_fn(**de_sel_call)
                elapsed = (datetime.datetime.now() - t0).total_seconds()
                if de_sel.get("status") == "ok":
                    history.append({
                        "rung": len(history),
                        "ceiling": float(de_escalation_ceiling),
                        "fit_id": de_fit["fit_id"],
                        "selection_id": de_sel["selection_id"],
                        "eta_min": de_sel.get("selected_eta"),
                        "hit_near_boundary": False,
                        "time_seconds": round(elapsed, 2),
                        "de_escalation": True,
                    })
                    final = (de_fit, de_sel, float(de_escalation_ceiling))
                    final_near_boundary = False
                    de_escalated = True

    fit_result, sel_result, last_ceiling = final
    out = {
        "status": "ok",
        "family": family,
        "final_fit_id": fit_result["fit_id"],
        "final_selection_id": sel_result["selection_id"],
        "final_eta_min": sel_result.get("selected_eta"),
        "final_eta_ceiling": last_ceiling,
        "final_hit_near_boundary": final_near_boundary,
        "de_escalated": de_escalated,
        "escalation_history": history,
    }
    if final_near_boundary:
        # Ladder exhausted at strict-equality boundary.
        out["_notice"] = (
            f"Ladder exhausted at ceiling={last_ceiling:g} but the "
            f"optimum (eta={out['final_eta_min']}) is still exactly at "
            f"the grid maximum. The true optimum may lie beyond "
            f"{last_ceiling:g}. To widen further, re-run with "
            f"escalation_ceilings extended (e.g. [200, 500])."
        )
    return out


@mcp.tool()
def brier_predict(
    newx_expr: str,
    selection_id: Optional[str] = None,
    fit_id: Optional[str] = None,
    eta: Optional[float] = None,
    lambda_: Optional[float] = None,
    which_eta: Optional[int] = None,
    which_lambda: Optional[int] = None,
    type: str = "response",
    y_center: Optional[float] = None,
    y_scale: Optional[float] = None,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
    output_dir: Optional[str] = None,
) -> dict:
    """Predict from a cached BRIER fit or selection on a new X.

    Two ways to identify the model to predict from:
      * `selection_id` (PREFERRED): the id returned by a prior
        `brier_i_selection` call. The chosen (eta, lambda) are used
        automatically; no need to specify them again.
      * `fit_id`: the id returned by a prior `brier_i` call. With this,
        you must also pass an operating point on the grid. Two ways:
        - `eta` + `lambda_` numeric (must EXACTLY match grid values;
          floating-point equality is checked).
        - `which_eta` + `which_lambda` integer indices (1-based) into
          the fit's eta.grid / lambda grid. Safer than numeric because
          you don't have to know the exact grid values.

    The full prediction vector is written to a CSV in the cache
    directory; the MCP response carries only summary statistics (min,
    median, mean, quantiles, max). The CSV path is in
    `predictions_path` so the user can load the raw numbers in R or
    Python if needed.

    Args:
        data_path: Absolute path to the .rda/.RData/.rds file containing
            the new X.
        newx_expr: R expression that evaluates to the new predictor
            matrix (e.g. "Data_BRIERi$target$testing$X").
        selection_id: ID of a cached selection object.
        fit_id: Alternative to selection_id; cached raw fit.
        eta: Explicit eta override (must match grid exactly).
        lambda_: Explicit lambda override. Python keyword `lambda` is
            reserved, so we use `lambda_`; it maps to BRIER's `lambda`.
        which_eta: 1-based integer index into the eta grid (safer than
            numeric eta for grid lookup).
        which_lambda: 1-based integer index into the lambda grid.
        type: One of "response" (default), "link", "coefficients",
            "vars", "nvars".
        y_center: Optional. y mean used to un-standardize BRIERs
            predictions back to the raw outcome scale. Must be paired
            with y_scale. SOURCE MATTERS: if the user has training y
            available, mean(y_train) is unbiased; if only test y is
            available, mean(y_test) is slightly biased; an external
            literature value is as good as the source. The MCP does
            NOT auto-source these scalars (the v0.8.0 auto-stash logic
            was removed in v0.8.1 because it hid the choice). Leave
            both null to keep predictions on the standardized scale.
        y_scale: Optional. y standard deviation, paired with y_center.
            Must be positive. Formula:
            raw_pred = std_pred * y_scale + y_center.

    SCALE: BRIERs fits are inherently on the STANDARDIZED scale (sumstats
    contain only standardized correlations; LD is a correlation matrix).
    Predictions stay standardized unless the caller explicitly supplies
    y_center / y_scale. For cross-family comparisons across BRIERi /
    BRIERfull / BRIERs, the cleaner approach is to fit BRIERi and
    BRIERfull on standardized X and y too, so all three families
    produce predictions on the same scale; see the wizard's
    cross_family_comparison guidance in _PATHS[\"BRIERs\"].

    Returns:
        On success: {status: "ok", eta_used, lambda_used, type,
            n_predicted, summary, predictions_path, _notice_*}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(newx_expr=newx_expr)
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:brier_predict"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    payload = {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "newx_expr": newx_expr,
        "selection_id": selection_id,
        "fit_id": fit_id,
        "eta": eta,
        "lambda": lambda_,
        "which_eta": which_eta,
        "which_lambda": which_lambda,
        "type": type,
        "y_center": y_center,
        "y_scale": y_scale,
        "output_dir": output_dir,
    }
    return _run_r("brier_predict.R", payload)


@mcp.tool()
def brier_evaluate(
    newx_expr: str,
    newy_expr: str,
    criteria: str,
    selection_id: Optional[str] = None,
    fit_id: Optional[str] = None,
    eta: Optional[float] = None,
    lambda_: Optional[float] = None,
    which_eta: Optional[int] = None,
    which_lambda: Optional[int] = None,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Score a cached BRIER fit on a new (X, y) pair using evalMetric.

    Use this to evaluate generalization on a held-out test set after
    selecting hyperparameters. Combines predict + evalMetric in one
    tool call. Returns just the metric value (a single number); use
    brier_predict if you also want the prediction vector.

    Like brier_predict, prefers `selection_id` over `fit_id` so the
    selected (eta, lambda) are used automatically. With `fit_id`, supply
    either numeric eta/lambda or integer which_eta/which_lambda to pick
    an operating point on the grid.

    Args:
        data_path: Path to .rda/.RData/.rds with the new X and y.
        newx_expr: R expression for the new predictor matrix.
        newy_expr: R expression for the new outcome vector.
        criteria: One of the family-specific metrics:
            "gaussian.mspe", "gaussian.rsq",
            "binomial.dev", "binomial.tjurrsq", "binomial.AUC",
            "poisson.dev".
        selection_id: ID of a cached selection object (preferred).
        fit_id: Alternative to selection_id.
        eta: Explicit eta override (must match grid exactly).
        lambda_: Explicit lambda override.
        which_eta: 1-based integer index into the eta grid.
        which_lambda: 1-based integer index into the lambda grid.

    Returns:
        On success: {status: "ok", eta_used, lambda_used, criteria,
            metric_value, n_evaluated, _notice_*}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(newx_expr=newx_expr, newy_expr=newy_expr)
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:brier_evaluate"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    payload = {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "newx_expr": newx_expr,
        "newy_expr": newy_expr,
        "criteria": criteria,
        "selection_id": selection_id,
        "fit_id": fit_id,
        "eta": eta,
        "lambda": lambda_,
        "which_eta": which_eta,
        "which_lambda": which_lambda,
    }
    return _run_r("brier_evaluate.R", payload)


@mcp.tool()
def score_external_prs(
    newx_expr: str,
    newy_expr: str,
    beta_expr: str,
    criteria: str,
    family: str = "gaussian",
    has_intercept: Optional[bool] = None,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Score a RAW external coefficient vector directly on a new (X, y) pair.

    This is the "external-only comparator": it applies a pretrained external
    PRS (a beta vector, e.g. from prep_auto's beta_external or an external
    coefficient file) to a target cohort's genotypes as a linear predictor and
    scores it with the SAME BRIER::evalMetric used by brier_evaluate, so the
    number is directly comparable to a fitted BRIERi metric or an eta=0
    baseline. It does NOT fit anything; use it to answer "does BRIER's transfer
    fit actually beat scoring the external model as-is?".

    The genotypes in newx_expr MUST be on the SAME scale the beta was trained
    on (e.g. the standardized X_test that prep_auto produced, since the
    external model is on the standardized scale). A leading intercept row is
    auto-detected from the vector length (length == ncol(X)+1 means the first
    element is an intercept, the BRIERi convention); override with
    has_intercept if needed.

    Args:
        data_path: Path to a .rds/.rda/.RData holding X, y, and the beta.
        newx_expr: R expression for the predictor matrix (correct scale).
        newy_expr: R expression for the outcome vector.
        beta_expr: R expression for the external coefficient vector.
        criteria: A family-specific metric, same set as brier_evaluate:
            "gaussian.mspe", "gaussian.rsq", "binomial.dev",
            "binomial.tjurrsq", "binomial.AUC", "poisson.dev".
        family: "gaussian" | "binomial" | "poisson"; sets the link applied to
            the linear predictor before scoring.
        has_intercept: Optional override for intercept-row detection.

    Returns:
        On success: {status: "ok", criteria, metric_value, n_evaluated,
            used_intercept, p}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_exprs(
        newx_expr=newx_expr, newy_expr=newy_expr, beta_expr=beta_expr
    )
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:score_external_prs"}
    if family not in ("gaussian", "binomial", "poisson"):
        return {"status": "error",
                "message": "family must be 'gaussian', 'binomial', or 'poisson'",
                "class": "ValueError",
                "where": "server.py:score_external_prs"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    payload = {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "newx_expr": newx_expr,
        "newy_expr": newy_expr,
        "beta_expr": beta_expr,
        "criteria": criteria,
        "family": family,
        "has_intercept": has_intercept,
    }
    return _run_r("score_external_prs.R", payload)


# --------------------------------------------------------------------------
# Reproducibility: emit a runnable R script that replays a tool sequence
# --------------------------------------------------------------------------

def _r_literal(value: Any) -> str:
    """Render a Python value as an R literal (for a generated reproduce script)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        # All-scalar numeric/logical/string list -> c(...); else list(...).
        if value and all(isinstance(v, (int, float, bool)) for v in value):
            return "c(" + ", ".join(_r_literal(v) for v in value) + ")"
        return "list(" + ", ".join(_r_literal(v) for v in value) + ")"
    if isinstance(value, dict):
        parts = [f"{k} = {_r_literal(v)}" for k, v in value.items()]
        return "list(" + ", ".join(parts) + ")"
    return _r_literal(str(value))


def _render_arg(value: Any, valmap: dict) -> str:
    """Render one argument, substituting a prior-step reference when the value
    matches a value produced by an earlier step (threads generated ids/paths)."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        ref = valmap.get(value)
        if ref is not None:
            return ref
    if isinstance(value, dict):
        parts = [f"{k} = {_render_arg(v, valmap)}" for k, v in value.items()]
        return "list(" + ", ".join(parts) + ")"
    if isinstance(value, list):
        if value and all(isinstance(v, (int, float, bool)) for v in value):
            return "c(" + ", ".join(_render_arg(v, valmap) for v in value) + ")"
        return "list(" + ", ".join(_render_arg(v, valmap) for v in value) + ")"
    return _r_literal(value)


@mcp.tool()
def generate_reproduce_script(
    steps: list,
    output_path: str,
    mcp_dir: Optional[str] = None,
    title: Optional[str] = None,
) -> dict:
    """Emit a runnable R script that replays a recorded BRIER-MCP tool sequence.

    Given the ordered analysis steps of a session (each an executed tool call
    with its arguments and result), write a self-contained reproduce_*.R that
    re-runs the SAME underlying R scripts, in order, to reproduce the reported
    numbers. Generated ids and paths (prepared_path, fit_id, selection_id) are
    threaded automatically: any argument whose value equals a value returned by
    an earlier step is rewritten to reference that step's result, so a fresh run
    (which mints new ids) still chains correctly.

    This is meant to be called by the harness with the captured trace, not
    hand-driven by the model. Only steps whose tool maps to an r_scripts/<tool>.R
    are emitted; inspection / wizard / config tools are skipped (they do not
    affect the numbers).

    Args:
        steps: Ordered list of {"tool": str, "args": dict, "result": dict}.
        output_path: Absolute path to write the reproduce_*.R to.
        mcp_dir: Absolute path to the mcp/ directory (defaults to this server's
            directory) so the script can locate r_scripts/.
        title: Optional label for the script header (e.g. the case id).

    Returns:
        {status: "ok", output_path, n_steps_emitted, tools} or
        {status: "error", ...}.
    """
    try:
        base_dir = mcp_dir or str(Path(__file__).resolve().parent)
        r_dir = Path(base_dir) / "r_scripts"

        valmap: dict = {}
        lines: list = []
        emitted = 0
        tools_emitted: list = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            tool = step.get("tool") or step.get("name")
            if not tool:
                continue
            script = f"{tool}.R"
            if not (r_dir / script).exists():
                continue  # not an analysis tool; skip

            # SKIP THE STEPS THAT FAILED. An agent self-corrects: it calls the fitter
            # with a junk optional argument, or a selection criterion the module does
            # not accept, gets an error, and reissues the corrected call. Those false
            # starts are in the trace, and replaying them makes the script die on the
            # first one -- reproducing the model's mistakes instead of its analysis.
            # Every reproduce script for a run that self-corrected was broken this way,
            # and nobody noticed, because nothing ever ran them.
            step_result = step.get("result")
            if isinstance(step_result, dict) and step_result.get("status") != "ok":
                continue

            args = dict(step.get("args") or step.get("arguments") or {})

            # REPLAY WHAT RAN, NOT WHAT WAS ASKED FOR. The eta grid is RESOLVED by this
            # server: when the caller omits `eta_list` (which the agent is now told to
            # do, because an explicit grid overrides eta_ceiling and defeats escalation)
            # the server builds the principled log grid and passes it down. The r_scripts
            # have their OWN, different legacy default, so a script that replays only the
            # ARGUMENTS silently refits on a different grid, selects a different eta, and
            # reports different numbers. That is exactly what happened: a run selected
            # eta=5.99 and its "reproduce" script selected 4.83.
            #
            # The fit echoes the grid it actually used, so pin it.
            if (isinstance(step_result, dict) and args.get("eta_list") is None
                    and step_result.get("eta_list_used") is not None):
                args["eta_list"] = step_result["eta_list_used"]

            rendered = ", ".join(
                f"{k} = {_render_arg(v, valmap)}"
                for k, v in args.items()
                if v is not None
            )
            emitted += 1
            var = f"r{emitted}"
            lines.append(f'# step {emitted}: {tool}')
            lines.append(f'{var} <- run_tool("{script}", list({rendered}))')
            lines.append(
                f'if (!identical({var}$status, "ok")) '
                f'stop("step {emitted} ({tool}) failed: ", {var}$message)'
            )
            lines.append("")
            tools_emitted.append(tool)
            # Register this step's GENERATED id/path fields for downstream
            # threading. Restrict to id/path-like keys so constant strings
            # (family, criteria) are left as literals rather than threaded
            # through a coincidental value match.
            result = step.get("result")
            if isinstance(result, dict):
                for fk, fv in result.items():
                    if not (isinstance(fv, str) and fv):
                        continue
                    if fk.endswith("_id") or fk.endswith("_path"):
                        valmap.setdefault(fv, f"{var}${fk}")

        header = [
            "#!/usr/bin/env Rscript",
            f"# Auto-generated reproduce script"
            + (f" for {title}" if title else "") + ".",
            "# Replays the recorded BRIER-MCP tool sequence against the same R",
            "# scripts to reproduce the reported numbers. Generated ids/paths are",
            "# threaded from each step's result into the next call.",
            "",
            "suppressPackageStartupMessages(library(jsonlite))",
            f'mcp_dir <- "{base_dir}"',
            "",
            "run_tool <- function(script, payload) {",
            '  inp <- tempfile(fileext = ".in.json")',
            '  out <- tempfile(fileext = ".out.json")',
            '  write_json(payload, inp, auto_unbox = TRUE, null = "null", digits = NA)',
            '  rscript <- file.path(R.home("bin"), "Rscript")',
            "  system2(rscript, c(",
            '    "--no-save", "--no-restore", "--no-init-file",',
            '    shQuote(file.path(mcp_dir, "r_scripts", script)),',
            "    shQuote(inp), shQuote(out)),",
            "    stdout = TRUE, stderr = TRUE)",
            '  if (!file.exists(out)) stop(paste("no output from", script))',
            "  read_json(out, simplifyVector = TRUE)",
            "}",
            "",
        ]
        script_text = "\n".join(header + lines) + "\n"

        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(script_text, encoding="utf-8")

        return {
            "status": "ok",
            "output_path": str(out_p),
            "n_steps_emitted": emitted,
            "tools": tools_emitted,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "message": str(e),
            "class": type(e).__name__,
            "where": "server.py:generate_reproduce_script",
        }


# --------------------------------------------------------------------------
# Plot tools (v0.9): wrap BRIER's plot.eta / plot.box / plot.importance
# --------------------------------------------------------------------------

# Criteria validity per family. Used by all three plot wrappers below.
_VALID_CRITERIA_BY_FAMILY = {
    "gaussian": {"gaussian.mspe", "gaussian.rsq"},
    "binomial": {"binomial.dev", "binomial.mcfrsq", "binomial.tjursq",
                  "binomial.auc"},
    "poisson":  {"poisson.dev"},
}
_ALL_VALID_CRITERIA = set().union(*_VALID_CRITERIA_BY_FAMILY.values())


def _validate_plot_criteria(criteria: str,
                             family: Optional[str] = None) -> Optional[str]:
    """Return an error message if (criteria, family) is invalid, else None.

    If family is None, only checks that criteria is a known string.
    """
    if criteria not in _ALL_VALID_CRITERIA:
        return (f"criteria '{criteria}' is not a recognized validation-set "
                f"metric. Valid options are: "
                f"{sorted(_ALL_VALID_CRITERIA)}.")
    if family is None:
        return None
    fam = family.lower()
    if fam == "cox":
        return ("family 'cox' is not yet functional in BRIER. The plot "
                "tools cannot operate on cox fits.")
    valid_for_fam = _VALID_CRITERIA_BY_FAMILY.get(fam)
    if valid_for_fam is None:
        return f"family '{family}' is not supported for plotting."
    if criteria not in valid_for_fam:
        return (f"criteria '{criteria}' is not compatible with family "
                f"'{family}'. Valid criteria for family='{family}' are: "
                f"{sorted(valid_for_fam)}.")
    return None


def _lookup_family_from_selection(selection_id: str) -> Optional[str]:
    """Read the family from a cached selection's source fit meta.

    Returns None if the lookup fails. Plot tools call this defensively
    before plot.eta etc. to fail fast on family-criterion mismatches.
    """
    try:
        cache_root = os.environ.get(
            "XDG_CACHE_HOME",
            os.path.expanduser("~/.cache"),
        )
        fits_dir = os.path.join(cache_root, "brier-mcp", "fits")
        sel_path = os.path.join(fits_dir, f"{selection_id}.rds")
        if not os.path.exists(sel_path):
            return None
        # Find the source fit_id from a quick R subprocess
        rscript = _find_rscript()
        r_code = (
            f"sel <- readRDS('{sel_path}'); "
            "src_id <- if (!is.null(sel$source_fit_id)) sel$source_fit_id "
            "else NA; "
            f"if (!is.na(src_id)) {{ "
            f"  fit_path <- file.path('{fits_dir}', paste0(src_id, '.rds'));"
            "  if (file.exists(fit_path)) { "
            "    src <- readRDS(fit_path); "
            "    cat(if (!is.null(src$meta$family)) src$meta$family else '') "
            "  } "
            "}"
        )
        result = subprocess.run(
            [rscript, "--no-save", "--no-restore", "--no-init-file",
             "-e", r_code],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        fam = result.stdout.strip()
        return fam if fam else None
    except Exception:
        return None


@mcp.tool()
def brier_plot_eta(
    selection_id: str,
    newx_expr: str,
    newy_expr: str,
    criteria: str,
    covar_expr: Optional[str] = None,
    adjust_covar: Optional[str] = None,
    standardize: bool = False,
    bootstrap: bool = False,
    bootstrap_n: int = 100,
    seed: Optional[int] = None,
    width: int = 800,
    height: int = 600,
    dpi: int = 100,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
    output_dir: Optional[str] = None,
) -> dict:
    """Plot performance vs eta for a cached BRIER selection.

    Wraps BRIER::plot.eta with automatic handling for the M>=2 case:
      * M=1: returns the standard ggplot curve.
      * M=2: BRIER's plot.eta returns NULL in $plot and only a
        summary.df. This wrapper auto-builds a geom_tile heatmap from
        summary.df so the user does not need to drop to R.
      * M>=3: a single 2D heatmap is not directly meaningful. This
        wrapper renders marginal slices (one curve per external, with
        other etas held at zero). Full multi-dimensional data is in
        the CSV.

    Output artifacts (in the configured output_directory):
      * PNG file with the rendered plot
      * CSV file with the underlying summary.df

    Args:
        selection_id: ID of a cached BRIERi / BRIERfull / BRIERs selection.
        data_path: Absolute path to .rda/.RData/.rds with held-out data.
        newx_expr: R expression for the held-out X (e.g.
            "data$target$testing$X").
        newy_expr: R expression for the held-out y.
        criteria: Validation-set metric. Must be compatible with the
            fit's family. See plot tool error messages for valid pairs.
        covar_expr: Optional R expression for additional covariate data
            (e.g. "data$target$testing$covariates").
        adjust_covar: Optional adjustment formula passed to plot.eta.
        standardize: If True, plot.eta standardizes inputs internally.
        bootstrap: If True, plot.eta computes bootstrap confidence bands.
        bootstrap_n: Number of bootstrap replicates if bootstrap=True.
        seed: RNG seed for bootstrap reproducibility.
        width, height: PNG dimensions in pixels.
        dpi: PNG resolution.

    Returns:
        On success: {status: "ok", plot_id, plot_png_path, plot_csv_path,
            M, rendered_kind, criteria, n_eta_points, fit_seconds,
            _notice_*}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_plot_criteria(criteria,
                                   family=_lookup_family_from_selection(selection_id))
    if err:
        return {"status": "error", "message": err,
                "class": "InvalidCriteria",
                "where": "server.py:brier_plot_eta"}

    expr_check = _validate_exprs(newx_expr=newx_expr, newy_expr=newy_expr)
    if expr_check:
        return {"status": "error", "message": expr_check,
                "class": "DenylistViolation",
                "where": "server.py:brier_plot_eta"}
    if covar_expr:
        cv = _validate_exprs(covar_expr=covar_expr)
        if cv:
            return {"status": "error", "message": cv,
                    "class": "DenylistViolation",
                    "where": "server.py:brier_plot_eta"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:brier_plot_eta"}

    return _run_r("brier_plot_eta.R", {
        "selection_id": selection_id,
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "newx_expr": newx_expr,
        "newy_expr": newy_expr,
        "criteria": criteria,
        "covar_expr": covar_expr,
        "adjust_covar": adjust_covar,
        "standardize": standardize,
        "bootstrap": bootstrap,
        "bootstrap_n": bootstrap_n,
        "seed": seed,
        "width": width,
        "height": height,
        "dpi": dpi,
        "output_dir": output_dir,
    })


@mcp.tool()
def brier_plot_box(
    selection_id: str,
    newx_expr: str,
    newy_expr: str,
    criteria: str,
    covar_expr: Optional[str] = None,
    adjust_covar: Optional[str] = None,
    standardize: bool = False,
    bootstrap_n: int = 20,
    seed: Optional[int] = None,
    width: int = 800,
    height: int = 600,
    dpi: int = 100,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
    output_dir: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> dict:
    """Bootstrap performance comparison: target-only vs external-only vs integrated.

    Wraps BRIER::plot.box. Generates a boxplot comparing predictive
    performance of three model variants (target-only, external-only,
    integrated BRIER) on a held-out test set, with the spread coming
    from bootstrap resampling.

    Useful for answering "is integration actually better than the
    alternatives?" with confidence-interval-level evidence.

    BOOTSTRAP UNIT AND COST: each replicate resamples the TRAINING
    SAMPLES (rows) with replacement and recomputes the criterion. The
    resampling is over individuals, NOT over predictors. Cost scales
    with bootstrap_n x (cost of one refit); a single refit is expensive
    when the predictor count p is large. For p in the thousands, 100
    replicates can exceed the MCP subprocess timeout. Lower bootstrap_n
    or omit this plot for high-p fits.

    Output artifacts (in the configured output_directory):
      * PNG file with the boxplot
      * CSV file with the bootstrap replicate matrix (one row per
        replicate, columns target_only / external_only / integrated)

    Args:
        selection_id: ID of a cached BRIER selection.
        data_path, newx_expr, newy_expr: held-out test set.
        criteria: Validation-set metric (any compatible with the fit's family).
        covar_expr: Optional covariates.
        adjust_covar: Optional adjustment formula.
        standardize: If True, internal standardization in plot.box.
        bootstrap_n: Number of bootstrap replicates. Default 20.
            v0.13.3 dropped this from 100 to 20 because each replicate
            refits the model, and at high p (10k+ SNPs) 100 replicates
            ran tens of minutes per plot. n=20 is enough for a rough
            comparison; bump back up (e.g. 100) for publication-quality
            variance estimates.
        seed: RNG seed.
        width, height, dpi: PNG settings.

    Returns:
        On success: {status: "ok", plot_id, plot_png_path, plot_csv_path,
            criteria, n_bootstrap, fit_seconds}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_plot_criteria(criteria,
                                   family=_lookup_family_from_selection(selection_id))
    if err:
        return {"status": "error", "message": err,
                "class": "InvalidCriteria",
                "where": "server.py:brier_plot_box"}

    expr_check = _validate_exprs(newx_expr=newx_expr, newy_expr=newy_expr)
    if expr_check:
        return {"status": "error", "message": expr_check,
                "class": "DenylistViolation",
                "where": "server.py:brier_plot_box"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:brier_plot_box"}

    return _run_r("brier_plot_box.R", {
        "selection_id": selection_id,
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "newx_expr": newx_expr,
        "newy_expr": newy_expr,
        "criteria": criteria,
        "covar_expr": covar_expr,
        "adjust_covar": adjust_covar,
        "standardize": standardize,
        "bootstrap_n": bootstrap_n,
        "seed": seed,
        "width": width,
        "height": height,
        "dpi": dpi,
        "output_dir": output_dir,
    }, timeout_s=timeout_s)


@mcp.tool()
def brier_plot_importance(
    selection_id: str,
    newx_expr: str,
    newy_expr: str,
    criteria: str,
    covar_expr: Optional[str] = None,
    adjust_covar: Optional[str] = None,
    standardize: bool = False,
    n_top: int = 20,
    replications: int = 20,
    seed: Optional[int] = None,
    width: int = 800,
    height: int = 600,
    dpi: int = 100,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
    output_dir: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> dict:
    """Variable importance bar plot from bootstrap selection frequencies.

    Wraps BRIER::plot.importance. Shows the top n_top predictors ranked
    by how often they are selected across bootstrap replicates.
    Useful for stability selection in high-dimensional applications.

    BOOTSTRAP UNIT AND COST: each replicate resamples the TRAINING
    SAMPLES (rows) with replacement and refits, then records which
    predictors were selected. The resampling is over individuals, NOT
    over predictors. Cost scales with replications x (cost of one refit),
    and a single refit is expensive when the predictor count p is large.
    For p in the thousands (e.g. genome-wide SNP sets), 100 replicates
    can take many minutes and may exceed the MCP subprocess timeout.
    Lower `replications` or omit this plot for high-p fits.

    Output artifacts (in the configured output_directory):
      * PNG file with the importance bar plot
      * CSV file with the importance scores

    Args:
        selection_id: ID of a cached BRIER selection.
        data_path, newx_expr, newy_expr: held-out test set.
        criteria: Metric used during bootstrap fitting.
        covar_expr: Optional covariates.
        adjust_covar: Optional adjustment formula.
        standardize: Internal standardization flag.
        n_top: Number of top predictors to display. Default 20.
        replications: Number of bootstrap replicates. Default 20.
            v0.13.3 dropped this from 100 to 20 to match the box-plot
            change; same cost-vs-precision tradeoff.
        seed: RNG seed.
        width, height, dpi: PNG settings.

    Returns:
        On success: {status: "ok", plot_id, plot_png_path, plot_csv_path,
            criteria, n_top, n_replications, fit_seconds}.
        On error:   {status: "error", message, class, where}.
    """
    err = _validate_plot_criteria(criteria,
                                   family=_lookup_family_from_selection(selection_id))
    if err:
        return {"status": "error", "message": err,
                "class": "InvalidCriteria",
                "where": "server.py:brier_plot_importance"}

    expr_check = _validate_exprs(newx_expr=newx_expr, newy_expr=newy_expr)
    if expr_check:
        return {"status": "error", "message": expr_check,
                "class": "DenylistViolation",
                "where": "server.py:brier_plot_importance"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:brier_plot_importance"}

    return _run_r("brier_plot_importance.R", {
        "selection_id": selection_id,
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "newx_expr": newx_expr,
        "newy_expr": newy_expr,
        "criteria": criteria,
        "covar_expr": covar_expr,
        "adjust_covar": adjust_covar,
        "standardize": standardize,
        "n_top": n_top,
        "replications": replications,
        "seed": seed,
        "width": width,
        "height": height,
        "dpi": dpi,
        "output_dir": output_dir,
    }, timeout_s=timeout_s)


@mcp.tool()
def brier_plot_selection(
    selection_id: str,
    width: int = 800,
    height: int = 600,
    dpi: int = 100,
    output_dir: Optional[str] = None,
) -> dict:
    """Plot the selection criterion vs eta from a cached selection (no test data needed).

    Unlike brier_plot_eta (which runs BRIER::plot.eta and requires
    held-out X/y to compute a validation metric), this tool visualizes
    the criterion the SELECTION step already optimized to choose eta.min.
    It reads the selection object's stored eta.lambda table, so it works
    with NOTHING but a selection_id - no test set, no refit, no bootstrap.

    Use it to answer "why was this eta chosen?" The selected eta is
    marked on the plot. This is the cheapest eta diagnostic available
    and is never subject to the high-p timeout (it just plots stored
    values).

    Rendering by number of external sources M:
      * M=1:   line plot of criterion vs eta, selected eta marked
      * M=2:   heatmap over (eta_1, eta_2), values labelled
      * M>=3:  marginal slices, each external's eta varied with the
               others held at their grid minimum

    Output artifacts (in output_dir, else configured output_directory,
    else ~/.cache/brier-mcp/plots/):
      * PNG with the selection-criterion plot
      * CSV with the underlying eta.lambda table

    Args:
        selection_id: ID of a cached BRIER selection (from any
            brier_*_selection call).
        width, height, dpi: PNG geometry.
        output_dir: Optional per-call output directory override.
            Precedence: this arg, then configured output_directory,
            then the cache fallback. Created if it does not exist.

    Returns:
        On success: {status: "ok", plot_id, plot_png_path,
            plot_csv_path, M, rendered_kind, criteria, selected_eta,
            n_eta_points}.
        On error:   {status: "error", message, class, where}.
    """
    return _run_r("brier_plot_selection.R", {
        "selection_id": selection_id,
        "width": width,
        "height": height,
        "dpi": dpi,
        "output_dir": output_dir,
    })


# --------------------------------------------------------------------------
# Summary report (v0.10): compose a comprehensive HTML report + reproduce.R
# --------------------------------------------------------------------------

def _mcp_version() -> str:
    """Read MCP version from manifest.json next to this file."""
    try:
        manifest = Path(__file__).parent / "manifest.json"
        if manifest.exists():
            with open(manifest) as f:
                return json.load(f).get("version", "unknown")
    except Exception:
        pass
    return "unknown"


def _embed_png_as_base64(png_path: str) -> Optional[str]:
    """Read a PNG file and return a base64-embedded data URI, or None."""
    if not png_path or not os.path.exists(png_path):
        return None
    try:
        with open(png_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


def _read_file_text(path: str) -> Optional[str]:
    """Read a text file (e.g. reproduce.R) into a string, or None."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def _format_eta_list(eta_list) -> str:
    """Format an eta list for human-readable display."""
    if eta_list is None:
        return "not available"
    if isinstance(eta_list, list):
        if all(isinstance(x, (int, float)) for x in eta_list):
            # Flat numeric list (M=1)
            if len(eta_list) <= 8:
                return ", ".join(f"{x:g}" for x in eta_list)
            return (f"{eta_list[0]:g}, {eta_list[1]:g}, ..., "
                    f"{eta_list[-1]:g} ({len(eta_list)} points)")
        if all(isinstance(x, list) for x in eta_list):
            # M>=2 list of lists
            return (f"M={len(eta_list)} grids of "
                    f"{[len(g) for g in eta_list]} points each")
    return str(eta_list)


def _section(title: str, body_html: str, anchor: str = "") -> str:
    """Render one HTML section with a title bar."""
    anchor_attr = f' id="{anchor}"' if anchor else ""
    return (f'<section{anchor_attr}>\n'
            f'  <h2>{html.escape(title)}</h2>\n'
            f'  {body_html}\n'
            f'</section>\n')


def _kv_table(items: list) -> str:
    """Render a list of (key, value) tuples as a clean two-column table."""
    rows = []
    for k, v in items:
        v_str = "<em>not available</em>" if v is None or v == "" else html.escape(str(v))
        rows.append(
            f'  <tr><td class="k">{html.escape(str(k))}</td>'
            f'<td class="v">{v_str}</td></tr>'
        )
    return ('<table class="kv">\n' + "\n".join(rows) + "\n</table>")


def _compose_html_report(
    *,
    report_id: str,
    title: str,
    data_context: dict,
    fitting_summary: dict,
    selection_summary: dict,
    metadata: dict,
    plots: list,
    reproduce_script: Optional[str],
    notices: list,
) -> str:
    """Build the full HTML document. All sections present; missing data
    shows as 'not available'."""
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mcp_ver = _mcp_version()
    page_title = title or "BRIER fit summary"

    # --- Header section ---
    header_items = [
        ("Report ID", report_id),
        ("Generated", generated_at),
        ("BRIER MCP version", mcp_ver),
        ("Fit ID", metadata.get("fit_id")),
        ("Selection ID", metadata.get("selection_id")),
    ]
    header_html = _section("Header", _kv_table(header_items), "header")

    # --- Data context section ---
    dc_items = [
        ("Source data path", data_context.get("data_path")),
        ("Family", data_context.get("family")),
        ("Fitting tool", data_context.get("tool")),
    ]
    if data_context.get("inspection_found") is True:
        dc_items.append(("Inspection cache",
                         f"found ({len(data_context.get('inspection_files', []))} files)"))
        if data_context.get("inspection_top_keys"):
            dc_items.append(("Inspection top-level keys",
                             ", ".join(data_context["inspection_top_keys"])))
    elif data_context.get("inspection_found") is False:
        dc_items.append(("Inspection cache", "inspection_id not found"))
    else:
        dc_items.append(("Inspection cache", "not provided"))
    data_html = _section("Data context", _kv_table(dc_items), "data-context")

    # --- Fitting summary section ---
    dims = fitting_summary.get("dimensions") or {}
    fs_items = [
        ("Tool", fitting_summary.get("tool")),
        ("Family", fitting_summary.get("family")),
        ("Multi-method", fitting_summary.get("multi_method")),
        ("Predictors (p)", dims.get("p")),
        ("Training n", dims.get("n_train")),
        ("M external", dims.get("M_external")),
        ("Eta grid used",
         _format_eta_list(fitting_summary.get("eta_list_used"))),
    ]
    fit_html = _section("Fitting summary", _kv_table(fs_items),
                         "fitting-summary")

    # --- Data preparation steps section (v0.13) ---
    prep_html = ""
    prep_ids = fitting_summary.get("prep_session_ids") or []
    # R serializes length-1 character vectors as a bare string in JSON;
    # wrap in a list so we don't iterate character-by-character.
    if isinstance(prep_ids, str):
        prep_ids = [prep_ids]
    if prep_ids:
        prep_pieces = []
        for sid in prep_ids:
            try:
                log_records = _prep_log_read(sid)
            except Exception:
                log_records = []
            rows = []
            for rec in log_records:
                ts = rec.get("timestamp", "")
                op = rec.get("operation", "")
                summary = rec.get("result_summary") or {}
                # Pick a one-line summary string per op
                summary_bits = []
                for k in ("aliases_added", "renamed", "n_common_out",
                           "n_kept", "alias_out", "out_alias",
                           "output_path", "n_in_common"):
                    if k in summary:
                        v = summary[k]
                        if isinstance(v, dict):
                            v = ", ".join(f"{kk}={vv}" for kk, vv in v.items())
                        summary_bits.append(f"{k}={v}")
                summary_str = "; ".join(summary_bits) if summary_bits else ""
                rows.append((f"{ts} {op}", summary_str))
            if rows:
                inner = _kv_table(rows)
                prep_pieces.append(
                    f"<h3>prep session <code>{html.escape(sid)}</code></h3>\n"
                    + inner
                )
            else:
                prep_pieces.append(
                    f"<p>prep session <code>{html.escape(sid)}</code>: "
                    "log not found</p>"
                )
        prep_html = _section("Data preparation steps",
                               "\n".join(prep_pieces),
                               "data-preparation")

    # --- Selection summary section ---
    ss = selection_summary
    best_eta = ss.get("best_eta")
    if isinstance(best_eta, list):
        best_eta_str = "(" + ", ".join(f"{x:g}" for x in best_eta) + ")"
    elif best_eta is None:
        best_eta_str = None
    else:
        best_eta_str = f"{best_eta:g}"
    best_lambda = ss.get("best_lambda")
    best_lambda_str = (f"{best_lambda:g}" if isinstance(best_lambda, (int, float))
                       else best_lambda)
    metric_value = ss.get("best_metric_value")
    metric_value_str = (f"{metric_value:g}" if isinstance(metric_value, (int, float))
                        else metric_value)
    sel_items = [
        ("Criterion", ss.get("criteria")),
        ("Selected eta", best_eta_str),
        ("Selected lambda", best_lambda_str),
        ("Best metric value", metric_value_str),
    ]
    sel_html = _section("Selection summary", _kv_table(sel_items),
                         "selection-summary")

    # --- Plots section ---
    if plots:
        plot_blocks = []
        for p in plots:
            name = p.get("name", "")
            data_uri = p.get("data_uri")
            caption = p.get("caption", "")
            if data_uri:
                plot_blocks.append(
                    f'<figure>\n'
                    f'  <img src="{data_uri}" alt="{html.escape(name)}" />\n'
                    f'  <figcaption>{html.escape(caption or name)}</figcaption>\n'
                    f'</figure>'
                )
            else:
                missing_html = (
                    f'<div class="plot-missing">'
                    f'<strong>{html.escape(name)}</strong>: '
                    f'{html.escape(p.get("note") or "not available")}'
                    f'</div>'
                )
                fallback_r = p.get("fallback_r")
                if fallback_r:
                    missing_html += (
                        f'\n<details class="fallback-snippet">'
                        f'<summary>Standalone R snippet to regenerate '
                        f'this plot</summary>'
                        f'<pre><code>{html.escape(fallback_r)}'
                        f'</code></pre></details>'
                    )
                plot_blocks.append(missing_html)
        plots_html = _section("Plots", "\n".join(plot_blocks), "plots")
    else:
        plots_html = _section(
            "Plots",
            ('<p class="note">No test set provided. Re-run summarize_fit '
             'with data_path / newx_expr / newy_expr / criteria to '
             'include plots.</p>'),
            "plots",
        )

    # --- Reproducibility section ---
    if reproduce_script:
        escaped = html.escape(reproduce_script)
        repro_html = _section(
            "Reproducibility",
            (f'<p class="note">The R script below regenerates this fit from '
             f'the source data file, using only the BRIER R package '
             f'(no MCP required). Save and source().</p>\n'
             f'<pre class="code-block"><code>{escaped}</code></pre>'),
            "reproducibility",
        )
    else:
        repro_html = _section(
            "Reproducibility",
            '<p class="note">Reproducibility script not available.</p>',
            "reproducibility",
        )

    # --- MCP metadata section ---
    md_items = [
        ("Tool", metadata.get("tool")),
        ("Fit ID", metadata.get("fit_id")),
        ("Selection ID", metadata.get("selection_id")),
        ("Data path", metadata.get("data_path")),
    ]
    md_html = _section("MCP metadata", _kv_table(md_items), "mcp-metadata")

    # --- Notices, if any ---
    notices_html = ""
    if notices:
        notice_blocks = [f'<li>{html.escape(n)}</li>' for n in notices]
        notices_html = _section(
            "Notices",
            '<ul>' + "".join(notice_blocks) + '</ul>',
            "notices",
        )

    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                       Helvetica, Arial, sans-serif;
           max-width: 920px; margin: 2em auto; padding: 0 1em;
           color: #222; line-height: 1.5; }
    h1 { border-bottom: 2px solid #2c5282; padding-bottom: 0.3em; }
    h2 { color: #2c5282; margin-top: 1.5em; border-bottom: 1px solid #cbd5e0;
         padding-bottom: 0.2em; }
    section { margin-bottom: 2em; }
    table.kv { border-collapse: collapse; width: 100%; max-width: 720px; }
    table.kv td { padding: 0.4em 0.8em; border-bottom: 1px solid #edf2f7; }
    table.kv td.k { font-weight: 600; color: #4a5568; width: 35%; }
    table.kv td.v { font-family: ui-monospace, "SF Mono", "Menlo", monospace;
                    font-size: 0.92em; }
    pre.code-block { background: #f7fafc; border: 1px solid #cbd5e0;
                     border-radius: 4px; padding: 1em;
                     overflow-x: auto; font-size: 0.85em; line-height: 1.4; }
    figure { margin: 1em 0; text-align: center; }
    figure img { max-width: 100%; border: 1px solid #cbd5e0;
                 border-radius: 4px; }
    figcaption { color: #4a5568; font-style: italic; margin-top: 0.4em;
                 font-size: 0.9em; }
    .plot-missing { background: #fffbeb; border: 1px solid #fbd38d;
                    padding: 0.7em; border-radius: 4px; margin: 0.5em 0; }
    .note { color: #4a5568; font-size: 0.95em; }
    em { color: #a0aec0; font-style: italic; }
    """

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>{html.escape(page_title)}</title>
  <style>{css}</style>
</head>
<body>
  <h1>{html.escape(page_title)}</h1>
{header_html}{data_html}{fit_html}{prep_html}{sel_html}{plots_html}{repro_html}{md_html}{notices_html}
</body>
</html>
'''


@mcp.tool()
def summarize_fit(
    selection_id: str,
    inspection_id: Optional[str] = None,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
    newx_expr: Optional[str] = None,
    newy_expr: Optional[str] = None,
    criteria: Optional[str] = None,
    include_eta_plot: bool = True,
    include_box_plot: bool = True,
    include_importance_plot: bool = True,
    include_selection_plot: bool = True,
    bootstrap_n: int = 20,
    bootstrap_plot_max_p: Optional[int] = None,
    plot_timeout_seconds: Optional[int] = None,
    report_title: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> dict:
    """Generate a comprehensive HTML report + reproducibility R script for a cached fit.

    Produces two artifacts in the configured output_directory (or
    ~/.cache/brier-mcp/reports/ if not set):

    * **report_<id>.html** - self-contained HTML report with embedded
      plots (as base64) and the reproducibility script displayed
      inline. Sections: header, data context, fitting summary,
      selection summary, plots, reproducibility, MCP metadata.
      Every section present; missing data shows as "not available".

    * **reproduce_<id>.R** - standalone R script that regenerates the
      fit from the source data file using ONLY the BRIER R package
      (no MCP, no Claude Desktop required). Templated per family
      (BRIERi/BRIERfull/BRIERs) from the fit cache's meta dict.

    Args:
        selection_id: ID of a cached BRIER selection. Required.
        inspection_id: Optional. ID from inspect_user_data; surfaces
            data-shape context in the report.
        data_path, newx_expr, newy_expr, criteria: Optional held-out
            test set for plot generation. If ALL four are provided,
            the report embeds calibration / box / importance plots.
            Otherwise the Plots section says "test set not provided".
        include_eta_plot, include_box_plot, include_importance_plot:
            Toggle individual plot inclusion (only matters if test
            set is provided). Default all True.
        bootstrap_n: Bootstrap replicates for plot_box / plot_importance.
            Default 20. v0.13.3 dropped this from 100 because at high p
            (10k+ SNPs) each replicate is expensive and 100 replicates
            could exceed wall-clock budgets. n=20 is a quick-look
            default; for publication-quality variance estimates, raise
            it (e.g. 100) on a final run.
        bootstrap_plot_max_p: DEPRECATED in v0.13.1. v0.13 silently
            skipped bootstrap plots when p exceeded this threshold;
            v0.13.1 always attempts the plots and falls back gracefully
            on timeout instead. Kept in the signature for backward
            compatibility; pass an int to restore the v0.13 behavior
            (skip bootstrap plots above the threshold), or omit / None
            to use the new soft-degradation path. None by default.
        plot_timeout_seconds: Optional per-plot wall-clock cap for the
            bootstrap box and importance plots. Default None (no cap;
            plots run as long as they need, which is the right behavior
            on a server or for very large p). Pass an int (e.g. 300)
            to opt in to a cap. If a plot trips the cap, the report
            still completes; the plot is replaced by a fallback notice
            plus a runnable R snippet the user can run standalone with
            a smaller bootstrap_n. Snippets also appear for R subprocess
            crashes (where retrying with different resources may help);
            for structural failures (bad selection_id, criterion-family
            mismatch, etc.), no snippet is shown - the error message is
            sufficient.
        report_title: Optional custom report title.

    Returns:
        On success: {status: "ok", report_id, report_html_path,
            reproduce_r_path, summary, _notice_*}.
        On error:   {status: "error", message, class, where}.
    """
    # ---- Stage 1: ask R for metadata + reproduce.R --------------------
    r_payload = {
        "selection_id": selection_id,
        "inspection_id": inspection_id,
        "include_repro": True,
        "output_dir": output_dir,
    }
    meta_resp = _run_r("summarize_fit.R", r_payload)
    if meta_resp.get("status") != "ok":
        return meta_resp

    reports_dir = meta_resp.get("reports_dir")
    report_id = meta_resp.get("report_id")
    if not reports_dir or not report_id:
        return {
            "status": "error",
            "message": "summarize_fit.R did not return reports_dir/report_id",
            "class": "InternalError",
            "where": "server.py:summarize_fit",
        }

    notices = []
    plots = []

    # ---- Stage 2: generate plots, if test set provided ----------------
    # Accept either data_path (legacy) or data_paths (v0.11). Normalize so
    # the plot tools receive both forms.
    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    has_data = dp_list is not None
    has_test_set = all([has_data, newx_expr, newy_expr, criteria])

    # High-p guard: the bootstrap plots (box, importance) refit the model
    # once per replicate. Each refit is expensive when the predictor count
    # p is large, so for high-p fits we skip them by default to avoid
    # hanging the MCP subprocess. The eta plot is cheap (no bootstrap) and
    # is always kept. p comes from the stage-1 metadata.
    fit_p = None
    try:
        fit_p = (meta_resp.get("fitting_summary", {})
                          .get("dimensions", {}) or {}).get("p")
    except Exception:
        fit_p = None
    # v0.13.1: hard cap is opt-in only (back-compat). Default path:
    # always attempt the bootstrap plots; emit a heads-up notice if p is
    # large; rely on plot_timeout_seconds to bail per-plot if too slow.
    if bootstrap_plot_max_p is not None and fit_p is not None \
            and fit_p > bootstrap_plot_max_p:
        if include_box_plot or include_importance_plot:
            notices.append(
                f"Predictor count p={fit_p} exceeds bootstrap_plot_max_p="
                f"{bootstrap_plot_max_p}; skipped the bootstrap box and "
                f"importance plots per legacy v0.13 behavior. (As of "
                f"v0.13.1, bootstrap_plot_max_p is deprecated; omit it "
                f"to always attempt the plots and rely on "
                f"plot_timeout_seconds for graceful failure instead.)"
            )
        include_box_plot = False
        include_importance_plot = False
    elif fit_p is not None and fit_p > 2000 and \
            (include_box_plot or include_importance_plot):
        notices.append(
            f"Heads-up: predictor count p={fit_p} is large. Each "
            f"bootstrap replicate refits the model, so plot_box and "
            f"plot_importance may take several minutes. Per-plot "
            f"timeout is {plot_timeout_seconds}s; if a plot exceeds it, "
            f"the report still completes and the missing plot is "
            f"replaced by a runnable R snippet you can execute "
            f"standalone."
        )

    # Selection-criterion-vs-eta plot: always available (no test set
    # needed), sourced from the stored eta.lambda. Answers "why this eta".
    if include_selection_plot:
        r = brier_plot_selection(
            selection_id=selection_id,
            output_dir=output_dir,
        )
        if r.get("status") == "ok" and r.get("plot_png_path"):
            uri = _embed_png_as_base64(r["plot_png_path"])
            sel_eta = r.get("selected_eta")
            caption = (
                f"Selection criterion ({r.get('criteria', 'criterion')}) "
                f"vs eta, from the values the selection optimized. "
                f"Selected eta = {sel_eta}. No held-out data used."
            )
            plots.append({
                "name": "Selection criterion vs eta",
                "data_uri": uri,
                "caption": caption,
            })
        else:
            plots.append({
                "name": "Selection criterion vs eta",
                "note": f"plot_selection failed: {r.get('message', 'unknown error')}",
            })

    if has_test_set:
        # eta plot
        if include_eta_plot:
            r = brier_plot_eta(
                selection_id=selection_id,
                data_path=dp_legacy,
                data_paths=dp_list,
                newx_expr=newx_expr,
                newy_expr=newy_expr,
                criteria=criteria,
                output_dir=output_dir,
            )
            if r.get("status") == "ok" and r.get("plot_png_path"):
                uri = _embed_png_as_base64(r["plot_png_path"])
                caption = ("Performance vs eta. "
                           f"Criterion: {criteria}. "
                           f"Rendered as {r.get('rendered_kind', 'curve')}.")
                plots.append({
                    "name": "Eta performance",
                    "data_uri": uri,
                    "caption": caption,
                })
            else:
                plots.append({
                    "name": "Eta performance",
                    "note": f"plot_eta failed: {r.get('message', 'unknown error')}",
                })

        # ---- helper: build a runnable R fallback snippet ----------------
        def _fallback_snippet(tool_name: str, suggest_n: int) -> str:
            """Return a small standalone R snippet the user can run to
            regenerate this plot outside of summarize_fit. Reflects the
            current args, but suggests a smaller bootstrap_n in the
            comment."""
            quote = lambda s: '"' + str(s).replace('"', '\\"') + '"' \
                                if s is not None else "NULL"
            paths_r = ("c(" + ", ".join(quote(p) for p in dp_list) + ")") \
                        if dp_list and len(dp_list) > 1 \
                        else quote(dp_legacy)
            return (
                f"# Standalone fallback for {tool_name} - run in your own "
                f"R session.\n"
                f"# Each bootstrap replicate refits the model on a "
                f"row-resample of the\n"
                f"# training data. With p={fit_p} this is expensive; "
                f"lower bootstrap_n if needed.\n"
                f"suppressPackageStartupMessages(library(BRIER))\n"
                f"# bootstrap_n suggestion: {suggest_n} (was "
                f"{bootstrap_n} in this run)\n"
                f"# load your data file(s) and recreate selection_id="
                f"{selection_id!r} first;\n"
                f"# then call the plot tool yourself, e.g.\n"
                f"# {tool_name}(\n"
                f"#   selection_id = {quote(selection_id)},\n"
                f"#   data_path    = {paths_r},\n"
                f"#   newx_expr    = {quote(newx_expr)},\n"
                f"#   newy_expr    = {quote(newy_expr)},\n"
                f"#   criteria     = {quote(criteria)},\n"
                f"#   bootstrap_n  = {suggest_n}\n"
                f"# )\n"
            )

        def _suggest_smaller_n() -> int:
            """Pick a smaller bootstrap_n suggestion: half the current,
            floor 25."""
            return max(25, bootstrap_n // 2)

        # box plot
        if include_box_plot:
            r = brier_plot_box(
                selection_id=selection_id,
                data_path=dp_legacy,
                data_paths=dp_list,
                newx_expr=newx_expr,
                newy_expr=newy_expr,
                criteria=criteria,
                bootstrap_n=bootstrap_n,
                output_dir=output_dir,
                timeout_s=plot_timeout_seconds,
            )
            if r.get("status") == "ok" and r.get("plot_png_path"):
                uri = _embed_png_as_base64(r["plot_png_path"])
                caption = ("Bootstrap performance comparison "
                           f"(target-only / external-only / integrated). "
                           f"n={bootstrap_n} replicates.")
                plots.append({
                    "name": "Bootstrap comparison",
                    "data_uri": uri,
                    "caption": caption,
                })
            else:
                # Two failure categories:
                #   retry-worthy: TimeoutExpired, RscriptCrash, JSONDecodeError
                #     -> show the standalone snippet (running it elsewhere
                #     with different resources may help)
                #   structural: InvalidCriteria, MissingArg, DenylistViolation,
                #     SelectionNotFound, etc. -> error message only; the
                #     standalone snippet would just produce the same error
                cls = r.get("class", "")
                is_timeout = cls == "TimeoutExpired"
                retry_worthy = cls in ("TimeoutExpired", "RscriptCrash",
                                        "JSONDecodeError")
                if is_timeout:
                    notices.append(
                        f"Bootstrap box plot exceeded the per-plot "
                        f"timeout of {plot_timeout_seconds}s and was "
                        f"skipped; the rest of the report is intact. "
                        f"See the snippet below to regenerate it "
                        f"standalone, optionally with a smaller "
                        f"bootstrap_n."
                    )
                elif retry_worthy:
                    notices.append(
                        f"Bootstrap box plot failed: "
                        f"{r.get('message', 'unknown error')}. "
                        f"See the snippet below to retry standalone."
                    )
                else:
                    notices.append(
                        f"Bootstrap box plot failed: "
                        f"{r.get('message', 'unknown error')}."
                    )
                plot_block = {
                    "name": "Bootstrap comparison",
                    "note": ("Skipped (timeout)" if is_timeout
                              else f"plot_box failed: "
                                   f"{r.get('message', 'unknown error')}"),
                }
                if retry_worthy:
                    plot_block["fallback_r"] = _fallback_snippet(
                        "brier_plot_box", _suggest_smaller_n())
                plots.append(plot_block)

        # importance plot
        if include_importance_plot:
            r = brier_plot_importance(
                selection_id=selection_id,
                data_path=dp_legacy,
                data_paths=dp_list,
                newx_expr=newx_expr,
                newy_expr=newy_expr,
                criteria=criteria,
                replications=bootstrap_n,
                output_dir=output_dir,
                timeout_s=plot_timeout_seconds,
            )
            if r.get("status") == "ok" and r.get("plot_png_path"):
                uri = _embed_png_as_base64(r["plot_png_path"])
                caption = ("Variable importance from bootstrap selection "
                           f"frequencies (n={bootstrap_n} replicates).")
                plots.append({
                    "name": "Variable importance",
                    "data_uri": uri,
                    "caption": caption,
                })
            else:
                cls = r.get("class", "")
                is_timeout = cls == "TimeoutExpired"
                retry_worthy = cls in ("TimeoutExpired", "RscriptCrash",
                                        "JSONDecodeError")
                if is_timeout:
                    notices.append(
                        f"Importance plot exceeded the per-plot timeout "
                        f"of {plot_timeout_seconds}s and was skipped; "
                        f"the rest of the report is intact. See the "
                        f"snippet below to regenerate it standalone, "
                        f"optionally with a smaller bootstrap_n."
                    )
                elif retry_worthy:
                    notices.append(
                        f"Importance plot failed: "
                        f"{r.get('message', 'unknown error')}. "
                        f"See the snippet below to retry standalone."
                    )
                else:
                    notices.append(
                        f"Importance plot failed: "
                        f"{r.get('message', 'unknown error')}."
                    )
                plot_block = {
                    "name": "Variable importance",
                    "note": ("Skipped (timeout)" if is_timeout
                              else f"plot_importance failed: "
                                   f"{r.get('message', 'unknown error')}"),
                }
                if retry_worthy:
                    plot_block["fallback_r"] = _fallback_snippet(
                        "brier_plot_importance", _suggest_smaller_n())
                plots.append(plot_block)
    else:
        if has_data or newx_expr or newy_expr or criteria:
            notices.append(
                "Partial test-set arguments provided; need ALL of "
                "data_path, newx_expr, newy_expr, criteria for plots."
            )

    # ---- Stage 3: read reproduce.R into the report --------------------
    reproduce_path = meta_resp.get("reproduce_r_path")
    reproduce_script = _read_file_text(reproduce_path) if reproduce_path and reproduce_path != "NA" else None

    # ---- Stage 4: compose HTML and write to disk ----------------------
    html_doc = _compose_html_report(
        report_id=report_id,
        title=report_title or f"BRIER fit summary ({meta_resp['metadata']['tool']})",
        data_context=meta_resp.get("data_context", {}),
        fitting_summary=meta_resp.get("fitting_summary", {}),
        selection_summary=meta_resp.get("selection_summary", {}),
        metadata=meta_resp.get("metadata", {}),
        plots=plots,
        reproduce_script=reproduce_script,
        notices=notices,
    )

    report_html_path = os.path.join(reports_dir, f"{report_id}.html")
    try:
        with open(report_html_path, "w") as f:
            f.write(html_doc)
    except Exception as e:
        return {
            "status": "error",
            "message": f"Could not write report HTML: {e}",
            "class": "WriteError",
            "where": "server.py:summarize_fit",
        }

    summary = {
        "tool": meta_resp["metadata"].get("tool"),
        "family": meta_resp["data_context"].get("family"),
        "best_eta": meta_resp["selection_summary"].get("best_eta"),
        "best_lambda": meta_resp["selection_summary"].get("best_lambda"),
        "best_metric_value": meta_resp["selection_summary"].get("best_metric_value"),
        "criteria": meta_resp["selection_summary"].get("criteria"),
        "plots_included": len([p for p in plots if p.get("data_uri")]),
    }

    out = {
        "status": "ok",
        "report_id": report_id,
        "report_html_path": report_html_path,
        "reproduce_r_path": reproduce_path,
        "summary": summary,
    }
    if notices:
        out["_notice"] = "; ".join(notices)
    if not has_test_set:
        out["_notice_no_plots"] = (
            "No test set provided; report has no plots embedded. "
            "Re-run with data_path/data_paths, newx_expr, newy_expr, "
            "criteria to include plots."
        )
    # Backstop: warn if the report landed in the MCP cache because neither
    # an explicit output_dir was passed NOR a global output_directory was
    # configured. Users routinely cannot find files under ~/.cache/.
    if output_dir is None:
        try:
            cfg_od = _load_config().get("output_directory")
        except Exception:
            cfg_od = None
        if not cfg_od:
            out["_notice_default_output"] = (
                "Report saved to the default MCP cache directory "
                f"({report_html_path}). To save to a project folder "
                "instead, re-run summarize_fit with output_dir set to a "
                "writable directory, or call set_output_directory once "
                "to configure a default for the session."
            )
    return out


# --------------------------------------------------------------------------
# prep_data (v0.13): composable, leakage-aware data preparation
# --------------------------------------------------------------------------

_PREP_OPERATIONS = (
    "alias_root", "rename_columns", "derive_corr_from_pvalue",
    "reshape_to_matrix", "subset_to_common_snps", "harmonize_alleles",
    "verify_aligned", "assemble", "persist",
)


@mcp.tool()
def prep_data(
    operation: str,
    session_id: Optional[str] = None,
    # alias_root
    data_path: Optional[str] = None,
    alias: Optional[str] = None,
    # rename_columns
    mapping: Optional[dict] = None,
    # derive_corr_from_pvalue
    pvalue_col: Optional[str] = None,
    n_col: Optional[str] = None,
    beta_col: Optional[str] = None,
    output_col: Optional[str] = None,
    pvalue_sided: Optional[str] = None,
    # reshape_to_matrix
    value_col: Optional[str] = None,
    id_col: Optional[str] = None,
    out_alias: Optional[str] = None,
    # subset_to_common_snps / verify_aligned
    aliases: Optional[list] = None,
    # harmonize_alleles
    target_alias: Optional[str] = None,
    external_alias: Optional[str] = None,
    a1_col: Optional[str] = None,
    a2_col: Optional[str] = None,
    coef_col: Optional[str] = None,
    drop_strand_ambiguous: Optional[bool] = None,
    drop_mismatched: Optional[bool] = None,
    # assemble
    bundle: Optional[dict] = None,
    # persist
    output_path: Optional[str] = None,
) -> dict:
    """Composable, leakage-aware data preparation (v0.13).

    Runs ONE operation at a time on a prep session. Sessions are cached
    on disk so intermediate state survives between calls and the user
    can inspect what has been built up. Every call appends to an audit
    log; when a downstream fit tool consumes a persisted prep file, the
    prep history attaches to the fit cache and surfaces in summarize_fit.

    Operations (whitelist):
      - alias_root: load a .rds/.rda/.RData file into the session bench
      - rename_columns: rename data.frame columns
      - derive_corr_from_pvalue: derive a 'corr' column from p-value,
        sample size, and a signed effect
      - reshape_to_matrix: long-form data.frame -> single-column matrix
        keyed by variant id
      - subset_to_common_snps: intersect SNP sets across named aliases,
        subset each to the common set in matched order
      - harmonize_alleles: for SNPs with swapped A1/A2 between target
        and external, flip the sign on the external coef; drop strand-
        ambiguous (A/T, C/G) by default
      - verify_aligned: sanity-check that named aliases have the same
        SNP set in the same order
      - assemble: bundle named aliases into a single list for use by
        a fit tool
      - persist: save the session state (or one alias) to a .rds with
        the prep_session_id embedded so fit tools can pick up the audit
        trail

    Cross-source statistical operations (joint PCA, joint normalization,
    leak-prone transforms) are NOT in the whitelist. If they are ever
    added, they will require an explicit `unsafe_cross_source=True`
    flag.

    SPECULATIVE DEFAULTS to be aware of (v0.13 was built before the
    operations had been pressure-tested against real data; if your
    workflow contradicts these, override via the relevant kwarg):

      derive_corr_from_pvalue:
        - pvalue_sided defaults to "two" (two-sided p-values).
          Set "one" for one-sided p-values.
        - sign of correlation comes from the `beta` column.
      subset_to_common_snps / verify_aligned:
        - id_col defaults to "rsid"; falls back to "variable" if missing
          for data.frames.
        - For matrices, matching uses rownames; if absent, errors.
      harmonize_alleles:
        - drop_strand_ambiguous defaults to True (matches
          BRIER::preprocessI/S). A/T and C/G SNPs are dropped because
          strand cannot be determined from alleles alone.
        - drop_mismatched defaults to True. SNPs whose external alleles
          are neither identical nor a clean swap of the target are
          dropped rather than kept with NA.

    Args:
        operation: One of "alias_root", "rename_columns",
            "derive_corr_from_pvalue", "reshape_to_matrix",
            "subset_to_common_snps", "harmonize_alleles",
            "verify_aligned", "assemble", "persist".
        session_id: Existing session id to continue. If omitted (typical
            on the first call), a fresh session is created.
        data_path: For alias_root. Absolute path to .rds/.rda/.RData.
        alias: For alias_root (target alias name for .rds, default
            basename) and other ops (which alias to operate on).
        mapping: For rename_columns. Dict {old_col: new_col, ...}.
        pvalue_col, n_col, beta_col, output_col, pvalue_sided:
            For derive_corr_from_pvalue.
        value_col, id_col, out_alias: For reshape_to_matrix.
        aliases: For subset_to_common_snps and verify_aligned. List of
            alias names (>=2).
        id_col: For subset/verify; defaults to "rsid".
        target_alias, external_alias, a1_col, a2_col, coef_col,
        drop_strand_ambiguous, drop_mismatched: For harmonize_alleles.
        bundle: For assemble. Dict {out_key: alias_name, ...}.
        out_alias: For assemble (name of the bundled alias, default
            "assembled").
        output_path: For persist. Path to write .rds.

    Returns:
        On success: {status: "ok", operation, session_id, summary,
            aliases (current bench, with shapes)}.
        On error:   {status: "error", message, class, where}.

    Cache layout:
        ~/.cache/brier-mcp/prep_sessions/<session_id>/
          state.rds   - the working bench (named list of aliases)
          log.jsonl   - audit log, one record per prep_data call
    """
    if operation not in _PREP_OPERATIONS:
        return {
            "status": "error",
            "message": (f"unknown operation '{operation}'; valid: "
                        + ", ".join(_PREP_OPERATIONS)),
            "class": "InvalidOperation",
            "where": "server.py:prep_data",
        }

    # Start or continue a session.
    if not session_id:
        session_id = _generate_prep_session_id()
        new_session = True
    else:
        new_session = False
    session_dir = _prep_session_dir(session_id)

    r_payload = {
        "operation": operation,
        "session_id": session_id,
        "session_dir": session_dir,
        "data_path": data_path,
        "alias": alias,
        "mapping": mapping,
        "pvalue_col": pvalue_col,
        "n_col": n_col,
        "beta_col": beta_col,
        "output_col": output_col,
        "pvalue_sided": pvalue_sided,
        "value_col": value_col,
        "id_col": id_col,
        "out_alias": out_alias,
        "aliases": aliases,
        "target_alias": target_alias,
        "external_alias": external_alias,
        "a1_col": a1_col,
        "a2_col": a2_col,
        "coef_col": coef_col,
        "drop_strand_ambiguous": drop_strand_ambiguous,
        "drop_mismatched": drop_mismatched,
        "bundle": bundle,
        "output_path": output_path,
    }
    result = _run_r("prep_data.R", r_payload)
    if result.get("status") == "ok":
        # Append to audit log. We log the user-facing args (non-None) and
        # a compact result summary.
        logged_args = {k: v for k, v in r_payload.items()
                        if v is not None
                        and k not in ("session_dir",)}
        _prep_log_append(session_id, operation, logged_args,
                          result.get("summary", {}))
        result["session_id"] = session_id
        if new_session:
            result["_notice_new_session"] = (
                f"Started new prep session '{session_id}'. Pass "
                f"session_id='{session_id}' on subsequent prep_data "
                f"calls to continue working in this session."
            )
    return result


@mcp.tool()
def prep_data_log(session_id: str) -> dict:
    """Return the audit log for a prep_data session.

    Reads ~/.cache/brier-mcp/prep_sessions/<session_id>/log.jsonl and
    returns the records in order. Useful for inspecting what has been
    done in a session and for embedding the trail in reports.

    Args:
        session_id: The prep session id (from prep_data's response).

    Returns:
        {status: "ok", session_id, log: [{timestamp, operation, args,
            result_summary}, ...]}.
    """
    session_dir = os.path.join(_prep_cache_root(), session_id)
    if not os.path.isdir(session_dir):
        return {
            "status": "error",
            "message": f"prep session '{session_id}' not found",
            "class": "SessionNotFound",
            "where": "server.py:prep_data_log",
        }
    return {
        "status": "ok",
        "session_id": session_id,
        "log": _prep_log_read(session_id),
    }


# --------------------------------------------------------------------------
# Preprocessing tools (v0.8.1): align SNP info across sources
# --------------------------------------------------------------------------

@mcp.tool()
def preprocess_i(
    target_info_expr: str,
    external_coef_exprs: list,
    target_info_cols: Optional[dict] = None,
    external_ss_cols: Optional[dict] = None,
    external_coef_cols: Optional[list] = None,
    drop_ambiguous: bool = True,
    verbose: bool = False,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Align target SNP info with one or more external coefficient tables.

    Wraps BRIER::preprocessI. Use this BEFORE brier_i when the user's
    target SNP table and the external coefficient tables use different
    SNP identifiers, coordinates, or allele codings. The function aligns
    by CHR/BP/REF/ALT and (optionally) drops strand-ambiguous SNPs.

    Result is cached on disk and returned by preprocess_id; the user can
    read it back in R with `readRDS(preprocess_path)`.

    Args:
        data_path: Absolute path to a .rda/.RData/.rds file containing
            the target SNP table and external coefficient tables.
        target_info_expr: R expression for the target SNP info
            data.frame (must contain the columns named in
            target_info_cols).
        external_coef_exprs: List of R expressions, one per external
            coefficient source. Each resolves to a data.frame with
            columns named in external_ss_cols plus the coef column(s)
            named in external_coef_cols.
        target_info_cols: Mapping of standard names to actual column
            names in target_info, e.g. {"chr": "CHR", "bp": "BP",
            "ref": "A2", "alt": "A1"}. Default uses CHR/BP/REF/ALT.
        external_ss_cols: Same idea for the external data.frames.
        external_coef_cols: Names of the coefficient column(s) in
            external data.frames (e.g. ["coef"]). Optional; if omitted,
            preprocessI uses its own heuristic.
        drop_ambiguous: If True, drop strand-ambiguous SNPs (A/T, C/G).
            Default True.
        verbose: If True, preprocessI prints progress messages.

    Returns:
        On success: {status: "ok", preprocess_id, preprocess_path,
            summary: {n_target_in, n_external_in_per_source,
            n_aligned_out, n_dropped_ambiguous, M_external,
            fit_seconds}, _notice_*}.
        On error:   {status: "error", message, class, where}.
    """
    if not external_coef_exprs:
        return {
            "status": "error",
            "message": "external_coef_exprs must be a non-empty list",
            "class": "InvalidInput",
            "where": "server.py:preprocess_i",
        }
    exprs_to_validate = {"target_info_expr": target_info_expr}
    for i, e in enumerate(external_coef_exprs):
        exprs_to_validate[f"external_coef_exprs[{i}]"] = e
    err = _validate_exprs(**exprs_to_validate)
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:preprocess_i"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:preprocess_i"}

    return _run_r("preprocess_i.R", {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "target_info_expr": target_info_expr,
        "external_coef_exprs": external_coef_exprs,
        "target_info_cols": target_info_cols,
        "external_ss_cols": external_ss_cols,
        "external_coef_cols": external_coef_cols,
        "drop_ambiguous": drop_ambiguous,
        "verbose": verbose,
    })


@mcp.tool()
def preprocess_s(
    target_ss_expr: str,
    target_ld_expr: str,
    target_ld_mat_expr: str,
    external_coef_exprs: list,
    target_ind: str = "gwas",
    target_ss_cols: Optional[dict] = None,
    target_ld_cols: Optional[dict] = None,
    external_ss_cols: Optional[dict] = None,
    external_coef_cols: Optional[list] = None,
    drop_ambiguous: bool = True,
    verbose: bool = False,
    data_path: Optional[str] = None,
    data_paths: Optional[list] = None,
) -> dict:
    """Align target sumstats + LD + external coefficient tables.

    Wraps BRIER::preprocessS. Use this BEFORE brier_s when target
    summary statistics, the LD matrix, and external coefficient tables
    use mismatched SNP identifiers / coordinates / allele codings.
    Aligns by CHR/BP/REF/ALT across all sources and (optionally) drops
    strand-ambiguous SNPs.

    Result is cached on disk and returned by preprocess_id; the user
    can read it back in R with `readRDS(preprocess_path)`.

    Args:
        data_path: Absolute path to a .rda/.RData/.rds file containing
            the target sumstats / LD info / LD matrix / external coefs.
        target_ss_expr: R expression for the target summary-statistics
            data.frame.
        target_ld_expr: R expression for the LD info data.frame (one
            row per variant in the LD matrix, with CHR/BP/REF/ALT).
        target_ld_mat_expr: R expression for the LD matrix itself
            (typically a Matrix::dgCMatrix).
        external_coef_exprs: List of R expressions for external
            coefficient tables.
        target_ind: One of "gwas" (default; sumstats are GWAS-style
            with beta/pval/n) or "corr" (sumstats already in
            correlation form). Default "gwas".
        target_ss_cols: Mapping for target sumstats columns. Default
            uses CHR/BP/REF/ALT/pval/n/sgn/beta/corr.
        target_ld_cols: Mapping for LD info columns.
        external_ss_cols: Mapping for external coefficient table
            columns.
        external_coef_cols: Names of the coefficient column(s).
        drop_ambiguous: If True, drop strand-ambiguous SNPs.
        verbose: If True, preprocessS prints progress messages.

    Returns:
        On success: {status: "ok", preprocess_id, preprocess_path,
            summary: {n_target_ss_in, n_target_ld_in,
            n_external_in_per_source, n_aligned_out,
            n_dropped_ambiguous, M_external, fit_seconds},
            _notice_*}.
        On error:   {status: "error", message, class, where}.
    """
    if not external_coef_exprs:
        return {
            "status": "error",
            "message": "external_coef_exprs must be a non-empty list",
            "class": "InvalidInput",
            "where": "server.py:preprocess_s",
        }
    exprs_to_validate = {
        "target_ss_expr": target_ss_expr,
        "target_ld_expr": target_ld_expr,
        "target_ld_mat_expr": target_ld_mat_expr,
    }
    for i, e in enumerate(external_coef_exprs):
        exprs_to_validate[f"external_coef_exprs[{i}]"] = e
    err = _validate_exprs(**exprs_to_validate)
    if err:
        return {"status": "error", "message": err,
                "class": "DenylistViolation",
                "where": "server.py:preprocess_s"}

    dp_legacy, dp_list = _normalize_data_paths(data_path, data_paths)
    if dp_list is None:
        return {"status": "error",
                "message": "either data_path or data_paths is required",
                "class": "MissingArg", "where": "server.py:preprocess_s"}

    return _run_r("preprocess_s.R", {
        "data_path": dp_legacy,
        "data_paths": dp_list,
        "target_ss_expr": target_ss_expr,
        "target_ld_expr": target_ld_expr,
        "target_ld_mat_expr": target_ld_mat_expr,
        "external_coef_exprs": external_coef_exprs,
        "target_ind": target_ind,
        "target_ss_cols": target_ss_cols,
        "target_ld_cols": target_ld_cols,
        "external_ss_cols": external_ss_cols,
        "external_coef_cols": external_coef_cols,
        "drop_ambiguous": drop_ambiguous,
        "verbose": verbose,
    })


@mcp.tool()
def prep_auto(
    shape: str,
    data_dir: str,
    roles: dict,
    standardize: bool = False,
    standardize_method: str = "sd",
    outcome_family: str = "gaussian",
    align_method: str = "auto",
    ld_ancestry: Optional[str] = None,
    ld_build: Optional[str] = None,
    external_ld_ancestry: Optional[str] = None,
    external_ld_build: Optional[str] = None,
    coverage_min: Optional[float] = None,
    predictor_type: str = "auto",
    persist: bool = True,
    out_dir: Optional[str] = None,
) -> dict:
    """Assemble fit-ready BRIER inputs in ONE call: alignment, LD, external fit, scale.

    prep_auto OWNS the alignment (it no longer calls BRIER::preprocessI /
    preprocessS -- those hard-require CHR/BP/REF/ALT and so cannot express the
    non-genotype path). Their correctness is inherited as a TEST, not as a runtime
    dependency: mcp/tests/test_aligner_differential.R asserts BITWISE agreement
    with them on genotype data.

    What one call does:
      * MATCH the external(s) to the target -- by coordinate (CHR/BP/REF/ALT) when
        the map carries them, else by predictor NAME.
      * CORRECT allele flips (sign-flip the coefficient / corr), and RESOLVE
        strand-ambiguous palindromes (A/T, C/G) by allele frequency rather than
        dropping them: for a palindrome the LETTERS carry no orientation, so the
        rule is "negate iff AF is nearer 1-AF_ref than AF_ref, by a margin". The
        margin makes the undecidable band near AF=0.5 fall out on its own.
      * ALIGN THE EXTERNAL TO THE TARGET, and NOT by intersection: every target
        predictor is kept (the target panel defines p), a target predictor the
        external does not cover gets coefficient 0, and an external-only predictor
        is dropped. brier_full is the exception -- pooling raw genotypes CANNOT
        impute, so it takes the intersection of the cohorts.
      * DERIVE `corr` for a summary target when the GWAS ships none (from p, N and
        the sign of the effect).
      * BUILD THE LD when only a reference panel is given (role `target_ld_panel`
        + `ld_ancestry` + `ld_build` -> Berisa blocks). You do NOT orchestrate
        get_ldb -> cal_ld yourself.
      * FIT A RAW EXTERNAL. If the external is raw data rather than a pretrained
        coefficient vector, prep_auto FITS it (the matching fitter at eta=0, on the
        external's own data, restricted to the target's panel FIRST so the
        coefficients are not a truncated shadow of a bigger model) and integrates
        the result. Summary external: roles external_sumstats + external_ld_panel +
        external_snp_info, params external_ld_ancestry/_build. Individual external:
        external_X + external_y (+ optional external_X_val/_y_val). Number them
        (_1, _2, ...) for several. You do NOT fit the external yourself.
      * STANDARDIZE conditionally, add the BRIERi intercept row, subset X / the LD
        to the surviving predictors, and align the val/test splits to the training
        scale.

    It returns {prepared_path, expr_hints, report, external_fits}. `report` lists
    every step it performed; `external_fits` records each internally-fit external
    (nonzero coefficients, selection criterion) so the fit is auditable.

    DECISIONS stay with the caller:
      * shape: which target shape you routed to.
      * standardize: CONDITIONAL, not automatic. TRUE when the target must match a
        standardized-scale external (a pretrained external usually is, and a fitted
        one always is). A raw-scale target fit against a standardized external is a
        SILENT corruption -- no error, just wrong numbers.
      * standardize_method: "sd" ((x-mean)/sd) or "maf" (center 2p, scale
        sqrt(2p(1-p))).
      * outcome_family: only Gaussian y is standardized; binary/Poisson y never are.
      * align_method: "auto" (coordinate when the map has CHR/BP/REF/ALT, else
        names), "coordinate", or "varnames".
      * predictor_type: "auto" DETECTS it (a map with CHR + BP is a genome; nothing
        else can be). Set "generic" for non-genotype predictors (gene expression,
        proteins): they match by NAME, there are no LD blocks, and the LD becomes a
        plain correlation -- so omit ld_ancestry/ld_build for them.

    Args:
        shape: "brier_i" | "brier_s" | "brier_full".
        data_dir: Absolute path to the folder holding the data files.
        roles: Mapping of logical role -> filename (relative to data_dir).
            brier_i: target_X_train, target_y_train, snp_info, and one of
                external_coef OR external_coef_1/_2/...; optionally
                target_X_val/y_val, target_X_test/y_test.
            brier_s: target_sumstats, snp_info (LD panel info), the LD as
                EITHER target_ld (a prebuilt LD matrix / cal_ld object) OR
                target_ld_panel (a reference predictor panel that prep_auto builds
                the LD from -- pass the ld_ancestry/ld_build args for genotype
                data, omit them for non-genotype); external_coef(s); optional
                target_ind ("gwas"/"corr"), target_X_train/val/test + y for
                val/test standardization.
            brier_full: snp_info, target_X_train, target_y_train, and
                external_X_1/external_y_1, external_X_2/... for each raw
                external cohort; optionally target_X_val/y_val and
                target_X_test/y_test (for selecting the pooled fit and scoring
                on held-out target data), and per-cohort external validation
                external_X_1_val/external_y_1_val, external_X_2_val/... (so each
                external-only comparator can be selected on its own held-out
                data instead of by BIC).
        standardize: See above.
        standardize_method: "sd" or "maf".
        outcome_family: "gaussian" | "binomial" | "poisson".
        align_method: "auto" | "coordinate" | "varnames".
        ld_ancestry: Only for brier_s with a target_ld_panel. GENOTYPE ancestry
            ("AFR"/"EUR"/"EAS") selecting the Berisa LD blocks. Omit for
            non-genotype predictors (prep_auto then builds a plain correlation).
        ld_build: Only for brier_s with a target_ld_panel. Genome build
            ("hg19"/"hg38") for the Berisa blocks. Omit for non-genotype.
        external_ld_ancestry: Ancestry ("AFR"/"EUR"/"EAS") for a RAW SUMMARY
            external's OWN LD, when the external is given as external_sumstats +
            external_ld_panel (prep_auto fits the external internally at eta=0).
            E.g. a EUR external -> "EUR". Defaults to ld_ancestry if omitted.
        external_ld_build: Genome build ("hg19"/"hg38") for a raw summary
            external's LD. Defaults to ld_build if omitted.
        coverage_min: Minimum fraction of the fitted model's predictors that a
            validation or testing split must carry, in (0, 1]. Default 0.8.
            A split at or below it is REFUSED rather than silently scored on
            the overlap: a partial split scores a model that cannot see part of
            its own panel, so the metric is computed against a different model
            than the one being selected and the chosen lambda is biased while
            looking healthy. A refused VALIDATION split falls back to an
            information criterion; a refused TESTING split is an error, because
            there is no substitute for it. For brier_full (which pools raw
            genotypes and therefore cannot impute a missing predictor at all)
            this gates the shared panel and is an error below the threshold.
            OMIT unless the user asks: the default is right.
        predictor_type: What the predictors ARE: "genotype" (SNPs) or "generic"
            (gene expression, proteins, anything with no allele). Default
            "auto", which DETECTS it from the variant map: CHR + BP means a
            genome, and nothing else can be one. It matters because orientation
            is a genotype concept -- a gene's expression level has no opposite
            allele, so there is nothing to flip and no strand to be ambiguous
            about, and its LD is a plain correlation rather than Berisa blocks.
            OMIT it: the detection is reliable. Pass it only to override a map
            whose coordinates are not genomic.
        persist: If True, write the assembled object to
            out_dir/prep_auto_<shape>.rds and return its path.
        out_dir: Where to persist (defaults to data_dir).

    Returns:
        On success: {status: "ok", shape, prepared_path, standardize,
            standardize_method, expr_hints, report}. Load the object with
            prepared <- readRDS(prepared_path), then use expr_hints (e.g.
            X_expr="prepared$X") in the fit call. report lists the steps.
        On error: {status: "error", message, class, where}.
    """
    if shape not in ("brier_i", "brier_s", "brier_full"):
        return {"status": "error", "message": f"unknown shape: {shape}",
                "class": "ValueError", "where": "server.py:prep_auto"}
    if not isinstance(roles, dict) or not roles:
        return {"status": "error",
                "message": "roles must be a non-empty mapping of role -> filename",
                "class": "ValueError", "where": "server.py:prep_auto"}
    if standardize_method not in ("sd", "maf"):
        return {"status": "error",
                "message": "standardize_method must be 'sd' or 'maf'",
                "class": "ValueError", "where": "server.py:prep_auto"}
    if align_method not in ("auto", "coordinate", "varnames"):
        return {"status": "error",
                "message": "align_method must be 'auto', 'coordinate', or 'varnames'",
                "class": "ValueError", "where": "server.py:prep_auto"}
    if str(predictor_type).lower() not in (
            "auto", "genotype", "snp", "genetic", "variant",
            "generic", "gene_expression", "expression", "protein", "proteomic",
            "continuous", "other"):
        return {"status": "error",
                "message": ("predictor_type must be 'auto', 'genotype' (SNP) or "
                            "'generic' (gene expression, protein, ...); got "
                            f"'{predictor_type}'"),
                "class": "ValueError", "where": "server.py:prep_auto"}

    return _run_r("prep_auto.R", {
        "shape": shape,
        "data_dir": data_dir,
        "roles": roles,
        "standardize": bool(standardize),
        "standardize_method": standardize_method,
        "outcome_family": outcome_family,
        "align_method": align_method,
        "ld_ancestry": ld_ancestry,
        "ld_build": ld_build,
        "external_ld_ancestry": external_ld_ancestry,
        "external_ld_build": external_ld_build,
        "coverage_min": coverage_min,
        "predictor_type": predictor_type,
        "persist": bool(persist),
        "out_dir": out_dir,
    }, timeout_s=_PREP_AUTO_TIMEOUT_S)


# --------------------------------------------------------------------------
# Configuration tools
# --------------------------------------------------------------------------

def _config_file_path() -> str:
    """Where the per-user MCP config lives."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        if os.name == "nt":
            base = os.environ.get(
                "APPDATA",
                os.path.join(os.path.expanduser("~"), "AppData", "Roaming"),
            )
        else:
            base = os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "brier-mcp")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")


def _load_config() -> dict:
    """Load the MCP config file. Returns {} if missing or unreadable."""
    p = _config_file_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(cfg: dict) -> bool:
    """Write the config dict to disk."""
    p = _config_file_path()
    try:
        with open(p, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except OSError:
        return False


@mcp.tool()
def set_output_directory(path: str) -> dict:
    """Set the directory where BRIER MCP writes user-visible outputs.

    Affects future calls to tools that produce files the user typically
    wants to find easily (prediction CSVs, plots, exports). The setting
    is persistent across sessions and stored in
    ~/.config/brier-mcp/config.json (or platform equivalent).

    Cache directories (fits, LD matrices, inspections) are NOT affected
    by this setting; those stay in ~/.cache/brier-mcp/ where the AI
    references them by id.

    Args:
        path: Absolute path to an existing directory. Tilde expansion is
            applied (~/foo becomes /Users/you/foo on Mac).

    Returns:
        On success: {status: "ok", output_directory, config_file}.
        On error:   {status: "error", message, class, where}.
    """
    if not path or not isinstance(path, str):
        return {
            "status": "error",
            "message": "path must be a non-empty string",
            "class": "InvalidInput",
            "where": "server.py:set_output_directory",
        }
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        return {
            "status": "error",
            "message": f"Directory does not exist: {expanded}",
            "class": "InvalidInput",
            "where": "server.py:set_output_directory",
        }
    if not os.path.isdir(expanded):
        return {
            "status": "error",
            "message": f"Path exists but is not a directory: {expanded}",
            "class": "InvalidInput",
            "where": "server.py:set_output_directory",
        }
    cfg = _load_config()
    cfg["output_directory"] = expanded
    if not _save_config(cfg):
        return {
            "status": "error",
            "message": "Failed to write config file",
            "class": "IOError",
            "where": "server.py:set_output_directory",
        }
    return {
        "status": "ok",
        "output_directory": expanded,
        "config_file": _config_file_path(),
    }


@mcp.tool()
def get_output_directory() -> dict:
    """Return the currently configured output directory, if any.

    Returns:
        {status: "ok", output_directory, config_file}. If the user has
        not set an output_directory, output_directory will be null and
        the response includes a note about the default (no special dir,
        files land in ~/.cache/brier-mcp/).
    """
    cfg = _load_config()
    od = cfg.get("output_directory")
    out = {
        "status": "ok",
        "output_directory": od,
        "config_file": _config_file_path(),
    }
    if od is None:
        out["_notice_default"] = (
            "No output_directory configured. Tools place output files "
            "in ~/.cache/brier-mcp/ (cache directory). Set an explicit "
            "directory with set_output_directory(path) if you want "
            "outputs in a more visible location."
        )
    return out


# --------------------------------------------------------------------------
# Roadmap (v0.x complete; future versions):
#   * v0.7.1: M-based recommendation, coarser BRIERfull eta grid (7),
#             eta=0 baseline auto-fix, time-expectation field, BRIERs
#             predict un-standardize, output-directory config,
#             preprocessI/preprocessS wrappers, llms.txt corrections.
#   * v0.8:   Stateful wizard tracking (if v0.7 instructional fix
#             proves insufficient).
#   * v1.0:   INSTALL.md polish, signed .mcpb, GitHub release.
#   * v1.1:   SSH remote launch.
#   * v1.2:   PLINK, bcftools, GWAS Catalog, PGS Catalog wrappers.
#   * v1.3:   skills layer for AI assistants.
# --------------------------------------------------------------------------


def _selfcheck() -> dict:
    """Environment self-check for remote-launch debugging.

    Verifies that the things the MCP tools depend on are actually present
    and usable, WITHOUT starting the MCP protocol loop. Intended to be run
    over SSH exactly the way Claude Desktop will launch the server, so a
    misconfigured remote can be diagnosed where its output is visible:

        ssh host 'cd /path/to/BRIER-MCP && \\
            BRIER_RSCRIPT=/path/to/Rscript /path/to/uv run server.py --selfcheck'

    Checks (all failures here would otherwise show up as silent,
    hard-to-diagnose breakage inside Claude Desktop):
      * Rscript is discoverable (the #1 remote failure: non-interactive
        SSH shells often lack R on PATH)
      * R actually runs and reports a version
      * the BRIER package loads via library(BRIER) - a real load, not just
        a namespace existence test, so a broken dependency (e.g. Matrix)
        surfaces here rather than at fit time
      * the cache directory is creatable and writable (clusters with
        quota'd home dirs routinely fail this; fitting then appears to
        work but saving fails deep in a tool call)

    Returns a dict (printed as JSON by the --selfcheck entry point).
    status is "ok" only if every blocking check passes.
    """
    report: dict = {
        "brier_mcp_version": __version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cwd": os.getcwd(),
    }
    failures = []

    # --- Rscript discovery -------------------------------------------------
    try:
        rscript = _find_rscript()
        report["rscript"] = rscript
        report["rscript_found"] = True
    except FileNotFoundError as e:
        report["rscript"] = None
        report["rscript_found"] = False
        report["rscript_error"] = str(e)
        failures.append("rscript_not_found")
        rscript = None

    # --- R version + BRIER load (single R call) ---------------------------
    # We do both in one Rscript invocation: print R version, then attempt a
    # REAL load of BRIER and print a clearly-delimited version line. A real
    # library() load pulls in BRIER's Imports and dlopen's its compiled .so,
    # so a broken dependency (missing GLIBCXX, unresolved symbol) fails here.
    #
    # Hardening notes (learned the hard way): a shared-object load failure does
    # not always surface as a catchable R error -- it can appear as a warning
    # or partial message while packageVersion() still returns text. So we:
    #   (1) wrap the load in tryCatch for both errors AND warnings,
    #   (2) emit the version on its own line with unambiguous BRIERVER= / 
    #       BRIERERR= markers so parsing never mistakes an error blob for a
    #       version, and
    #   (3) additionally scan stderr for known load-failure signatures.
    if rscript is not None:
        r_probe = (
            'cat(R.version.string, "\\n");'
            'res <- tryCatch('
            '{ suppressMessages(suppressWarnings(library(BRIER))); '
            'v <- as.character(utils::packageVersion("BRIER")); '
            'paste0("BRIERVER=", v) }, '
            'error=function(e) paste0("BRIERERR=", conditionMessage(e)), '
            'warning=function(w) paste0("BRIERERR=", conditionMessage(w)));'
            'cat(res, "\\n")'
        )
        try:
            proc = subprocess.run(
                [rscript, "--no-save", "--no-restore", "--no-init-file",
                 "-e", r_probe],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=60,
            )
            out_lines = [l for l in proc.stdout.splitlines() if l.strip()]
            report["r_version"] = out_lines[0].strip() if out_lines else None

            # Find the marker line rather than trusting "last line".
            marker = ""
            for l in out_lines:
                s = l.strip()
                if s.startswith("BRIERVER=") or s.startswith("BRIERERR="):
                    marker = s
                    break

            # Detect known shared-object load-failure signatures in stderr,
            # which can appear even when stdout looks superficially fine.
            stderr_blob = (proc.stderr or "")
            load_fail_signatures = (
                "GLIBCXX", "GLIBC_", "unable to load shared object",
                "cannot open shared object", "undefined symbol",
                "version `", "failed to load",
            )
            stderr_load_fail = any(sig in stderr_blob
                                   for sig in load_fail_signatures)

            def _looks_like_version(s: str) -> bool:
                # A real package version is digits and dots (optionally with
                # a trailing build component), e.g. 1.0.2 or 1.0.2.9000.
                import re
                return bool(re.fullmatch(r"\d+(\.\d+){1,3}", s.strip()))

            if marker.startswith("BRIERVER="):
                version = marker[len("BRIERVER="):].strip()
                if _looks_like_version(version) and not stderr_load_fail:
                    report["brier_package_installed"] = True
                    report["brier_package_version"] = version
                else:
                    # Either the "version" is not a version (an error blob
                    # leaked through), or stderr shows a load failure.
                    report["brier_package_installed"] = False
                    report["brier_load_error"] = (
                        ("version string did not look valid: "
                         + repr(version) + "; ") if not _looks_like_version(version)
                        else "")
                    if stderr_load_fail:
                        report["brier_load_error"] += (
                            "shared-object load failure in stderr: "
                            + stderr_blob.strip()[:300])
                    failures.append("brier_not_loadable")
            elif marker.startswith("BRIERERR="):
                report["brier_package_installed"] = False
                report["brier_load_error"] = marker[len("BRIERERR="):].strip()
                failures.append("brier_not_loadable")
            else:
                # No marker at all -> something went wrong before our line.
                report["brier_package_installed"] = False
                report["brier_load_error"] = (
                    "no BRIER marker in probe output; stderr: "
                    + stderr_blob.strip()[:300])
                failures.append("brier_not_loadable")
        except subprocess.TimeoutExpired:
            report["r_version"] = None
            report["brier_package_installed"] = False
            report["brier_load_error"] = "R probe timed out after 60s"
            failures.append("r_probe_timeout")
        except Exception as e:  # noqa: BLE001 - selfcheck must never throw
            report["r_version"] = None
            report["brier_package_installed"] = False
            report["brier_load_error"] = f"{type(e).__name__}: {e}"
            failures.append("r_probe_failed")
    else:
        report["r_version"] = None
        report["brier_package_installed"] = False

    # --- cache dir writable ------------------------------------------------
    cache_root = os.environ.get(
        "XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    cache_dir = os.path.join(cache_root, "brier-mcp")
    report["cache_dir"] = cache_dir
    try:
        os.makedirs(cache_dir, exist_ok=True)
        test_path = os.path.join(cache_dir, ".selfcheck_write_test")
        with open(test_path, "w") as fh:
            fh.write("ok")
        os.remove(test_path)
        report["cache_dir_writable"] = True
    except Exception as e:  # noqa: BLE001
        report["cache_dir_writable"] = False
        report["cache_dir_error"] = f"{type(e).__name__}: {e}"
        failures.append("cache_dir_not_writable")

    # --- overall status ----------------------------------------------------
    report["status"] = "ok" if not failures else "error"
    if failures:
        report["failures"] = failures
    return report


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        print(json.dumps(_selfcheck(), indent=2))
        sys.exit(0)
    if "--version" in sys.argv:
        print(__version__)
        sys.exit(0)
    mcp.run()
