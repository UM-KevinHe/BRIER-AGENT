#!/usr/bin/env Rscript
# _common.R - shared utilities sourced by every BRIER MCP dispatcher script.
#
# What lives here:
#   * read_input(args)        - parse argv positions [1] (input.json path)
#                               and [2] (output.json path), load input JSON.
#   * write_output(result, p) - serialize a result list to output JSON
#                               with consistent options across all scripts.
#   * load_data_file(path)    - load .rda / .RData / .rds into a fresh env;
#                               returns the env (use `as.list(env)` to peek).
#   * safe_eval(expr_str, env)- eval an R expression string from MCP input,
#                               with belt-and-suspenders deny-list check
#                               matching server.py's pre-flight filter.
#   * make_error(msg, where, class)
#                             - construct a standard error payload.
#
# What does NOT live here:
#   * Statistical logic. Each dispatcher calls BRIER:: directly.
#   * Tool-specific shaping. Each dispatcher returns its own result list.

suppressPackageStartupMessages({
  library(jsonlite)
})


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

read_input <- function(args) {
  if (length(args) < 2) {
    stop(
      "Usage: Rscript <script>.R <input.json> <output.json>",
      call. = FALSE
    )
  }
  list(
    input  = fromJSON(args[1], simplifyVector = FALSE),
    output_path = args[2]
  )
}

write_output <- function(result, path) {
  # Consistent JSON output across all scripts:
  #   * auto_unbox: scalars come out as JSON scalars, not 1-element arrays.
  #   * matrix = "rowmajor": match jsonlite default for matrices.
  #   * na, null: explicit "null" for missing values.
  #   * pretty: readable in audit logs and easier to diff in tests.
  writeLines(
    toJSON(
      result,
      auto_unbox = TRUE,
      matrix = "rowmajor",
      na = "null",
      null = "null",
      pretty = TRUE
    ),
    con = path
  )
}


# Tolerant boolean coercion. The MCP server has typed signatures, so a small
# model's string "TRUE"/"FALSE" is coerced to a real logical BEFORE the R
# script runs. But direct-to-R callers (the auto-generated reproduce_*.R, which
# replays the recorded raw args, and any human running a script by hand) bypass
# that coercion, so a string "TRUE" reaches the script and isTRUE("TRUE") is
# FALSE -- silently flipping e.g. persist off. Coerce string/numeric forms here.
# Returns `default` for NULL / empty / unrecognized so callers keep their own
# default semantics.
as_bool <- function(x, default = FALSE) {
  if (is.null(x) || length(x) != 1L || (is.character(x) && !nzchar(x)) || is.na(x)) {
    return(default)
  }
  if (is.logical(x)) return(isTRUE(x))
  if (is.numeric(x)) return(x != 0)
  s <- tolower(trimws(as.character(x)))
  if (s %in% c("true", "t", "1", "yes", "y")) return(TRUE)
  if (s %in% c("false", "f", "0", "no", "n")) return(FALSE)
  default
}


# --------------------------------------------------------------------------
# Data file loading
# --------------------------------------------------------------------------

load_data_file <- function(path) {
  # Returns a fresh environment containing the loaded object(s).
  # For .rda / .RData: multi-object load() into env.
  # For .rds: single-object readRDS, named after the file basename.
  if (is.null(path) || !nzchar(path)) {
    stop("data_path is required", call. = FALSE)
  }
  if (!file.exists(path)) {
    stop(sprintf("File not found: %s", path), call. = FALSE)
  }

  ext <- tolower(tools::file_ext(path))
  e <- new.env()

  if (ext %in% c("rda", "rdata")) {
    load(path, envir = e)
  } else if (ext == "rds") {
    obj_name <- tools::file_path_sans_ext(basename(path))
    assign(obj_name, readRDS(path), envir = e)
  } else {
    hint <- if (grepl("\\.(csv|tsv|txt)(\\.(gz|bgz))?$", path, ignore.case = TRUE) ||
                grepl("\\.(gz|bgz)$", path, ignore.case = TRUE)) {
      " This looks like a text/tabular file; use inspect_user_data instead, which handles .csv/.tsv/.txt and their .gz forms."
    } else {
      ""
    }
    stop(
      sprintf(
        "Unsupported file extension: .%s (this tool loads only .rda .RData .rds).%s",
        ext, hint
      ),
      call. = FALSE
    )
  }

  e
}


# Load a genotype BINARY panel into a numeric matrix (samples x variants) with
# variant IDs as column names, so it can feed cal_ld / the fitters as a raw X.
# PLINK1 .bed needs .bim + .fam; PLINK2 .pgen needs .pvar + .psam. Reading needs
# a package (genio or BEDMatrix for .bed, pgenlibr for .pgen); if none is present
# the error names what to install. BGEN is not loaded (convert to PLINK/text).
.read_genotype_binary <- function(path, ext) {
  base <- tools::file_path_sans_ext(path)
  need_companions <- function(exts) {
    for (c in exts) {
      if (!file.exists(paste0(base, c))) {
        stop(sprintf("%s needs its %s companion (missing: %s%s).",
                     toupper(ext), c, base, c), call. = FALSE)
      }
    }
  }
  if (ext == "bed") {
    need_companions(c(".bim", ".fam"))
    if (requireNamespace("genio", quietly = TRUE)) {
      pl <- genio::read_plink(base, verbose = FALSE)
      M <- t(pl$X)                                  # genio X is loci x indiv
      if (!is.null(pl$bim$id)) colnames(M) <- pl$bim$id
      if (!is.null(pl$fam$id)) rownames(M) <- pl$fam$id
      return(M)
    }
    if (requireNamespace("BEDMatrix", quietly = TRUE)) {
      return(as.matrix(BEDMatrix::BEDMatrix(path, simple_names = TRUE)))
    }
    stop(paste("Reading PLINK1 .bed requires the 'genio' or 'BEDMatrix' R",
               "package; install one to enable .bed input."), call. = FALSE)
  } else if (ext == "pgen") {
    need_companions(c(".pvar", ".psam"))
    if (!requireNamespace("pgenlibr", quietly = TRUE)) {
      stop(paste("Reading PLINK2 .pgen requires the 'pgenlibr' R package;",
                 "install it to enable .pgen input."), call. = FALSE)
    }
    pvar <- pgenlibr::NewPvar(paste0(base, ".pvar"))
    pgen <- pgenlibr::NewPgen(path, pvar = pvar)
    nv <- pgenlibr::GetVariantCt(pgen)
    M <- pgenlibr::ReadList(pgen, seq_len(nv), meanimpute = FALSE)
    ids <- tryCatch(
      vapply(seq_len(nv), function(i) pgenlibr::GetVariantId(pvar, i),
             character(1)),
      error = function(e) NULL)
    if (!is.null(ids) && length(ids) == ncol(M)) colnames(M) <- ids
    pgenlibr::ClosePgen(pgen)
    pgenlibr::ClosePvar(pvar)
    return(M)
  } else if (ext == "bgen") {
    stop(paste("BGEN genotype loading is not supported; convert to PLINK",
               "(.bed/.pgen) or a tabular (.txt/.csv[.gz]) matrix."),
         call. = FALSE)
  }
  stop(sprintf("Unsupported genotype-binary extension: .%s", ext), call. = FALSE)
}


# Coerce an LD-block spec into the NUMERIC matrix (chr, start, stop) that
# BRIER::calLD expects. Accepts a data.frame / matrix with chr/start/stop (or
# their positional equivalents) and strips any "chr" prefix so the block chr
# matches SNP.info$CHR integers. A numeric matrix is passed through unchanged.
# Shared by cal_ld and prep_auto (both build an LD from a reference panel).
.normalize_ldb <- function(x) {
  if (is.matrix(x) && is.numeric(x)) {
    return(x)
  }
  x <- as.data.frame(x, stringsAsFactors = FALSE)
  names(x) <- trimws(tolower(names(x)))
  pick <- function(nm, pos) if (nm %in% names(x)) x[[nm]] else x[[pos]]
  chr_col <- pick("chr", 1L)
  start_col <- pick("start", 2L)
  stop_col <- if ("stop" %in% names(x)) x[["stop"]] else
              if ("end" %in% names(x)) x[["end"]] else x[[3L]]
  as.matrix(data.frame(
    chr = as.integer(sub("^chr", "", trimws(as.character(chr_col)),
                         ignore.case = TRUE)),
    start = as.numeric(trimws(as.character(start_col))),
    stop = as.numeric(trimws(as.character(stop_col)))
  ))
}


load_data_files <- function(paths) {
  # v0.11: multi-file loading with basename wrapping.
  #
  # Each file's contents are wrapped under a top-level variable named
  # after the file basename, regardless of the file format. This makes
  # expressions like "height_AFR$sumstats" resolve consistently whether
  # the source was .rds or .RData.
  #
  # Behavior:
  # - For .rds: the readRDS() result is assigned to a variable named
  #   after the file basename.
  # - For .RData / .rda with one internal variable: that variable's
  #   value is assigned to a variable named after the file basename
  #   (whatever the internal name was, it gets renamed).
  # - For .RData / .rda with multiple internal variables: a list of
  #   all variables is assigned to a variable named after the file
  #   basename. Each can be accessed as basename$internal_var_name.
  #
  # This is a backward-incompatible change for .RData files: previously
  # load() exposed internal variable names directly. Now they live
  # under the file basename. This was a deliberate trade for predictable
  # naming across file formats. The single-file load_data_file() is
  # retained for use by code that explicitly wants the legacy behavior.
  if (length(paths) == 0L) {
    stop("data_paths must contain at least one path", call. = FALSE)
  }

  e <- new.env()

  # Determine if this is single-file legacy mode (one .RData/.rda path).
  # In that case we ALSO expose the internal variables at the top level
  # so pre-v0.11 expressions like "X" / "y" / "beta.external" still
  # resolve. The basename-wrapped name is also assigned, so new code
  # that uses "basename$X" continues to work too.
  single_legacy_rdata <- (length(paths) == 1L &&
                          tolower(tools::file_ext(paths[1])) %in%
                            c("rda", "rdata"))

  for (path in paths) {
    if (is.null(path) || !nzchar(path)) {
      stop("encountered an empty path in data_paths", call. = FALSE)
    }
    if (!file.exists(path)) {
      stop(sprintf("File not found: %s", path), call. = FALSE)
    }
    ext <- tolower(tools::file_ext(path))
    basename_var <- tools::file_path_sans_ext(basename(path))

    if (ext == "rds") {
      assign(basename_var, readRDS(path), envir = e)
    } else if (ext %in% c("rda", "rdata")) {
      tmp <- new.env()
      load(path, envir = tmp)
      vars <- ls(tmp)
      if (length(vars) == 1L) {
        # Single internal variable -> alias under basename
        assign(basename_var, get(vars[1], envir = tmp), envir = e)
        if (single_legacy_rdata) {
          # Backward compat: also expose the internal variable at the
          # top level under its original name.
          assign(vars[1], get(vars[1], envir = tmp), envir = e)
        }
      } else {
        # Multiple internal variables -> wrap as list under basename
        assign(basename_var, mget(vars, envir = tmp), envir = e)
        if (single_legacy_rdata) {
          # Backward compat: ALSO expose each variable at the top level.
          for (v in vars) {
            assign(v, get(v, envir = tmp), envir = e)
          }
        }
      }
    } else if (ext %in% c("bed", "pgen", "bgen")) {
      # Genotype BINARY formats (PLINK1 .bed + .bim/.fam, PLINK2 .pgen +
      # .pvar/.psam). Loaded into a numeric matrix (samples x variants, variant
      # colnames) so a raw reference panel can feed cal_ld / the fitters. Needs a
      # reader package (genio or BEDMatrix for .bed, pgenlibr for .pgen); a clear
      # error names the package if it is absent. Pass ONLY the .bed / .pgen path
      # (the companions are found by basename), not the .bim/.pvar separately.
      assign(basename_var, .read_genotype_binary(path, ext), envir = e)
    } else {
      # Text/tabular files (.csv/.tsv/.txt/.tab/.dat) including gzipped
      # (.gz/.bgz) forms: read into a data.frame bound under the
      # extension-stripped basename, so tools that consume a RAW reference
      # panel (e.g. cal_ld building an LD from genotypes) can read the
      # benchmark's .txt.gz files. The .rds/.rda path above is unchanged; the
      # fitters that load a prep_auto .rds never reach this branch.
      bare <- sub("\\.(gz|bgz)$", "", path, ignore.case = TRUE)
      text_ext <- tolower(tools::file_ext(bare))
      if (text_ext %in% c("csv", "tsv", "txt", "tab", "dat")) {
        basename_var <- tools::file_path_sans_ext(basename(bare))
        sep <- if (text_ext == "csv") "," else "\t"
        df <- if (requireNamespace("data.table", quietly = TRUE)) {
          as.data.frame(
            data.table::fread(path, sep = sep, header = TRUE,
                              data.table = FALSE, check.names = FALSE)
          )
        } else {
          # base-R read.table auto-decompresses .gz via the file connection.
          utils::read.table(path, header = TRUE, sep = sep,
                            check.names = FALSE, stringsAsFactors = FALSE)
        }
        assign(basename_var, df, envir = e)
      } else {
        stop(sprintf("Unsupported file extension: .%s", ext), call. = FALSE)
      }
    }
  }

  e
}


resolve_data_paths_input <- function(inp) {
  # Backward-compat shim. Accept either `data_paths` (preferred, list)
  # or `data_path` (legacy, single string) from the input JSON.
  # Returns a character vector of paths.
  if (!is.null(inp$data_paths) && length(inp$data_paths) > 0L) {
    return(as.character(inp$data_paths))
  }
  if (!is.null(inp$data_path) && nzchar(inp$data_path)) {
    return(as.character(inp$data_path))
  }
  stop("either data_paths or data_path is required", call. = FALSE)
}


extract_prep_session_ids <- function(paths) {
  # v0.13: when a fit reads a .rds that was written by prep_data's
  # `persist` op, the file contains a `.prep_meta` list with the prep
  # session id. Scan every input path and collect any prep_session_ids
  # found. Returns a character vector (possibly empty).
  ids <- character(0)
  for (p in paths) {
    if (is.null(p) || !nzchar(p)) next
    ext <- tolower(tools::file_ext(p))
    if (ext != "rds") next  # only persisted-prep files use .rds
    if (!file.exists(p)) next
    obj <- tryCatch(readRDS(p), error = function(e) NULL)
    if (is.list(obj) && !is.data.frame(obj) &&
        !is.null(obj$.prep_meta) &&
        is.list(obj$.prep_meta) &&
        !is.null(obj$.prep_meta$prep_session_id)) {
      ids <- c(ids, as.character(obj$.prep_meta$prep_session_id))
    }
  }
  unique(ids)
}


# --------------------------------------------------------------------------
# Expression evaluation (with deny-list)
# --------------------------------------------------------------------------

# Mirror of server.py:DENY_PATTERNS. Belt-and-suspenders: server.py runs
# this check before writing JSON, but R-side enforcement protects against
# any path that bypasses the Python pre-flight (e.g. direct Rscript
# invocation during development).
# Mirror of server.py:DENY_PATTERNS. Belt-and-suspenders: server.py runs
# this check before writing JSON, but R-side enforcement protects against
# any path that bypasses the Python pre-flight (e.g. direct Rscript
# invocation during development).
#
# `::` is not on the deny list; instead it is whitelist-checked below so
# `BRIER::standardize_X(X)` and similar safe namespace calls are allowed.
# `:::` (non-exported access) stays denied.
.DENY_PATTERNS <- c(
  "system(", "system2(", "shell(", "shell.exec(",
  "unlink(", "file.remove(", "file.rename(",
  "file.create(", "file.copy(",
  "eval(", "parse(", "source(",
  "Sys.setenv(", "Sys.unsetenv(",
  "do.call(",
  ":::",
  "`", ";"
)

# Safe namespace prefixes mirroring server.py:SAFE_NAMESPACE_PREFIXES.
# An expression containing `::` is rejected unless every occurrence is
# preceded by one of these prefixes.
.SAFE_NAMESPACE_PREFIXES <- c(
  "BRIER::", "base::", "stats::", "utils::", "Matrix::"
)

.expr_uses_only_safe_namespaces <- function(expr_str) {
  if (grepl(":::", expr_str, fixed = TRUE)) return(FALSE)
  if (!grepl("::", expr_str, fixed = TRUE)) return(TRUE)
  remaining <- expr_str
  while (grepl("::", remaining, fixed = TRUE)) {
    idx <- regexpr("::", remaining, fixed = TRUE)
    pos <- as.integer(idx)
    # Walk back from `pos` to find the start of the identifier
    i <- pos - 1L
    while (i >= 1L) {
      ch <- substr(remaining, i, i)
      if (grepl("[A-Za-z0-9_.]", ch)) {
        i <- i - 1L
      } else {
        break
      }
    }
    ns <- substr(remaining, i + 1L, pos + 1L)  # includes the "::"
    if (!(ns %in% .SAFE_NAMESPACE_PREFIXES)) return(FALSE)
    remaining <- substr(remaining, pos + 2L, nchar(remaining))
  }
  TRUE
}

safe_eval <- function(expr_str, env) {
  # Returns NULL if expr_str is NULL / empty; otherwise evaluates inside `env`.
  # Throws if the expression matches any deny-list pattern OR uses an
  # unwhitelisted namespace.
  if (is.null(expr_str) || !is.character(expr_str) || !nzchar(expr_str)) {
    return(NULL)
  }
  for (pat in .DENY_PATTERNS) {
    if (grepl(pat, expr_str, fixed = TRUE)) {
      stop(
        sprintf(
          "Refusing to evaluate expression: contains disallowed pattern %s",
          shQuote(pat)
        ),
        call. = FALSE
      )
    }
  }
  if (!.expr_uses_only_safe_namespaces(expr_str)) {
    stop(
      sprintf(
        "Refusing to evaluate expression: contains '::' but not from an allowed namespace (%s)",
        paste(.SAFE_NAMESPACE_PREFIXES, collapse = ", ")
      ),
      call. = FALSE
    )
  }
  eval(parse(text = expr_str), envir = env)
}


# When a fitter is called WITHOUT a family arg, recover it from the prepared object
# (prep_auto records prepared$family from the detected/declared outcome family), so a
# binary outcome is not silently fit as gaussian. Scans the loaded env for a list with
# a character $family member (same pattern brier_s uses to recover $XtX / $n_train).
# Returns NULL if no prepared object carries a family.
family_from_prepared <- function(env) {
  for (v in ls(env)) {
    obj <- tryCatch(get(v, envir = env), error = function(e) NULL)
    if (is.list(obj) && is.character(obj[["family"]]) && nzchar(obj[["family"]][1])) {
      return(obj[["family"]][1])
    }
  }
  NULL
}


# --------------------------------------------------------------------------
# Penalty knobs (shared by BRIERi / BRIERfull / BRIERs)
# --------------------------------------------------------------------------
# Thread the optional penalty knobs onto a BRIER fit-args list. They flow via
# BRIERi/BRIERs/BRIERfull's `...` into the per-eta worker (BRIERi.eta /
# BRIERs.eta), which is where they are actually consumed. Every knob is
# OPTIONAL: when a caller omits one, that key is never set and BRIER applies its
# own default. penalty_factor is passed PRE-EVALUATED (a numeric vector or NULL)
# because each fit script resolves the expression in its own data env.
#   alpha          - elastic-net mixing, must be in (0, 1] (1 = lasso; a small
#                    positive value approaches ridge). BRIER errors on alpha<=0.
#   penalty        - one of "LASSO" (default), "SCAD", "MCP"; BRIER match.arg is
#                    case-SENSITIVE, so normalize to upper case.
#   gamma          - concavity for SCAD/MCP (default 3.7 SCAD, 3 MCP; must be >2
#                    for SCAD, >1 for MCP). Ignored under LASSO.
#   penalty.factor - per-predictor weights of length p, non-negative (0 =
#                    unpenalized, e.g. demographic covariates to adjust for;
#                    1 = penalized). Default is all-ones.
add_penalty_args <- function(fit_args, inp, penalty_factor = NULL) {
  # Read every knob with [[ ]] (exact), NEVER $ (partial): `inp[["penalty"]]` PARTIAL-MATCHES
  # `inp[["penalty"]]_factor_expr` when no exact `penalty` key is present, so a call that passes
  # only penalty_factor_expr (e.g. a replayed reproduce script, which omits the server's
  # penalty default) would read the penalty.factor EXPRESSION as the penalty name and fail
  # "penalty must be one of LASSO, SCAD, MCP". Same $-partial-match footgun as $X vs $XtX.
  if (!is.null(inp[["alpha"]])) {
    a <- as.numeric(inp[["alpha"]])
    if (!is.finite(a) || a <= 0 || a > 1) {
      stop("OMIT `alpha` entirely to use the BRIER default (LASSO, alpha=1); it is an OPTIONAL knob and you should not set it unless the user asked for elastic net. If you do set it, alpha must be in (0, 1]. BRIER rejects alpha <= 0.",
           call. = FALSE)
    }
    fit_args$alpha <- a
  }
  if (!is.null(inp[["penalty"]]) && nzchar(inp[["penalty"]])) {
    pen <- toupper(inp[["penalty"]])
    if (!pen %in% c("LASSO", "SCAD", "MCP")) {
      stop(sprintf("penalty must be one of LASSO, SCAD, MCP (got '%s').",
                   inp[["penalty"]]), call. = FALSE)
    }
    fit_args$penalty <- pen
  }
  if (!is.null(inp[["gamma"]])) {
    fit_args$gamma <- as.numeric(inp[["gamma"]])
  }
  # An all-1 penalty.factor is BRIER's default (every predictor penalized); treat
  # it as omitted regardless of length, so a small model passing rep(1, k) with a
  # wrong k does not misalign or trip a length check.
  if (!is.null(penalty_factor) && all(penalty_factor == 1)) {
    penalty_factor <- NULL
  }
  if (!is.null(penalty_factor)) {
    fit_args$penalty.factor <- penalty_factor
  }
  fit_args
}

# Echo the penalty knobs actually applied back into an output list, so the fit
# metadata records exactly what was used (the BRIER default when a knob is unset).
add_penalty_echo <- function(out, inp, penalty_factor = NULL) {
  out$penalty_used <- if (!is.null(inp[["penalty"]]) && nzchar(inp[["penalty"]])) toupper(inp[["penalty"]]) else "LASSO"
  out$alpha_used <- if (!is.null(inp[["alpha"]])) as.numeric(inp[["alpha"]]) else 1
  if (!is.null(inp[["gamma"]])) out$gamma_used <- as.numeric(inp[["gamma"]])
  out$penalty_factor_used <- !is.null(penalty_factor)
  out
}


# --------------------------------------------------------------------------
# Error construction
# --------------------------------------------------------------------------

make_error <- function(msg, where, class = "Error") {
  list(
    status = "error",
    message = msg,
    class = class,
    where = where
  )
}

# ---------------------------------------------------------------------------
# The eta grid a selection searched, for the boundary-optimum diagnostic.
#
# `sel$eta.lambda` has one ROW per grid point and one `eta_k` COLUMN per external
# model. For M = 1 that is a single axis. For M > 1 (multi.method = "ind") eta is a
# VECTOR, and each component has its OWN axis: with two sources and 7 rungs the grid
# is 7 x 7 = 49 points.
#
# The old code `unlist`ed every eta_k column into ONE flat pool, so the Python layer
# compared each component against the GLOBAL maximum across all sources. When the
# per-source grids differ (BRIER accepts an `eta.list` of M vectors), a source that
# pins at the top of ITS OWN axis but below the global maximum is MISSED -- which is
# precisely the silent truncation the diagnostic exists to catch. So emit one grid
# PER SOURCE and let the comparison be per-axis.
eta_grid_values_of <- function(sel) {
  tryCatch({
    el <- sel$eta.lambda
    eta_cols <- grep("^eta_\\d+$", colnames(el), value = TRUE)
    if (length(eta_cols) == 0) {
      return(NULL)
    }
    if (length(eta_cols) == 1) {
      return(as.numeric(el[[eta_cols]]))
    }
    lapply(
      eta_cols,
      function(cn) sort(unique(as.numeric(el[[cn]])))
    )
  }, error = function(e) NULL)
}

# ---------------------------------------------------------------------------
# multi.method: which one, when the caller does not say.
#
# With M > 1 externals the two methods are structurally different:
#
#   ind       one eta PER SOURCE. eta is a VECTOR and the grid is a PRODUCT, so each
#             external gets its own transfer strength.
#   stacking  the sources are COLLAPSED into one combined predictor BEFORE transfer, so a
#             single scalar eta covers all of them. It cannot weight them differently.
#
# Measured on T2_afr-summary_eur-2ind (EUR1 = 37 nonzero coefficients, EUR2 = 2 -- one
# useful source and one nearly empty one), selecting each on the AFR val:
#
#     stacking  eta 10       test R^2 0.0054  MSPE 0.9946    25s
#     ind       eta (10,10)  test R^2 0.0076  MSPE 0.9919   550s
#
# ind wins on val AND test, because it can lean on EUR1 and ignore EUR2; stacking must
# apply one strength to their blend. The same ordering was found on T2_multisource.
#
# But ind's grid is n^M, so it does not scale: at 21 points per axis, M=2 is 441 fits
# (~9 min, done above) and M=3 is 9261 (~3 hours). So the DEFAULT is ind up to M=2 and
# stacking from M=3 -- the better method where it is affordable, the affordable one where
# it is not.
#
# An EXPLICIT multi_method ALWAYS WINS. This only fires when the caller omits it (or
# passes "auto"): the default gets smarter, the knob stays the caller's.
.MULTI_METHOD_IND_MAX <- 2L

resolve_multi_method <- function(requested, M) {
  if (!is.null(requested) && nzchar(requested) &&
      !identical(tolower(requested), "auto")) {
    return(requested)
  }
  if (is.null(M) || !is.finite(M) || M <= .MULTI_METHOD_IND_MAX) "ind" else "stacking"
}


# =============================================================================
# THE PREPARED-OBJECT CONTRACT
#
# The prepared object is the interface between PREPARATION and FITTING, and the
# fitter is entitled to assume things about it: that beta.external's rows line up
# with the predictors, that the external is not all zeros, that the matrix the model
# is EVALUATED on is on the same scale as the matrix it was FIT on. prep_auto
# guarantees all of that. Until now NOTHING CHECKED IT, and the checks that existed
# were the wrong kind:
#
#   * brier_s only compared ROW COUNTS (nrow(sumstats) == nrow(XtX) == nrow(beta)).
#     Three objects with matching counts in DIFFERENT ORDER passed, and every
#     coefficient landed on the wrong predictor. Silently.
#   * the standardization "check" was an ALWAYS-ON warning: it fired whether or not
#     anything was wrong, so it carried no information about the data at all.
#   * the one data-driven check (.x_looks_standardized) was a KNOWN FALSE POSITIVE.
#
# A warning that fires when nothing is wrong is worse than no warning: it trains the
# reader, human or model, to ignore exactly the signal that matters. This project has
# already paid for that once (the selection tools emitted _notice_eta_boundary on
# every pinned run and the harness dropped it every time).
#
# So these are CHECKS, not notices: they refuse, and they NAME the violated clause.
# They are called from the fitters automatically rather than offered as a tool the
# model may remember to call, because the object most likely to violate the contract
# is the one the model built itself, and a model that improvised badly is exactly the
# model that will not think to validate.
#
# WHAT THIS CANNOT CHECK: ALLELE ORIENTATION. It verifies shapes, order, scale,
# sparsity and non-degeneracy. It cannot verify that a coefficient carries the right
# SIGN. A wrongly flipped panel matches by name, aligns, fits, and reports a number
# with every coefficient inverted, and nothing downstream can see it. That is why
# prep_auto's aligner is pinned BITWISE against BRIER's own preprocessors
# (mcp/tests/test_aligner_differential.R). Validation here is NECESSARY and it is
# NOT SUFFICIENT.
# =============================================================================

CONTRACT_ORIENTATION_NOTE <- paste(
  "The prepared-object contract was checked (predictor alignment, external",
  "non-degeneracy, scale consistency). It does NOT check ALLELE ORIENTATION: a",
  "coefficient whose effect allele is flipped relative to the target still matches",
  "by name, still fits, and reports a number with the wrong SIGN. prep_auto's",
  "aligner is verified against BRIER's preprocessors; if you aligned the external",
  "yourself, that correctness is yours to own."
)


# Which SCALE REGIME a predictor matrix is in.
#
# NOT "is it standardized" against a tight tolerance. Two real cases break that:
#   * a val/test split standardized by the TRAINING moments does NOT have mean 0 and
#     sd 1 in ITSELF (it has them up to sampling error: median |mean| ~ 0.025 on
#     n = 2000). The old heuristic used |mean| < 0.05 on 20 RANDOM columns and that
#     is why it false-positived.
#   * a POOLED cross-ancestry matrix (BRIERfull) standardized by the TARGET's moments
#     has large column means for the external cohorts, because their allele
#     frequencies differ. T1_brierfull's stacked X has median |colmean| = 0.5 and is
#     nonetheless correctly standardized.
#
# So key on the COLUMN SD, which separates the regimes by an order of magnitude and
# is robust to both: a raw genotype column has sd = sqrt(2p(1-p)) <= 0.707, so
# |sd - 1| >= 0.29 ALWAYS; a standardized column has |sd - 1| ~ 0.02 even off training
# moments. Median over columns, with a wide margin between them.
.SCALE_SD_TOL <- 0.2

scale_regime_matrix <- function(M) {
  if (is.null(M) || !is.numeric(as.matrix(M)[1, 1])) return("unknown")
  M <- as.matrix(M)
  if (ncol(M) < 2 || nrow(M) < 3) return("unknown")
  j <- if (ncol(M) > 200) seq(1, ncol(M), length.out = 200) else seq_len(ncol(M))
  sds <- apply(M[, round(j), drop = FALSE], 2, stats::sd)
  sds <- sds[is.finite(sds) & sds > 0]
  if (!length(sds)) return("unknown")
  if (stats::median(abs(sds - 1)) < .SCALE_SD_TOL) "standardized" else "raw"
}

# For a gaussian outcome. Standardized y has mean ~0 and sd ~1; raw height has mean
# 170 and sd 10. The regimes are orders of magnitude apart, so this is not delicate.
scale_regime_vector <- function(v) {
  if (is.null(v) || !is.numeric(v) || length(v) < 3) return("unknown")
  s <- stats::sd(v)
  if (!is.finite(s) || s <= 0) return("unknown")
  if (abs(s - 1) < .SCALE_SD_TOL && abs(mean(v)) < .SCALE_SD_TOL) "standardized" else "raw"
}


.name_mismatch <- function(a, b) {
  if (is.null(a) || is.null(b)) return(TRUE)
  if (length(a) != length(b)) return(TRUE)
  !identical(as.character(a), as.character(b))
}


# Every column all-zero => the external carries NOTHING and the transfer term is a
# no-op, but eta still "selects" and the run still reports a number. A run once scored
# a hollow 70/70 on an external whose single nonzero coefficient was 5.9e-17, so this
# is TOLERANCE-based: all(cf == 0) misses the dust.
.EXTERNAL_ZERO_TOL <- 1e-12


# Validate what the FITTER is about to use. Not the persisted file: the RESOLVED
# components, so a hand-composed or improvised set of expressions is held to the same
# contract as a prep_auto object, through one code path.
#
# Returns a character vector of violations (empty = the contract holds).
validate_fit_inputs <- function(shape, X = NULL, y = NULL, sumstats = NULL,
                                XtX = NULL, beta_external = NULL,
                                family = "gaussian",
                                allow_zero_external = FALSE) {
  v <- character(0)

  # A DELIBERATE no-transfer baseline (eta.list == 0) uses an all-zero external as a
  # placeholder: at eta=0 the external is multiplied by 0, so it contributes nothing and
  # its alignment is irrelevant. This is exactly the brier_i(eta=0) target-only baseline
  # and the single-cohort external-only comparators that a brier_full comparison drives.
  # For that case the caller sets allow_zero_external=TRUE, which suppresses the
  # zero-external "[degenerate]" clause and the rownames "[alignment]" clause (both of
  # which correctly fire for a REAL transfer with a nonzero eta, where a zero or
  # unaligned external silently degrades the fit). The shape (p+1 row-count) check stays.
  .zero_ext <- !is.null(beta_external) &&
    all(abs(as.numeric(beta_external)) < .EXTERNAL_ZERO_TOL)
  .skip_ext_align_degen <- isTRUE(allow_zero_external) && .zero_ext

  # ---- predictor alignment. The count check that existed passes three objects in
  # DIFFERENT ORDER. Names are the only thing that can prove alignment, so an object
  # with no names FAILS: "cannot verify" is not "fine" (a check that skips is not a
  # check). prep_auto now stamps these names, so this costs the deterministic path
  # nothing.
  panel <- NULL
  if (identical(shape, "brier_i")) {
    panel <- colnames(X)
    if (is.null(panel)) {
      v <- c(v, paste(
        "[alignment] X has no column names, so beta.external cannot be proved to",
        "line up with the predictors. Set colnames(X) to the variant panel."))
    }
    if (!is.null(beta_external)) {
      if (nrow(beta_external) != ncol(X) + 1L) {
        v <- c(v, sprintf(paste(
          "[shape] beta.external has %d rows; BRIERi requires p+1 = %d (the",
          "intercept row first, then one row per predictor)."),
          nrow(beta_external), ncol(X) + 1L))
      } else {
        rn <- rownames(beta_external)
        if (is.null(rn) && !.skip_ext_align_degen) {
          v <- c(v, paste(
            "[alignment] beta.external has no row names, so it cannot be proved to",
            "line up with X's columns. Set",
            "rownames(beta.external) <- c('(Intercept)', colnames(X))."))
        } else if (!is.null(rn) && !is.null(panel) && .name_mismatch(rn[-1], panel)) {
          v <- c(v, paste(
            "[alignment] beta.external's rows (after the intercept) do not match",
            "colnames(X) in ORDER. Row counts can match while every coefficient",
            "sits on the WRONG predictor, which fits and reports a number."))
        }
      }
    }
  }

  if (identical(shape, "brier_s")) {
    panel <- rownames(XtX)
    if (is.null(panel)) {
      v <- c(v, paste(
        "[alignment] XtX has no dimnames, so the LD cannot be proved to line up with",
        "the sumstats and the external. Name it with the variant panel."))
    }
    ss_names <- if (!is.null(sumstats) && "varnames" %in% names(sumstats))
      as.character(sumstats$varnames) else NULL
    if (!is.null(panel) && !is.null(ss_names) && .name_mismatch(ss_names, panel)) {
      v <- c(v, paste(
        "[alignment] sumstats$varnames does not match rownames(XtX) in ORDER."))
    }
    if (!is.null(beta_external)) {
      if (!is.null(XtX) && nrow(beta_external) != nrow(XtX)) {
        v <- c(v, sprintf(paste(
          "[shape] beta.external has %d rows but BRIERs requires p = %d rows and NO",
          "intercept row (that is the BRIERi convention, not the BRIERs one)."),
          nrow(beta_external), nrow(XtX)))
      } else {
        rn <- rownames(beta_external)
        if (is.null(rn)) {
          v <- c(v, paste(
            "[alignment] beta.external has no row names, so it cannot be proved to",
            "line up with the LD panel. Set rownames(beta.external) <- rownames(XtX)."))
        } else if (!is.null(panel) && .name_mismatch(rn, panel)) {
          v <- c(v, paste(
            "[alignment] beta.external's rows do not match rownames(XtX) in ORDER.",
            "Row counts can match while every coefficient sits on the WRONG",
            "predictor, which fits and reports a number."))
        }
      }
    }
    if (!is.null(XtX) && !inherits(XtX, "sparseMatrix")) {
      v <- c(v, "[ld] BRIERs requires a SPARSE LD matrix; XtX is dense.")
    }
  }

  # ---- the external must carry something. An all-zero external silently degenerates
  # the transfer to no-transfer, and eta becomes meaningless (it multiplies zero).
  if (!is.null(beta_external) && !.skip_ext_align_degen) {
    nz <- apply(as.matrix(beta_external), 2,
                function(col) sum(abs(col) > .EXTERNAL_ZERO_TOL))
    if (all(nz == 0)) {
      v <- c(v, paste(
        "[degenerate] every column of beta.external is numerically ZERO, so there is",
        "nothing to transfer: the fit will silently reduce to no-transfer while eta",
        "still 'selects'. This is a DATA signal (too few samples, or the external's",
        "own selection collapsed to the null model), not something to fit around."))
    }
  }

  # ---- scale. NOT "must be standardized": BRIERfull legitimately pools raw cohorts.
  # The invariant is CONSISTENCY. For a gaussian outcome, MSPE is scale-DEPENDENT, so
  # a standardized X with a raw y makes the criterion meaningless: it once produced a
  # val MSPE of ~28964 (= mean(y^2) for raw height) for EVERY model, and selection
  # collapsed. That failure is silent and it is checkable, so we check it.
  if (identical(family, "gaussian") && !is.null(X) && !is.null(y)) {
    rx <- scale_regime_matrix(X)
    ry <- scale_regime_vector(y)
    if (identical(rx, "standardized") && identical(ry, "raw")) {
      v <- c(v, sprintf(paste(
        "[scale] X is standardized but the gaussian y is NOT (mean %.1f, sd %.1f).",
        "gaussian.mspe is scale-DEPENDENT, so it collapses to ~mean(y^2) for every",
        "model and selection cannot discriminate. Standardize y by its TRAINING",
        "mean/sd (never a binary or count outcome)."), mean(y), stats::sd(y)))
    }
  }

  v
}


# Validate a HELD-OUT split against the scale the model was FIT on. A model fitted on
# standardized predictors and evaluated on raw ones (or the reverse) reports a number
# and is meaningless. The fit records its regimes; selection/evaluate compare.
validate_eval_inputs <- function(X = NULL, y = NULL, family = "gaussian",
                                 fit_x_regime = NULL, fit_y_regime = NULL,
                                 split = "the evaluation split") {
  v <- character(0)
  rx <- scale_regime_matrix(X)
  if (!is.null(fit_x_regime) && rx != "unknown" && fit_x_regime != "unknown" &&
      !identical(rx, fit_x_regime)) {
    v <- c(v, sprintf(paste(
      "[scale] the model was fit on %s predictors but %s is %s. The coefficients do",
      "not apply to a different scale: the prediction is meaningless, and nothing",
      "downstream errors."), fit_x_regime, split, rx))
  }
  if (identical(family, "gaussian") && !is.null(y)) {
    ry <- scale_regime_vector(y)
    if (!is.null(fit_y_regime) && ry != "unknown" && fit_y_regime != "unknown" &&
        !identical(ry, fit_y_regime)) {
      v <- c(v, sprintf(paste(
        "[scale] the model was fit on a %s y but %s's y is %s. gaussian.mspe is",
        "scale-DEPENDENT, so the two are not comparable."),
        fit_y_regime, split, ry))
    }
    if (identical(rx, "standardized") && identical(ry, "raw")) {
      v <- c(v, sprintf(paste(
        "[scale] %s has standardized predictors but a RAW gaussian y (mean %.1f, sd",
        "%.1f). MSPE collapses to ~mean(y^2) for every model."),
        split, mean(y), stats::sd(y)))
    }
  }
  v
}


# One place that turns violations into a refusal, so every caller phrases it the same.
stop_on_contract_violations <- function(v, where) {
  if (!length(v)) return(invisible(NULL))
  stop(paste0(
    "PREPARED-OBJECT CONTRACT violated, so ", where, " refused to run. Each clause ",
    "below describes a failure that would otherwise have produced a NUMBER rather ",
    "than an error:\n  - ",
    paste(v, collapse = "\n  - "),
    "\nIf prep_auto built this object, this is a bug: report it. If you assembled it ",
    "yourself, fix the clause above and retry."
  ), call. = FALSE)
}
