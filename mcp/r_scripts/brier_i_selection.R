#!/usr/bin/env Rscript
# brier_i_selection.R - select optimal (eta, lambda) from a cached BRIERi fit.
#
# Called by mcp/server.py as:
#   Rscript brier_i_selection.R <input.json> <output.json>
#
# input.json: {
#   fit_id:    "brier_i_a4f2c1",   # required; from a prior brier_i call
#   fit_path:  "/path/to/fit.rds", # alternative to fit_id; full path
#   criteria:  "BIC" | "Cp" | "GCV"     # IC-based, no validation set needed
#              | "gaussian.mspe" | "gaussian.rsq"
#              | "binomial.dev" | "binomial.tjurrsq" | "binomial.AUC"
#              | "poisson.dev",
#   X_val_expr: "X.val",           # required for validation-set criteria
#   y_val_expr: "y.val",           # required for validation-set criteria
#   data_path:  "/path/to/data.rda" # required if X_val_expr / y_val_expr given
# }
#
# output.json: {
#   status: "ok",
#   fit_id: "...",
#   criteria: "BIC",
#   selected_eta: float | [float, ...],   # vector for multi.method="ind"
#   selected_lambda: float,
#   selected_metric: float,
#   _notice_*: "..."                       # post-call hints
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

# Mirror the fit cache root from brier_i.R. Must be a stable location
# (not tempdir()) because Rscript subprocesses each have their own
# tempdir which is destroyed on exit.
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


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$criteria) || !nzchar(inp$criteria)) {
    stop("criteria is required", call. = FALSE)
  }

  # Resolve fit object: prefer explicit fit_path, fall back to fit_id lookup.
  if (!is.null(inp$fit_path) && nzchar(inp$fit_path)) {
    fit_path <- inp$fit_path
  } else if (!is.null(inp$fit_id) && nzchar(inp$fit_id)) {
    fit_path <- file.path(.cache_root(), paste0(inp$fit_id, ".rds"))
  } else {
    stop("Either fit_id or fit_path is required", call. = FALSE)
  }

  if (!file.exists(fit_path)) {
    stop(sprintf(
      "Fitted object not found at %s. The fit cache may have been cleared. Refit with brier_i.",
      fit_path
    ), call. = FALSE)
  }

  cached <- readRDS(fit_path)
  fit <- cached$fit
  meta <- cached$meta

  # Detect validation-set criteria vs IC criteria.
  is_validation_criterion <- grepl("\\.", inp$criteria)

  selection_args <- list(
    object = fit,
    criteria = inp$criteria
  )

  if (is_validation_criterion) {
    has_path <- !is.null(inp$data_paths) || !is.null(inp$data_path)
    if (is.null(inp$X_val_expr) || is.null(inp$y_val_expr) || !has_path) {
      stop(sprintf(paste(
        "criteria='%s' requires X_val_expr, y_val_expr, and",
        "data_paths (or data_path)"
      ), inp$criteria), call. = FALSE)
    }
    resolved_paths <- resolve_data_paths_input(inp)
    env <- load_data_files(resolved_paths)
    X_val <- safe_eval(inp$X_val_expr, env)
    y_val <- safe_eval(inp$y_val_expr, env)
    if (!is.matrix(X_val)) X_val <- as.matrix(X_val)
    if (is.matrix(y_val) && ncol(y_val) == 1) y_val <- as.vector(y_val)
    selection_args$X.val <- X_val
    selection_args$y.val <- y_val

    # The held-out split must be on the scale the model was FIT on. Coefficients do not
    # apply to a different scale: the prediction is meaningless and nothing errors.
    stop_on_contract_violations(
      validate_eval_inputs(X = X_val, y = y_val, family = meta$family,
                           fit_x_regime = meta$x_scale_regime,
                           fit_y_regime = meta$y_scale_regime,
                           split = "the validation split"),
      "brier_i_selection"
    )
  }

  sel <- do.call(BRIER::BRIERi.selection, selection_args)

  # BRIERi.selection adds these fields to the input fit object:
  # eta.min            : the optimum eta value (named scalar for single
  #                      external; vector for multi.method="ind")
  # eta.min.index      : its row in the eta.lambda grid
  # lambda.min         : the optimum lambda value at eta.min
  # lambda.min.index   : its column index in the lambda grid
  # eta.lambda         : data.frame with columns eta.index, eta_1[,_2,..],
  #                      criteria, measure.min, lambda.min.index, lambda.min
  #                      (one row per eta in the grid; measure.min is the
  #                      criterion value at the best lambda for that eta)

  # Strip the "named" attribute so JSON output is clean.
  selected_eta <- if (is.list(sel$eta.min)) {
    lapply(sel$eta.min, function(x) as.numeric(unname(x)))
  } else {
    as.numeric(unname(sel$eta.min))
  }
  selected_lambda <- as.numeric(unname(sel$lambda.min))

  # The selection metric is in sel$eta.lambda$measure.min at row eta.min.index.
  selected_metric <- tryCatch({
    idx <- sel$eta.min.index
    if (!is.null(idx) && !is.null(sel$eta.lambda$measure.min)) {
      as.numeric(sel$eta.lambda$measure.min[idx])
    } else {
      NULL
    }
  }, error = function(e) NULL)

  # Cache the BRIER.selection object so brier_predict / brier_evaluate
  # can use the chosen (eta, lambda) without re-running selection.
  selection_id <- paste0(
    "brier_i_sel_",
    format(Sys.time(), "%Y%m%d_%H%M%S"),
    "_",
    paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  )
  selection_path <- file.path(.cache_root(), paste0(selection_id, ".rds"))
  saveRDS(
    list(
      selection = sel,
      source_fit_id = inp$fit_id,
      criteria = inp$criteria
    ),
    file = selection_path
  )

  # All eta values present in the grid (for boundary-optimum diagnostic
  # in the Python layer). Collect every eta_* column from eta.lambda.
  eta_grid_values <- eta_grid_values_of(sel)

  out <- list(
    status = "ok",
    fit_id = inp$fit_id,
    selection_id = selection_id,
    selection_path = selection_path,
    criteria = inp$criteria,
    selected_eta = selected_eta,
    selected_lambda = selected_lambda,
    selected_metric = selected_metric,
    eta_grid_values = eta_grid_values
  )

  # If the family was binomial and the user passed gaussian.mspe-style
  # criterion (a common gotcha), surface that.
  if (!is.null(meta$family) && meta$family == "binomial" &&
      grepl("^gaussian\\.", inp$criteria)) {
    out$`_notice_family_criterion_mismatch` <- paste(
      "Fit used family='binomial' but selection criterion is",
      sprintf("'%s' (a gaussian metric).", inp$criteria),
      "Use 'binomial.dev', 'binomial.tjurrsq', or 'binomial.AUC' instead."
    )
  }

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_i_selection.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
