#!/usr/bin/env Rscript
# brier_plot_selection.R - plot the SELECTION criterion vs eta, sourced
# entirely from the cached selection object's $eta.lambda table. Unlike
# brier_plot_eta (which runs BRIER::plot.eta and needs held-out X/y), this
# requires NO test data: it visualizes the exact criterion the selection
# optimized to choose eta.min, with the chosen point marked.
#
# The selection object stores $eta.lambda: a data.frame with columns
#   eta.index, eta_1[, eta_2, ...], criteria, measure.min,
#   lambda.min.index, lambda.min
# where measure.min is the selection-criterion value at the best lambda
# for each eta grid point.
#
# Rendering:
#   M=1   -> line plot of measure.min vs eta_1, selected eta marked
#   M=2   -> geom_tile heatmap over (eta_1, eta_2), selected cell marked
#   M>=3  -> marginal slices (each external's eta varied, others at min)
#
# Inputs (JSON):
#   selection_id: required; cached selection
#   width/height/dpi: PNG geometry (defaults 800/600/100)
#   output_dir: optional; per-call output directory override
#
# Outputs:
#   {status, plot_id, plot_png_path, plot_csv_path, M, rendered_kind,
#    criteria, selected_eta, n_eta_points}

.script_dir <- (function() {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) > 0) {
    dirname(normalizePath(sub("^--file=", "", file_arg[1])))
  } else { getwd() }
})()
source(file.path(.script_dir, "_common.R"))


`%||%` <- function(a, b) if (is.null(a)) b else a


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

.generate_plot_id <- function(prefix = "plot_selection") {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0(prefix, "_", ts, "_", suffix)
}


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input
  if (is.null(inp$selection_id)) {
    stop("selection_id is required", call. = FALSE)
  }

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

  el <- sel_obj$eta.lambda
  if (is.null(el) || nrow(el) == 0L) {
    stop("selection object has no eta.lambda table to plot", call. = FALSE)
  }
  criteria_name <- if (!is.null(sel_obj$criteria)) sel_obj$criteria else
    (cached$criteria %||% "criterion")

  eta_cols <- grep("^eta_\\d+$", colnames(el), value = TRUE)
  M <- length(eta_cols)
  if (M == 0L) {
    stop("eta.lambda has no eta_* columns", call. = FALSE)
  }

  # Selected eta (for marking)
  sel_eta <- sel_obj$eta.min
  if (is.list(sel_eta)) sel_eta <- unlist(sel_eta)
  sel_eta <- as.numeric(unname(sel_eta))

  if (!requireNamespace("ggplot2", quietly = TRUE)) {
    stop("ggplot2 is required for plotting", call. = FALSE)
  }

  plot_id <- .generate_plot_id("plot_selection")
  out_dir <- .plots_output_dir(inp$output_dir)
  png_path <- file.path(out_dir, paste0(plot_id, ".png"))
  csv_path <- file.path(out_dir, paste0(plot_id, ".csv"))

  width <- if (!is.null(inp$width)) as.integer(inp$width) else 800L
  height <- if (!is.null(inp$height)) as.integer(inp$height) else 600L
  dpi <- if (!is.null(inp$dpi)) as.integer(inp$dpi) else 100L

  rendered_kind <- NA_character_

  if (M == 1L) {
    df <- data.frame(eta = el[[eta_cols[1]]],
                     criterion = el$measure.min)
    sel_pt <- df[which.min(abs(df$eta - sel_eta[1])), , drop = FALSE]
    p <- ggplot2::ggplot(df, ggplot2::aes(x = eta, y = criterion)) +
      ggplot2::geom_line(color = "#2c7bb6") +
      ggplot2::geom_point(color = "#2c7bb6") +
      ggplot2::geom_point(data = sel_pt,
                          ggplot2::aes(x = eta, y = criterion),
                          color = "#d7191c", size = 4) +
      ggplot2::geom_vline(xintercept = sel_eta[1], linetype = "dashed",
                          color = "#d7191c") +
      ggplot2::labs(
        title = sprintf("Selection criterion (%s) vs eta", criteria_name),
        subtitle = sprintf("Selected eta = %g (marked); lower is better for IC/error criteria",
                            sel_eta[1]),
        x = "eta (external information weight)",
        y = criteria_name
      ) +
      ggplot2::theme_minimal()
    ggplot2::ggsave(png_path, plot = p, width = width/dpi,
                    height = height/dpi, dpi = dpi, units = "in")
    rendered_kind <- "M1_selection_curve"

  } else if (M == 2L) {
    df <- data.frame(eta_1 = el[[eta_cols[1]]],
                     eta_2 = el[[eta_cols[2]]],
                     criterion = el$measure.min)
    p <- ggplot2::ggplot(df, ggplot2::aes(x = factor(eta_1),
                                          y = factor(eta_2),
                                          fill = criterion)) +
      ggplot2::geom_tile(color = "white") +
      ggplot2::geom_text(ggplot2::aes(label = sprintf("%.3g", criterion)),
                         size = 3, color = "black") +
      ggplot2::scale_fill_gradient(low = "#2c7bb6", high = "#fdae61",
                                   name = criteria_name) +
      ggplot2::labs(
        title = sprintf("Selection criterion (%s) over (eta_1, eta_2)",
                        criteria_name),
        subtitle = sprintf("Selected eta = (%g, %g)",
                            sel_eta[1], sel_eta[2]),
        x = "eta_1", y = "eta_2"
      ) +
      ggplot2::theme_minimal()
    ggplot2::ggsave(png_path, plot = p, width = width/dpi,
                    height = height/dpi, dpi = dpi, units = "in")
    rendered_kind <- "M2_selection_heatmap"

  } else {
    # M >= 3: marginal slices, each external's eta varied with the others
    # held at their minimum grid value.
    marg_list <- list()
    for (m in seq_len(M)) {
      others <- setdiff(seq_len(M), m)
      keep <- rep(TRUE, nrow(el))
      for (o in others) {
        keep <- keep & (el[[eta_cols[o]]] == min(el[[eta_cols[o]]]))
      }
      sub <- el[keep, , drop = FALSE]
      if (nrow(sub) > 0) {
        marg_list[[m]] <- data.frame(
          external = paste0("eta_", m),
          eta_value = sub[[eta_cols[m]]],
          criterion = sub$measure.min
        )
      }
    }
    marg <- do.call(rbind, marg_list)
    p <- ggplot2::ggplot(marg, ggplot2::aes(x = eta_value, y = criterion,
                                            color = external,
                                            group = external)) +
      ggplot2::geom_line(linewidth = 1) +
      ggplot2::geom_point(size = 2) +
      ggplot2::labs(
        title = sprintf("Marginal selection criterion (%s) by external",
                        criteria_name),
        subtitle = sprintf("M=%d externals; other etas held at grid minimum", M),
        x = "eta value (this external)", y = criteria_name,
        color = "External"
      ) +
      ggplot2::theme_minimal()
    ggplot2::ggsave(png_path, plot = p, width = width/dpi,
                    height = height/dpi, dpi = dpi, units = "in")
    rendered_kind <- "M3_plus_selection_marginal"
  }

  # CSV: the eta.lambda table (the data behind the plot)
  write.csv(el, csv_path, row.names = FALSE)

  list(
    status = "ok",
    plot_id = plot_id,
    plot_png_path = png_path,
    plot_csv_path = csv_path,
    M = as.integer(M),
    rendered_kind = rendered_kind,
    criteria = criteria_name,
    selected_eta = if (length(sel_eta) == 1L) sel_eta[1] else as.list(sel_eta),
    n_eta_points = nrow(el)
  )
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_plot_selection.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
