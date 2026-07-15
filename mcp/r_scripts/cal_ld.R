#!/usr/bin/env Rscript
# cal_ld.R - build an LD matrix from a genotype reference panel + sumstats.
#
# Called by mcp/server.py as:
#   Rscript cal_ld.R <input.json> <output.json>
#
# Wraps BRIER::calLD(). Returns a CACHE PATH for the resulting LD matrix
# (which can be large for genome-wide data) plus a small metadata summary.
# Returns ld_id parallel to fit_id from the model fitters.
#
# CRITICAL: calLD drops constant-genotype columns. The retained indices
# come back in $nz. The caller MUST subset both sumstats and beta.external
# by $nz before passing to BRIERs(). This is silent-failure trap #X in
# llms.txt. We surface it in a _notice_ and also return $nz so brier_s
# can apply it automatically when given an ld_id.
#
# input.json: {
#   data_path:     "/path/to/data.rds",         # required
#   X_expr:        "Data_BRIERs$target$train$X", # required (reference panel)
#   tau:           0,                            # optional shrinkage (default 0)
#   ldb_expr:      "LDB",                        # optional block coords
#   ldb_path:      "/path/to/Berisa.EUR.hg38.bed", # alternative to ldb_expr
#   snp_info_expr: "snp.info"                   # optional; req'd if ldb given
# }
#
# output.json: {
#   status: "ok",
#   ld_id, ld_path,
#   p_input, p_retained, n_dropped,
#   sparsity, block_count,
#   _notice_subset_required: "..."  # always-on reminder about $nz
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
  library(Matrix)
})


# .normalize_ldb is shared via _common.R (used by cal_ld and prep_auto).


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
  d <- file.path(base, "brier-mcp", "ld")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.generate_ld_id <- function() {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0("ld_", ts, "_", suffix)
}


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$data_paths) && is.null(inp$data_path)) {
    stop("either data_paths or data_path is required", call. = FALSE)
  }
  if (is.null(inp$X_expr)) stop("X_expr is required", call. = FALSE)

  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)
  X <- safe_eval(inp$X_expr, env)

  if (is.null(X)) stop("X_expr resolved to NULL", call. = FALSE)
  if (!is.matrix(X)) X <- as.matrix(X)

  # Build call to BRIER::calLD. Signature: calLD(X, SNP.info, LDB, tau).
  call_args <- list(X = X)
  if (!is.null(inp$tau)) {
    call_args$tau <- as.numeric(inp$tau)
  }

  ldb_obj <- NULL
  if (!is.null(inp$ldb_expr) && nzchar(inp$ldb_expr)) {
    ldb_obj <- safe_eval(inp$ldb_expr, env)
  } else if (!is.null(inp$ldb_path) && nzchar(inp$ldb_path)) {
    if (!file.exists(inp$ldb_path)) {
      stop(sprintf("LDB file not found: %s", inp$ldb_path), call. = FALSE)
    }
    ldb_obj <- utils::read.table(inp$ldb_path, header = TRUE, sep = "\t",
                                  stringsAsFactors = FALSE)
  }

  snp_info <- NULL
  if (!is.null(inp$snp_info_expr) && nzchar(inp$snp_info_expr)) {
    snp_info <- safe_eval(inp$snp_info_expr, env)
  }

  if (!is.null(ldb_obj)) {
    if (is.null(snp_info)) {
      stop(paste(
        "When ldb is provided, snp_info_expr is also required (calLD needs",
        "to map SNPs to LD blocks via SNP.info columns CHR and BP)."
      ), call. = FALSE)
    }
    # BRIER::calLD wants LDB as a NUMERIC matrix (chr, start, stop) with chr an
    # integer (no "chr" prefix). The bundled Berisa .bed (and get_ldb's ldb_path)
    # ships chr as "chr1" strings inside a data.frame, which the calLD C++ rejects
    # ("Not compatible: list -> double"). Normalize whatever we were handed into
    # that shape, and strip the chr prefix so it matches SNP.info$CHR integers.
    call_args$LDB <- .normalize_ldb(ldb_obj)
    call_args$SNP.info <- snp_info
  }

  ld <- do.call(BRIER::calLD, call_args)

  # calLD returns XtX WITHOUT dimnames. Make the LD self-describing by naming its
  # rows/cols with the retained reference-panel variant names ($nz indexes the
  # input columns), so downstream alignment (prep_auto brier_s / brier_s) can
  # match by variant name rather than fragile positional order.
  if (is.null(rownames(ld$XtX)) && !is.null(colnames(X))) {
    retained <- if (!is.null(ld$nz)) ld$nz else seq_len(ncol(ld$XtX))
    vnames <- colnames(X)[retained]
    if (length(vnames) == ncol(ld$XtX)) {
      dimnames(ld$XtX) <- list(vnames, vnames)
    }
  }

  # Persist the LD object (small object with the sparse XtX + nz indices).
  ld_id <- .generate_ld_id()
  ld_path <- file.path(.cache_root(), paste0(ld_id, ".rds"))
  saveRDS(ld, file = ld_path)

  # Summary statistics for the response.
  XtX <- ld$XtX
  p_retained <- ncol(XtX)
  p_input <- ncol(X)
  n_dropped <- p_input - p_retained
  # Sparsity: fraction of off-diagonal entries that are zero.
  if (inherits(XtX, "Matrix")) {
    nnz <- Matrix::nnzero(XtX)
  } else {
    nnz <- sum(XtX != 0)
  }
  total_entries <- as.numeric(p_retained) * as.numeric(p_retained)
  sparsity <- 1 - (nnz / total_entries)

  block_count <- if (!is.null(ld$blk) && length(ld$blk) > 0) {
    length(ld$blk)
  } else {
    NULL
  }

  out <- list(
    status = "ok",
    ld_id = ld_id,
    ld_path = ld_path,
    p_input = p_input,
    p_retained = p_retained,
    n_dropped = n_dropped,
    sparsity = round(sparsity, 4),
    block_count = block_count
  )

  out$`_notice_subset_required` <- paste(
    sprintf(
      "calLD dropped %d constant column(s) out of %d; %d retained. ",
      n_dropped, p_input, p_retained
    ),
    "If you pass the resulting LD matrix to brier_s, the sumstats and",
    "beta.external arguments MUST be subset by the SAME retained indices",
    "($nz, stored alongside the LD matrix at ld_path). brier_s does this",
    "automatically when given ld_id. If you bypass ld_id and pass XtX",
    "directly, do the subset yourself or fits will silently misalign."
  )

  if (n_dropped == 0) {
    # If nothing was dropped, the notice is still useful but downgrade tone.
    out$`_notice_subset_required` <- paste(
      "calLD did not drop any columns (all input variants had non-zero",
      "variance). Subsetting downstream sumstats by $nz is still",
      "recommended as defensive practice."
    )
  }

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "cal_ld.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
