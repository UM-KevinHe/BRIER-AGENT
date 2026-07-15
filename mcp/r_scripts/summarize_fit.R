#!/usr/bin/env Rscript
# summarize_fit.R - extract everything we know about a cached BRIER fit
# + selection, generate a standalone reproduce.R script, and return
# structured data for the Python side to compose into an HTML report.
#
# The MCP's job here is the heavy lifting; the Python side will do the
# templating into HTML.
#
# Inputs (JSON):
#   selection_id:    required; cached selection
#   inspection_id:   optional; for data-context section
#   include_repro:   bool (default true); whether to emit reproduce.R
#   output_dir:      optional; where to write reproduce.R
#
# Outputs:
#   {status, report_id, reproduce_r_path,
#    data_context: {...},
#    fitting_summary: {...},
#    selection_summary: {...},
#    metadata: {fit_id, selection_id, ...}}

.script_dir <- (function() {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) > 0) {
    dirname(normalizePath(sub("^--file=", "", file_arg[1])))
  } else { getwd() }
})()
source(file.path(.script_dir, "_common.R"))


`%||%` <- function(a, b) if (is.null(a)) b else a


# Return the source data path(s) for a fit, as a character vector.
# Prefers meta$data_paths (v0.11+, a list) and falls back to the legacy
# singular meta$data_path. Returns character(0) if neither is set.
.meta_paths <- function(meta) {
  if (!is.null(meta$data_paths) && length(meta$data_paths) > 0L) {
    return(as.character(unlist(meta$data_paths)))
  }
  if (!is.null(meta$data_path) && nzchar(meta$data_path)) {
    return(as.character(meta$data_path))
  }
  character(0)
}


# Human-readable "Source data" comment value for the reproduce.R header.
.source_data_label <- function(meta) {
  paths <- .meta_paths(meta)
  if (length(paths) == 0L) return("<unknown>")
  if (length(paths) == 1L) return(paths[1])
  paste0(length(paths), " files: ", paste(paths, collapse = ", "))
}


# Generate the R code block that loads the source data for reproduce.R.
# Handles both single-file and multi-file fits. Each file's contents are
# assigned to a variable named after the file basename, matching the MCP's
# load_data_files() convention so the captured *_expr strings resolve as
# written (e.g. "height_AFR$sumstats", "height_EUR$beta.external").
.repro_load_block <- function(meta) {
  paths <- .meta_paths(meta)
  header <- paste(
    "# 1. Load data. MCP convention: each file's contents are assigned to",
    "# a variable named after the file basename, so the captured *_expr",
    "# expressions resolve as written (e.g. \"height_AFR$sumstats\").",
    sep = "\n"
  )
  if (length(paths) == 0L) {
    return(paste(
      header,
      'data_paths <- character(0)   # original source path(s) not recorded',
      '# Provide your data file path(s) here and load them under the',
      '# basename convention before running the fit below.',
      sep = "\n"
    ))
  }
  # Build a quoted R character vector of the paths.
  quoted <- paste0('"', paths, '"', collapse = ",\n  ")
  loader <- paste(
    sprintf("data_paths <- c(\n  %s\n)", quoted),
    "for (.p in data_paths) {",
    "  .nm <- tools::file_path_sans_ext(basename(.p))",
    "  .ext <- tolower(tools::file_ext(.p))",
    "  if (.ext == \"rds\") {",
    "    assign(.nm, readRDS(.p))",
    "  } else {",
    "    # .rda / .RData: load into a temp env, then alias under basename.",
    "    .tmp <- new.env(); load(.p, envir = .tmp); .vars <- ls(.tmp)",
    "    if (length(.vars) == 1L) {",
    "      assign(.nm, get(.vars[1], envir = .tmp))",
    "    } else {",
    "      assign(.nm, mget(.vars, envir = .tmp))",
    "    }",
    "  }",
    "}",
    sep = "\n"
  )
  paste(header, loader, sep = "\n")
}


.cache_root <- function() {
  base <- Sys.getenv("XDG_CACHE_HOME", unset = NA)
  if (is.na(base) || !nzchar(base)) {
    base <- file.path(Sys.getenv("HOME"), ".cache")
  }
  file.path(base, "brier-mcp")
}

.reports_output_dir <- function(explicit_dir = NULL) {
  if (!is.null(explicit_dir) && nzchar(explicit_dir)) {
    dir.create(explicit_dir, recursive = TRUE, showWarnings = FALSE)
    return(explicit_dir)
  }
  cfg_path <- file.path(
    Sys.getenv("XDG_CONFIG_HOME",
               unset = file.path(Sys.getenv("HOME"), ".config")),
    "brier-mcp", "config.json"
  )
  if (file.exists(cfg_path)) {
    cfg <- tryCatch(jsonlite::fromJSON(cfg_path), error = function(e) NULL)
    if (!is.null(cfg) && !is.null(cfg$output_directory) &&
        nzchar(cfg$output_directory) && dir.exists(cfg$output_directory)) {
      return(cfg$output_directory)
    }
  }
  d <- file.path(.cache_root(), "reports")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.generate_report_id <- function() {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0("report_", ts, "_", suffix)
}


# Format an R value as an R expression for the reproduce script.
# Handles numerics, character strings, simple vectors and lists.
.r_repr <- function(x) {
  if (is.null(x)) return("NULL")
  if (is.logical(x) && length(x) == 1) return(if (x) "TRUE" else "FALSE")
  if (is.character(x) && length(x) == 1) {
    return(sprintf('"%s"', gsub('"', '\\\\"', x)))
  }
  if (is.numeric(x) && length(x) == 1) {
    return(format(x, digits = 8))
  }
  if (is.numeric(x) && length(x) > 1) {
    return(sprintf("c(%s)",
                    paste(format(x, digits = 8), collapse = ", ")))
  }
  if (is.list(x)) {
    # JSON-deserialized numeric vectors arrive as R lists of length-1
    # numerics. If every element is a length-1 numeric scalar, emit as a
    # single c(...) so BRIER reads it as a one-external eta grid, not as
    # M separate externals. (M>=2 case has elements that are themselves
    # length>=1 numeric vectors, handled below.)
    is_flat_numeric <- all(vapply(x, function(v)
        is.numeric(v) && length(v) == 1, logical(1)))
    if (length(x) > 0 && is_flat_numeric) {
      return(sprintf("c(%s)",
                      paste(format(unlist(x), digits = 8),
                            collapse = ", ")))
    }
    # Otherwise: list of numeric vectors -> list(c(...), c(...))
    inner <- vapply(x, function(v) {
      if (is.numeric(v)) {
        sprintf("c(%s)", paste(format(v, digits = 8), collapse = ", "))
      } else {
        .r_repr(v)
      }
    }, character(1))
    return(sprintf("list(%s)", paste(inner, collapse = ", ")))
  }
  # Fallback
  deparse(x)[1]
}


# Templates for reproduce.R, one per family.
.template_brier_i <- function(meta, selection_meta, fit_id, selection_id) {
  eta_repr <- if (!is.null(selection_meta$eta_list_used)) {
    .r_repr(selection_meta$eta_list_used)
  } else "NULL"
  multi_method_repr <- .r_repr(meta$multi_method %||% "stacking")
  family_repr <- .r_repr(meta$family %||% "gaussian")
  criteria_repr <- .r_repr(selection_meta$criteria %||% "BIC")

  sprintf(
'# Auto-generated by BRIER MCP summarize_fit (v0.10)
# Reproduces fit_id: %s
# Source data: %s
# Generated: %s
#
# This script regenerates the BRIER fit using only the BRIER R package.
# No MCP or Claude Desktop required.

library(BRIER)

%s

# 2. Resolve inputs from the captured expressions
X_train      <- %s
y_train      <- %s
beta_external <- %s

# 3. Fit BRIERi
fit <- BRIERi(
  X             = X_train,
  y             = y_train,
  family        = %s,
  beta.external = beta_external,
  multi.method  = %s,
  eta.list      = %s
)

# 4. Select hyperparameters (criteria = %s)
selection <- BRIERi.selection(
  object   = fit,
  criteria = %s
)

# Inspect the result
cat("Selected eta:   ", selection$eta.min, "\\n")
cat("Selected lambda:", selection$lambda.min, "\\n")
',
    fit_id,
    .source_data_label(meta),
    format(Sys.time(), "%Y-%m-%d %H:%M:%S"),
    .repro_load_block(meta),
    meta$X_expr %||% "<X_expr missing>",
    meta$y_expr %||% "<y_expr missing>",
    meta$beta_external_expr %||% "<beta_external_expr missing>",
    family_repr,
    multi_method_repr,
    eta_repr,
    criteria_repr,
    criteria_repr
  )
}


.template_brier_full <- function(meta, selection_meta, fit_id, selection_id) {
  eta_repr <- if (!is.null(selection_meta$eta_list_used)) {
    .r_repr(selection_meta$eta_list_used)
  } else "NULL"
  family_repr <- .r_repr(meta$family %||% "gaussian")
  criteria_repr <- .r_repr(selection_meta$criteria %||% "gaussian.mspe")

  # BRIERfull.selection needs X.val/y.val; we can't reconstruct those
  # without knowing what the user passed. Emit a placeholder comment.
  sprintf(
'# Auto-generated by BRIER MCP summarize_fit (v0.10)
# Reproduces fit_id: %s
# Source data: %s
# Generated: %s
#
# This script regenerates the BRIER fit using only the BRIER R package.

library(BRIER)

%s

# 2. Resolve inputs from the captured expressions
X_pooled <- %s
y_pooled <- %s
cohort   <- %s

# 3. Fit BRIERfull
fit <- BRIERfull(
  X        = X_pooled,
  y        = y_pooled,
  cohort   = cohort,
  family   = %s,
  eta.list = %s
)

# 4. Select hyperparameters via validation set (BRIERfull.selection is
#    validation-only; no IC support). REPLACE the X.val and y.val
#    expressions with your actual held-out target data.
# X_val <- <your held-out target X>
# y_val <- <your held-out target y>
# selection <- BRIERfull.selection(
#   object   = fit,
#   criteria = %s,
#   X.val    = X_val,
#   y.val    = y_val
# )
# cat("Selected eta:   ", selection$eta.min, "\\n")
# cat("Selected lambda:", selection$lambda.min, "\\n")

cat("Fit complete. Uncomment the selection block above with your",
    "validation set to tune hyperparameters.\\n")
',
    fit_id,
    .source_data_label(meta),
    format(Sys.time(), "%Y-%m-%d %H:%M:%S"),
    .repro_load_block(meta),
    meta$X_expr %||% "<X_expr missing>",
    meta$y_expr %||% "<y_expr missing>",
    meta$cohort_expr %||% "<cohort_expr missing>",
    family_repr,
    eta_repr,
    criteria_repr
  )
}


.template_brier_s <- function(meta, selection_meta, fit_id, selection_id) {
  eta_repr <- if (!is.null(selection_meta$eta_list_used)) {
    .r_repr(selection_meta$eta_list_used)
  } else "NULL"
  multi_method_repr <- .r_repr(meta$multi_method %||% "stacking")
  family_repr <- .r_repr(meta$family %||% "gaussian")
  criteria_repr <- .r_repr(selection_meta$criteria %||% "Cp")

  # Three cases for the LD matrix:
  #  (1) ld_id was used  -> the MCP's LD cache isn't available standalone;
  #      emit a rebuild note.
  #  (2) XtX_expr is recorded -> emit it directly; it resolves against the
  #      loaded data files.
  #  (3) neither -> placeholder.
  ld_section <- if (!is.null(meta$ld_id_used) && nzchar(meta$ld_id_used)) {
    sprintf(
'# 2b. Rebuild LD from a reference panel.
# IMPORTANT: this script does NOT have access to the MCP\'s LD cache.
# You must rebuild the LD matrix here. Replace X_ref with your
# reference-panel genotype matrix.
# X_ref <- <your reference-panel X>
# ld <- calLD(X = X_ref, SNP.info = NULL, LDB = NULL, tau = 0)
# XtX <- ld$XtX

XtX <- <YOUR_LD_MATRIX_HERE>   # rebuild with calLD() as needed
'
    )
  } else if (!is.null(meta$XtX_expr) && nzchar(meta$XtX_expr)) {
    sprintf(
'# 2b. LD matrix, resolved from the captured expression.
XtX <- %s', meta$XtX_expr
    )
  } else {
    "XtX <- <YOUR_LD_MATRIX_HERE>   # the original XtX_expr could not be recovered"
  }

  sprintf(
'# Auto-generated by BRIER MCP summarize_fit (v0.10)
# Reproduces fit_id: %s
# Source data: %s
# Generated: %s
#
# This script regenerates the BRIER fit using only the BRIER R package.

library(BRIER)

%s

# 2a. Resolve summary-statistics inputs from the captured expressions
sumstats     <- %s
beta_external <- %s

%s

# 3. Fit BRIERs
fit <- BRIERs(
  sumstats      = sumstats,
  XtX           = XtX,
  family        = %s,
  beta.external = beta_external,
  multi.method  = %s,
  eta.list      = %s
)

# 4. Select hyperparameters (criteria = %s)
# For IC criteria (Cp, GIC, pseu.val) you must supply TN and h2.
# selection <- BRIERs.selection(
#   object   = fit,
#   criteria = %s,
#   TN       = <training cohort sample size>,
#   h2       = 0           # SNP heritability estimate; 0 is a safe fallback
# )
# cat("Selected eta:   ", selection$eta.min, "\\n")
# cat("Selected lambda:", selection$lambda.min, "\\n")

cat("Fit complete. Uncomment the selection block above with TN and h2",
    "to tune hyperparameters.\\n")
',
    fit_id,
    .source_data_label(meta),
    format(Sys.time(), "%Y-%m-%d %H:%M:%S"),
    .repro_load_block(meta),
    meta$sumstats_expr %||% "<sumstats_expr missing>",
    meta$beta_external_expr %||% "<beta_external_expr missing>",
    ld_section,
    family_repr,
    multi_method_repr,
    eta_repr,
    criteria_repr,
    criteria_repr
  )
}


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$selection_id)) {
    stop("selection_id is required", call. = FALSE)
  }

  # Load selection cache
  fits_dir <- file.path(.cache_root(), "fits")
  sel_path <- file.path(fits_dir, paste0(inp$selection_id, ".rds"))
  if (!file.exists(sel_path)) {
    stop(sprintf("selection_id '%s' not found in cache",
                 inp$selection_id), call. = FALSE)
  }
  cached <- readRDS(sel_path)
  sel_obj <- cached$selection
  source_fit_id <- cached$source_fit_id
  selection_criteria <- cached$criteria

  # Load source fit cache
  fit_path <- file.path(fits_dir, paste0(source_fit_id, ".rds"))
  if (!file.exists(fit_path)) {
    stop(sprintf("source fit_id '%s' not found", source_fit_id),
         call. = FALSE)
  }
  fit_cached <- readRDS(fit_path)
  fit_obj <- fit_cached$fit
  meta <- fit_cached$meta
  if (is.null(meta)) meta <- list()

  # Detect family-tool from meta
  tool_name <- meta$tool
  if (is.null(tool_name)) {
    # Infer
    if (!is.null(meta$cohort_expr)) tool_name <- "brier_full"
    else if (!is.null(meta$sumstats_expr)) tool_name <- "brier_s"
    else if (!is.null(meta$beta_external_expr) &&
             !is.null(meta$X_expr)) tool_name <- "brier_i"
    else tool_name <- "unknown"
  }

  # Try to extract data-context info from the fit object
  data_context <- list(
    family = meta$family %||% "unknown",
    tool = tool_name,
    data_path = meta$data_path %||% "unknown"
  )

  # Try to read additional context from inspection_id if provided
  if (!is.null(inp$inspection_id) && nzchar(inp$inspection_id)) {
    insp_dir <- file.path(.cache_root(), "inspections")
    insp_path <- file.path(insp_dir, paste0(inp$inspection_id, ".rds"))
    if (file.exists(insp_path)) {
      insp <- tryCatch(readRDS(insp_path), error = function(e) NULL)
      if (!is.null(insp)) {
        data_context$inspection_found <- TRUE
        data_context$inspection_files <- names(insp$files %||% list())
        # Summarize top-level structure if available
        if (!is.null(insp$combined)) {
          data_context$inspection_top_keys <- names(insp$combined)
        }
      }
    } else {
      data_context$inspection_found <- FALSE
    }
  }

  # Pull dimensions from fit object where possible
  fit_dims <- list()
  fit_dims$p <- tryCatch({
    if (!is.null(fit_obj$beta)) {
      if (is.list(fit_obj$beta) && length(fit_obj$beta) > 0) {
        nrow(fit_obj$beta[[1]])
      } else if (is.matrix(fit_obj$beta)) {
        nrow(fit_obj$beta)
      } else NULL
    } else NULL
  }, error = function(e) NULL)
  fit_dims$n_train <- tryCatch(fit_obj$n %||% NULL, error = function(e) NULL)
  fit_dims$M_external <- tryCatch({
    if (!is.null(fit_obj$M)) fit_obj$M
    else if (!is.null(fit_obj$eta.list) && is.list(fit_obj$eta.list)) {
      length(fit_obj$eta.list)
    } else NULL
  }, error = function(e) NULL)

  # Fitting summary
  fitting_summary <- list(
    tool = tool_name,
    family = meta$family %||% "unknown",
    multi_method = meta$multi_method %||% NA,
    fit_id = source_fit_id,
    eta_list_used = tryCatch({
      # Prefer the explicit grid stored in meta (v0.10.3+); fall back to
      # reading it off the fit object for older fits.
      if (!is.null(meta$eta_list)) {
        meta$eta_list
      } else {
        eu <- fit_obj$eta.list
        if (is.list(eu) && length(eu) == 1) as.numeric(eu[[1]])
        else if (is.list(eu)) lapply(eu, as.numeric)
        else as.numeric(eu)
      }
    }, error = function(e) NULL),
    dimensions = fit_dims,
    # v0.13: prep_data session IDs whose persisted output fed this fit
    prep_session_ids = if (!is.null(meta$prep_session_ids))
                          as.character(meta$prep_session_ids)
                        else character(0)
  )

  # Selection summary
  selection_summary <- list(
    selection_id = inp$selection_id,
    criteria = selection_criteria,
    best_eta = tryCatch({
      v <- sel_obj$eta.min
      if (is.null(v)) sel_obj$best.eta else v
    }, error = function(e) NULL),
    best_lambda = tryCatch({
      v <- sel_obj$lambda.min
      if (is.null(v)) sel_obj$best.lambda else v
    }, error = function(e) NULL),
    best_metric_value = tryCatch({
      v <- sel_obj$value.min
      if (is.null(v)) sel_obj$best.value else v
    }, error = function(e) NULL)
  )

  # Build reproduce.R if requested
  report_id <- .generate_report_id()
  reports_dir <- .reports_output_dir(inp$output_dir)
  reproduce_path <- NA_character_

  if (isTRUE(inp$include_repro) || is.null(inp$include_repro)) {
    selection_meta <- list(
      criteria = selection_criteria,
      eta_list_used = fitting_summary$eta_list_used
    )
    script_text <- switch(tool_name,
      brier_i = .template_brier_i(meta, selection_meta,
                                    source_fit_id, inp$selection_id),
      brier_full = .template_brier_full(meta, selection_meta,
                                          source_fit_id, inp$selection_id),
      brier_s = .template_brier_s(meta, selection_meta,
                                    source_fit_id, inp$selection_id),
      paste("# Unknown fit type", tool_name, "- cannot generate",
            "reproduce script.\n")
    )
    reproduce_path <- file.path(reports_dir,
                                  sub("^report_", "reproduce_", report_id))
    reproduce_path <- paste0(reproduce_path, ".R")
    writeLines(script_text, reproduce_path)
  }

  list(
    status = "ok",
    report_id = report_id,
    reproduce_r_path = reproduce_path,
    reports_dir = reports_dir,
    data_context = data_context,
    fitting_summary = fitting_summary,
    selection_summary = selection_summary,
    metadata = list(
      selection_id = inp$selection_id,
      fit_id = source_fit_id,
      tool = tool_name,
      data_path = meta$data_path
    )
  )
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "summarize_fit.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
