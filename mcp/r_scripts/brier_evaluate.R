#!/usr/bin/env Rscript
# brier_evaluate.R - predict and score a cached BRIER fit on a new
# (X, y) pair using evalMetric.
#
# Called by mcp/server.py as:
#   Rscript brier_evaluate.R <input.json> <output.json>
#
# input.json: {
#   selection_id: "brier_i_sel_xxx" or "brier_full_sel_xxx",  # PREFERRED
#       OR
#   fit_id:    "brier_i_xxx" or "brier_full_xxx",  # alternative with explicit eta/lambda
#   eta:       7.85,                    # optional
#   lambda:    0.58,                    # optional
#   data_path: "/path/to/data.rds",
#   newx_expr: "Data_BRIERi$target$testing$X",
#   newy_expr: "Data_BRIERi$target$testing$y",
#   criteria:  "gaussian.mspe" | "gaussian.rsq" | "binomial.dev" |
#              "binomial.tjurrsq" | "binomial.AUC" | "poisson.dev"
# }
#
# output.json: {
#   status: "ok",
#   eta_used, lambda_used,
#   criteria, metric_value,
#   n_evaluated,
#   _notice_*: "..."
# } or {status: "error", ...}
#
# Unlike brier_predict, the full predictions are NOT written to a side
# file by default - the use case here is "give me one number" and the
# raw vector adds little. Use brier_predict if you also want the
# predictions out.

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

.cache_root <- function() {
  base <- Sys.getenv("XDG_CACHE_HOME", unset = NA)
  if (is.na(base) || !nzchar(base)) {
    base <- if (.Platform$OS.type == "windows") {
      Sys.getenv("LOCALAPPDATA", unset = file.path(Sys.getenv("HOME"), "AppData", "Local"))
    } else {
      file.path(Sys.getenv("HOME"), ".cache")
    }
  }
  d <- file.path(base, "brier-mcp", "fits")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$data_paths) && is.null(inp$data_path)) {
    stop("either data_paths or data_path is required", call. = FALSE)
  }
  if (is.null(inp$newx_expr)) stop("newx_expr is required", call. = FALSE)
  if (is.null(inp$newy_expr)) stop("newy_expr is required", call. = FALSE)
  if (is.null(inp$criteria)) stop("criteria is required", call. = FALSE)

  # Resolve cached object: selection preferred, fit as fallback. Also recover the
  # SOURCE FIT's meta, which records the scale the model was fitted on: the test split
  # has to be on that same scale or the reported metric is meaningless.
  brier_obj <- NULL
  fit_meta <- NULL
  .load_fit_meta <- function(fid) {
    if (is.null(fid) || !nzchar(fid)) return(NULL)
    p <- file.path(.cache_root(), paste0(fid, ".rds"))
    if (!file.exists(p)) return(NULL)
    tryCatch(readRDS(p)$meta, error = function(e) NULL)
  }
  if (!is.null(inp$selection_id) && nzchar(inp$selection_id)) {
    sel_path <- file.path(.cache_root(), paste0(inp$selection_id, ".rds"))
    if (!file.exists(sel_path)) {
      stop(sprintf("Selection object not found at %s", sel_path), call. = FALSE)
    }
    cached_sel <- readRDS(sel_path)
    brier_obj <- cached_sel$selection
    fit_meta <- .load_fit_meta(cached_sel$source_fit_id)
  } else if (!is.null(inp$fit_id) && nzchar(inp$fit_id)) {
    fit_path <- file.path(.cache_root(), paste0(inp$fit_id, ".rds"))
    if (!file.exists(fit_path)) {
      stop(sprintf("Fit object not found at %s", fit_path), call. = FALSE)
    }
    cached_fit <- readRDS(fit_path)
    brier_obj <- cached_fit$fit
    fit_meta <- cached_fit$meta
  } else {
    stop("Either selection_id or fit_id is required", call. = FALSE)
  }

  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)
  newx <- safe_eval(inp$newx_expr, env)
  newy <- safe_eval(inp$newy_expr, env)
  if (is.null(newx)) stop("newx_expr resolved to NULL", call. = FALSE)
  if (is.null(newy)) stop("newy_expr resolved to NULL", call. = FALSE)
  if (!is.matrix(newx)) newx <- as.matrix(newx)
  if (is.matrix(newy) && ncol(newy) == 1) newy <- as.vector(newy)

  # THE CONTRACT, on the test split. A model fitted on standardized predictors and
  # evaluated on raw ones reports a NUMBER, and that number goes in the paper.
  stop_on_contract_violations(
    validate_eval_inputs(
      X = newx, y = newy,
      family = if (!is.null(fit_meta$family)) fit_meta$family else "gaussian",
      fit_x_regime = fit_meta$x_scale_regime,
      fit_y_regime = fit_meta$y_scale_regime,
      split = "the evaluation split"),
    "brier_evaluate"
  )

  # Predict via the appropriate type for the criteria. evalMetric expects
  # predictions on the response scale for non-gaussian families.
  predict_type <- if (grepl("^gaussian", inp$criteria)) "response" else "response"
  predict_args <- list(object = brier_obj, X = newx, type = predict_type)
  if (!is.null(inp$eta)) predict_args$eta <- as.numeric(inp$eta)
  if (!is.null(inp$lambda)) predict_args$lambda <- as.numeric(inp$lambda)
  if (!is.null(inp$which_eta)) {
    predict_args$which.eta <- as.integer(inp$which_eta)
  }
  if (!is.null(inp$which_lambda)) {
    predict_args$which.lambda <- as.integer(inp$which_lambda)
  }

  preds <- as.numeric(do.call(predict, predict_args))

  metric_value <- as.numeric(BRIER::evalMetric(
    pred = preds, y = newy, criteria = inp$criteria
  ))

  eta_used <- if (!is.null(inp$eta)) {
    as.numeric(inp$eta)
  } else if (!is.null(inp$which_eta) && !is.null(brier_obj$eta.grid)) {
    idx <- as.integer(inp$which_eta)
    as.numeric(brier_obj$eta.grid[idx, 1])
  } else if (!is.null(brier_obj$eta.min)) {
    as.numeric(unname(brier_obj$eta.min))
  } else {
    NULL
  }
  lambda_used <- if (!is.null(inp$lambda)) {
    as.numeric(inp$lambda)
  } else if (!is.null(brier_obj$lambda.min)) {
    as.numeric(unname(brier_obj$lambda.min))
  } else {
    NULL
  }

  out <- list(
    status = "ok",
    eta_used = eta_used,
    lambda_used = lambda_used,
    criteria = inp$criteria,
    metric_value = metric_value,
    n_evaluated = length(preds)
  )

  # Family-criterion mismatch notice (parallel to the one in
  # brier_i_selection.R but read from brier_obj$family).
  obj_family <- brier_obj$family
  if (!is.null(obj_family)) {
    if (obj_family == "binomial" && grepl("^gaussian\\.", inp$criteria)) {
      out$`_notice_family_criterion_mismatch` <- paste(
        "Fit used family='binomial' but evaluation criterion is",
        sprintf("'%s' (a gaussian metric).", inp$criteria),
        "Use 'binomial.dev', 'binomial.tjurrsq', or 'binomial.AUC' instead."
      )
    } else if (obj_family == "poisson" && grepl("^gaussian\\.", inp$criteria)) {
      out$`_notice_family_criterion_mismatch` <- paste(
        "Fit used family='poisson' but evaluation criterion is a gaussian",
        "metric. Use 'poisson.dev' instead."
      )
    }
  }

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_evaluate.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
