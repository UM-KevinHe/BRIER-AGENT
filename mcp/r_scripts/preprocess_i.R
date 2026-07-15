#!/usr/bin/env Rscript
# preprocess_i.R - wrap BRIER::preprocessI to align target SNP info with
# one or more external coefficient tables by CHR/BP/REF/ALT, optionally
# dropping strand-ambiguous SNPs.
#
# Use when the user has SNP info for their target cohort and one or more
# external coefficient tables, and the SNP identifiers / coordinates /
# allele codings do not match across sources. preprocessI returns
# aligned versions ready to be passed downstream (e.g. to brier_i as
# beta.external).
#
# Inputs (JSON via _common.R::read_input):
#   data_path:           "/abs/path/to/data.rds",
#   target_info_expr:    "Data$target.info",                # required
#   external_coef_exprs: ["Data$external.coef1", "Data$external.coef2", ...],
#   target_info_cols:    {chr: "CHR", bp: "BP", ref: "REF", alt: "ALT"},
#   external_ss_cols:    {chr: "CHR", bp: "BP", ref: "REF", alt: "ALT"},
#   external_coef_cols:  ["coef"] (optional),
#   drop_ambiguous:      true,                              # default true
#   verbose:             false                              # default false
#
# Outputs:
#   status:        "ok",
#   preprocess_id: "preproc_i_yyyymmdd_hhmmss_xxxxxx",
#   preprocess_path: "/tmp/.../preproc.rds",   # cached result on disk
#   summary: {
#     n_target_in: int,
#     n_external_in_per_source: [int, ...],
#     n_aligned_out: int,
#     n_dropped_ambiguous: int,
#     M_external: int,
#   },
#   _notice_*:     "..."
#
# After this call the user can read the cached result in R via
#   readRDS(preprocess_path)
# which yields a list with $target.info and $external.coefs (a list with
# one entry per external source, each containing aligned $coef vector).

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


.cache_root_preprocess <- function() {
  base <- Sys.getenv("XDG_CACHE_HOME", unset = NA)
  if (is.na(base) || !nzchar(base)) {
    base <- if (.Platform$OS.type == "windows") {
      Sys.getenv("LOCALAPPDATA",
                 unset = file.path(Sys.getenv("HOME"), "AppData", "Local"))
    } else {
      file.path(Sys.getenv("HOME"), ".cache")
    }
  }
  d <- file.path(base, "brier-mcp", "preprocess")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.generate_preprocess_id <- function(prefix) {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0(prefix, "_", ts, "_", suffix)
}


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$data_path)) {
    stop("data_path is required", call. = FALSE)
  }
  if (is.null(inp$target_info_expr)) {
    stop("target_info_expr is required", call. = FALSE)
  }
  if (is.null(inp$external_coef_exprs) ||
      length(inp$external_coef_exprs) == 0L) {
    stop("external_coef_exprs is required (list of one or more)",
         call. = FALSE)
  }

  suppressPackageStartupMessages(library(BRIER))

  # Load data (v0.11: multi-file via load_data_files)
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)

  target_info <- safe_eval(inp$target_info_expr, env)
  if (is.null(target_info)) {
    stop(sprintf("target_info_expr %s resolved to NULL",
                  inp$target_info_expr), call. = FALSE)
  }
  external_coefs <- lapply(inp$external_coef_exprs, function(e) {
    out <- safe_eval(e, env)
    if (is.null(out)) {
      stop(sprintf("external_coef_expr %s resolved to NULL", e),
           call. = FALSE)
    }
    out
  })

  # Translate JSON named lists into R named character vectors for the
  # *_cols args.
  to_named_cv <- function(x) {
    if (is.null(x)) return(NULL)
    nm <- names(x)
    vals <- as.character(unlist(x, use.names = FALSE))
    setNames(vals, nm)
  }
  target_info_cols <- to_named_cv(inp$target_info_cols)
  if (is.null(target_info_cols)) {
    target_info_cols <- c(chr = "CHR", bp = "BP", ref = "REF", alt = "ALT")
  }
  external_ss_cols <- to_named_cv(inp$external_ss_cols)
  if (is.null(external_ss_cols)) {
    external_ss_cols <- c(chr = "CHR", bp = "BP", ref = "REF", alt = "ALT")
  }
  external_coef_cols <- if (!is.null(inp$external_coef_cols)) {
    as.character(unlist(inp$external_coef_cols))
  } else NULL

  drop_ambiguous <- if (is.null(inp$drop_ambiguous)) TRUE
                    else isTRUE(inp$drop_ambiguous)
  verbose <- isTRUE(inp$verbose)

  n_target_in <- nrow(target_info)
  n_external_in_per_source <- vapply(external_coefs, NROW, integer(1))

  t0 <- Sys.time()
  aligned <- BRIER::preprocessI(
    target.info        = target_info,
    external.ss        = external_coefs,
    target.info.cols   = target_info_cols,
    external.ss.cols   = external_ss_cols,
    external.coef.cols = external_coef_cols,
    drop.ambiguous     = drop_ambiguous,
    verbose            = verbose
  )
  t1 <- Sys.time()
  fit_seconds <- as.numeric(difftime(t1, t0, units = "secs"))

  # Cache the aligned result.
  preproc_id <- .generate_preprocess_id("preproc_i")
  preproc_path <- file.path(.cache_root_preprocess(),
                             paste0(preproc_id, ".rds"))
  saveRDS(aligned, file = preproc_path)

  # Summary: count what survived
  aligned_target <- if (!is.null(aligned$target.info)) {
    aligned$target.info
  } else if (!is.null(aligned$target)) {
    aligned$target
  } else NULL
  n_aligned_out <- if (!is.null(aligned_target)) nrow(aligned_target) else NA_integer_
  n_dropped_ambiguous <- n_target_in - if (is.na(n_aligned_out)) 0L else n_aligned_out

  out <- list(
    status = "ok",
    preprocess_id = preproc_id,
    preprocess_path = preproc_path,
    summary = list(
      n_target_in = n_target_in,
      n_external_in_per_source = as.list(n_external_in_per_source),
      n_aligned_out = n_aligned_out,
      n_dropped_ambiguous = max(0L, n_dropped_ambiguous),
      M_external = length(external_coefs),
      fit_seconds = round(fit_seconds, 3)
    )
  )

  out$`_notice_next_step` <- paste(
    "Aligned target.info and external.coefs are cached at",
    sprintf("'%s'.", preproc_path),
    "To use downstream: in R load with `aligned <- readRDS(",
    sprintf("'%s')`, then either save", preproc_path),
    "aligned$target.info and aligned$external.coefs (or whatever the",
    "preprocessI return-fields are named on your BRIER version) back",
    "into a .rds and reference them as X_expr / beta_external_expr in",
    "brier_i. preprocessI does NOT compute X itself; you still need",
    "individual-level X aligned to the same SNP order."
  )

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "preprocess_i.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
