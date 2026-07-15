#!/usr/bin/env Rscript
# brier_plot_importance.R - wrap BRIER::plot.importance for bootstrap
# variable-importance bar plots based on selection frequencies.
#
# Returns PNG + CSV of the underlying importance scores.

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
    base <- file.path(Sys.getenv("HOME"), ".cache")
  }
  d <- file.path(base, "brier-mcp", "fits")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.plots_output_dir <- function(explicit_dir = NULL) {
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

.generate_plot_id <- function(prefix = "plot_importance") {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0(prefix, "_", ts, "_", suffix)
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

  cache_dir <- .cache_root_fits()
  sel_path <- file.path(cache_dir, paste0(inp$selection_id, ".rds"))
  if (!file.exists(sel_path)) {
    stop(sprintf("selection_id '%s' not found", inp$selection_id),
         call. = FALSE)
  }
  cached <- readRDS(sel_path)
  sel_obj <- cached$selection
  if (is.null(sel_obj)) {
    stop("cached object missing $selection", call. = FALSE)
  }

  # v0.11: multi-file via load_data_files
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)

  X_new <- safe_eval(inp$newx_expr, env)
  y_new <- safe_eval(inp$newy_expr, env)
  if (is.null(X_new) || is.null(y_new)) {
    stop("newx_expr or newy_expr resolved to NULL", call. = FALSE)
  }

  covar_data <- data.frame(y = as.numeric(y_new))
  if (!is.null(inp$covar_expr) && nzchar(inp$covar_expr)) {
    extra <- safe_eval(inp$covar_expr, env)
    if (is.data.frame(extra)) covar_data <- cbind(covar_data, extra)
  }

  n_top <- if (!is.null(inp$n_top)) as.integer(inp$n_top) else 20L
  replications <- if (!is.null(inp$replications)) as.integer(inp$replications) else 100L
  seed <- if (!is.null(inp$seed)) as.integer(inp$seed) else NULL
  standardize <- isTRUE(inp$standardize)
  adjust_covar <- inp$adjust_covar

  t0 <- Sys.time()
  res <- BRIER::plot.importance(
    object = sel_obj,
    X = X_new,
    covar.data = covar_data,
    criteria = inp$criteria,
    pheno.name = "y",
    adjust.covar = adjust_covar,
    standardize.data = standardize,
    n.top = n_top,
    replications = replications,
    seed = seed
  )
  t1 <- Sys.time()

  plot_id <- .generate_plot_id("plot_importance")
  out_dir <- .plots_output_dir(inp$output_dir)
  png_path <- file.path(out_dir, paste0(plot_id, ".png"))
  csv_path <- file.path(out_dir, paste0(plot_id, ".csv"))

  width <- if (!is.null(inp$width)) as.integer(inp$width) else 800L
  height <- if (!is.null(inp$height)) as.integer(inp$height) else 600L
  dpi <- if (!is.null(inp$dpi)) as.integer(inp$dpi) else 100L

  # plot.importance returns a list; try to find the plot and the data
  plot_obj <- NULL
  importance_df <- NULL
  if (is.list(res)) {
    if (!is.null(res$plot)) plot_obj <- res$plot
    if (!is.null(res$importance.df)) importance_df <- res$importance.df
    if (is.null(importance_df) && !is.null(res$summary.df)) {
      importance_df <- res$summary.df
    }
  } else if (inherits(res, "gg") || inherits(res, "ggplot")) {
    plot_obj <- res
  }

  if (!is.null(plot_obj) && requireNamespace("ggplot2", quietly = TRUE)) {
    ggplot2::ggsave(png_path, plot = plot_obj,
                    width = width/dpi, height = height/dpi,
                    dpi = dpi, units = "in")
  } else {
    stop("plot.importance did not return a renderable ggplot",
         call. = FALSE)
  }

  if (!is.null(importance_df)) {
    write.csv(importance_df, csv_path, row.names = FALSE)
  }

  list(
    status = "ok",
    plot_id = plot_id,
    plot_png_path = png_path,
    plot_csv_path = if (!is.null(importance_df)) csv_path else NA_character_,
    criteria = inp$criteria,
    n_top = n_top,
    n_replications = replications,
    fit_seconds = round(as.numeric(difftime(t1, t0, units = "secs")), 3)
  )
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_plot_importance.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
