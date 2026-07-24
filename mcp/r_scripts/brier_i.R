#!/usr/bin/env Rscript
# brier_i.R - fit BRIERi() on user-supplied data, save the result to a
# session cache, return metadata.
#
# Called by mcp/server.py as:
#   Rscript brier_i.R <input.json> <output.json>
#
# input.json: {
#   data_path:        "/path/to/data.rda",                # required
#   X_expr:           "X",                                # required (expr inside loaded env)
#   y_expr:           "y",                                # required
#   beta_external_expr: "beta.external",                  # required (p+1 x M matrix)
#   family:           "gaussian" | "binomial" | "poisson", # required
#   eta_list:         [0, 0.1, 1, 5],                     # optional; default = NULL = let BRIER decide
#   multi_method:     "stacking" | "PCA" | "ind",          # optional; default = "stacking"
#   penalty_factor_expr: "c(rep(0, 3), rep(1, 10000))",   # optional (length p)
#   alpha:            0.5,                                # optional; (0,1], default 1
#   penalty:          "MCP",                              # optional; LASSO|SCAD|MCP
#   gamma:            3,                                  # optional; SCAD/MCP concavity
#   trace:            false,                              # optional
#   cache_dir:        "/tmp/brier-mcp-fits"               # optional; default = tempdir()
# }
#
# output.json: {
#   status: "ok",
#   fit_id: "brier_i_a4f2c1",   # used by brier_i_selection later
#   fit_path: "...rds",          # absolute path of the cached fit
#   family: ...,
#   n_target: int,
#   p: int,
#   M_external: int,
#   eta_list_used: [...],
#   penalty: "LASSO",            # default from BRIER
#   multi_method_used: "stacking",
#   timing: {fit_seconds: float},
#   _notice_*: "..."             # post-call hints (family default, multi.method=ind slowdown)
# }
# or {status: "error", ...}

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


# --------------------------------------------------------------------------
# Cache directory + fit ID generation
# --------------------------------------------------------------------------

.cache_root <- function(override) {
  d <- if (!is.null(override) && nzchar(override)) {
    override
  } else {
    # Persistent across Rscript invocations: under user cache home.
    # XDG_CACHE_HOME if set, else ~/.cache on Linux/macOS, ~/AppData/Local on Windows.
    base <- Sys.getenv("XDG_CACHE_HOME", unset = NA)
    if (is.na(base) || !nzchar(base)) {
      base <- if (.Platform$OS.type == "windows") {
        Sys.getenv("LOCALAPPDATA", unset = file.path(Sys.getenv("HOME"), "AppData", "Local"))
      } else {
        file.path(Sys.getenv("HOME"), ".cache")
      }
    }
    file.path(base, "brier-mcp", "fits")
  }
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.generate_fit_id <- function(prefix = "brier_i") {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0(prefix, "_", ts, "_", suffix)
}


# --------------------------------------------------------------------------
# Post-call hint construction
# --------------------------------------------------------------------------

.build_notices <- function(family_source, family, multi_method, M_external) {
  notices <- list()

  if (identical(family_source, "prepared")) {
    notices$`_notice_family_from_prepared` <- sprintf(paste(
      "Family was not supplied; recovered family='%s' from the prepared object",
      "(prep_auto's detected/declared outcome family). Selection and evaluation",
      "should use the matching %s metrics."), family,
      if (identical(family, "binomial")) "binomial (binomial.dev / binomial.auc)"
      else if (identical(family, "poisson")) "poisson (poisson.dev)" else "gaussian")
  } else if (identical(family_source, "default")) {
    notices$`_notice_family_default` <- paste(
      "Family was not explicitly supplied; BRIERi used the gaussian default.",
      "If the outcome is binary or count, refit with family='binomial' or",
      "'poisson'. Mismatched family is a silent failure: predict(type='response')",
      "returns the linear predictor instead of probabilities/rates."
    )
  }

  if (identical(multi_method, "ind") && M_external >= 5) {
    notices$`_notice_multi_ind_slow` <- sprintf(
      paste(
        "multi.method='ind' tunes an independent eta per external model.",
        "With M=%d externals the hyperparameter grid grows multiplicatively",
        "and this fit is expected to be slow. Consider re-running with",
        "multi.method='stacking' (recommended default) or 'PCA'."
      ),
      M_external
    )
  }

  notices
}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$data_paths) && is.null(inp$data_path)) {
    stop("either data_paths or data_path is required", call. = FALSE)
  }
  if (is.null(inp$X_expr)) stop("X_expr is required", call. = FALSE)
  if (is.null(inp$y_expr)) stop("y_expr is required", call. = FALSE)
  if (is.null(inp$beta_external_expr)) {
    stop("beta_external_expr is required", call. = FALSE)
  }

  family_was_supplied <- !is.null(inp$family) && nzchar(inp$family)

  # The REQUEST. It is resolved below, once beta_external is loaded and M is known:
  # the default depends on M (see resolve_multi_method in _common.R). An explicit value
  # always wins.
  multi_method_requested <- inp$multi_method

  # 1. Load data (v0.11: multi-file via load_data_files).
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)

  # Resolve the family: an explicit arg wins; else recover it from the prepared object
  # (prep_auto records prepared$family), so an auto-detected binomial outcome is fit as
  # logistic even when the model omits family; else fall back to gaussian.
  family <- if (family_was_supplied) inp$family else family_from_prepared(env)
  family_source <- if (family_was_supplied) "supplied" else if (!is.null(family)) "prepared" else "default"
  if (is.null(family)) family <- "gaussian"

  # 2. Resolve expression strings inside that environment.
  X <- safe_eval(inp$X_expr, env)
  y <- safe_eval(inp$y_expr, env)
  beta_external <- safe_eval(inp$beta_external_expr, env)

  if (is.null(X)) stop("X_expr resolved to NULL", call. = FALSE)
  if (is.null(y)) stop("y_expr resolved to NULL", call. = FALSE)
  if (is.null(beta_external)) {
    stop("beta_external_expr resolved to NULL", call. = FALSE)
  }

  # 3. Coerce types where BRIER expects matrices.
  if (!is.matrix(X)) X <- as.matrix(X)
  if (!is.matrix(beta_external)) beta_external <- as.matrix(beta_external)
  if (is.matrix(y) && ncol(y) == 1) y <- as.vector(y)

  # 4. Sanity: beta.external for BRIERi must be (p+1) x M (intercept row first).
  if (nrow(beta_external) != ncol(X) + 1) {
    stop(sprintf(
      paste(
        "Shape mismatch: beta.external has %d rows but BRIERi requires p+1 = %d.",
        "Did you forget to prepend an intercept row of zeros? See llms.txt",
        "'BRIERi() requires an intercept slot as the first row of",
        "beta.external'."
      ),
      nrow(beta_external),
      ncol(X) + 1
    ), call. = FALSE)
  }

  # 4b. THE PREPARED-OBJECT CONTRACT. Everything above this point checks that the fit
  # will RUN. This checks that it will MEAN something: that beta.external's rows sit on
  # the predictors they name, that the external is not numerically zero, that a gaussian
  # y is on the same scale as X. Each of those failures otherwise produces a NUMBER, not
  # an error. See _common.R for what it deliberately CANNOT check (allele orientation).
  # A deliberate no-transfer baseline pins eta.list to 0 (target-only LASSO, or a
  # single-cohort external-only comparator in a brier_full comparison). It carries a
  # zero placeholder external, which at eta=0 is a no-op; the contract's zero-external
  # and rownames clauses must not refuse it (they still fire for a real transfer).
  eta_all_zero <- !is.null(inp$eta_list) && length(unlist(inp$eta_list)) > 0L &&
    all(as.numeric(unlist(inp$eta_list)) == 0)
  stop_on_contract_violations(
    validate_fit_inputs("brier_i", X = X, y = y, beta_external = beta_external,
                        family = family, allow_zero_external = eta_all_zero),
    "brier_i"
  )

  # multi.method: now that beta_external is loaded, M is known and the default can be
  # resolved. ind up to M=2 (it weights each source separately, and wins), stacking from
  # M=3 (ind's grid is n^M and stops being affordable). An explicit request always wins.
  multi_method <- resolve_multi_method(multi_method_requested, ncol(beta_external))

  # 5. Resolve optional penalty.factor.
  penalty_factor <- safe_eval(inp$penalty_factor_expr, env)
  fit_args <- list(
    X = X, y = y,
    family = family,
    beta.external = beta_external,
    multi.method = multi_method,
    trace = isTRUE(inp$trace)
  )
  if (!is.null(inp$eta_list)) {
    fit_args$eta.list <- as.numeric(unlist(inp$eta_list))
  } else {
    # BRIERi requires eta.list (no default). Use the recommended log-spaced
    # grid from llms.txt: 0 plus 20 values log-spaced from 0.1 to 10.
    fit_args$eta.list <- c(0, exp(seq(log(0.1), log(10), length.out = 20)))
  }
  # Optional penalty knobs (alpha / penalty / gamma / penalty.factor); each
  # defaults to BRIER's own default when the caller omits it.
  fit_args <- add_penalty_args(fit_args, inp, penalty_factor)

  # 5b. Zero-external auto-fix for stacking.
  # When beta_external is all-zero (or near-zero), BRIERi's stacking
  # path computes y.external = ginv_link(X %*% 0 + 0, family) = constant
  # vector, then calls stacking_gaussian(constant_vec, y), which fails
  # with a singular-matrix error because regressing y on a constant
  # column is degenerate.
  #
  # Most commonly this happens with the BRIERi-baseline use case
  # (target-only LASSO: eta_list=[0] and a placeholder zero
  # beta_external). But the same crash can happen with any eta_list as
  # long as multi_method='stacking' with all-zero externals.
  # multi_method='ind' avoids the stacking_*() call entirely and gives
  # the same result when externals are all zero (no information to
  # combine). Emit a notice so the auto-fix is visible.
  baseline_auto_ind_applied <- FALSE
  is_zero_external <- !is.null(beta_external) &&
                       all(abs(as.numeric(beta_external)) <
                           .Machine$double.eps * 10)
  if (is_zero_external && identical(multi_method, "stacking")) {
    fit_args$multi.method <- "ind"
    baseline_auto_ind_applied <- TRUE
  }

  # 5c. M=1 auto-substitute.
  # With a single external (M=1), multi_method='stacking' and 'PCA'
  # collapse to 'ind' mathematically but go through extra code paths
  # (stacking_gaussian regression, prcomp on a 1-vector). Both have
  # documented edge cases at M=1. 'ind' is the simplest and equivalent
  # path. Silently substitute and emit a notice.
  m_one_auto_ind_applied <- FALSE
  M_external_pre <- ncol(beta_external)
  if (!baseline_auto_ind_applied && M_external_pre == 1L &&
      identical(multi_method, "stacking")) {
    fit_args$multi.method <- "ind"
    m_one_auto_ind_applied <- TRUE
  } else if (!baseline_auto_ind_applied && M_external_pre == 1L &&
             identical(multi_method, "PCA")) {
    fit_args$multi.method <- "ind"
    m_one_auto_ind_applied <- TRUE
  }

  # 6. Run the fit, capture wall time.
  t0 <- Sys.time()
  fit <- do.call(BRIER::BRIERi, fit_args)
  t1 <- Sys.time()
  fit_seconds <- as.numeric(difftime(t1, t0, units = "secs"))

  # 7. Cache the fit on disk; return fit_id + path.
  cache_dir <- .cache_root(inp$cache_dir)
  fit_id <- .generate_fit_id("brier_i")
  fit_path <- file.path(cache_dir, paste0(fit_id, ".rds"))
  saveRDS(
    list(
      fit = fit,
      meta = list(
        family = family,
        multi_method = multi_method,
        data_path = inp$data_path,
        data_paths = resolved_paths,
        X_expr = inp$X_expr,
        y_expr = inp$y_expr,
        beta_external_expr = inp$beta_external_expr,
        eta_list = inp$eta_list,
        tool = "brier_i",
        # The scale the model was FIT on. Selection and evaluate compare the held-out
        # split against these: coefficients do not apply to a different scale, and the
        # prediction is meaningless rather than wrong-looking.
        x_scale_regime = scale_regime_matrix(X),
        y_scale_regime = scale_regime_vector(y),
        prep_session_ids = extract_prep_session_ids(resolved_paths)
      )
    ),
    file = fit_path
  )

  # 8. Build the result payload + notices.
  # fit$eta.list is BRIER's normalized form: a list of length M (one
  # numeric vector per external model). Convert to a JSON-serializable
  # shape: flat vector if M==1, list of vectors otherwise.
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

  M_external <- ncol(beta_external)

  out <- list(
    status = "ok",
    fit_id = fit_id,
    fit_path = fit_path,
    family = family,
    n_target = nrow(X),
    p = ncol(X),
    M_external = M_external,
    eta_list_used = eta_serialized,
    multi_method_used = multi_method,
    timing = list(fit_seconds = round(fit_seconds, 3))
  )
  out <- add_penalty_echo(out, inp, penalty_factor)

  notices <- .build_notices(family_source, family, multi_method, M_external)
  for (nm in names(notices)) out[[nm]] <- notices[[nm]]

  if (baseline_auto_ind_applied) {
    out$`_notice_baseline_auto_ind` <- paste(
      "Detected all-zero beta_external with multi_method='stacking'",
      "and silently switched to multi_method='ind'. The stacking path",
      "computes y.external = inverse-link(X %*% 0) = constant vector,",
      "then regresses y on this constant column, which is degenerate",
      "(singular matrix). 'ind' avoids that step entirely. With",
      "all-zero externals the result is identical regardless of",
      "multi_method. If you have actual external information to use,",
      "pass a non-zero beta_external."
    )
    out$multi_method_used <- "ind"
  }

  if (m_one_auto_ind_applied) {
    out$`_notice_m_one_auto_ind` <- paste(
      "Detected M=1 (a single external) with multi_method=",
      sprintf("'%s'.", multi_method),
      "With one external, stacking and PCA collapse mathematically to",
      "ind but go through extra code paths with documented edge cases.",
      "Silently switched to multi_method='ind' for robustness; the",
      "result is identical. Pass multi_method='ind' explicitly to",
      "skip this notice."
    )
    out$multi_method_used <- "ind"
  }

  # Always-on followup hint: this fit is unconverted to a selection;
  # remind the AI to offer that next step.
  out$`_followup_offer_selection` <- paste(
    "This is the raw fit across the (eta, lambda) grid. To select the",
    sprintf("optimal hyperparameters, call brier_i_selection with fit_id='%s'", fit_id),
    "and either (a) criteria='BIC' for IC-based selection, or",
    "(b) criteria='gaussian.mspe' / 'binomial.dev' / etc. plus X.val / y.val",
    "for held-out validation. Recommended default for BRIERi is BIC."
  )

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_i.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
