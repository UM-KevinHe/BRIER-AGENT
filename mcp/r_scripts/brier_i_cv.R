#!/usr/bin/env Rscript
# brier_i_cv.R - cross-validation tuning for BRIERi.
#
# Same input shape as brier_i.R, plus optional CV-specific args (nfolds,
# seed). Returns selected hyperparameters from CV; does NOT cache a fit
# object (BRIERi.cv returns CV summary, not a single fit).
#
# input.json: {
#   data_path, X_expr, y_expr, beta_external_expr, family,  # same as brier_i
#   eta_list, multi_method, penalty_factor_expr,            # same as brier_i
#   criteria:   "BIC" | "gaussian.mspe" | ...,             # optional; passed to BRIERi.cv
#   nfolds:     5,                                          # optional; default 5
#   seed:       1                                           # optional; default 1
# }
#
# output.json: {
#   status: "ok",
#   selected_eta, selected_lambda,
#   cv_metric, nfolds_used, seed_used,
#   timing: {cv_seconds: float},
#   _notice_brier_i_cv_leakage: "...",   # ALWAYS emitted - this is the highest-value
#                                         # silent-failure trap to surface
#   _notice_family_default: "..."         # if family was defaulted
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


# --------------------------------------------------------------------------
# Heuristic: try to detect pre-standardized X. The standardization-leak
# trap is one of the highest-value pitfalls in llms.txt; we can't refuse
# the call (some users genuinely want to pass pre-standardized inputs),
# but we can flag it loudly.
# --------------------------------------------------------------------------

.x_looks_standardized <- function(X) {
  if (!is.matrix(X) || ncol(X) < 5 || nrow(X) < 10) return(FALSE)
  # Sample a few columns to keep this cheap on large genotype matrices.
  k <- min(20, ncol(X))
  cols <- sample.int(ncol(X), k)
  means <- apply(X[, cols, drop = FALSE], 2, mean)
  sds <- apply(X[, cols, drop = FALSE], 2, sd)
  mean_close_to_zero <- mean(abs(means) < 0.05)
  sd_close_to_one <- mean(abs(sds - 1) < 0.1)
  # If 80%+ of sampled columns look standardized, flag it.
  mean_close_to_zero > 0.8 && sd_close_to_one > 0.8
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
  family <- if (family_was_supplied) inp$family else "gaussian"

  # The REQUEST; resolved below once beta_external is loaded and M is known.
  multi_method_requested <- inp$multi_method

  nfolds <- if (!is.null(inp$nfolds)) as.integer(inp$nfolds) else 5L
  seed <- if (!is.null(inp$seed)) as.integer(inp$seed) else 1L

  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)
  X <- safe_eval(inp$X_expr, env)
  y <- safe_eval(inp$y_expr, env)
  beta_external <- safe_eval(inp$beta_external_expr, env)

  if (!is.matrix(X)) X <- as.matrix(X)
  if (!is.matrix(beta_external)) beta_external <- as.matrix(beta_external)

  # multi.method: M is known now. ind up to M=2 (one eta per source, so it can lean on a
  # strong external and ignore a weak one -- it wins on val AND test), stacking from M=3
  # (ind's grid is n^M and stops being affordable). An explicit request always wins.
  multi_method <- resolve_multi_method(multi_method_requested, ncol(beta_external))
  if (is.matrix(y) && ncol(y) == 1) y <- as.vector(y)

  if (nrow(beta_external) != ncol(X) + 1) {
    stop(sprintf(
      "Shape mismatch: beta.external has %d rows but BRIERi requires p+1 = %d (intercept row).",
      nrow(beta_external), ncol(X) + 1
    ), call. = FALSE)
  }

  x_looks_std <- .x_looks_standardized(X)

  penalty_factor <- safe_eval(inp$penalty_factor_expr, env)
  cv_args <- list(
    X = X, y = y,
    family = family,
    beta.external = beta_external,
    multi.method = multi_method,
    nfolds = nfolds,
    seed = seed
  )
  # BRIERi.cv requires eta.list (no default). If caller didn't supply one,
  # use the recommended log-spaced grid from llms.txt: 0 plus 20 values
  # log-spaced from 0.1 to 10.
  if (!is.null(inp$eta_list)) {
    cv_args$eta.list <- as.numeric(unlist(inp$eta_list))
  } else {
    cv_args$eta.list <- c(0, exp(seq(log(0.1), log(10), length.out = 20)))
  }
  # Optional penalty knobs (alpha / penalty / gamma / penalty.factor); each
  # defaults to BRIER's own default when the caller omits it.
  cv_args <- add_penalty_args(cv_args, inp, penalty_factor)

  t0 <- Sys.time()
  cv <- do.call(BRIER::BRIERi.cv, cv_args)
  t1 <- Sys.time()
  cv_seconds <- as.numeric(difftime(t1, t0, units = "secs"))

  # BRIERi.cv returns the same eta.min / lambda.min / eta.lambda shape as
  # BRIERi.selection.
  selected_eta <- if (is.list(cv$eta.min)) {
    lapply(cv$eta.min, function(x) as.numeric(unname(x)))
  } else {
    as.numeric(unname(cv$eta.min))
  }
  selected_lambda <- as.numeric(unname(cv$lambda.min))

  selected_metric <- tryCatch({
    idx <- cv$eta.min.index
    if (!is.null(idx) && !is.null(cv$eta.lambda$measure.min)) {
      as.numeric(cv$eta.lambda$measure.min[idx])
    } else {
      NULL
    }
  }, error = function(e) NULL)

  out <- list(
    status = "ok",
    selected_eta = selected_eta,
    selected_lambda = selected_lambda,
    cv_metric = selected_metric,
    nfolds_used = nfolds,
    seed_used = seed,
    timing = list(cv_seconds = round(cv_seconds, 3))
  )

  # ALWAYS emit the BRIERi.cv leakage warning. It's the highest-value
  # silent-failure pitfall from llms.txt and the docstring alone is not
  # enough.
  out$`_notice_brier_i_cv_leakage` <- paste(
    "BRIERi.cv does NOT standardize X or y internally. If your inputs were",
    "pre-standardized (e.g. column-scaled X, residualized y), CV estimates",
    "leak information across folds and become optimistic. Pass raw X / y,",
    "or perform any preprocessing inside each fold yourself."
  )

  if (x_looks_std) {
    out$`_notice_x_appears_standardized` <- paste(
      "Heuristic check: the X matrix passed to brier_i_cv appears to be",
      "column-standardized (column means near 0, sds near 1). If you",
      "applied standardization on the full sample before this call, the CV",
      "estimate is likely optimistic. See _notice_brier_i_cv_leakage."
    )
  }

  if (!family_was_supplied) {
    out$`_notice_family_default` <- paste(
      "Family was not explicitly supplied; BRIERi.cv used the gaussian default.",
      "If the outcome is binary or count, re-run with family='binomial' or",
      "'poisson'."
    )
  }

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_i_cv.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
