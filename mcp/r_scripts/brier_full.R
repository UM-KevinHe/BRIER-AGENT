#!/usr/bin/env Rscript
# brier_full.R - fit BRIERfull() on user-supplied data, save the result
# to the session cache, return metadata.
#
# Called by mcp/server.py as:
#   Rscript brier_full.R <input.json> <output.json>
#
# Distinct from brier_i:
#   * Inputs are STACKED (target + externals concatenated) X and y.
#   * Requires a cohort vector (0 = target, 1+ = external cohorts).
#   * No beta.external (raw external data, not pretrained coefficients).
#
# input.json: {
#   data_path:        "/path/to/data.rda",         # required
#   X_expr:           "X.full",                    # required (stacked X)
#   y_expr:           "y.full",                    # required (stacked y)
#   cohort_expr:      "cohort.full",               # required (0 / 1 / 2 ...)
#   family:           "gaussian" | ...,             # required
#   eta_list:         [0, 0.1, 1, 5],              # optional; default grid
#   penalty_factor_expr: "...",                    # optional (length p)
#   alpha:            0.5,                          # optional; (0,1], default 1
#   penalty:          "MCP",                        # optional; LASSO|SCAD|MCP
#   gamma:            3,                            # optional; SCAD/MCP concavity
#   trace:            false                        # optional
# }
#
# output.json: {
#   status: "ok",
#   fit_id, fit_path,
#   family,
#   n_target, n_external_per_cohort, n_total,
#   p, M_external,
#   eta_list_used, timing,
#   _notice_*, _followup_*
# } or {status: "error", ...}

.script_dir <- (function() {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) > 0) {
    dirname(normalizePath(sub("^--file=", "", file_arg[1])))
  } else {
    getwd()
  }
})()
source(file.path(.script_dir, "_common.R"))

suppressPackageStartupMessages({
  library(BRIER)
})


.cache_root <- function(override) {
  d <- if (!is.null(override) && nzchar(override)) {
    override
  } else {
    base <- Sys.getenv("XDG_CACHE_HOME", unset = NA)
    if (is.na(base) || !nzchar(base)) {
      base <- if (.Platform$OS.type == "windows") {
        Sys.getenv("LOCALAPPDATA",
                   unset = file.path(Sys.getenv("HOME"), "AppData", "Local"))
      } else {
        file.path(Sys.getenv("HOME"), ".cache")
      }
    }
    file.path(base, "brier-mcp", "fits")
  }
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.generate_fit_id <- function(prefix = "brier_full") {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0(prefix, "_", ts, "_", suffix)
}


# Post-call hint construction. Different surface than brier_i:
# no multi.method here (BRIERfull doesn't take one), no intercept-row
# shape check, but the cohort-vector check matters.
.build_notices <- function(family_was_supplied, M_external, n_target,
                            n_external_total, has_eta_zero) {
  notices <- list()

  if (!family_was_supplied) {
    notices$`_notice_family_default` <- paste(
      "Family was not explicitly supplied; BRIERfull used the gaussian default.",
      "If the outcome is binary or count, refit with family='binomial' or",
      "'poisson'. Mismatched family is a silent failure."
    )
  }

  # External sample size dominance: if externals total >> 5x target, prediction
  # may be dominated by the external cohorts. Worth surfacing.
  if (n_external_total >= 5 * n_target && n_target >= 50) {
    notices$`_notice_external_dominance` <- sprintf(
      paste(
        "External cohorts (n_total=%d) outnumber the target cohort",
        "(n_target=%d) by %.1fx. BRIERfull may borrow heavily; if the",
        "external cohorts are heterogeneous with the target, consider",
        "narrowing the eta grid downward or comparing against a",
        "target-only baseline (BRIERi with eta=0, or BRIERfull with",
        "eta.list=0 in the grid)."
      ),
      n_external_total, n_target, n_external_total / n_target
    )
  }

  notices
}


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$data_paths) && is.null(inp$data_path)) {
    stop("either data_paths or data_path is required", call. = FALSE)
  }
  if (is.null(inp$X_expr)) stop("X_expr is required", call. = FALSE)
  if (is.null(inp$y_expr)) stop("y_expr is required", call. = FALSE)
  if (is.null(inp$cohort_expr)) {
    stop("cohort_expr is required (vector with 0=target, 1+=external)",
         call. = FALSE)
  }

  family_was_supplied <- !is.null(inp$family) && nzchar(inp$family)
  family <- if (family_was_supplied) inp$family else "gaussian"

  # 1. Load data (v0.11: multi-file via load_data_files).
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)

  # 2. Resolve expression strings.
  X <- safe_eval(inp$X_expr, env)
  y <- safe_eval(inp$y_expr, env)
  cohort <- safe_eval(inp$cohort_expr, env)

  if (is.null(X)) stop("X_expr resolved to NULL", call. = FALSE)
  if (is.null(y)) stop("y_expr resolved to NULL", call. = FALSE)
  if (is.null(cohort)) stop("cohort_expr resolved to NULL", call. = FALSE)

  # 3. Coerce types.
  if (!is.matrix(X)) X <- as.matrix(X)
  if (is.matrix(y) && ncol(y) == 1) y <- as.vector(y)
  cohort <- as.integer(cohort)

  # 4. Sanity checks specific to BRIERfull.
  if (length(y) != nrow(X)) {
    stop(sprintf(
      "Length mismatch: y has length %d but X has %d rows",
      length(y), nrow(X)
    ), call. = FALSE)
  }

  # THE PREPARED-OBJECT CONTRACT. BRIERfull pools RAW cohorts and takes no external
  # coefficient vector, so the alignment and degeneracy clauses do not apply; the scale
  # clause does. It is NOT "X must be standardized" (BRIERfull legitimately pools on the
  # raw scale, and T1_brierfull does): it is that a STANDARDIZED X with a RAW gaussian y
  # makes gaussian.mspe collapse to ~mean(y^2) for every model, so selection cannot
  # discriminate. That has happened here before, and it is silent.
  stop_on_contract_violations(
    validate_fit_inputs("brier_full", X = X, y = y, family = family),
    "brier_full"
  )
  if (length(cohort) != nrow(X)) {
    stop(sprintf(
      "Length mismatch: cohort has length %d but X has %d rows",
      length(cohort), nrow(X)
    ), call. = FALSE)
  }
  if (!any(cohort == 0)) {
    stop(paste(
      "cohort vector must contain at least one 0 (target). Use cohort=0",
      "for target samples and positive integers (1, 2, ...) for external",
      "cohorts."
    ), call. = FALSE)
  }
  if (!any(cohort > 0)) {
    stop(paste(
      "cohort vector must contain at least one positive integer (external).",
      "If you want a target-only model, use BRIERi(eta=0) instead."
    ), call. = FALSE)
  }
  if (any(cohort < 0)) {
    stop("cohort values must be non-negative integers (0=target, 1+=external)",
         call. = FALSE)
  }

  # 5. Resolve optional penalty.factor.
  penalty_factor <- safe_eval(inp$penalty_factor_expr, env)

  # 6. Build args, including default eta grid.
  fit_args <- list(
    X = X, y = y, cohort = cohort,
    family = family,
    trace = isTRUE(inp$trace)
  )
  if (!is.null(inp$eta_list)) {
    fit_args$eta.list <- as.numeric(unlist(inp$eta_list))
  } else {
    # v0.8.0: tune default eta grid density to M.
    # M=1: full 21-value grid is fast and gives clean tuning.
    # M>=2: BRIERfull's wall time scales with |eta_grid|^M (each
    # external gets its own eta), so a 21-value grid balloons. Use 7
    # log-spaced values to stay under the 4-minute MCP timeout.
    # Users can pass an explicit eta_list to override either default.
    M_pre <- length(unique(cohort[cohort != 0L]))
    if (M_pre <= 1L) {
      fit_args$eta.list <- c(0, exp(seq(log(0.1), log(10),
                                         length.out = 20)))
    } else {
      fit_args$eta.list <- c(0, exp(seq(log(0.1), log(10),
                                         length.out = 6)))
    }
  }
  # Optional penalty knobs (alpha / penalty / gamma / penalty.factor); each
  # defaults to BRIER's own default when the caller omits it.
  fit_args <- add_penalty_args(fit_args, inp, penalty_factor)

  # 7. Fit, capture wall time.
  t0 <- Sys.time()
  fit <- do.call(BRIER::BRIERfull, fit_args)
  t1 <- Sys.time()
  fit_seconds <- as.numeric(difftime(t1, t0, units = "secs"))

  # 8. Cache the fit on disk.
  cache_dir <- .cache_root(inp$cache_dir)
  fit_id <- .generate_fit_id("brier_full")
  fit_path <- file.path(cache_dir, paste0(fit_id, ".rds"))
  saveRDS(
    list(
      fit = fit,
      meta = list(
        family = family,
        data_path = inp$data_path,
        data_paths = resolved_paths,
        X_expr = inp$X_expr,
        y_expr = inp$y_expr,
        cohort_expr = inp$cohort_expr,
        eta_list = inp$eta_list,
        tool = "brier_full",
        x_scale_regime = scale_regime_matrix(X),
        y_scale_regime = scale_regime_vector(y),
        prep_session_ids = extract_prep_session_ids(resolved_paths)
      )
    ),
    file = fit_path
  )

  # 9. Build result payload.
  eta_used <- tryCatch(fit$eta.list, error = function(e) NULL)
  eta_serialized <- if (is.null(eta_used)) {
    NULL
  } else if (is.list(eta_used) && length(eta_used) == 1) {
    as.numeric(eta_used[[1]])
  } else if (is.list(eta_used)) {
    lapply(eta_used, as.numeric)
  } else {
    as.numeric(eta_used)
  }

  # Cohort breakdown stats.
  cohort_tbl <- table(cohort)
  n_target <- as.integer(cohort_tbl["0"])
  n_external_per_cohort <- as.list(cohort_tbl[names(cohort_tbl) != "0"])
  n_external_per_cohort <- lapply(n_external_per_cohort, as.integer)
  M_external <- length(n_external_per_cohort)
  n_external_total <- sum(unlist(n_external_per_cohort))

  has_eta_zero <- any(abs(fit_args$eta.list) < 1e-12)

  out <- list(
    status = "ok",
    fit_id = fit_id,
    fit_path = fit_path,
    family = family,
    n_target = n_target,
    n_external_per_cohort = n_external_per_cohort,
    n_total = nrow(X),
    p = ncol(X),
    M_external = M_external,
    eta_list_used = eta_serialized,
    timing = list(fit_seconds = round(fit_seconds, 3))
  )
  out <- add_penalty_echo(out, inp, penalty_factor)

  notices <- .build_notices(family_was_supplied, M_external, n_target,
                             n_external_total, has_eta_zero)
  for (nm in names(notices)) out[[nm]] <- notices[[nm]]

  if (is.null(inp$eta_list)) {
    n_eta_used <- length(fit_args$eta.list)
    if (n_eta_used >= 15L) {
      out$`_notice_default_eta_grid` <- paste(
        "Used the default 21-value eta grid (M=1 case). With one",
        "external, BRIERfull's wall time is manageable with the dense",
        "default. For M >= 2 the dispatcher uses a coarser 7-value",
        "grid automatically; pass eta_list explicitly to override."
      )
    } else {
      out$`_notice_default_eta_grid` <- paste(
        "Used the coarser 7-value eta grid (M >= 2 case).",
        "BRIERfull's wall time scales with |eta_grid|^M, so for",
        "multiple externals the dispatcher coarsens automatically to",
        "stay under the 4-minute MCP transport timeout. For a denser",
        "grid pass eta_list explicitly, e.g.",
        "eta_list=[0, 0.1, 0.3, 0.7, 1.5, 3, 5, 8, 10]."
      )
    }
  }

  out$`_followup_offer_selection` <- paste(
    "This is the raw fit across the (eta, lambda) grid. To select the",
    sprintf("optimal hyperparameters, call brier_full_selection with fit_id='%s'", fit_id),
    "and either (a) an IC criterion like 'BIC' or 'Cp', or (b) a",
    "family-specific validation metric ('gaussian.mspe', etc.) plus a",
    "held-out X.val / y.val. Note BRIERfull.selection accepts the same",
    "criteria as BRIERi.selection."
  )

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_full.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
