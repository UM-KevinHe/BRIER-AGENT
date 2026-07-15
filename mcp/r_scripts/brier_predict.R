#!/usr/bin/env Rscript
# brier_predict.R - generate predictions from a cached BRIER fit or
# selection object.
#
# Called by mcp/server.py as:
#   Rscript brier_predict.R <input.json> <output.json>
#
# input.json: {
#   selection_id: "brier_i_sel_xxx" or "brier_full_sel_xxx",
#                                       # PREFERRED: uses selected eta/lambda
#       OR
#   fit_id:    "brier_i_xxx" or "brier_full_xxx",
#                                       # alternative; requires explicit eta/lambda
#   eta:       7.85,                    # optional override
#   lambda:    0.58,                    # optional override
#   data_path: "/path/to/data.rds",    # required (where newx_expr lives)
#   newx_expr: "Data_BRIERi$target$testing$X",  # required
#   type:      "response" | "link" | "coefficients" | "vars" | "nvars"
#                                       # optional; default "response"
# }
#
# output.json: {
#   status:    "ok",
#   eta_used, lambda_used,
#   type:      "response",
#   n_predicted,
#   summary:   {min, q25, median, mean, q75, max},  # summary stats only
#   predictions_path: "/tmp/.../predictions.csv",   # CSV side file with full vector
#   _notice_*: "..."
# } or {status: "error", ...}
#
# WHY a CSV side file: a vector of 1000+ predictions would balloon the
# MCP response payload (and Anthropic's logs). We write the full vector
# to a temp CSV and surface only summary stats in the JSON return. The
# AI / user can read the CSV with R or pandas if they need the raw values.

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

# Same cache root as brier_i.R / brier_i_selection.R.
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

  # Resolve which cached object to load. Prefer selection_id (carries
  # the chosen eta/lambda); fall back to fit_id (raw fit, needs explicit
  # eta/lambda).
  brier_obj <- NULL
  if (!is.null(inp$selection_id) && nzchar(inp$selection_id)) {
    sel_path <- file.path(.cache_root(), paste0(inp$selection_id, ".rds"))
    if (!file.exists(sel_path)) {
      stop(sprintf("Selection object not found at %s", sel_path), call. = FALSE)
    }
    cached <- readRDS(sel_path)
    brier_obj <- cached$selection
  } else if (!is.null(inp$fit_id) && nzchar(inp$fit_id)) {
    fit_path <- file.path(.cache_root(), paste0(inp$fit_id, ".rds"))
    if (!file.exists(fit_path)) {
      stop(sprintf("Fit object not found at %s", fit_path), call. = FALSE)
    }
    cached <- readRDS(fit_path)
    brier_obj <- cached$fit
  } else {
    stop("Either selection_id or fit_id is required", call. = FALSE)
  }

  type <- if (!is.null(inp$type) && nzchar(inp$type)) inp$type else "response"

  # Load the new X.
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)
  newx <- safe_eval(inp$newx_expr, env)
  if (is.null(newx)) stop("newx_expr resolved to NULL", call. = FALSE)
  if (!is.matrix(newx)) newx <- as.matrix(newx)

  # Build predict args. Two paths to specify the operating point:
  #   1. Explicit numeric eta / lambda (must match grid values exactly).
  #   2. Integer which_eta / which_lambda indices into the grid.
  # If neither is supplied, predict.BRIER uses the cached selection's
  # eta.min / lambda.min if the object is a BRIER.selection, otherwise
  # the first grid point.
  predict_args <- list(object = brier_obj, X = newx, type = type)
  if (!is.null(inp$eta)) predict_args$eta <- as.numeric(inp$eta)
  if (!is.null(inp$lambda)) predict_args$lambda <- as.numeric(inp$lambda)
  if (!is.null(inp$which_eta)) {
    predict_args$which.eta <- as.integer(inp$which_eta)
  }
  if (!is.null(inp$which_lambda)) {
    predict_args$which.lambda <- as.integer(inp$which_lambda)
  }

  preds <- do.call(predict, predict_args)

  # preds is typically a numeric vector. For type="coefficients" it could
  # be a matrix; for "vars" / "nvars" it's an index/count.
  preds_vec <- as.numeric(preds)

  # Optional un-standardization for BRIERs predictions.
  #
  # BRIERs predictions are inherently on the STANDARDIZED scale: GWAS
  # sumstats provide standardized marginal correlations, the LD matrix
  # is a correlation matrix, fitted coefficients are standardized, and
  # predictions are produced on the standardized y scale (mean 0, sd 1
  # nominally). To recover raw-scale predictions:
  #
  #     raw_pred = std_pred * sd(y) + mean(y)
  #
  # The caller must supply mean(y) and sd(y) as y_center / y_scale.
  # The MCP does NOT auto-source these because the right source
  # depends on the user's situation:
  #   - If y_train is available -> mean(y_train), sd(y_train). Unbiased.
  #   - If only y_test available -> mean(y_test), sd(y_test). Slightly
  #     biased (sampling).
  #   - If no y at all available -> external scalars from prior
  #     knowledge of the trait. Variable bias.
  # The v0.8.0 auto-apply from stashed values was removed in v0.8.1
  # because it hid this decision from the user.
  unstandardize_applied <- FALSE
  unstandardize_warning <- NULL
  if (!is.null(inp$y_center) && !is.null(inp$y_scale)) {
    y_center_val <- as.numeric(inp$y_center)
    y_scale_val <- as.numeric(inp$y_scale)
    if (is.finite(y_center_val) && is.finite(y_scale_val) &&
        y_scale_val > 0) {
      preds_vec <- preds_vec * y_scale_val + y_center_val
      unstandardize_applied <- TRUE
    } else {
      unstandardize_warning <- paste(
        "y_center and y_scale supplied but invalid (need finite numerics,",
        "scale > 0); skipped un-standardization. Predictions are on the",
        "model's native scale."
      )
    }
  }

  # Write the full prediction vector to a CSV next to the cache so the
  # user / AI can pick it up later.
  # Choose where to write the predictions CSV.
  # Priority: user-configured output_directory from MCP config file,
  # else default to the cache root's predictions/ subdirectory.
  preds_dir <- tryCatch({
    # Precedence: explicit output_dir (per-call) -> configured
    # output_directory -> cache root predictions/.
    if (!is.null(inp$output_dir) && nzchar(inp$output_dir)) {
      inp$output_dir
    } else {
      cfg_path <- file.path(
        Sys.getenv("XDG_CONFIG_HOME",
                    unset = file.path(Sys.getenv("HOME"), ".config")),
        "brier-mcp", "config.json"
      )
      if (file.exists(cfg_path)) {
        cfg <- jsonlite::fromJSON(cfg_path)
        if (!is.null(cfg$output_directory) && nzchar(cfg$output_directory) &&
            dir.exists(cfg$output_directory)) {
          file.path(cfg$output_directory, "brier-mcp-predictions")
        } else {
          file.path(.cache_root(), "predictions")
        }
      } else {
        file.path(.cache_root(), "predictions")
      }
    }
  }, error = function(e) file.path(.cache_root(), "predictions"))
  dir.create(preds_dir, recursive = TRUE, showWarnings = FALSE)
  pred_id <- paste0(
    "pred_",
    format(Sys.time(), "%Y%m%d_%H%M%S"),
    "_",
    paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  )
  pred_csv <- file.path(preds_dir, paste0(pred_id, ".csv"))
  write.csv(
    data.frame(idx = seq_along(preds_vec), prediction = preds_vec),
    file = pred_csv, row.names = FALSE
  )

  # Summary stats are what we ship through MCP; the full vector stays on disk.
  qs <- quantile(preds_vec, probs = c(0.25, 0.5, 0.75), na.rm = TRUE)
  summary_stats <- list(
    min = as.numeric(min(preds_vec, na.rm = TRUE)),
    q25 = as.numeric(qs[1]),
    median = as.numeric(qs[2]),
    mean = as.numeric(mean(preds_vec, na.rm = TRUE)),
    q75 = as.numeric(qs[3]),
    max = as.numeric(max(preds_vec, na.rm = TRUE))
  )

  # Surface which (eta, lambda) was actually used. Priority:
  #   1. Explicit numeric inp$eta / inp$lambda
  #   2. Grid lookup via which_eta / which_lambda
  #   3. The cached object's eta.min / lambda.min (if it's a selection)
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
    type = type,
    n_predicted = length(preds_vec),
    summary = summary_stats,
    predictions_path = pred_csv
  )

  if (unstandardize_applied) {
    out$`_notice_unstandardize_applied` <- paste(
      "Predictions were un-standardized using user-supplied y_center =",
      sprintf("%g and y_scale = %g.",
              as.numeric(inp$y_center), as.numeric(inp$y_scale)),
      "Summary stats and predictions_path file reflect the raw outcome",
      "scale. The accuracy of this rescaling depends on how y_center",
      "and y_scale were sourced (training y is unbiased; test y is",
      "slightly biased; external scalars are as good as the source)."
    )
  }
  if (!is.null(unstandardize_warning)) {
    out$`_notice_unstandardize_warning` <- unstandardize_warning
  }

  # If the caller passed fit_id without explicit eta/lambda and the fit
  # has no built-in selection, predict.BRIER will have used arbitrary
  # defaults. Surface that as a notice.
  if (!is.null(inp$fit_id) && is.null(inp$eta) && is.null(inp$lambda) &&
      is.null(brier_obj$eta.min)) {
    out$`_notice_default_eta_lambda` <- paste(
      "fit_id was used without explicit eta or lambda, and the cached fit",
      "has no built-in selection. predict() used the first grid point;",
      "the result may not reflect the tuned operating point. Run",
      "brier_i_selection first and then call brier_predict with the",
      "selection_id instead."
    )
  }

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_predict.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
