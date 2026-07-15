#!/usr/bin/env Rscript
# brier_plot_eta.R - wrap BRIER::plot.eta to produce a PNG + CSV of the
# eta-vs-criterion curve, with automatic heatmap rendering when M >= 2.
#
# BRIER's plot.eta returns:
#   - $plot: a ggplot object (only for M=1; NULL for M>=2)
#   - $summary.df: tabular data with columns eta_1, ..., eta_M, criteria,
#                  metric, metric.lo, metric.hi
#   - $bootstrap.mat: matrix of bootstrap replicates (if bootstrap=TRUE)
#
# This dispatcher handles three cases:
#   M=1 -> render the returned ggplot directly
#   M=2 -> auto-build a geom_tile heatmap from summary.df
#   M>=3 -> render a faceted version showing marginal heatmaps, plus
#           warn that 3D+ is not directly visualizable
#
# Inputs (JSON):
#   selection_id:    required; cached BRIERi/BRIERfull/BRIERs selection
#   data_path:       required; path to test/validation data file
#   newx_expr:       required; R expression for held-out X
#   newy_expr:       required; R expression for held-out y
#   criteria:        validation-set criterion (e.g. "gaussian.mspe")
#   covar_expr:      optional; R expression for covariate data.frame
#   adjust_covar:    optional; covariate adjustment formula or NULL
#   standardize:     bool; whether plot.eta should standardize inputs
#   bootstrap:       bool
#   bootstrap_n:     integer; bootstrap replicates (default 100)
#   seed:            integer; for reproducibility
#   width:           PNG width in pixels (default 800)
#   height:          PNG height in pixels (default 600)
#   dpi:             PNG dpi (default 100)
#
# Outputs:
#   {status, plot_id, plot_png_path, plot_csv_path, M, summary,
#    _notice_*}

.script_dir <- (function() {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) > 0) {
    dirname(normalizePath(sub("^--file=", "", file_arg[1])))
  } else { getwd() }
})()
source(file.path(.script_dir, "_common.R"))


.cache_root_fits <- function() {
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


.plots_output_dir <- function(explicit_dir = NULL) {
  # Precedence: explicit_dir (per-call override) -> configured
  # output_directory -> cache root's plots/.
  if (!is.null(explicit_dir) && nzchar(explicit_dir)) {
    dir.create(explicit_dir, recursive = TRUE, showWarnings = FALSE)
    return(explicit_dir)
  }
  cfg_path <- file.path(
    Sys.getenv("XDG_CONFIG_HOME",
               unset = file.path(Sys.getenv("HOME"), ".config")),
    "brier-mcp", "config.json"
  )
  cfg_dir <- NULL
  if (file.exists(cfg_path)) {
    cfg <- tryCatch(jsonlite::fromJSON(cfg_path), error = function(e) NULL)
    if (!is.null(cfg) && !is.null(cfg$output_directory) &&
        nzchar(cfg$output_directory) && dir.exists(cfg$output_directory)) {
      cfg_dir <- cfg$output_directory
    }
  }
  if (is.null(cfg_dir)) {
    base <- Sys.getenv("XDG_CACHE_HOME",
                       unset = file.path(Sys.getenv("HOME"), ".cache"))
    cfg_dir <- file.path(base, "brier-mcp", "plots")
  }
  dir.create(cfg_dir, recursive = TRUE, showWarnings = FALSE)
  cfg_dir
}

.generate_plot_id <- function(prefix = "plot_eta") {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0(prefix, "_", ts, "_", suffix)
}


# Build a geom_tile heatmap from summary.df for the M=2 case
.build_m2_heatmap <- function(summary_df, criteria_name) {
  if (!requireNamespace("ggplot2", quietly = TRUE)) {
    stop("ggplot2 is required for M>=2 heatmap rendering")
  }
  # summary.df has eta_1, eta_2, criteria, metric, ...
  # Build the heatmap
  ggplot2::ggplot(summary_df,
                  ggplot2::aes(x = factor(eta_1),
                               y = factor(eta_2),
                               fill = metric)) +
    ggplot2::geom_tile(color = "white") +
    ggplot2::geom_text(ggplot2::aes(label = sprintf("%.3g", metric)),
                       size = 3, color = "black") +
    ggplot2::scale_fill_gradient(low = "#2c7bb6", high = "#fdae61",
                                 name = criteria_name) +
    ggplot2::labs(
      title = sprintf("Multi-external diagnostic: %s over (eta_1, eta_2)",
                       criteria_name),
      subtitle = "Each cell shows validation criterion; lambda already optimized",
      x = "eta_1 (external 1 weight)",
      y = "eta_2 (external 2 weight)"
    ) +
    ggplot2::theme_minimal() +
    ggplot2::theme(legend.position = "right")
}


# Build a marginal-heatmap faceted plot for M>=3
.build_m3_plus_facet <- function(summary_df, criteria_name) {
  # For M>=3, just show pairs of etas faceting by remaining etas at their
  # min/max levels. Simpler: just plot a parallel-coordinates view.
  if (!requireNamespace("ggplot2", quietly = TRUE)) {
    stop("ggplot2 is required")
  }
  # Identify eta_* columns
  eta_cols <- grep("^eta_\\d+$", colnames(summary_df), value = TRUE)
  M <- length(eta_cols)
  # Find rows where all but one eta are at zero. Show curves of metric
  # vs that single nonzero eta. This is a sliced view.
  zero_mask <- summary_df[, eta_cols] == 0
  marginal_rows <- list()
  for (m in seq_along(eta_cols)) {
    # Rows where eta_m varies but other etas are 0
    others <- setdiff(seq_along(eta_cols), m)
    keep <- apply(zero_mask[, others, drop = FALSE], 1, all)
    sub <- summary_df[keep, , drop = FALSE]
    if (nrow(sub) > 0) {
      sub$external <- paste0("eta_", m)
      sub$eta_value <- sub[[eta_cols[m]]]
      marginal_rows[[m]] <- sub[, c("external", "eta_value", "metric")]
    }
  }
  if (length(marginal_rows) == 0) {
    return(NULL)
  }
  marg <- do.call(rbind, marginal_rows)
  ggplot2::ggplot(marg, ggplot2::aes(x = eta_value, y = metric,
                                      color = external, group = external)) +
    ggplot2::geom_line(linewidth = 1) +
    ggplot2::geom_point(size = 2) +
    ggplot2::labs(
      title = sprintf("Marginal slices of %s by each external", criteria_name),
      subtitle = sprintf("M=%d externals; other etas held at 0 for each curve", M),
      x = "eta value (this external)",
      y = criteria_name,
      color = "External"
    ) +
    ggplot2::theme_minimal()
}


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  for (req in c("selection_id", "newx_expr", "newy_expr", "criteria")) {
    if (is.null(inp[[req]])) {
      stop(sprintf("%s is required", req), call. = FALSE)
    }
  }
  # Data location comes via data_paths (preferred) or data_path (legacy);
  # resolve_data_paths_input handles either. Require at least one.
  if (is.null(inp$data_paths) && is.null(inp$data_path)) {
    stop("either data_paths or data_path is required", call. = FALSE)
  }

  suppressPackageStartupMessages(library(BRIER))

  # Load the cached selection
  cache_dir <- .cache_root_fits()
  sel_path <- file.path(cache_dir, paste0(inp$selection_id, ".rds"))
  if (!file.exists(sel_path)) {
    stop(sprintf("selection_id '%s' not found in cache",
                 inp$selection_id), call. = FALSE)
  }
  cached <- readRDS(sel_path)
  sel_obj <- cached$selection
  if (is.null(sel_obj)) {
    stop("cached object missing $selection", call. = FALSE)
  }

  # Load held-out data (v0.11: multi-file via load_data_files)
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)

  X_new <- safe_eval(inp$newx_expr, env)
  y_new <- safe_eval(inp$newy_expr, env)
  if (is.null(X_new) || is.null(y_new)) {
    stop("newx_expr or newy_expr resolved to NULL", call. = FALSE)
  }

  # Determine M from the selection object
  # Look for eta_1, eta_2, ... in $eta.lambda$best.eta or similar; safest
  # is to peek at the selection metric structure
  M <- NULL
  if (!is.null(sel_obj$best.eta)) {
    M <- length(sel_obj$best.eta)
  }
  if (is.null(M) || M < 1L) {
    M <- 1L  # default assumption
  }

  # covar.data: plot.eta requires y to be passed via covar.data
  covar_data <- data.frame(y = as.numeric(y_new))
  pheno_name <- "y"
  if (!is.null(inp$covar_expr) && nzchar(inp$covar_expr)) {
    extra <- safe_eval(inp$covar_expr, env)
    if (is.data.frame(extra)) {
      # merge by row order
      covar_data <- cbind(covar_data, extra)
    }
  }

  bootstrap <- isTRUE(inp$bootstrap)
  bootstrap_n <- if (!is.null(inp$bootstrap_n)) as.integer(inp$bootstrap_n) else 100L
  seed <- if (!is.null(inp$seed)) as.integer(inp$seed) else NULL
  standardize <- isTRUE(inp$standardize)
  adjust_covar <- inp$adjust_covar

  t0 <- Sys.time()
  res <- BRIER::plot.eta(
    object = sel_obj,
    X = X_new,
    covar.data = covar_data,
    criteria = inp$criteria,
    pheno.name = pheno_name,
    adjust.covar = adjust_covar,
    standardize.data = standardize,
    bootstrap = bootstrap,
    bootstrap.n = bootstrap_n,
    seed = seed
  )
  t1 <- Sys.time()

  summary_df <- res$summary.df

  # Determine M from summary.df columns if not yet pinned
  eta_cols <- grep("^eta_\\d+$", colnames(summary_df), value = TRUE)
  M_from_df <- length(eta_cols)
  if (M_from_df > 0L) M <- M_from_df

  # Build plot based on M
  plot_id <- .generate_plot_id("plot_eta")
  out_dir <- .plots_output_dir(inp$output_dir)
  png_path <- file.path(out_dir, paste0(plot_id, ".png"))
  csv_path <- file.path(out_dir, paste0(plot_id, ".csv"))

  width <- if (!is.null(inp$width)) as.integer(inp$width) else 800L
  height <- if (!is.null(inp$height)) as.integer(inp$height) else 600L
  dpi <- if (!is.null(inp$dpi)) as.integer(inp$dpi) else 100L

  rendered_kind <- NA_character_
  ggsave_ok <- FALSE
  if (M == 1L && !is.null(res$plot)) {
    if (requireNamespace("ggplot2", quietly = TRUE)) {
      ggplot2::ggsave(png_path, plot = res$plot,
                      width = width/dpi, height = height/dpi,
                      dpi = dpi, units = "in")
      ggsave_ok <- TRUE
      rendered_kind <- "M1_curve"
    }
  } else if (M == 2L) {
    # Build the heatmap
    p <- .build_m2_heatmap(summary_df, inp$criteria)
    ggplot2::ggsave(png_path, plot = p,
                    width = width/dpi, height = height/dpi,
                    dpi = dpi, units = "in")
    ggsave_ok <- TRUE
    rendered_kind <- "M2_heatmap"
  } else if (M >= 3L) {
    p <- .build_m3_plus_facet(summary_df, inp$criteria)
    if (!is.null(p)) {
      ggplot2::ggsave(png_path, plot = p,
                      width = width/dpi, height = height/dpi,
                      dpi = dpi, units = "in")
      ggsave_ok <- TRUE
      rendered_kind <- "M3_plus_marginal"
    } else {
      rendered_kind <- "M3_plus_no_plot"
    }
  }

  # Write the CSV regardless
  if (!is.null(summary_df)) {
    write.csv(summary_df, csv_path, row.names = FALSE)
  }

  out <- list(
    status = "ok",
    plot_id = plot_id,
    plot_png_path = if (ggsave_ok) png_path else NA_character_,
    plot_csv_path = if (!is.null(summary_df)) csv_path else NA_character_,
    M = as.integer(M),
    rendered_kind = rendered_kind,
    criteria = inp$criteria,
    n_eta_points = nrow(summary_df),
    fit_seconds = round(as.numeric(difftime(t1, t0, units = "secs")), 3)
  )

  if (M >= 3L) {
    out$`_notice_m_ge_3` <- paste(
      "M >= 3 externals: a single 2D heatmap is not directly meaningful.",
      "Generated marginal-slice plot instead (each curve fixes all other",
      "etas at 0). For the full multi-dimensional surface, inspect the CSV"
    )
  }
  if (!ggsave_ok) {
    out$`_notice_no_plot` <- paste(
      "Could not render a plot for this M/configuration. CSV with",
      "underlying data is still available at plot_csv_path."
    )
  }

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_plot_eta.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
