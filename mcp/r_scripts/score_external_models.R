#!/usr/bin/env Rscript
# score_external_models.R - score a RAW external coefficient vector (a pretrained
# PRS) directly on a new (X, y) pair, without fitting a BRIER model.
#
# This is the EUR-only comparator: apply an external beta vector to a target
# cohort's genotypes as a linear predictor and score it with the SAME
# BRIER::evalMetric used by brier_evaluate, so the number is directly
# comparable to a fitted BRIERi / baseline metric. It does NOT fit anything;
# it is the deterministic "score the external PRS as-is" step.
#
# Called by mcp/server.py as:
#   Rscript score_external_models.R <input.json> <output.json>
#
# input.json: {
#   data_path:  "/path/to/prepared.rds",
#   newx_expr:  "prep_auto_brier_i$X_test",   # genotypes on the SAME scale
#                                             # (e.g. standardized) as the beta
#   newy_expr:  "prep_auto_brier_i$y_test",
#   beta_expr:  "prep_auto_brier_i$beta_external",  # coef vector; may carry a
#                                             # leading intercept row (length
#                                             # ncol(X)+1) or not (length ncol(X))
#   criteria:   "gaussian.mspe" | "gaussian.rsq" | "binomial.dev" |
#               "binomial.tjurrsq" | "binomial.AUC" | "poisson.dev"
#   family:     "gaussian" | "binomial" | "poisson"   # link for the predictor
#   has_intercept: true | false                        # optional override
# }
#
# output.json: {
#   status: "ok", criteria, metric_value, n_evaluated, used_intercept, p
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

args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$data_paths) && is.null(inp$data_path)) {
    stop("either data_paths or data_path is required", call. = FALSE)
  }
  if (is.null(inp$newx_expr)) stop("newx_expr is required", call. = FALSE)
  if (is.null(inp$newy_expr)) stop("newy_expr is required", call. = FALSE)
  if (is.null(inp$beta_expr)) stop("beta_expr is required", call. = FALSE)
  if (is.null(inp$criteria)) stop("criteria is required", call. = FALSE)
  family <- if (!is.null(inp$family)) inp$family else "gaussian"
  if (!family %in% c("gaussian", "binomial", "poisson")) {
    stop("family must be 'gaussian', 'binomial', or 'poisson'", call. = FALSE)
  }

  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)
  newx <- safe_eval(inp$newx_expr, env)
  newy <- safe_eval(inp$newy_expr, env)
  beta <- safe_eval(inp$beta_expr, env)
  if (is.null(newx)) stop("newx_expr resolved to NULL", call. = FALSE)
  if (is.null(newy)) stop("newy_expr resolved to NULL", call. = FALSE)
  if (is.null(beta)) stop("beta_expr resolved to NULL", call. = FALSE)
  if (!is.matrix(newx)) newx <- as.matrix(newx)
  if (is.matrix(newy) && ncol(newy) == 1) newy <- as.vector(newy)
  beta <- as.numeric(beta)

  p <- ncol(newx)
  # Decide whether beta carries a leading intercept row (the BRIERi
  # convention prepends one, so the vector is length ncol(X)+1). Auto-detect
  # from the length, with an optional explicit override.
  intercept_note <- NULL
  if (!is.null(inp$has_intercept)) {
    used_intercept <- isTRUE(inp$has_intercept)
    # Reconcile an explicit flag that CONTRADICTS an unambiguous length. A BRIERi
    # external is p+1 (a prepended intercept row); a raw coef vector is p. A small
    # model routinely guesses this flag wrong, so when the length settles it, the
    # length wins and we note the override (rather than erroring on a guess).
    if (used_intercept && length(beta) == p) {
      used_intercept <- FALSE
      intercept_note <- "has_intercept=TRUE overridden to FALSE (beta length == ncol(X))"
    } else if (!used_intercept && length(beta) == p + 1) {
      used_intercept <- TRUE
      intercept_note <- "has_intercept=FALSE overridden to TRUE (beta length == ncol(X)+1, the BRIERi intercept row)"
    }
  } else if (length(beta) == p + 1) {
    used_intercept <- TRUE
  } else if (length(beta) == p) {
    used_intercept <- FALSE
  } else {
    stop(
      sprintf(
        "beta length %d does not match ncol(X)=%d (expected %d or %d)",
        length(beta), p, p, p + 1
      ),
      call. = FALSE
    )
  }

  if (used_intercept) {
    if (length(beta) != p + 1) {
      stop(
        sprintf("has_intercept=TRUE but beta length %d != ncol(X)+1 = %d",
                length(beta), p + 1),
        call. = FALSE
      )
    }
    lin_pred <- as.numeric(beta[1] + newx %*% beta[-1])
  } else {
    if (length(beta) != p) {
      stop(
        sprintf("has_intercept=FALSE but beta length %d != ncol(X) = %d",
                length(beta), p),
        call. = FALSE
      )
    }
    lin_pred <- as.numeric(newx %*% beta)
  }

  # Map the linear predictor to the response scale for the family, matching
  # what evalMetric expects (brier_evaluate predicts on the response scale).
  preds <- switch(
    family,
    gaussian = lin_pred,
    binomial = stats::plogis(lin_pred),
    poisson  = exp(lin_pred)
  )

  metric_value <- as.numeric(BRIER::evalMetric(
    pred = preds, y = newy, criteria = inp$criteria
  ))

  out <- list(
    status = "ok",
    criteria = inp$criteria,
    metric_value = metric_value,
    n_evaluated = length(preds),
    used_intercept = used_intercept,
    p = p
  )
  if (!is.null(intercept_note)) out$`_notice_intercept` <- intercept_note
  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "score_external_models.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
