#!/usr/bin/env Rscript
# preprocess_s.R - wrap BRIER::preprocessS to align target summary
# statistics + target LD + external coefficient tables by CHR/BP/REF/ALT,
# optionally dropping strand-ambiguous SNPs.
#
# Use for the BRIERs path when the user has GWAS summary statistics for
# the target, an LD matrix (with its own SNP info), and one or more
# external coefficient tables, and the SNP identifiers / coordinates /
# allele codings do not match across sources.
#
# Inputs (JSON via _common.R::read_input):
#   data_path:           "/abs/path/to/data.rds",
#   target_ss_expr:      "Data$target.ss",                  # required
#   target_ld_expr:      "Data$target.ld",                  # required (LD info df)
#   target_ld_mat_expr:  "Data$target.ld.mat",              # required (Matrix)
#   external_coef_exprs: ["Data$external.coef1", ...],      # required
#   target_ind:          "gwas",                            # default "gwas" (vs "corr")
#   target_ss_cols:      {chr, bp, ref, alt, p, n, sgn, beta, corr},
#   target_ld_cols:      {chr, bp, ref, alt},
#   external_ss_cols:    {chr, bp, ref, alt},
#   external_coef_cols:  ["coef"],
#   drop_ambiguous:      true,
#   verbose:             false
#
# Outputs:
#   status:        "ok",
#   preprocess_id: "preproc_s_yyyymmdd_hhmmss_xxxxxx",
#   preprocess_path: "/tmp/.../preproc.rds",
#   summary: {
#     n_target_ss_in, n_target_ld_in, n_external_in_per_source,
#     n_aligned_out, n_dropped_ambiguous, M_external, fit_seconds
#   },
#   _notice_*

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

  for (req in c("data_path", "target_ss_expr", "target_ld_expr",
                 "target_ld_mat_expr", "external_coef_exprs")) {
    if (is.null(inp[[req]]) ||
        (length(inp[[req]]) == 0L && req == "external_coef_exprs")) {
      stop(sprintf("%s is required", req), call. = FALSE)
    }
  }

  suppressPackageStartupMessages(library(BRIER))

  # v0.11: multi-file via load_data_files
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)

  target_ss <- safe_eval(inp$target_ss_expr, env)
  target_ld <- safe_eval(inp$target_ld_expr, env)
  target_ld_mat <- safe_eval(inp$target_ld_mat_expr, env)
  if (is.null(target_ss)) stop("target_ss_expr resolved to NULL", call. = FALSE)
  if (is.null(target_ld)) stop("target_ld_expr resolved to NULL", call. = FALSE)
  if (is.null(target_ld_mat)) {
    stop("target_ld_mat_expr resolved to NULL", call. = FALSE)
  }

  external_coefs <- lapply(inp$external_coef_exprs, function(e) {
    out <- safe_eval(e, env)
    if (is.null(out)) {
      stop(sprintf("external_coef_expr %s resolved to NULL", e),
           call. = FALSE)
    }
    out
  })

  to_named_cv <- function(x) {
    if (is.null(x)) return(NULL)
    nm <- names(x)
    vals <- as.character(unlist(x, use.names = FALSE))
    setNames(vals, nm)
  }
  target_ss_cols <- to_named_cv(inp$target_ss_cols)
  if (is.null(target_ss_cols)) {
    target_ss_cols <- c(chr = "CHR", bp = "BP", ref = "REF", alt = "ALT",
                        p = "pval", n = "n", sgn = "sgn",
                        beta = "beta", corr = "corr")
  }
  target_ld_cols <- to_named_cv(inp$target_ld_cols)
  if (is.null(target_ld_cols)) {
    target_ld_cols <- c(chr = "CHR", bp = "BP", ref = "REF", alt = "ALT")
  }
  external_ss_cols <- to_named_cv(inp$external_ss_cols)
  if (is.null(external_ss_cols)) {
    external_ss_cols <- c(chr = "CHR", bp = "BP", ref = "REF", alt = "ALT")
  }
  external_coef_cols <- if (!is.null(inp$external_coef_cols)) {
    as.character(unlist(inp$external_coef_cols))
  } else NULL
  target_ind <- if (!is.null(inp$target_ind)) inp$target_ind else "gwas"
  drop_ambiguous <- if (is.null(inp$drop_ambiguous)) TRUE
                    else isTRUE(inp$drop_ambiguous)
  verbose <- isTRUE(inp$verbose)

  n_target_ss_in <- nrow(target_ss)
  n_target_ld_in <- nrow(target_ld)
  n_external_in_per_source <- vapply(external_coefs, NROW, integer(1))

  t0 <- Sys.time()
  aligned <- BRIER::preprocessS(
    target.ss          = target_ss,
    target.ind         = target_ind,
    target.ld          = target_ld,
    external.ss        = external_coefs,
    target.ss.cols     = target_ss_cols,
    target.ld.cols     = target_ld_cols,
    external.ss.cols   = external_ss_cols,
    external.coef.cols = external_coef_cols,
    drop.ambiguous     = drop_ambiguous,
    verbose            = verbose
  )
  t1 <- Sys.time()
  fit_seconds <- as.numeric(difftime(t1, t0, units = "secs"))

  # preprocessS aligns the SNP info tables but does NOT subset the LD
  # matrix. The user's downstream call to brier_s expects the LD matrix
  # rows/cols to match the aligned sumstats SNP order. Subset here so
  # the cached payload is self-consistent.
  #
  # We need to find which rows of the original target.ld survive in
  # aligned$target.ld (typically named differently in the return).
  aligned_ld_info <- aligned$target.ld
  if (is.null(aligned_ld_info)) {
    # Some BRIER versions name it differently
    aligned_ld_info <- aligned$ld.info
  }
  aligned_ld_mat <- NULL
  if (!is.null(aligned_ld_info)) {
    # Match by CHR + BP. Aligned LD info has the same CHR/BP order as the
    # aligned sumstats; we map back to original row indices of target.ld.
    orig_key <- paste(target_ld[[target_ld_cols["chr"]]],
                      target_ld[[target_ld_cols["bp"]]], sep = ":")
    aligned_key <- paste(aligned_ld_info[[target_ld_cols["chr"]]],
                          aligned_ld_info[[target_ld_cols["bp"]]], sep = ":")
    keep_idx <- match(aligned_key, orig_key)
    keep_idx <- keep_idx[!is.na(keep_idx)]
    if (length(keep_idx) > 0L &&
        !is.null(target_ld_mat) &&
        (inherits(target_ld_mat, "Matrix") || is.matrix(target_ld_mat))) {
      aligned_ld_mat <- target_ld_mat[keep_idx, keep_idx, drop = FALSE]
    }
  }
  # Attach to the return value for the cached payload.
  if (!is.null(aligned_ld_mat)) {
    aligned$target.ld.mat <- aligned_ld_mat
  }

  preproc_id <- .generate_preprocess_id("preproc_s")
  preproc_path <- file.path(.cache_root_preprocess(),
                             paste0(preproc_id, ".rds"))
  saveRDS(aligned, file = preproc_path)

  # Summary: figure out post-alignment row counts
  aligned_ss <- if (!is.null(aligned$target.ss)) aligned$target.ss
                else if (!is.null(aligned$sumstats)) aligned$sumstats
                else NULL
  n_aligned_out <- if (!is.null(aligned_ss)) nrow(aligned_ss) else NA_integer_

  out <- list(
    status = "ok",
    preprocess_id = preproc_id,
    preprocess_path = preproc_path,
    summary = list(
      n_target_ss_in = n_target_ss_in,
      n_target_ld_in = n_target_ld_in,
      n_external_in_per_source = as.list(n_external_in_per_source),
      n_aligned_out = n_aligned_out,
      n_dropped_ambiguous = if (!is.na(n_aligned_out)) {
        max(0L, n_target_ss_in - n_aligned_out)
      } else NA_integer_,
      M_external = length(external_coefs),
      fit_seconds = round(fit_seconds, 3)
    )
  )

  out$`_notice_next_step` <- paste(
    "Aligned sumstats, LD, and external.coefs are cached at",
    sprintf("'%s'.", preproc_path),
    "To use downstream: in R load with `aligned <- readRDS(",
    sprintf("'%s')`, then save the aligned sumstats /", preproc_path),
    "ld.mat / external.coefs back into a .rds and reference them as",
    "sumstats_expr / XtX_expr (or via ld_id) / beta_external_expr in",
    "brier_s."
  )

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "preprocess_s.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
