#!/usr/bin/env Rscript
# brier_full_selection.R - validation-set tuning for BRIERfull.
#
# Called by mcp/server.py as:
#   Rscript brier_full_selection.R <input.json> <output.json>
#
# UNLIKE brier_i_selection, BRIERfull.selection ONLY accepts validation-set
# criteria (gaussian.mspe, gaussian.rsq, binomial.dev, binomial.mcfrsq,
# binomial.tjursq, binomial.auc, poisson.dev). It does NOT accept BIC, Cp,
# or GCV. X.val and y.val are required.
#
# input.json: {
#   fit_id:    "brier_full_xxx",       # required
#   criteria:  "gaussian.mspe" | ...,   # required (validation-set only)
#   X_val_expr, y_val_expr,             # required
#   data_path                           # required
# }
#
# output.json: parallel to brier_i_selection: includes selection_id so
# predict/evaluate can use it.

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
      Sys.getenv("LOCALAPPDATA",
                 unset = file.path(Sys.getenv("HOME"), "AppData", "Local"))
    } else {
      file.path(Sys.getenv("HOME"), ".cache")
    }
  }
  d <- file.path(base, "brier-mcp", "fits")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.VALID_BRIERFULL_CRITERIA <- c(
  "gaussian.mspe", "gaussian.rsq",
  "binomial.dev", "binomial.mcfrsq", "binomial.tjursq", "binomial.auc",
  "poisson.dev"
)


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$criteria) || !nzchar(inp$criteria)) {
    stop("criteria is required", call. = FALSE)
  }

  # Reject IC criteria with a helpful error message before invoking BRIER.
  if (!inp$criteria %in% .VALID_BRIERFULL_CRITERIA) {
    if (inp$criteria %in% c("BIC", "Cp", "GCV")) {
      stop(sprintf(
        paste(
          "BRIERfull.selection does not support IC criteria.",
          "criteria='%s' is only valid for brier_i_selection.",
          "BRIERfull.selection requires a validation-set criterion:",
          "%s. Pass X_val_expr, y_val_expr, and data_path."
        ),
        inp$criteria,
        paste(.VALID_BRIERFULL_CRITERIA, collapse = ", ")
      ), call. = FALSE)
    }
    stop(sprintf(
      "Unknown criteria='%s'. Valid options: %s",
      inp$criteria,
      paste(.VALID_BRIERFULL_CRITERIA, collapse = ", ")
    ), call. = FALSE)
  }

  # Resolve fit object via fit_id (no fit_path fallback here; keeping the
  # interface minimal).
  if (is.null(inp$fit_id) || !nzchar(inp$fit_id)) {
    stop("fit_id is required", call. = FALSE)
  }
  fit_path <- file.path(.cache_root(), paste0(inp$fit_id, ".rds"))
  if (!file.exists(fit_path)) {
    stop(sprintf(
      "Fitted object not found at %s. The fit cache may have been cleared. Refit with brier_full.",
      fit_path
    ), call. = FALSE)
  }
  cached <- readRDS(fit_path)
  fit <- cached$fit
  meta <- cached$meta

  # Validation set is always required for BRIERfull.selection.
  has_path <- !is.null(inp$data_paths) || !is.null(inp$data_path)
  if (is.null(inp$X_val_expr) || is.null(inp$y_val_expr) || !has_path) {
    stop(
      paste("BRIERfull.selection requires X_val_expr, y_val_expr, and",
            "data_paths (or data_path)"),
      call. = FALSE
    )
  }
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)
  X_val <- safe_eval(inp$X_val_expr, env)
  y_val <- safe_eval(inp$y_val_expr, env)
  if (!is.matrix(X_val)) X_val <- as.matrix(X_val)
  if (is.matrix(y_val) && ncol(y_val) == 1) y_val <- as.vector(y_val)

  sel <- BRIER::BRIERfull.selection(
    object = fit, criteria = inp$criteria,
    X.val = X_val, y.val = y_val
  )

  # Same field-name shape as BRIERi.selection: eta.min, eta.min.index,
  # lambda.min, eta.lambda (data.frame).
  selected_eta <- if (is.list(sel$eta.min)) {
    lapply(sel$eta.min, function(x) as.numeric(unname(x)))
  } else {
    as.numeric(unname(sel$eta.min))
  }
  selected_lambda <- as.numeric(unname(sel$lambda.min))

  selected_metric <- tryCatch({
    idx <- sel$eta.min.index
    if (!is.null(idx) && !is.null(sel$eta.lambda$measure.min)) {
      as.numeric(sel$eta.lambda$measure.min[idx])
    } else {
      NULL
    }
  }, error = function(e) NULL)

  # Cache the selection object so brier_predict / brier_evaluate can use it.
  selection_id <- paste0(
    "brier_full_sel_",
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

  # Family-criterion mismatch notice.
  if (!is.null(meta$family) && meta$family == "binomial" &&
      grepl("^gaussian\\.", inp$criteria)) {
    out$`_notice_family_criterion_mismatch` <- paste(
      "Fit used family='binomial' but selection criterion is",
      sprintf("'%s' (a gaussian metric).", inp$criteria),
      "Use 'binomial.dev', 'binomial.mcfrsq', 'binomial.tjursq', or",
      "'binomial.auc' instead."
    )
  } else if (!is.null(meta$family) && meta$family == "poisson" &&
             grepl("^gaussian\\.", inp$criteria)) {
    out$`_notice_family_criterion_mismatch` <- paste(
      "Fit used family='poisson' but selection criterion is a gaussian",
      "metric. Use 'poisson.dev' instead."
    )
  }

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_full_selection.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
