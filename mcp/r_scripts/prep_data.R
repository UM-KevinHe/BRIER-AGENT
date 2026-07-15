#!/usr/bin/env Rscript
# prep_data.R - operation dispatcher for the prep_data tool (v0.13).
#
# State convention: each prep session has a directory
#   ~/.cache/brier-mcp/prep_sessions/<id>/
# containing state.rds, log.jsonl, and any operation outputs.
#
# state.rds holds an R list:
#   $aliases: named list of working objects (data.frames, matrices, ...)
#   $meta:    bookkeeping (paths read, options)
#
# Each operation reads state, mutates aliases, writes state back, returns
# a small JSON summary.
#
# Input JSON (from server.py):
#   {operation, session_id, session_dir, ...op-specific args}
# Output JSON (to server.py):
#   {status, summary, [aliases, ...]}

.script_dir <- (function() {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) > 0) {
    dirname(normalizePath(sub("^--file=", "", file_arg[1])))
  } else { getwd() }
})()
source(file.path(.script_dir, "_common.R"))


# ---- state helpers ---------------------------------------------------------

.state_path <- function(session_dir) file.path(session_dir, "state.rds")

.load_state <- function(session_dir) {
  sp <- .state_path(session_dir)
  if (file.exists(sp)) {
    readRDS(sp)
  } else {
    list(aliases = list(), meta = list(paths = character(0)))
  }
}

.save_state <- function(state, session_dir) {
  saveRDS(state, .state_path(session_dir))
  invisible(NULL)
}

.require_alias <- function(state, name) {
  if (!is.character(name) || length(name) != 1L || !nzchar(name)) {
    stop("alias name must be a non-empty string", call. = FALSE)
  }
  if (is.null(state$aliases[[name]])) {
    stop(sprintf("alias '%s' not found; current aliases: %s",
                 name,
                 paste(names(state$aliases), collapse = ", ")),
         call. = FALSE)
  }
  state$aliases[[name]]
}

.short_class <- function(x) {
  cls <- class(x)
  if (length(cls) > 1L) paste0(cls[1], "(+", length(cls) - 1L, ")")
  else cls[1]
}

.describe_alias <- function(x) {
  if (is.data.frame(x)) {
    list(kind = "data.frame",
         nrow = nrow(x), ncol = ncol(x),
         columns = colnames(x))
  } else if (is.matrix(x) ||
              inherits(x, c("dgCMatrix", "dgRMatrix", "dsCMatrix"))) {
    list(kind = "matrix",
         class = .short_class(x),
         nrow = nrow(x), ncol = ncol(x))
  } else if (is.atomic(x) && is.null(dim(x))) {
    list(kind = "vector",
         class = .short_class(x),
         length = length(x))
  } else if (is.list(x)) {
    list(kind = "list",
         names = names(x), length = length(x))
  } else {
    list(kind = "other", class = .short_class(x))
  }
}


# ---- op: alias_root --------------------------------------------------------
# Load a data file (.rds/.rda/.RData). For .rds files the contents are
# placed under the basename (matching load_data_files convention). For
# .rda/.RData files, each top-level name in the file becomes a separate
# alias (so the user can refer to multiple loaded objects).

.op_alias_root <- function(state, inp, session_dir) {
  if (is.null(inp$data_path) || !nzchar(inp$data_path)) {
    stop("alias_root requires data_path", call. = FALSE)
  }
  if (!file.exists(inp$data_path)) {
    stop(sprintf("data_path not found: %s", inp$data_path),
         call. = FALSE)
  }
  ext <- tolower(tools::file_ext(inp$data_path))
  added <- character(0)
  if (ext == "rds") {
    basename_no_ext <- tools::file_path_sans_ext(basename(inp$data_path))
    alias_name <- if (!is.null(inp$alias) && nzchar(inp$alias))
                     inp$alias else basename_no_ext
    state$aliases[[alias_name]] <- readRDS(inp$data_path)
    added <- alias_name
  } else if (ext %in% c("rda", "rdata")) {
    tmp_env <- new.env()
    load(inp$data_path, envir = tmp_env)
    for (nm in ls(tmp_env)) {
      state$aliases[[nm]] <- get(nm, envir = tmp_env)
      added <- c(added, nm)
    }
  } else {
    stop(sprintf("unsupported file extension '.%s' (need .rds/.rda/.RData)",
                 ext),
         call. = FALSE)
  }
  state$meta$paths <- unique(c(state$meta$paths, inp$data_path))
  list(
    state = state,
    summary = list(
      aliases_added = added,
      aliases = lapply(state$aliases[added], .describe_alias)
    )
  )
}


# ---- op: rename_columns ----------------------------------------------------
# Rename columns in a data.frame alias. Mapping is a named list:
#   {"old_name1": "new_name1", "old_name2": "new_name2"}

.op_rename_columns <- function(state, inp, session_dir) {
  if (is.null(inp$alias)) stop("rename_columns requires alias", call. = FALSE)
  if (is.null(inp$mapping) || !is.list(inp$mapping) ||
      length(inp$mapping) == 0L) {
    stop("rename_columns requires a non-empty mapping", call. = FALSE)
  }
  obj <- .require_alias(state, inp$alias)
  if (!is.data.frame(obj)) {
    stop(sprintf("alias '%s' is not a data.frame", inp$alias),
         call. = FALSE)
  }
  renamed <- list()
  not_found <- character(0)
  cn <- colnames(obj)
  for (old_name in names(inp$mapping)) {
    new_name <- inp$mapping[[old_name]]
    if (!is.character(new_name) || length(new_name) != 1L) {
      stop(sprintf("mapping for '%s' must be a single string", old_name),
           call. = FALSE)
    }
    idx <- which(cn == old_name)
    if (length(idx) == 0L) {
      not_found <- c(not_found, old_name)
    } else {
      cn[idx] <- new_name
      renamed[[old_name]] <- new_name
    }
  }
  colnames(obj) <- cn
  state$aliases[[inp$alias]] <- obj
  list(
    state = state,
    summary = list(
      alias = inp$alias,
      renamed = renamed,
      not_found = not_found,
      columns_after = cn
    )
  )
}


# ---- op: derive_corr_from_pvalue -------------------------------------------
# Given GWAS sumstats with p-value, sample size, and a signed effect,
# derive a `corr` column.
# Formula: t = sign(beta) * qt(p/2, df = n-2, lower.tail = FALSE)
#          r = t / sqrt(t^2 + n - 2)
# Speculative defaults:
#   - p-values are TWO-SIDED (override with pvalue_sided = "one")
#   - sign is taken from the `beta` column

.op_derive_corr_from_pvalue <- function(state, inp, session_dir) {
  if (is.null(inp$alias)) {
    stop("derive_corr_from_pvalue requires alias", call. = FALSE)
  }
  obj <- .require_alias(state, inp$alias)
  if (!is.data.frame(obj)) {
    stop(sprintf("alias '%s' is not a data.frame", inp$alias),
         call. = FALSE)
  }
  pcol <- if (!is.null(inp$pvalue_col)) inp$pvalue_col else "pval"
  ncol_name <- if (!is.null(inp$n_col)) inp$n_col else "n"
  bcol <- if (!is.null(inp$beta_col)) inp$beta_col else "beta"
  out_col <- if (!is.null(inp$output_col)) inp$output_col else "corr"
  sided <- if (!is.null(inp$pvalue_sided)) inp$pvalue_sided else "two"

  for (req in c(pcol, ncol_name, bcol)) {
    if (!(req %in% colnames(obj))) {
      stop(sprintf("column '%s' not found in alias '%s'", req, inp$alias),
           call. = FALSE)
    }
  }

  p <- as.numeric(obj[[pcol]])
  n <- as.numeric(obj[[ncol_name]])
  beta <- as.numeric(obj[[bcol]])
  if (sided == "one") {
    # one-sided p assumes sign already encoded; convert to two-sided-like
    tail_p <- p
  } else if (sided == "two") {
    tail_p <- p / 2
  } else {
    stop("pvalue_sided must be 'one' or 'two'", call. = FALSE)
  }
  t_stat <- sign(beta) * qt(tail_p, df = n - 2, lower.tail = FALSE)
  r <- t_stat / sqrt(t_stat^2 + (n - 2))
  obj[[out_col]] <- r
  state$aliases[[inp$alias]] <- obj

  list(
    state = state,
    summary = list(
      alias = inp$alias,
      output_col = out_col,
      pvalue_sided = sided,
      n_rows = nrow(obj),
      n_finite = sum(is.finite(r)),
      n_nonfinite = sum(!is.finite(r)),
      r_summary = list(
        min = if (any(is.finite(r))) min(r[is.finite(r)]) else NA_real_,
        max = if (any(is.finite(r))) max(r[is.finite(r)]) else NA_real_,
        mean_abs = if (any(is.finite(r))) mean(abs(r[is.finite(r)]))
                    else NA_real_
      )
    )
  )
}


# ---- op: reshape_to_matrix -------------------------------------------------
# Convert a long-format data.frame (one row per variant) into a single-
# column matrix keyed by variant id. Used to coerce external coefficient
# tables into the matrix form BRIERi/BRIERfull/BRIERs expect.

.op_reshape_to_matrix <- function(state, inp, session_dir) {
  if (is.null(inp$alias)) {
    stop("reshape_to_matrix requires alias", call. = FALSE)
  }
  obj <- .require_alias(state, inp$alias)
  if (!is.data.frame(obj)) {
    stop(sprintf("alias '%s' is not a data.frame", inp$alias),
         call. = FALSE)
  }
  value_col <- if (!is.null(inp$value_col)) inp$value_col else "coef"
  id_col <- if (!is.null(inp$id_col)) inp$id_col else "variable"
  out_alias <- if (!is.null(inp$out_alias)) inp$out_alias
               else paste0(inp$alias, "_matrix")
  if (!(value_col %in% colnames(obj))) {
    stop(sprintf("value_col '%s' not found in alias '%s'",
                 value_col, inp$alias), call. = FALSE)
  }
  if (!(id_col %in% colnames(obj))) {
    stop(sprintf("id_col '%s' not found in alias '%s'",
                 id_col, inp$alias), call. = FALSE)
  }
  mat <- matrix(as.numeric(obj[[value_col]]), ncol = 1L)
  rownames(mat) <- as.character(obj[[id_col]])
  colnames(mat) <- value_col
  state$aliases[[out_alias]] <- mat

  list(
    state = state,
    summary = list(
      alias_in = inp$alias,
      alias_out = out_alias,
      nrow = nrow(mat),
      ncol = ncol(mat)
    )
  )
}


# ---- op: subset_to_common_snps --------------------------------------------
# Take a list of alias names, find the SNPs present in ALL of them, and
# subset each to that common set in matched order.
# Speculative defaults:
#   - match on (CHR, BP) if both columns present in all, else on `id_col`
#     (default "rsid" or "variable")
#   - For matrix aliases, match by rowname; for data.frames, match on the
#     id column.

.op_subset_to_common_snps <- function(state, inp, session_dir) {
  if (is.null(inp$aliases) || length(inp$aliases) < 2L) {
    stop("subset_to_common_snps requires `aliases` with >= 2 names",
         call. = FALSE)
  }
  id_col <- if (!is.null(inp$id_col)) inp$id_col else "rsid"

  # Collect the SNP ids from each alias
  id_sets <- list()
  for (a in inp$aliases) {
    obj <- .require_alias(state, a)
    if (is.data.frame(obj)) {
      if (id_col %in% colnames(obj)) {
        ids <- as.character(obj[[id_col]])
      } else if ("variable" %in% colnames(obj)) {
        ids <- as.character(obj[["variable"]])
      } else {
        stop(sprintf("data.frame alias '%s' missing id column '%s' (and no fallback 'variable')",
                     a, id_col), call. = FALSE)
      }
    } else if (is.matrix(obj) || inherits(obj, "dgCMatrix")) {
      if (is.null(rownames(obj))) {
        stop(sprintf("matrix alias '%s' has no rownames to match",
                     a), call. = FALSE)
      }
      ids <- rownames(obj)
    } else {
      stop(sprintf("alias '%s' is neither data.frame nor matrix", a),
           call. = FALSE)
    }
    id_sets[[a]] <- ids
  }

  common <- Reduce(intersect, id_sets)
  if (length(common) == 0L) {
    stop("no SNPs in common across the provided aliases", call. = FALSE)
  }

  # Subset each alias to `common` (in `common` order)
  n_in_per <- list()
  for (a in inp$aliases) {
    obj <- state$aliases[[a]]
    n_in_per[[a]] <- if (is.data.frame(obj)) nrow(obj) else nrow(obj)
    if (is.data.frame(obj)) {
      if (id_col %in% colnames(obj)) {
        m <- match(common, as.character(obj[[id_col]]))
      } else {
        m <- match(common, as.character(obj[["variable"]]))
      }
      state$aliases[[a]] <- obj[m, , drop = FALSE]
      rownames(state$aliases[[a]]) <- NULL
    } else {
      m <- match(common, rownames(obj))
      state$aliases[[a]] <- obj[m, , drop = FALSE]
    }
  }

  list(
    state = state,
    summary = list(
      aliases = inp$aliases,
      id_col_used = id_col,
      n_in_per_alias = n_in_per,
      n_common_out = length(common)
    )
  )
}


# ---- op: harmonize_alleles -------------------------------------------------
# For pairs of aliases (target, external), find SNPs where the external
# A1/A2 is swapped relative to target; flip the sign on the external's
# coefficient column. Strand-ambiguous SNPs (A/T, C/G) are dropped by
# default - this matches BRIER::preprocessI/S behavior.
# Highly speculative defaults: behavior on mismatched alleles that AREN'T
# a clean swap (e.g. A vs G) is "warn and drop." Override with
# `drop_mismatched=FALSE` to keep them with NA sign.

.op_harmonize_alleles <- function(state, inp, session_dir) {
  if (is.null(inp$target_alias) || is.null(inp$external_alias)) {
    stop("harmonize_alleles requires target_alias and external_alias",
         call. = FALSE)
  }
  drop_ambig <- if (!is.null(inp$drop_strand_ambiguous))
                   isTRUE(inp$drop_strand_ambiguous) else TRUE
  drop_mismatch <- if (!is.null(inp$drop_mismatched))
                      isTRUE(inp$drop_mismatched) else TRUE
  a1_col <- if (!is.null(inp$a1_col)) inp$a1_col else "A1"
  a2_col <- if (!is.null(inp$a2_col)) inp$a2_col else "A2"
  coef_col <- if (!is.null(inp$coef_col)) inp$coef_col else "coef"
  id_col <- if (!is.null(inp$id_col)) inp$id_col else "rsid"

  tgt <- .require_alias(state, inp$target_alias)
  ext <- .require_alias(state, inp$external_alias)
  if (!is.data.frame(tgt) || !is.data.frame(ext)) {
    stop("harmonize_alleles requires data.frame aliases", call. = FALSE)
  }
  for (cn in c(id_col, a1_col, a2_col)) {
    if (!(cn %in% colnames(tgt))) {
      stop(sprintf("target_alias '%s' missing column '%s'",
                   inp$target_alias, cn), call. = FALSE)
    }
    if (!(cn %in% colnames(ext))) {
      stop(sprintf("external_alias '%s' missing column '%s'",
                   inp$external_alias, cn), call. = FALSE)
    }
  }
  if (!(coef_col %in% colnames(ext))) {
    stop(sprintf("external_alias '%s' missing coefficient column '%s'",
                 inp$external_alias, coef_col), call. = FALSE)
  }

  # Match by id
  m <- match(as.character(ext[[id_col]]), as.character(tgt[[id_col]]))
  keep <- !is.na(m)
  ext <- ext[keep, , drop = FALSE]
  m <- m[keep]
  tgt_aligned <- tgt[m, , drop = FALSE]

  # Categorize each row
  t1 <- toupper(as.character(tgt_aligned[[a1_col]]))
  t2 <- toupper(as.character(tgt_aligned[[a2_col]]))
  e1 <- toupper(as.character(ext[[a1_col]]))
  e2 <- toupper(as.character(ext[[a2_col]]))

  comp <- function(a) {
    out <- a
    out[a == "A"] <- "T"
    out[a == "T"] <- "A"
    out[a == "C"] <- "G"
    out[a == "G"] <- "C"
    out
  }

  ambig <- (t1 == "A" & t2 == "T") | (t1 == "T" & t2 == "A") |
            (t1 == "C" & t2 == "G") | (t1 == "G" & t2 == "C")

  same <- (t1 == e1 & t2 == e2)
  swap <- (t1 == e2 & t2 == e1)
  same_strand_flip <- (t1 == comp(e1) & t2 == comp(e2))
  swap_strand_flip <- (t1 == comp(e2) & t2 == comp(e1))

  matched <- same | swap | same_strand_flip | swap_strand_flip
  mismatched <- !matched

  n_in <- nrow(ext)
  n_ambig <- sum(ambig)
  n_same <- sum(same & !ambig)
  n_swap <- sum(swap & !ambig)
  n_strand_same <- sum(same_strand_flip & !ambig & !same)
  n_strand_swap <- sum(swap_strand_flip & !ambig & !swap)
  n_mismatch <- sum(mismatched & !ambig)

  drop_mask <- logical(n_in)
  if (drop_ambig) drop_mask <- drop_mask | ambig
  if (drop_mismatch) drop_mask <- drop_mask | mismatched

  # Flip coefficients where the allele swap requires it.
  # Same orientation -> no flip. Swap -> flip. Strand-flip (same/swap) ->
  # follow the swap state; strand only matters when keeping ambiguous.
  flip <- (swap | swap_strand_flip) & !drop_mask
  ext_coef <- as.numeric(ext[[coef_col]])
  ext_coef[flip] <- -ext_coef[flip]
  ext[[coef_col]] <- ext_coef

  ext <- ext[!drop_mask, , drop = FALSE]
  state$aliases[[inp$external_alias]] <- ext

  notice <- NULL
  if (sum(drop_mask) > 0) {
    notice <- sprintf(
      "harmonize_alleles dropped %d row(s) from '%s'. The external alias is now smaller than the target; call subset_to_common_snps with both aliases again to re-align before verify_aligned / assemble.",
      sum(drop_mask), inp$external_alias
    )
  }

  list(
    state = state,
    summary = list(
      target_alias = inp$target_alias,
      external_alias = inp$external_alias,
      n_in = n_in,
      n_dropped_ambiguous = if (drop_ambig) n_ambig else 0L,
      n_dropped_mismatched = if (drop_mismatch) n_mismatch else 0L,
      n_same_orientation = n_same,
      n_swap_flipped = n_swap,
      n_strand_same = n_strand_same,
      n_strand_swap_flipped = n_strand_swap,
      n_kept = nrow(ext),
      drop_strand_ambiguous = drop_ambig,
      drop_mismatched = drop_mismatch,
      `_notice_post_harmonize` = notice
    )
  )
}


# ---- op: verify_aligned ----------------------------------------------------
# Check that named aliases have the same SNP set in the same order.
# Reports n per alias, n in common, whether order matches, and the first
# few mismatches if any.

.op_verify_aligned <- function(state, inp, session_dir) {
  if (is.null(inp$aliases) || length(inp$aliases) < 2L) {
    stop("verify_aligned requires `aliases` with >= 2 names", call. = FALSE)
  }
  id_col <- if (!is.null(inp$id_col)) inp$id_col else "rsid"

  id_lists <- list()
  for (a in inp$aliases) {
    obj <- .require_alias(state, a)
    if (is.data.frame(obj)) {
      if (id_col %in% colnames(obj)) {
        ids <- as.character(obj[[id_col]])
      } else if ("variable" %in% colnames(obj)) {
        ids <- as.character(obj[["variable"]])
      } else {
        stop(sprintf("alias '%s' has no id column", a), call. = FALSE)
      }
    } else if (is.matrix(obj) || inherits(obj, "dgCMatrix")) {
      ids <- rownames(obj)
    } else {
      stop(sprintf("alias '%s' kind not supported", a), call. = FALSE)
    }
    id_lists[[a]] <- ids
  }

  lens <- vapply(id_lists, length, integer(1))
  same_length <- length(unique(lens)) == 1L
  first <- id_lists[[1]]
  same_order <- same_length &&
                  all(vapply(id_lists, function(x) identical(x, first),
                              logical(1)))
  n_common <- length(Reduce(intersect, id_lists))

  # If not same order, find a sample of differences
  diff_examples <- list()
  if (!same_order) {
    for (a in inp$aliases[-1]) {
      if (length(id_lists[[a]]) == length(first)) {
        diff_idx <- which(id_lists[[a]] != first)
        if (length(diff_idx) > 0) {
          take <- head(diff_idx, 5)
          diff_examples[[a]] <- list(
            n_disagree = length(diff_idx),
            example_positions = take,
            target_examples = first[take],
            this_examples = id_lists[[a]][take]
          )
        }
      } else {
        diff_examples[[a]] <- list(
          n_disagree = NA_integer_,
          note = sprintf("length differs (%d vs target %d)",
                          length(id_lists[[a]]), length(first))
        )
      }
    }
  }

  list(
    state = state,
    summary = list(
      aliases = inp$aliases,
      n_per_alias = as.list(lens),
      same_length = same_length,
      same_order = same_order,
      n_in_common = n_common,
      differences = diff_examples
    )
  )
}


# ---- op: assemble ----------------------------------------------------------
# Bundle named aliases into a single list (or named environment-like
# structure) so they can be passed to a fit tool together.

.op_assemble <- function(state, inp, session_dir) {
  if (is.null(inp$bundle) || !is.list(inp$bundle) ||
      length(inp$bundle) == 0L) {
    stop("assemble requires a non-empty `bundle` mapping output names to aliases",
         call. = FALSE)
  }
  out_alias <- if (!is.null(inp$out_alias)) inp$out_alias else "assembled"
  result <- list()
  for (key in names(inp$bundle)) {
    a <- inp$bundle[[key]]
    if (!is.character(a) || length(a) != 1L) {
      stop(sprintf("bundle value for '%s' must be a single alias name", key),
           call. = FALSE)
    }
    result[[key]] <- .require_alias(state, a)
  }
  state$aliases[[out_alias]] <- result
  list(
    state = state,
    summary = list(
      out_alias = out_alias,
      bundle_keys = names(inp$bundle),
      sources = inp$bundle
    )
  )
}


# ---- op: persist -----------------------------------------------------------
# Save the prepared state to a .rds at output_path, with the prep session
# id embedded so fit tools can attach prep history.

.op_persist <- function(state, inp, session_dir) {
  if (is.null(inp$output_path) || !nzchar(inp$output_path)) {
    stop("persist requires output_path", call. = FALSE)
  }
  out_dir <- dirname(inp$output_path)
  if (nzchar(out_dir) && !dir.exists(out_dir)) {
    dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
  }
  what <- if (!is.null(inp$alias) && nzchar(inp$alias)) {
    obj <- .require_alias(state, inp$alias)
    obj
  } else {
    state$aliases
  }
  # Attach prep_session_id so the fit cache can pick up the audit trail.
  prep_meta <- list(prep_session_id = inp$session_id,
                     persisted_at = format(Sys.time(),
                                            "%Y-%m-%d %H:%M:%S"))
  if (is.list(what) && !is.data.frame(what)) {
    what$.prep_meta <- prep_meta
  }
  saveRDS(what, inp$output_path)

  # Emit the SAME fit-ready contract as prep_auto so a fallback prep route
  # (prep_data ops -> assemble bundle -> persist) hands the downstream fitter
  # an interchangeable artifact: prepared_path (to pass as data_path) plus
  # expr_hints keyed to the variable the fitter's loader binds. load_data_files
  # binds an .rds under a variable named after the FILE BASENAME, so the hints
  # reference that (matching the prep_auto convention). Hints are emitted only
  # for recognized fit-ready keys present in the persisted bundle; if the
  # object is not a keyed bundle, expr_hints is empty and the caller supplies
  # the expressions itself.
  obj_var <- tools::file_path_sans_ext(basename(inp$output_path))
  known_keys <- c("X", "y", "beta_external", "sumstats", "XtX", "cohort",
                  "snp_info", "X_val", "y_val", "X_test", "y_test")
  expr_hints <- list()
  if (is.list(what) && !is.data.frame(what)) {
    for (k in intersect(known_keys, names(what))) {
      expr_hints[[paste0(k, "_expr")]] <- paste0(obj_var, "$", k)
    }
  }

  list(
    state = state,
    summary = list(
      output_path = inp$output_path,
      alias_persisted = inp$alias,
      session_id = inp$session_id,
      bytes = file.info(inp$output_path)$size,
      prepared_path = inp$output_path,
      expr_hints = expr_hints
    )
  )
}


# ---- dispatcher ------------------------------------------------------------

args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input
  op <- inp$operation
  if (is.null(op) || !nzchar(op)) {
    stop("operation is required", call. = FALSE)
  }
  session_dir <- inp$session_dir
  if (is.null(session_dir) || !nzchar(session_dir)) {
    stop("session_dir is required", call. = FALSE)
  }
  if (!dir.exists(session_dir)) {
    dir.create(session_dir, recursive = TRUE, showWarnings = FALSE)
  }
  state <- .load_state(session_dir)

  dispatch <- list(
    alias_root = .op_alias_root,
    rename_columns = .op_rename_columns,
    derive_corr_from_pvalue = .op_derive_corr_from_pvalue,
    reshape_to_matrix = .op_reshape_to_matrix,
    subset_to_common_snps = .op_subset_to_common_snps,
    harmonize_alleles = .op_harmonize_alleles,
    verify_aligned = .op_verify_aligned,
    assemble = .op_assemble,
    persist = .op_persist
  )
  if (is.null(dispatch[[op]])) {
    stop(sprintf("unknown operation '%s'; valid: %s",
                 op,
                 paste(names(dispatch), collapse = ", ")),
         call. = FALSE)
  }

  res <- dispatch[[op]](state, inp, session_dir)
  .save_state(res$state, session_dir)

  # Describe current alias bench (compact form)
  alias_desc <- lapply(res$state$aliases, .describe_alias)

  out <- list(
    status = "ok",
    operation = op,
    session_id = inp$session_id,
    summary = res$summary,
    aliases = alias_desc
  )
  # Surface the fit-ready contract at TOP LEVEL for persist (matching
  # prep_auto: result$prepared_path, result$expr_hints) so the fallback prep
  # route is a drop-in for prep_auto from the downstream fitter's view.
  if (!is.null(res$summary$prepared_path)) {
    out$prepared_path <- res$summary$prepared_path
  }
  if (!is.null(res$summary$expr_hints) && length(res$summary$expr_hints) > 0) {
    out$expr_hints <- res$summary$expr_hints
  }
  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "prep_data.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
