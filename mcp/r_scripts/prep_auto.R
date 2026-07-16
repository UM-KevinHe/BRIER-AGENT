#!/usr/bin/env Rscript
# prep_auto.R - one-call preprocessing: everything between "here are my files" and
# "here is a fit-ready object", in a single deterministic tool call.
#
# ALIGNMENT lives HERE (see .align_predictors), not in BRIER::preprocessI / preprocessS.
# We used to delegate to those. They are correct, but they hard-require CHR/BP/REF/ALT,
# which IS their identity model: BRIER is general (predictors can be SNPs, gene expression,
# proteins), yet its preprocessors are genotype-only, so the genotype and non-genotype paths
# cannot be made parallel on top of them. Our aligner takes a `predictor_type` instead, and
# is verified BITWISE identical to theirs on genotype data by a differential test
# (mcp/tests/test_aligner_differential.R). Their correctness is inherited as a TEST, not as
# a runtime dependency.
#
# What alignment does: QC the variant map (drop multi-allelic CHR:BP, optionally
# strand-ambiguous pairs), match every table to the reference by coordinate, RECORD which
# alleles are swapped and negate those effects (MATCH EARLY, FLIP LATE), derive `corr` via
# p2cor when a sumstats ships none, impute coefficient 0 where an external does not cover a
# target predictor, and drop external-only predictors. That is alignment TO THE TARGET, not
# an intersection: a missing external coefficient just means no transfer contribution there.
#
# On top of that, prep_auto does the numeric steps the package docs show a user doing BY
# HAND: subset X and the LD to the survivors, standardize (conditionally) X and a Gaussian
# y, prepend the intercept row to beta.external for BRIERi, align and standardize the
# validation/testing splits to the TRAINING set, FIT an external model when the external
# arrives as raw data, and assemble the fit-ready object. It is a state-threaded sequence a
# small model drives unreliably, which is why it is one tool call and not agent
# orchestration.
#
# brier_full is the one shape that INTERSECTS: it pools RAW genotypes across cohorts, and a
# genotype cannot be imputed the way a coefficient can.
#
# Input JSON (from server.py):
#   {shape, data_dir, roles, standardize, standardize_method, outcome_family,
#    persist, out_dir}

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

if (!requireNamespace("BRIER", quietly = TRUE)) {
  stop("prep_auto requires the BRIER package (BRIERi/BRIERs, mergeExternals, p2cor, calLD).",
       call. = FALSE)
}


# ---- reader boundary -------------------------------------------------------
# Resolve a role to an on-disk path (or NULL if absent and not required). A role
# value may be a bare filename (join with data_dir) OR an absolute path the agent
# copied from the location block (use as-is). Joining an absolute path onto
# data_dir doubles it, so detect absolute paths first.
.role_path <- function(data_dir, roles, role, required = TRUE) {
  if (is.null(roles[[role]])) {
    if (required) stop(sprintf("missing required role: %s", role), call. = FALSE)
    return(NULL)
  }
  raw <- roles[[role]]
  is_abs <- grepl("^(/|[A-Za-z]:[\\\\/]|~)", raw)
  path <- if (is_abs) raw else file.path(data_dir, raw)
  if (!file.exists(path)) {
    # Tolerate a filename given WITHOUT its extension (a common small-model slip,
    # e.g. "height_AFR_LD" for "height_AFR_LD.rds"): try known extensions and use
    # the first existing match.
    cands <- paste0(path, c(".txt.gz", ".rds", ".rda", ".RData", ".csv",
                            ".tsv", ".txt", ".bgz", ".gz"))
    hit <- cands[file.exists(cands)]
    if (length(hit) >= 1L) {
      path <- hit[1L]
    } else {
      stop(sprintf("file for role %s not found: %s", role, path), call. = FALSE)
    }
  }
  path
}

.read_role <- function(data_dir, roles, role, required = TRUE) {
  path <- .role_path(data_dir, roles, role, required)
  if (is.null(path)) return(NULL)
  inner <- sub("\\.(gz|bgz)$", "", path, ignore.case = TRUE)
  ext <- tolower(tools::file_ext(inner))
  if (ext %in% c("rds", "rda", "rdata")) {
    # load_data_file returns an ENVIRONMENT holding the object(s), not the
    # object itself. Extract it: for .rds it is named after the file basename;
    # for .rda/.RData take the first (or only) object.
    e <- load_data_file(path)
    nms <- ls(e)
    if (length(nms) == 0) stop(sprintf("no object found in %s", path), call. = FALSE)
    obj_name <- tools::file_path_sans_ext(basename(inner))
    if (obj_name %in% nms) return(get(obj_name, envir = e))
    return(get(nms[1], envir = e))
  }
  if (ext == "csv") {
    return(utils::read.csv(path, header = TRUE, stringsAsFactors = FALSE,
                           check.names = FALSE))
  }
  utils::read.table(path, header = TRUE, sep = if (ext == "tsv") "\t" else "",
                    stringsAsFactors = FALSE, check.names = FALSE)
}

# The sample-ID column, found STRUCTURALLY and not just by name.
#
# The name list is genotype-specific (PLINK's IID/FID, our benchmark's DeID_PatientID), so an
# expression or proteomics matrix from any other pipeline calls it something else
# (SampleID, sample, subject_id, ...) and sails straight past it. Its IDs then coerce to NA,
# become a PREDICTOR COLUMN OF NAs, and the run dies several frames later with "missing value
# where TRUE/FALSE needed" -- a real failure observed the first time the generic path ran.
#
# So fall back on the structure: a predictor is a NUMBER. A non-numeric column cannot be a
# predictor, whatever it is called. That rule needs no list and does not care what the
# predictors are.
.ID_NAMES <- c("DeID_PatientID", "IID", "ID", "FID", "SampleID", "sample_id", "sample",
               "subject_id", "SubjectID", "patient_id", "PatientID", "eid", "rowname")

.id_columns <- function(df) {
  by_name <- intersect(tolower(.ID_NAMES), tolower(colnames(df)))
  cols <- colnames(df)[tolower(colnames(df)) %in% by_name]
  # Structural: any column that is not numeric and not coercible to a number.
  non_numeric <- vapply(colnames(df), function(cn) {
    v <- df[[cn]]
    if (is.numeric(v)) return(FALSE)
    all(is.na(suppressWarnings(as.numeric(as.character(v[seq_len(min(50L, length(v)))])))))
  }, logical(1))
  unique(c(cols, colnames(df)[non_numeric]))
}

.geno_matrix <- function(df, id_candidates = NULL) {
  id_col <- .id_columns(df)
  keep <- setdiff(colnames(df), id_col)
  if (!length(keep)) {
    stop("the predictor matrix has no numeric columns (every column looks like an identifier)",
         call. = FALSE)
  }
  m <- as.matrix(df[, keep, drop = FALSE])
  storage.mode(m) <- "double"
  # A predictor column that is PARTLY non-numeric is a data problem, not an ID column, and
  # silently carrying NAs into the fit is how a wrong answer looks healthy.
  if (anyNA(m)) {
    bad <- colnames(m)[apply(is.na(m), 2, any)]
    stop(sprintf(paste0("the predictor matrix has non-numeric or missing values in %d ",
                        "column(s) (%s%s). Predictors must be numeric."),
                 length(bad), paste(utils::head(bad, 5), collapse = ", "),
                 if (length(bad) > 5) ", ..." else ""), call. = FALSE)
  }
  if (length(id_col) >= 1) rownames(m) <- as.character(df[[id_col[1]]])
  m
}

.pheno_vector <- function(df, id_candidates = NULL) {
  id_col <- .id_columns(df)
  trait_col <- setdiff(colnames(df), id_col)
  if (length(trait_col) < 1) stop("phenotype file has no trait column", call. = FALSE)
  y <- as.numeric(df[[trait_col[1]]])
  if (length(id_col) >= 1) names(y) <- as.character(df[[id_col[1]]])
  y
}

# ---- streaming genotype readers (brier_full memory path) -------------------
# brier_full pools RAW individual-level cohorts, so a single base-R read.table
# of a wide genotype matrix (the EUR training block is ~100MB gzipped) carries a
# multi-GB parse transient, and stacking every cohort at once plus the rbind copy
# doubles it again -> OOM. These readers cut peak RAM to ~(final X + one cohort):
# read only the shared-panel columns, straight into a numeric matrix, one cohort
# at a time. data.table::fread is used when available (lean + column-select),
# with a base-R colClasses fallback that skips unwanted columns at parse time.

.sep_for <- function(path) {
  inner <- sub("\\.(gz|bgz)$", "", path, ignore.case = TRUE)
  ext <- tolower(tools::file_ext(inner))
  if (ext == "csv") "," else if (ext == "tsv") "\t" else ""
}

# Column names of a delimited (optionally gzipped) file, without reading data.
.geno_header <- function(path) {
  if (requireNamespace("data.table", quietly = TRUE)) {
    h <- tryCatch(
      colnames(data.table::fread(path, nrows = 0L, showProgress = FALSE,
                                 check.names = FALSE, data.table = FALSE)),
      error = function(e) NULL)
    if (!is.null(h)) return(h)
  }
  colnames(utils::read.table(path, header = TRUE, nrows = 1L,
                             check.names = FALSE, sep = .sep_for(path),
                             stringsAsFactors = FALSE))
}

# Read exactly `cols` (in that order) from a genotype file as a double matrix,
# dropping the id column and every non-panel variant at parse time.
.read_geno_cols <- function(path, cols) {
  if (requireNamespace("data.table", quietly = TRUE)) {
    dt <- tryCatch(
      data.table::fread(path, select = cols, showProgress = FALSE,
                        check.names = FALSE, data.table = FALSE),
      error = function(e) NULL)
    if (!is.null(dt)) {
      m <- as.matrix(dt[, cols, drop = FALSE])
      storage.mode(m) <- "double"
      return(m)
    }
  }
  # base-R fallback: mark non-panel columns "NULL" so read.table skips them.
  hdr <- .geno_header(path)
  cc <- ifelse(hdr %in% cols, "numeric", "NULL")
  df <- utils::read.table(path, header = TRUE, colClasses = cc,
                          check.names = FALSE, sep = .sep_for(path),
                          stringsAsFactors = FALSE)
  m <- as.matrix(df[, cols, drop = FALSE])
  storage.mode(m) <- "double"
  m
}

# CHECKPOINT S1: standardization convention. "sd" = empirical (x-mean)/sd (n-1);
# "maf" = center 2p, scale sqrt(2p(1-p)) with p=mean(x)/2. TRAINING constants,
# applied to val/test. Which BRIER/model1 expects is the key correctness fact.
.fit_standardizer <- function(x_train, method) {
  if (identical(method, "maf")) {
    p <- colMeans(x_train) / 2
    ctr <- 2 * p
    scl <- sqrt(2 * p * (1 - p))
  } else {
    ctr <- colMeans(x_train)
    scl <- apply(x_train, 2, stats::sd)
  }
  scl[scl == 0 | is.na(scl)] <- 1
  list(center = ctr, scale = scl)
}
.apply_standardizer <- function(m, st) {
  sweep(sweep(m, 2, st$center, "-"), 2, st$scale, "/")
}

.guess_coef_col <- function(df) {
  for (c in c("coef","beta","BETA","weight","effect","b")) if (c %in% colnames(df)) return(c)
  num <- names(df)[vapply(df, is.numeric, logical(1))]
  if (length(num) < 1) stop("external table has no numeric coefficient column", call. = FALSE)
  tail(num, 1)
}

# Normalize an external coefficient table's IDENTIFIER column to `varnames`. The COEF column
# is already guessed (.guess_coef_col); the identifier used to require the literal name
# `varnames`, so an external whose id column was `SNP` / `id` / `rsID` / ... silently matched
# NOTHING and produced an all-zero external with no error. Now: keep `varnames` if present;
# else rename a recognized id alias (case-insensitive); else, for a two-part coefficient
# table (one id column + one numeric coef column), rename the single non-numeric column. If
# no identifier can be found the table is returned unchanged, and .check_external_overlap
# turns the resulting zero match into a LOUD error rather than a silent no-transfer external.
.ensure_external_varnames <- function(df) {
  df <- as.data.frame(df)
  cn <- colnames(df)
  if (is.null(cn) || "varnames" %in% cn) return(df)
  aliases <- c("snp","snpid","snp_id","rsid","rsids","rs","rs_id","id","variant",
               "variantid","variant_id","variable","marker","name","gene","geneid",
               "gene_id","feature","predictor","term","probe","protein")
  hit <- cn[tolower(cn) %in% aliases]
  if (length(hit) >= 1) {
    colnames(df)[colnames(df) == hit[1]] <- "varnames"
    return(df)
  }
  # No named identifier: a coefficient table is one id column plus one numeric coef column,
  # so a lone non-numeric column is the identifier.
  nonnum <- cn[!vapply(df, is.numeric, logical(1))]
  if (length(nonnum) == 1L) {
    colnames(df)[colnames(df) == nonnum] <- "varnames"
    return(df)
  }
  df
}

# Refuse an external that shares NO predictor names with the target panel. Such an external
# aligns to an all-zero coefficient vector (nothing joins), which is indistinguishable in the
# fit from "no transfer" but is almost always a mis-read identifier column or a genuinely
# disjoint panel. A degenerate external is a DATA signal, not something to paper over
# silently (mirrors .external_is_degenerate for FITTED externals).
.check_external_overlap <- function(ext_tab, panel) {
  if (is.null(ext_tab)) return(invisible(0L))
  vn <- if ("varnames" %in% colnames(ext_tab)) as.character(ext_tab$varnames) else character(0)
  n <- sum(vn %in% as.character(panel))
  if (n == 0L) {
    stop(paste0(
      "the external model shares NO predictor names with the target panel, so it would ",
      "contribute nothing (an all-zero external). Most often its IDENTIFIER column is not ",
      "recognized: a pretrained coefficient table must be a `varnames` column (the predictor ",
      "names, matching the target) plus a `coef` column. Columns found: ",
      paste(utils::head(colnames(ext_tab), 8), collapse = ", "),
      ". If the predictors genuinely differ, the external is not usable for this target."),
      call. = FALSE)
  }
  invisible(n)
}

# Is this object ALREADY an LD matrix, rather than a reference panel to build one
# from? `target_ld` and `target_ld_panel` are both "the LD thing", so a small model
# mixes them up: on a real run the 7B handed a prebuilt sparse LD as
# target_ld_panel, and prep_auto tried to BUILD an LD from an LD, demanding
# ld_ancestry/ld_build to do it, and looped until the guard aborted.
#
# The object says what it is. An LD is a SQUARE matrix whose rownames equal its
# colnames (variant x variant). A genotype reference panel is samples x variants:
# rectangular, and read from a text file it is a data.frame, not a matrix at all.
.looks_like_ld_matrix <- function(x) {
  if (is.null(x) || is.data.frame(x)) return(FALSE)
  if (!(is.matrix(x) || inherits(x, "Matrix"))) return(FALSE)
  d <- dim(x)
  if (length(d) != 2L || d[1] != d[2] || d[1] < 2L) return(FALSE)
  rn <- rownames(x); cn <- colnames(x)
  # Named: the variant names must agree on both margins.
  if (!is.null(rn) && !is.null(cn)) return(identical(rn, cn))
  # Unnamed but square: a sparse square matrix is an LD; a dense square genotype
  # matrix would need samples == variants, which does not happen in practice.
  inherits(x, "sparseMatrix")
}

# preprocessS renames the sumstats columns through a KEY -> COLUMN-NAME map, and
# its defaults are lowercase (p = "pval", n = "n", beta = "beta"). Real GWAS files
# (including this benchmark's) ship P / N / BETA, so the defaults silently do not
# match. That went unnoticed for a long time because target.ind = "corr" only needs
# `corr`, which happens to match: every case that ships a corr column works, and
# the target.ind = "gwas" branch (which DERIVES corr via p2cor(p, n, sgn)) could
# never run at all -- it died on "Column 'pval' (mapped from key 'p') not found".
#
# Resolve each key against the columns actually present, case-insensitively and
# over the usual aliases. Keys we cannot find keep BRIER's default, so a genuinely
# missing column still raises BRIER's own error rather than being papered over.
.ss_col_map <- function(ss) {
  cols <- colnames(ss)
  pick <- function(candidates, default) {
    hit <- cols[match(tolower(candidates), tolower(cols))]
    hit <- hit[!is.na(hit)]
    if (length(hit) >= 1) hit[1] else default
  }
  c(
    chr  = pick(c("CHR", "chromosome", "chrom"),                "CHR"),
    bp   = pick(c("BP", "POS", "position", "base_pair"),        "BP"),
    ref  = pick(c("REF", "A2", "other_allele", "non_effect_allele"), "REF"),
    alt  = pick(c("ALT", "A1", "effect_allele"),                "ALT"),
    p    = pick(c("P", "PVAL", "P_VALUE", "pvalue", "p_val"),   "pval"),
    n    = pick(c("N", "sample_size", "n_obs"),                 "n"),
    sgn  = pick(c("SGN", "sign", "direction"),                  "sgn"),
    beta = pick(c("BETA", "b", "effect", "effect_size"),        "beta"),
    corr = pick(c("corr", "CORR", "r"),                         "corr")
  )
}

# Returns a data.frame with CHR/BP/REF/ALT + coef1.. (mergeExternals for >1),
# or NULL if no external role present. If an external only carries a varnames
# key (no CHR/BP/REF/ALT), coordinates are attached by joining varnames to
# snp_info (which carries both), so preprocessI/S can align by coordinate.
# Normalize common role-name ALIASES to the canonical prep_auto role names. A
# small model often uses the FITTER's expr-argument vocabulary as role keys
# (`sumstats_expr`, `XtX_expr`, `beta_external_expr`) or short forms; map them so
# prep_auto still finds the files instead of erroring on a "missing required role"
# and looping. Only fills a canonical role that is NOT already present.
.normalize_roles <- function(roles) {
  if (is.null(roles) || length(roles) == 0) return(roles)
  amap <- c(
    sumstats = "target_sumstats", sumstats_expr = "target_sumstats",
    gwas = "target_sumstats", target_gwas = "target_sumstats",
    ld = "target_ld", ld_expr = "target_ld", ldm = "target_ld",
    xtx = "target_ld", xtx_expr = "target_ld", ld_matrix = "target_ld",
    beta_external = "external_coef", beta_external_expr = "external_coef",
    external = "external_coef", external_beta = "external_coef",
    snp = "snp_info", snpinfo = "snp_info", snp_info_expr = "snp_info",
    x_train = "target_X_train", y_train = "target_y_train",
    x_val = "target_X_val", y_val = "target_y_val",
    x_test = "target_X_test", y_test = "target_y_test"
  )
  out <- roles
  for (k in names(roles)) {
    lk <- tolower(k)
    canon <- unname(amap[lk])
    if (is.na(canon)) canon <- NULL
    if (is.null(canon)) {
      m <- regmatches(lk, regexec("^(beta_external|external)_([0-9]+)$", lk))[[1]]
      if (length(m) == 3L) canon <- sprintf("external_coef_%s", m[3L])
    }
    if (!is.null(canon) && is.null(out[[canon]])) out[[canon]] <- roles[[k]]
  }
  out
}


# Fit an external model from RAW external data (Bucket B) to produce a
# standardized-scale coefficient vector usable as beta.external. "Fitting an
# external" is just the matching BRIER fitter run at eta=0 with no external on the
# external's OWN data:
#   * SUMMARY external (external_sumstats + external_ld_panel): build the LD from
#     the external panel (Berisa blocks, subsampled), BRIERs(eta=0).
#   * INDIVIDUAL external (external_X + external_y): standardize the external X
#     (train + val) and Gaussian y by the external-train moments, BRIERi(eta=0).
# Selection: an external validation split (external_X_val/external_y_val) by
# gaussian.mspe when provided, else an information criterion (Cp for summary, BIC
# for individual). Returns a data.frame of varnames + CHR/BP/REF/ALT + coef (the
# per-SNP standardized effects; no intercept) for coordinate alignment to the
# target panel by the usual external pipeline. The external reference panel is
# sized appropriately in the fixture, so no runtime subsampling is done here.
# Fill an omitted external_X_val / external_y_val from the EXTERNAL TRAINING role's
# filename (training->validation, _train->_val) in the same data dir. Only fills a
# role that is ABSENT, and only when BOTH the X and the y sibling exist, so a case
# that genuinely ships no external val still falls through to an IC and a split is
# never fabricated from another split. Mirrors .discover_target_splits.
# Resolve a role's filename against the data dir -- UNLESS it is already absolute.
#
# The benchmark runner injects ABSOLUTE paths into the prompt ("resolve each filename to
# its absolute path below; do not use bare filenames"), so the agent passes absolute role
# values routinely. Every .discover_* helper built its sibling by string substitution and
# then did file.exists(file.path(data_dir, candidate)) -- which, for an absolute
# candidate, yields "<data_dir>//abs/path" and never exists. So discovery SILENTLY did
# nothing: T2_afr-summary_eur-summary assembled no test split, the agent invented
# `prepared$X_test`, and looped until the guard aborted it. .role_path already handles
# this for reading; the discovery helpers did not.
.exists_in_dir <- function(data_dir, f) {
  if (is.null(f) || length(f) != 1L || !nzchar(f)) return(FALSE)
  f <- as.character(f)
  p <- if (grepl("^(/|~|[A-Za-z]:[\\\\/])", f)) f else file.path(data_dir, f)
  file.exists(p)
}


.discover_external_val <- function(data_dir, roles) {
  if (!is.null(roles[["external_X_val"]]) || !is.null(roles[["external_y_val"]])) {
    return(roles)
  }
  sib <- function(f) {
    out <- gsub("training", "validation", f, ignore.case = TRUE)
    gsub("_train", "_val", out, ignore.case = TRUE)
  }
  take <- function(xc, yc) {
    if (is.null(xc) || is.null(yc)) return(FALSE)
    .exists_in_dir(data_dir, xc) && .exists_in_dir(data_dir, yc)
  }

  # (a) INDIVIDUAL external (external_X + external_y): the val split is the
  # training files' sibling.
  xt <- roles[["external_X"]]; yt <- roles[["external_y"]]
  if (!is.null(xt) && !is.null(yt) && length(xt) == 1L && length(yt) == 1L) {
    xc <- sib(xt); yc <- sib(yt)
    if (!identical(xc, xt) && !identical(yc, yt) && take(xc, yc)) {
      roles[["external_X_val"]] <- xc
      roles[["external_y_val"]] <- yc
    }
    return(roles)
  }

  # (b) SUMMARY external (external_sumstats + a reference panel). Its lambda is
  # still selected on an INDIVIDUAL-level val split (predictions are scored against
  # a phenotype), but there is no external_X to take a sibling of -- so the old
  # single-anchor rule never fired here, and an omitted val silently fell back to an
  # information criterion. Observed on a real 7B run: it dropped external_X_val /
  # external_y_val, the fit fell back to Cp, and the shipped validation set (which
  # exists precisely to tune the external) went unused.
  #
  # Derive the cohort prefix from the sumstats filename (height_EUR_GWAS_training
  # -> height_EUR) and look for that cohort's X/phenotype validation split.
  ss <- roles[["external_sumstats"]]
  if (is.null(ss) || length(ss) != 1L || !nzchar(ss)) return(roles)
  base <- basename(as.character(ss))
  prefix <- sub("_(GWAS|gwas|sumstats|SUMSTATS).*$", "", base)
  if (!nzchar(prefix) || identical(prefix, base)) return(roles)
  exts <- c(".txt.gz", ".txt", ".tsv.gz", ".tsv", ".csv.gz", ".csv", ".gz")
  for (e in exts) {
    xc <- paste0(prefix, "_X_validation", e)
    yc <- paste0(prefix, "_pheno_validation", e)
    if (take(xc, yc)) {
      roles[["external_X_val"]] <- xc
      roles[["external_y_val"]] <- yc
      return(roles)
    }
  }
  roles
}

# The same discovery, for brier_full's NUMBERED cohort roles (external_X_1 /
# external_y_1 -> external_X_1_val / external_y_1_val).
#
# brier_full's externals are RAW cohorts, and each external-only comparator is a
# single-cohort fit that must be selected on ITS OWN held-out data: selecting it on the
# TARGET's validation set would leak target data into a comparator whose entire point is
# to be purely external. Observed on a real run: the case shipped a EUR validation split
# precisely so the comparator could be tuned on it, the agent omitted the roles, and the
# comparator silently fell back to BIC. Same slip, same lever as .discover_target_splits:
# fill it from the training role's sibling, only when BOTH siblings exist, so a cohort
# that genuinely ships no val still falls through to an IC and a split is never
# fabricated from another split.
.discover_external_cohort_vals <- function(data_dir, roles) {
  sib <- function(f) {
    out <- gsub("training", "validation", f, ignore.case = TRUE)
    gsub("_train", "_val", out, ignore.case = TRUE)
  }
  for (k in 1:20) {
    xr <- sprintf("external_X_%d", k)
    yr <- sprintf("external_y_%d", k)
    xv <- sprintf("external_X_%d_val", k)
    yv <- sprintf("external_y_%d_val", k)
    xt <- roles[[xr]]
    yt <- roles[[yr]]
    if (is.null(xt) || is.null(yt)) next
    if (!is.null(roles[[xv]]) || !is.null(roles[[yv]])) next   # the agent named them
    if (length(xt) != 1L || length(yt) != 1L) next
    xc <- sib(as.character(xt))
    yc <- sib(as.character(yt))
    if (identical(xc, as.character(xt)) || identical(yc, as.character(yt))) next
    if (.exists_in_dir(data_dir, xc) && .exists_in_dir(data_dir, yc)) {
      roles[[xv]] <- xc
      roles[[yv]] <- yc
    }
  }
  roles
}

# An external whose coefficients are all (numerically) zero carries NO information:
# preprocessS/mergeExternals may drop it, the transfer fit silently degenerates to
# no-transfer, and the whole analysis reads as a result when it is not one. This is
# a DATA/SIZE signal (too few external samples for the penalized fit to select
# anything), so report it loudly rather than papering over it by swapping criteria
# until something looks nonzero. Tolerance-based: an IC-selected null model can leave
# floating-point dust (e.g. max|coef| ~ 6e-17), which `all(cf == 0)` would miss.
.external_is_degenerate <- function(cf, tol = 1e-12) {
  !any(abs(cf) > tol)
}

# Diagnostics for every external model prep_auto FITS internally (Bucket B). The
# fit happens inside prep_auto, so a trace-based scorer (and a human) otherwise has
# NO evidence of whether the external is real -- a run once scored full marks while
# beta_external was numerically zero, because "prep_auto returned ok" was the only
# thing visible. Recording the selection criterion and the nonzero count makes the
# external fit auditable from the tool result itself.
.EXT_DIAG <- new.env(parent = emptyenv())
.EXT_DIAG$fits <- list()
# Free-text warnings from the external fit that are not per-fit facts, e.g. a
# validation split that only partially covers the model's predictor panel.
.EXT_DIAG$notes <- character(0)
.record_external_fit <- function(kind, n, p, criteria, cf) {
  .EXT_DIAG$fits[[length(.EXT_DIAG$fits) + 1L]] <- list(
    kind          = kind,
    n_samples     = n,
    n_predictors  = p,
    selected_by   = criteria,
    nonzero_coefs = sum(abs(cf) > 1e-12),
    max_abs_coef  = signif(max(abs(cf)), 4)
  )
}

# ---- external-fit cache ------------------------------------------------------
# Fitting a raw external is the single most expensive thing prep_auto does: a
# penalized fit on a 20k x 10k genotype matrix runs ~7 minutes, and a two-external
# case pays that twice. The agent SELF-CORRECTS -- it fixes a role name, a shape, an
# ancestry -- and every retry re-ran the identical external fit from scratch, so two
# attempts exhausted a 45-minute budget and the case could never finish.
#
# The external fit depends ONLY on the external's own inputs and fit configuration --
# nothing about the target -- so it is safely cacheable across prep_auto invocations
# (each of which is a fresh Rscript process, hence a cache on disk, not in memory).
# The key is the identity of every external input file (path + size + mtime) plus the
# fit configuration, so editing or swapping any input misses the cache. Bump
# .EXT_FIT_CACHE_VERSION whenever the fitting logic changes, so stale fits from an
# older code path can never be served.
.EXT_FIT_CACHE_VERSION <- "1"

.ext_cache_enabled <- function() {
  !nzchar(Sys.getenv("BRIER_MCP_NO_EXT_CACHE", unset = ""))
}

.ext_fit_cache_dir <- function() {
  root <- Sys.getenv("BRIER_MCP_CACHE_DIR", unset = "")
  if (!nzchar(root)) {
    xdg <- Sys.getenv("XDG_CACHE_HOME", unset = "")
    root <- if (nzchar(xdg)) xdg else file.path(path.expand("~"), ".cache")
  }
  d <- file.path(root, "brier-mcp", "external_fits")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

# base-R string hash: md5sum works on files, so round-trip through a temp file.
.md5_string <- function(s) {
  tf <- tempfile()
  on.exit(unlink(tf), add = TRUE)
  writeLines(s, tf)
  unname(tools::md5sum(tf))
}

# NULL when there is nothing to key on (no external inputs resolved).
#
# `target_panel` is part of the key because the external is now SUBSET to the target's
# predictors BEFORE it is fitted, so the fitted coefficients DEPEND on the target's panel.
# Without it, a cached fit from a different target would be replayed and would be wrong.
.ext_fit_cache_key <- function(data_dir, roles, family, standardize_method,
                               ext_ld_ancestry, ext_ld_build, target_panel = NULL) {
  inputs <- c("external_sumstats", "external_ld_panel", "external_snp_info",
              "external_X", "external_y", "external_X_val", "external_y_val")
  parts <- character(0)
  for (k in inputs) {
    if (is.null(roles[[k]])) next
    p <- tryCatch(.role_path(data_dir, roles, k), error = function(e) NULL)
    if (is.null(p) || !file.exists(p)) next
    fi <- file.info(p)
    parts <- c(parts, sprintf("%s=%s|%s|%s", k, normalizePath(p),
                              format(fi$size, scientific = FALSE),
                              format(fi$mtime, "%Y-%m-%dT%H:%M:%OS6")))
  }
  if (length(parts) == 0L) return(NULL)
  cfg <- c(sprintf("family=%s", family),
           sprintf("standardize_method=%s", standardize_method),
           sprintf("ext_ld_ancestry=%s", if (is.null(ext_ld_ancestry)) "" else ext_ld_ancestry),
           sprintf("ext_ld_build=%s", if (is.null(ext_ld_build)) "" else ext_ld_build),
           sprintf("target_panel=%s",
                   if (is.null(target_panel)) ""
                   else .md5_string(paste(sort(as.character(target_panel)), collapse = "\n"))),
           sprintf("version=%s", .EXT_FIT_CACHE_VERSION))
  .md5_string(paste(c(sort(parts), cfg), collapse = "\n"))
}

.ext_fit_cache_get <- function(key) {
  f <- file.path(.ext_fit_cache_dir(), paste0(key, ".rds"))
  if (!file.exists(f)) return(NULL)
  # A corrupt or half-written entry must degrade to a cache MISS (refit), never an
  # error: a cache is an optimization and must not be able to break a run.
  tryCatch({
    v <- readRDS(f)
    if (is.list(v) && !is.null(v$out) && !is.null(v$diag)) v else NULL
  }, error = function(e) NULL)
}

.ext_fit_cache_put <- function(key, out, diag) {
  f <- file.path(.ext_fit_cache_dir(), paste0(key, ".rds"))
  tmp <- paste0(f, ".tmp", Sys.getpid())
  tryCatch({
    saveRDS(list(out = out, diag = diag), tmp)
    file.rename(tmp, f)   # atomic: a concurrent reader never sees a partial file
  }, error = function(e) {
    unlink(tmp)
    NULL
  })
  invisible(NULL)
}


# Align a validation genotype matrix to a FITTED MODEL's predictor panel.
#
# The val is a SCORING set, so it never gets to reshape the model: the model's panel
# is fixed by its training data, and the val is projected onto it. Three cases:
#   * a panel predictor the val CARRIES      -> use it (already standardized);
#   * a panel predictor the val LACKS        -> 0. On the standardized scale 0 IS the
#     mean, so the predictor drops out of the score. That is mean imputation, and it
#     is the standard PRS-scoring convention: it makes the val score equal to scoring
#     on the shared predictors alone.
#   * a val predictor NOT in the panel       -> dropped (no coefficient exists).
#
# Without this, prep_auto silently assumed the val's columns matched the model's
# panel exactly AND positionally: a partially-overlapping val died with "beta has
# 9000 rows but X has 7000 columns", and a same-width-but-differently-ordered val
# would have MISALIGNED silently and tuned lambda on garbage.
#
# `Xv_std` must already be on the model's standardized scale (the caller applies the
# right standardizer, which differs between the summary and individual branches).
#
# NOTE: this matches by NAME only. It does NOT check allele orientation, so a val
# coded on the opposite allele aligns silently and is WRONG. Same limitation as
# brier_full's pooling; the target/external TRAINING alignment IS allele-aware
# (preprocessI/preprocessS), the val is not.
# `fill` is the value a MISSING predictor takes, and it must be the one that makes that
# predictor contribute NOTHING. It is NOT always 0:
#   * caller standardized already (the summary branches) -> fill = 0, because on the
#     standardized scale 0 IS the mean.
#   * caller standardizes AFTER (the individual branches, which apply a TRAINING
#     standardizer) -> fill = the training CENTER, so (center - center)/scale = 0.
#   * caller does NOT standardize at all (BRIERi/BRIERfull may be fit on the raw scale)
#     -> fill = the training column MEAN, in the raw scale. A literal 0 here would be a
#     REAL GENOTYPE (homozygous reference), not "no information", and would silently
#     shift every prediction.
# The caller picks it; this function only applies it.
.align_split_to_panel <- function(Xv, panel, fill = 0) {
  vn <- colnames(Xv)
  if (is.null(vn)) {
    if (ncol(Xv) != length(panel)) {
      stop(sprintf(paste0("the split has %d columns but the model has %d predictors, ",
                          "and the split has no column names to align on"),
                   ncol(Xv), length(panel)), call. = FALSE)
    }
    out <- Xv                       # unnamed but the right width: positional agreement
    attr(out, "coverage") <- length(panel)
    attr(out, "n_panel") <- length(panel)
    return(out)
  }
  idx <- match(panel, vn)
  hit <- !is.na(idx)
  fill <- if (length(fill) == 1L) rep(as.numeric(fill), length(panel))
          else as.numeric(fill)
  out <- matrix(rep(fill, each = nrow(Xv)), nrow = nrow(Xv), ncol = length(panel),
                dimnames = list(rownames(Xv), panel))
  if (any(hit)) out[, hit] <- Xv[, idx[hit], drop = FALSE]
  attr(out, "coverage") <- sum(hit)
  attr(out, "n_panel") <- length(panel)
  out
}


# The 80% COVERAGE POLICY (PREP_AUTO_DESIGN.md 3.1). Coverage is the fraction of the
# FITTED MODEL's predictor panel that a split actually carries, matched by name.
#
#   coverage >  threshold   use the split; the missing predictors are imputed (see the
#                           `fill` note above) and therefore contribute nothing.
#   coverage <= threshold   REFUSE it:
#                             VAL  -> fall back to an information criterion.
#                             TEST -> ABORT. There is no IC substitute for evaluation, so
#                                     a test split this thin cannot report a metric at all.
#
# WHY REFUSE rather than quietly score the overlap: a partial split scores a model that
# cannot see part of its own panel. The missing predictors contribute nothing, so the
# metric is computed against a DIFFERENT model than the one being selected, and the chosen
# lambda is biased while looking perfectly healthy. That is the same silent-wrongness class
# as the hollow pass (a numerically-zero external that once scored full marks).
#
# The threshold is a PARAMETER, never hard-coded. Env override for the operator.
.COVERAGE_MIN_DEFAULT <- 0.8


# VARIANT QC ran all along, but invisibly, and one of its two steps was wrong.
#
# BRIER's preprocessors dropped multi-allelic positions (correct, and structural: CHR:BP is
# the join key, so a duplicated position makes the match undecidable) AND blanket-dropped
# every strand-ambiguous pair, unconditionally, even with no external to be ambiguous
# against. They reported both counts; prep_auto called them with verbose = FALSE and threw
# the counts away, so a run could lose ~15% of a real panel and say nothing.
#
# Both are fixed below: the counts are surfaced (.align_counts), and strand ambiguity is
# RESOLVED by allele frequency rather than blanket-dropped (.match_to_ref).
# =============================================================================
# THE ALIGNER (PREP_AUTO_DESIGN.md 5.0 to 5.6). Replaces BRIER::preprocessI and
# BRIER::preprocessS.
#
# WHY REPLACE THEM: they hard-require CHR/BP/REF/ALT, which IS their identity model. BRIER
# itself is general (predictors "can be SNPs, gene expression, proteins"), but its
# preprocessors are not: a gene's expression level has no "opposite allele", so orientation
# is UNDEFINED for it and the whole flip/QC/Berisa machinery is genotype-specific. The
# genotype and non-genotype paths therefore CANNOT be parallel on top of them.
#
# THE ABSTRACTION: a `predictor_type` supplies four hooks, and everything else is shared:
#
#              | genotype                          | generic (expression, protein, ...)
#   identity   | CHR:BP plus the allele pair        | a NAME
#   orientation| a flip is meaningful: negate       | UNDEFINED. There is nothing to flip.
#   qc         | duplicate CHR:BP + palindromic     | duplicate names
#   ld         | Berisa blocks -> block sparse      | plain correlation
#
# The generic path is the genotype path with the ORIENTATION MACHINERY REMOVED. Nothing else
# differs.
#
# THE RISK: allele orientation is the subtlest code here and its failures are SILENT. These
# functions are gated on the T3 exact-md5 answer keys (computed INDEPENDENTLY of prep_auto,
# so they are genuine external ground truth) plus a DIFFERENTIAL TEST against preprocessI/S.
# We keep the preprocessors as a verification ORACLE, not as a runtime dependency.

# QC one variant map. Returns a logical keep vector plus the counts.
#
# MULTI-ALLELIC is the ONLY drop here, and it is MANDATORY and structural: the join key IS
# CHR:BP, so a position carrying more than one REF/ALT pair makes the match undecidable.
# Drop EVERY variant there (not just the extras): we cannot know which one we matched.
#
# STRAND-AMBIGUITY is deliberately NOT handled here. It is not a property of a dataset, it is
# a property of a PAIR of datasets, and it is RESOLVABLE rather than merely droppable. It
# therefore lives in .match_to_ref, which is the only place that can see both sides. (This
# is where preprocessS went wrong: it dropped palindromic variants from the target
# unconditionally, EVEN WITH NO EXTERNAL to be ambiguous against, which is pure loss, and on
# real data palindromic variants are ~15% of common SNPs.)
.qc_variants <- function(map, predictor_type = "genotype") {
  n <- nrow(map)
  keep <- rep(TRUE, n)
  n_ma <- 0L

  if (identical(predictor_type, "genotype") &&
      all(c("CHR", "BP") %in% colnames(map))) {
    key <- paste(map$CHR, map$BP, sep = ":")
    dup <- key %in% key[duplicated(key)]
    if (any(dup)) { keep <- keep & !dup; n_ma <- sum(dup) }
  } else {
    # GENERIC predictors: the identity is a NAME, so the only QC is a duplicate name.
    nm <- as.character(map$varnames)
    dup <- nm %in% nm[duplicated(nm)]
    if (any(dup)) { keep <- keep & !dup; n_ma <- sum(dup) }
  }
  list(keep = keep, n_multiallelic = n_ma)
}


# What KIND of predictor is this? The answer selects the identity model, and everything
# orientation-related follows from it.
#
# BRIER is general: a predictor can be a SNP, a gene's expression level, a protein
# abundance. But orientation is a GENOTYPE concept. A gene's expression level has no
# "opposite allele", so there is nothing to flip, nothing to be strand-ambiguous about, and
# no Berisa LD block to assign it to. The generic path is the genotype path with the
# orientation machinery REMOVED; it is not a different pipeline.
#
# DETECTED, not asked. A variant map that carries CHR and BP is a genome; one that does not
# cannot be. Making this a required parameter would just be one more thing for a small model
# to get wrong, and it is derivable from the data. An explicit value still wins, for the case
# where a map happens to carry coordinates that are not genomic.
.resolve_predictor_type <- function(pt, map) {
  norm <- function(x) {
    x <- tolower(as.character(x))
    if (x %in% c("snp", "genotype", "genetic", "variant")) return("genotype")
    if (x %in% c("gene_expression", "expression", "protein", "proteomic", "generic",
                 "continuous", "other")) return("generic")
    x
  }
  if (!is.null(pt) && nzchar(pt) && !identical(tolower(pt), "auto")) {
    v <- norm(pt)
    if (!v %in% c("genotype", "generic")) {
      stop(sprintf(paste0("predictor_type must be 'auto', 'genotype' (SNP) or 'generic' ",
                          "(gene expression, protein, ...); got '%s'"), pt), call. = FALSE)
    }
    return(v)
  }
  if (!is.null(map) && all(c("CHR", "BP") %in% colnames(map))) "genotype" else "generic"
}


# Is this allele pair PALINDROMIC (A/T or C/G)? Such a pair reads the same on either strand,
# which is what makes its orientation ambiguous ACROSS datasets.
.is_palindromic <- function(ref_allele, alt_allele) {
  r <- toupper(as.character(ref_allele)); a <- toupper(as.character(alt_allele))
  p <- (r == "A" & a == "T") | (r == "T" & a == "A") |
       (r == "C" & a == "G") | (r == "G" & a == "C")
  p[is.na(p)] <- FALSE
  p
}


# The ALT-allele frequency, if the table carries one. NULL if it does not.
#
# It must be ALT-REFERENCED. A bare `MAF` is NOT usable: it is the MINOR allele's frequency
# and does not say WHICH allele is minor, so it cannot orient anything.
.alt_freq <- function(tbl) {
  cn <- colnames(tbl)
  for (nm in c("AF", "EAF", "ALT_AF", "ALT_FREQ", "AF_ALT", "FREQ", "A1_FREQ")) {
    j <- match(tolower(nm), tolower(cn))
    if (!is.na(j)) {
      v <- suppressWarnings(as.numeric(tbl[[cn[j]]]))
      if (any(is.finite(v))) return(v)
    }
  }
  NULL
}


# Match `tbl` to the reference map `ref`, by the predictor type's identity key.
#
# Returns, FOR EACH ROW OF `ref`:
#   idx         the row of `tbl` it matches, or NA
#   flip        TRUE when `tbl`'s effect must be NEGATED to speak in ref's orientation. This
#               is the flip LIST: RECORDED here, APPLIED by the caller. MATCH EARLY, FLIP LATE.
#   undecidable TRUE when the variant matched but its orientation CANNOT be determined. The
#               caller decides what that costs (the target drops the predictor, since it then
#               has no usable signal; an external imputes 0, since it then simply contributes
#               no transfer there).
#
# STRAND AMBIGUITY. For a PALINDROMIC pair (A/T, C/G) the allele letters carry NO orientation
# information at all: A/T on the plus strand reads as T/A on the minus strand, so an apparent
# swap could be a genuine allele swap OR a strand flip, and an apparent MATCH could equally
# be either. Both appearances are ambiguous. Allele frequency is the only thing that can
# separate them:
#
#     same effect allele  ->  AF_tbl ~= AF_ref
#     opposite            ->  AF_tbl ~= 1 - AF_ref
#
# So the decision collapses to ONE rule, independent of how the letters happen to read:
# NEGATE iff AF_tbl is closer to 1 - AF_ref than to AF_ref, and only by a clear MARGIN.
# Requiring the margin is what makes the undecidable band fall out for free rather than
# needing a hard-coded MAF window: as AF_ref approaches 0.5, AF_ref and 1 - AF_ref converge,
# the two distances converge with them, and the variant is declared undecidable on its own.
#
# CAVEAT, and it is why the margin exists: AF differs GENUINELY across ancestries, so a
# cross-ancestry comparison (an AFR target against a EUR external, exactly our benchmark)
# has real drift on top of the signal. The margin tolerates drift; a variant whose two
# hypotheses are close enough to be confused by drift is refused, not guessed.
#
#   ambiguous = "resolve"  (default) resolve by AF; drop only the undecidable. With no AF on
#                          either side, fall back to dropping (loudly): guessing is worse.
#               "drop"     blanket-drop every palindromic match (what preprocessS did).
#               "keep"     trust the coding, never drop. Only for data known to be on one
#                          consistent strand.
.match_to_ref <- function(tbl, ref, predictor_type = "genotype",
                          ambiguous = "resolve", af_margin = 0.1) {
  n <- nrow(ref)
  if (identical(predictor_type, "genotype") &&
      all(c("CHR", "BP", "REF", "ALT") %in% colnames(tbl)) &&
      all(c("CHR", "BP", "REF", "ALT") %in% colnames(ref))) {
    k_ref <- paste(ref$CHR, ref$BP, sep = ":")
    k_tbl <- paste(tbl$CHR, tbl$BP, sep = ":")
    m <- match(k_ref, k_tbl)

    up <- function(x) toupper(as.character(x))
    ref_pair <- paste(up(ref$REF), up(ref$ALT), sep = "/")
    tbl_same <- paste(up(tbl$REF[m]), up(tbl$ALT[m]), sep = "/")
    tbl_swap <- paste(up(tbl$ALT[m]), up(tbl$REF[m]), sep = "/")

    same <- !is.na(m) & ref_pair == tbl_same
    flip <- !is.na(m) & ref_pair == tbl_swap & !same
    m[!(same | flip)] <- NA_integer_        # allele mismatch is NOT a match
    flip <- ifelse(is.na(m), FALSE, flip)

    undec <- rep(FALSE, n)
    n_resolved <- 0L; n_no_af <- 0L
    pal <- !is.na(m) & .is_palindromic(ref$REF, ref$ALT)

    if (any(pal) && !identical(ambiguous, "keep")) {
      af_ref <- .alt_freq(ref)
      af_tbl <- .alt_freq(tbl)
      if (identical(ambiguous, "drop") || is.null(af_ref) || is.null(af_tbl)) {
        # No frequency on one side (or the caller asked for the conservative rule): the
        # letters alone cannot orient a palindrome, so refuse it rather than guess.
        if (!identical(ambiguous, "drop")) n_no_af <- sum(pal)
        undec[pal] <- TRUE
      } else {
        pr <- af_ref[seq_len(n)]
        pt <- af_tbl[m]
        d_same <- abs(pt - pr)
        d_swap <- abs(pt - (1 - pr))
        decided_same <- pal & is.finite(d_same) & is.finite(d_swap) &
                        (d_same < d_swap - af_margin)
        decided_swap <- pal & is.finite(d_same) & is.finite(d_swap) &
                        (d_swap < d_same - af_margin)
        # The AF verdict OVERRIDES the letters: for a palindrome the letters are noise.
        flip[decided_same] <- FALSE
        flip[decided_swap] <- TRUE
        n_resolved <- sum(decided_same | decided_swap)
        undec[pal & !(decided_same | decided_swap)] <- TRUE
      }
      flip[undec] <- FALSE
    }
    return(list(idx = m, flip = flip, undecidable = undec,
                n_ambiguous = sum(pal), n_ambiguous_resolved = n_resolved,
                n_ambiguous_no_af = n_no_af))
  }
  # Name identity (generic predictors, or a genotype table with no coordinates). There is no
  # opposite allele of an expression level, so orientation is undefined and a name either
  # matches or it does not.
  m <- match(as.character(ref$varnames), as.character(tbl$varnames))
  list(idx = m, flip = rep(FALSE, n), undecidable = rep(FALSE, n),
       n_ambiguous = 0L, n_ambiguous_resolved = 0L, n_ambiguous_no_af = 0L)
}


# THE ALIGNER. One function, replacing preprocessI (no target sumstats) and preprocessS
# (with one). Returns the SAME shapes prep_auto already consumes, so it is a drop-in:
#
#   keep        indices into `ref` of the surviving predictors, in panel order
#   sumstats    the target sumstats, aligned to `keep`, corr SIGN-CORRECTED and derived if
#               needed. NULL when there is no target sumstats (the brier_i case).
#   beta        p x M coefficient matrix on the surviving panel: 0 where an external does
#               not cover a predictor (imputed, so it simply contributes no transfer), and
#               external-only predictors dropped. NULL when there is no external.
#   n_*         every count, so the caller can SURFACE them (BRIER computed these and
#               prep_auto threw them away).
#
# IMPUTE 0, DO NOT INTERSECT: a target predictor the external does not cover just means "no
# transfer contribution there". Intersecting would discard target signal for nothing. (For
# brier_full, which pools RAW genotypes and cannot impute one, the intersection genuinely IS
# required, and that path does not come through here.)
#
# VARNAMES: we keep the ORIGINAL names. preprocessS rewrote them to CHR:BP:REF:ALT, which
# every T3 answer file had to carry a note about. Verified safe: BRIERs consumes
# `sumstats$corr` POSITIONALLY (XtY <- as.numeric(as.vector(sumstats$corr))) and uses
# varnames only as a label.
.align_predictors <- function(ref, target_ss = NULL, target_ind = NULL, ext_tab = NULL,
                              predictor_type = "genotype", ambiguous = "resolve",
                              af_margin = 0.1) {
  if (!"varnames" %in% colnames(ref)) {
    stop("the variant map needs a 'varnames' column", call. = FALSE)
  }
  out <- list(n_multiallelic = 0L, n_flipped_target = 0L, n_flipped_external = 0L,
              n_unmatched_target = 0L, n_ambiguous = 0L, n_ambiguous_resolved = 0L,
              n_ambiguous_dropped = 0L, n_ambiguous_no_af = 0L)
  bump <- function(mt) {
    out$n_ambiguous <<- out$n_ambiguous + mt$n_ambiguous
    out$n_ambiguous_resolved <<- out$n_ambiguous_resolved + mt$n_ambiguous_resolved
    out$n_ambiguous_no_af <<- out$n_ambiguous_no_af + mt$n_ambiguous_no_af
    out$n_ambiguous_dropped <<- out$n_ambiguous_dropped + sum(mt$undecidable)
  }

  # --- 1. QC the reference panel ------------------------------------------------
  q <- .qc_variants(ref, predictor_type)
  out$n_multiallelic <- q$n_multiallelic
  keep_idx <- which(q$keep)
  if (!length(keep_idx)) stop("every predictor was dropped by QC", call. = FALSE)
  ref_q <- ref[keep_idx, , drop = FALSE]

  # --- 2. the TARGET's sumstats, aligned to the reference (brier_s) --------------
  ss_out <- NULL
  if (!is.null(target_ss)) {
    qs <- .qc_variants(target_ss, predictor_type)
    ss_q <- target_ss[qs$keep, , drop = FALSE]
    out$n_multiallelic <- out$n_multiallelic + qs$n_multiallelic

    mt <- .match_to_ref(ss_q, ref_q, predictor_type, ambiguous, af_margin)
    bump(mt)
    # An UNDECIDABLE target variant has no usable signal: we cannot say which allele its
    # effect refers to, and unlike a missing external coefficient there is nothing to impute.
    # So it leaves the panel, exactly as an unmatched one does.
    got <- !is.na(mt$idx) & !mt$undecidable
    out$n_unmatched_target <- sum(is.na(mt$idx))
    if (!any(got)) {
      stop("the target's summary statistics share NO predictors with the variant map",
           call. = FALSE)
    }
    # A reference predictor the sumstats does not cover has no target signal at all, so it
    # cannot be kept: unlike a missing EXTERNAL coefficient (which just means no transfer),
    # a missing target effect is not imputable.
    keep_idx <- keep_idx[got]
    ref_q <- ref_q[got, , drop = FALSE]
    src <- mt$idx[got]
    flip <- mt$flip[got]
    out$n_flipped_target <- sum(flip)

    ss_out <- ss_q[src, , drop = FALSE]

    # DERIVE corr when the sumstats ships none (preprocessS's target.ind = "gwas" branch).
    cn <- colnames(ss_out)
    corr_col <- cn[match("corr", tolower(cn))]
    want_gwas <- identical(target_ind, "gwas") ||
                 (is.null(target_ind) && is.na(corr_col))
    if (want_gwas || is.na(corr_col)) {
      cmap <- .ss_col_map(ss_out)
      pcol <- cmap[["p"]]; ncol_ <- cmap[["n"]]; bcol <- cmap[["beta"]]
      if (!all(c(pcol, ncol_) %in% cn)) {
        stop(sprintf(paste0("the sumstats has no `corr` column, so it must be derived from ",
                            "p-value / N / effect sign, but columns %s are missing"),
                     paste(setdiff(c(pcol, ncol_), cn), collapse = ", ")), call. = FALSE)
      }
      sgn <- if ("sgn" %in% cn) sign(ss_out[["sgn"]])
             else if (bcol %in% cn) sign(ss_out[[bcol]])
             else stop("deriving corr needs a `sgn` or effect (`beta`) column", call. = FALSE)
      ss_out$corr <- BRIER::p2cor(ss_out[[pcol]], ss_out[[ncol_]], sign = sgn)
      corr_col <- "corr"
    } else if (!identical(corr_col, "corr")) {
      ss_out$corr <- ss_out[[corr_col]]
    }

    # SIGN-CORRECT: the reference is the orientation, so a swapped sumstats effect is negated
    # and the reference's alleles are adopted. This is the whole point of aligning the target
    # TO the LD rather than the other way round.
    ss_out$corr[flip] <- -ss_out$corr[flip]
    if (all(c("REF", "ALT") %in% colnames(ref_q))) {
      ss_out$REF <- ref_q$REF
      ss_out$ALT <- ref_q$ALT
    }
    ss_out$varnames <- ref_q$varnames     # ORIGINAL names, not CHR:BP:REF:ALT
    rownames(ss_out) <- NULL
  }

  # --- 3. the EXTERNALS, aligned to the surviving panel --------------------------
  beta <- NULL
  if (!is.null(ext_tab) && nrow(ext_tab) > 0) {
    coef_cols <- grep("^coef", colnames(ext_tab), value = TRUE)
    if (!length(coef_cols)) {
      stop("the external table has no coefficient column", call. = FALSE)
    }
    qe <- .qc_variants(ext_tab, predictor_type)
    ext_q <- ext_tab[qe$keep, , drop = FALSE]

    me <- .match_to_ref(ext_q, ref_q, predictor_type, ambiguous, af_margin)
    bump(me)
    # An UNDECIDABLE external coefficient cannot be oriented, so it is treated exactly like a
    # coefficient the external never had: imputed to 0. The target KEEPS the predictor (it has
    # its own data for it); the external simply contributes no transfer there. This is the
    # same impute-vs-drop asymmetry as everywhere else, and it is why the target above drops
    # the variant while the external here does not.
    got <- !is.na(me$idx) & !me$undecidable
    out$n_flipped_external <- sum(me$flip[got])
    out$n_external_missing <- sum(!got)          # imputed to 0
    out$n_external_only <- nrow(ext_q) - sum(got)  # dropped

    beta <- matrix(0.0, nrow = nrow(ref_q), ncol = length(coef_cols),
                   dimnames = list(NULL, coef_cols))
    if (any(got)) {
      vals <- as.matrix(ext_q[me$idx[got], coef_cols, drop = FALSE])
      storage.mode(vals) <- "double"
      vals[is.na(vals)] <- 0
      # FLIP LATE: negate the coefficient wherever the external's alleles were swapped.
      fl <- me$flip[got]
      if (any(fl)) vals[fl, ] <- -vals[fl, , drop = FALSE]
      beta[got, ] <- vals
    }
  }

  out$keep <- keep_idx
  out$sumstats <- ss_out
  out$beta <- beta
  out
}


# Surface EVERY count the aligner produced. BRIER computed these and prep_auto threw them
# away (verbose = FALSE, counts never read), so a run could silently lose predictors to QC,
# or silently correct hundreds of allele flips, and nothing in the report, the trace or the
# scorer would ever say so. Silence on a clean run; loud on anything else.
.align_counts <- function(al) {
  out <- character(0)
  # An absent count is integer(0), and a bare `if (!is.na(x))` on that dies with "missing
  # value where TRUE/FALSE needed". Reduce to a scalar first: a diagnostic must never be
  # able to break the run it is reporting on (this exact bug once took out all 7 T3 cases).
  say <- function(n, msg) {
    v <- suppressWarnings(as.integer(n))
    if (length(v) != 1L || is.na(v) || v <= 0L) return(invisible(NULL))
    out <<- c(out, sprintf(msg, v))
  }
  say(al$n_multiallelic,
      "_notice_qc: dropped %d MULTI-ALLELIC predictor(s) (duplicated CHR:BP, which the join key cannot resolve)")
  say(al$n_ambiguous,
      "_notice_qc: %d matched predictor(s) are STRAND-AMBIGUOUS (palindromic A/T or C/G: the allele letters read the same on either strand, so they cannot orient the effect)")
  say(al$n_ambiguous_resolved,
      "_notice_qc: RESOLVED %d of them by allele frequency (AF_tbl near AF_ref means the same effect allele; near 1 - AF_ref means the opposite)")
  say(al$n_ambiguous_no_af,
      "_notice_qc: %d could not be resolved because no ALT-allele frequency is available on both sides (a bare MAF does not say WHICH allele is minor, so it cannot orient anything)")
  say(al$n_ambiguous_dropped,
      "_notice_qc: %d strand-ambiguous predictor(s) stayed UNDECIDABLE (their two orientations are indistinguishable within the frequency margin); a target predictor is dropped, an external coefficient is imputed to 0")
  say(al$n_flipped_target,
      "_notice_align: sign-corrected %d TARGET effect(s) whose alleles were swapped relative to the reference")
  say(al$n_flipped_external,
      "_notice_align: sign-corrected %d EXTERNAL coefficient(s) whose alleles were swapped relative to the target")
  say(al$n_unmatched_target,
      "_notice_align: dropped %d reference predictor(s) the target's summary statistics do not cover")
  say(al$n_external_missing,
      "_notice_align: %d target predictor(s) are not covered by an external; their coefficient is imputed to 0 (no transfer contribution there)")
  say(al$n_external_only,
      "_notice_align: dropped %d external-only predictor(s) (the target has no data for them)")
  out
}


.coverage_min <- function(x = NULL) {
  if (!is.null(x) && is.finite(suppressWarnings(as.numeric(x)))) {
    v <- as.numeric(x)
    if (v > 0 && v <= 1) return(v)
  }
  e <- suppressWarnings(as.numeric(Sys.getenv("BRIER_MCP_COVERAGE_MIN", "")))
  if (is.finite(e) && e > 0 && e <= 1) return(e)
  .COVERAGE_MIN_DEFAULT
}

# Returns list(ok, coverage, frac, note). `which` is "validation" or "testing" and is used
# only to word the message; the ABORT-vs-fall-back decision belongs to the caller, because
# only it knows whether an IC substitute exists.
.check_coverage <- function(X_aligned, panel, which = "validation",
                            coverage_min = NULL) {
  thr <- .coverage_min(coverage_min)
  cov_n <- attr(X_aligned, "coverage")
  if (is.null(cov_n)) cov_n <- length(panel)
  frac <- if (length(panel) > 0) cov_n / length(panel) else 1
  ok <- frac > thr
  note <- if (ok && cov_n < length(panel)) {
    sprintf(paste0("%s split covers %d of the model's %d predictors (%.1f%%); the ",
                   "missing %d are imputed to the training mean and contribute nothing"),
            which, cov_n, length(panel), 100 * frac, length(panel) - cov_n)
  } else if (!ok) {
    sprintf(paste0("%s split covers only %d of the model's %d predictors (%.1f%%), at or ",
                   "below the %.0f%% threshold"),
            which, cov_n, length(panel), 100 * frac, 100 * thr)
  } else NULL
  list(ok = ok, coverage = cov_n, frac = frac, threshold = thr, note = note)
}


# SUBSET THEN FIT (PREP_AUTO_DESIGN.md 4.1). Restrict the external to the predictors the
# TARGET actually has, BEFORE fitting it.
#
# WHY THIS IS NOT COSMETIC. In a joint penalized model every coefficient is estimated
# CONDITIONAL on the other predictors in the model. Fitting the external on its own full
# panel and then truncating the vector to the target's predictors leaves coefficients that
# were estimated in the presence of predictors that are no longer there: a biased shadow of
# "the external's model over the target's predictors", not that model itself. And
# beta.external is the SHRINKAGE TARGET for the target's coefficient vector, which lives on
# the target's panel. So the honest object is the one obtained by restricting first.
#
# MATCH EARLY, FLIP LATE: this restricts the external's variant UNIVERSE but leaves it in
# ITS OWN allele orientation. The single flip into the target's orientation is applied later,
# to the FITTED coefficient vector, which is also why the external's val split never needs a
# cross-cohort flip.
#
# It also happens to make the external's LD build cheaper, since the LD is then only ever
# built over the predictors the analysis can actually use.
#
# Returns the row indices of `ext_map` the target covers.
.restrict_to_target_panel <- function(ext_map, target_map, predictor_type = "genotype") {
  if (is.null(target_map) || is.null(ext_map)) return(seq_len(nrow(ext_map)))
  if (identical(ext_map, target_map)) return(seq_len(nrow(ext_map)))
  m <- .match_to_ref(ext_map, target_map, predictor_type, ambiguous = "keep")
  keep <- sort(unique(m$idx[!is.na(m$idx)]))
  if (!length(keep)) {
    stop(paste(
      "the external shares NO predictors with the target's panel, so no external model",
      "can be fitted over the target's predictors. Check that the two maps use the same",
      "genome build and coordinate convention."), call. = FALSE)
  }
  keep
}


# Caching wrapper. The external's val split is discovered FIRST, because it changes
# the selection criterion and therefore the fitted coefficients -- so it must be part
# of the cache key, not applied after a lookup. The TARGET's panel is in the key too:
# the external is subset to it before fitting, so the fit depends on it.
# The agent names external_sumstats and the external's ancestry, then omits the PANEL the
# LD has to be built from -- and loops on the error until the guard aborts the run
# (observed on T2_afr-ind_eur-summary). Same slip, same lever as the target's LD panel:
# discover it. .discover_ld_panel REQUIRES an ancestry-name match, so it can only ever
# find the EXTERNAL's own panel and never silently grab the target's -- which matters,
# because building a EUR external's LD from an AFR panel would be quietly wrong rather
# than loudly broken.
#
# Runs in the CACHING WRAPPER, before the key: the panel is an input to the fit, so it has
# to be part of the cache key or a different panel could hit a stale entry.
.discover_external_ld_panel <- function(data_dir, roles, ext_ld_ancestry = NULL) {
  if (is.null(roles[["external_sumstats"]])) return(roles)
  if (!is.null(roles[["external_ld_panel"]])) return(roles)
  if (is.null(ext_ld_ancestry) || !nzchar(ext_ld_ancestry)) return(roles)
  found <- .discover_ld_panel(data_dir, ext_ld_ancestry)
  if (is.null(found)) return(roles)
  roles[["external_ld_panel"]] <- basename(found)
  roles
}


.fit_external_model <- function(data_dir, roles, target_snp, family,
                                standardize_method,
                                ext_ld_ancestry = NULL, ext_ld_build = NULL) {
  roles <- .discover_external_val(data_dir, roles)
  roles <- .discover_external_ld_panel(data_dir, roles, ext_ld_ancestry)
  key <- .ext_fit_cache_key(data_dir, roles, family, standardize_method,
                            ext_ld_ancestry, ext_ld_build,
                            target_panel = if (!is.null(target_snp))
                              as.character(target_snp$varnames) else NULL)
  if (!is.null(key) && .ext_cache_enabled()) {
    hit <- .ext_fit_cache_get(key)
    if (!is.null(hit)) {
      # Replay the diagnostics so a cached fit is still AUDITABLE: the tool result
      # must carry the same external_fits evidence (selection criterion, nonzero
      # count) as a fresh fit, or the scorer would fail a run purely for being fast.
      d <- hit$diag
      d$cached <- TRUE
      .EXT_DIAG$fits[[length(.EXT_DIAG$fits) + 1L]] <- d
      return(hit$out)
    }
  }
  out <- .fit_external_model_uncached(data_dir, roles, target_snp, family,
                                      standardize_method, ext_ld_ancestry, ext_ld_build)
  if (!is.null(out) && !is.null(key) && .ext_cache_enabled() &&
      length(.EXT_DIAG$fits) > 0L) {
    .ext_fit_cache_put(key, out, .EXT_DIAG$fits[[length(.EXT_DIAG$fits)]])
  }
  out
}


.fit_external_model_uncached <- function(data_dir, roles, target_snp, family,
                                         standardize_method,
                                         ext_ld_ancestry = NULL, ext_ld_build = NULL) {
  ext_snp <- if (!is.null(roles[["external_snp_info"]])) {
    .read_role(data_dir, roles, "external_snp_info")
  } else target_snp
  has_val <- !is.null(roles[["external_X_val"]]) && !is.null(roles[["external_y_val"]])

  # SUBSET THEN FIT: the external's universe is the predictors the TARGET has. Everything
  # downstream (the LD build, the sumstats alignment, the genotype matrix) keys off ext_snp,
  # so restricting it here restricts the whole fit.
  n_ext_before <- nrow(ext_snp)
  ext_keep <- .restrict_to_target_panel(ext_snp, target_snp)
  ext_snp <- ext_snp[ext_keep, , drop = FALSE]
  ext_vars <- as.character(ext_snp$varnames)
  if (length(ext_keep) < n_ext_before) {
    .EXT_DIAG$notes <- c(.EXT_DIAG$notes, sprintf(paste0(
      "subset the external to the target's panel BEFORE fitting: %d of its %d predictors ",
      "survive. (Fitting on the full panel and truncating afterwards would leave ",
      "coefficients estimated conditional on predictors the target does not have.)"),
      length(ext_keep), n_ext_before))
  }

  if (!is.null(roles[["external_sumstats"]])) {
    # ---- SUMMARY external -> BRIERs(eta=0) ----
    ss <- .read_role(data_dir, roles, "external_sumstats")

    if (is.null(roles[["external_ld_panel"]])) {
      stop(paste(
        "A summary external (external_sumstats) needs external_ld_panel (a",
        "reference genotype panel) to build its LD, plus external_ld_ancestry +",
        "external_ld_build for genotypes."), call. = FALSE)
    }
    built <- .build_ld_from_panel(
      .read_role(data_dir, roles, "external_ld_panel"), ext_snp,
      ancestry = ext_ld_ancestry, build = ext_ld_build,
      keep_vars = ext_vars
    )
    XtX <- built$XtX
    tind <- if ("corr" %in% tolower(colnames(ss))) "corr" else "gwas"
    # The external's OWN sumstats-to-LD alignment: exactly the target-side problem, one
    # cohort in. No external of its own (this IS the external), so ext_tab is NULL.
    al <- .align_predictors(ref = ext_snp, target_ss = ss, target_ind = tind)
    al_notes <- .align_counts(al)
    if (length(al_notes)) {
      .EXT_DIAG$notes <- c(
        .EXT_DIAG$notes,
        paste("the external's own alignment:",
              sub("^_notice_(qc|align): ", "", al_notes)))
    }
    # The aligner keeps the ORIGINAL varnames, which is what the XtX rownames carry, so
    # the two join directly.
    surv_orig <- as.character(ext_snp$varnames)[al$keep]
    li <- match(surv_orig, rownames(XtX))
    keep <- !is.na(li)
    XtX <- XtX[li[keep], li[keep], drop = FALSE]
    ss_keep <- al$sumstats[keep, , drop = FALSE]
    if (!methods::is(XtX, "sparseMatrix")) XtX <- Matrix::Matrix(XtX, sparse = TRUE)
    fit <- BRIER::BRIERs(sumstats = ss_keep, XtX = XtX, family = family,
                         eta.list = c(0))
    ext_panel <- surv_orig[keep]
    ic_fallback <- function() {
      TN <- as.integer(stats::median(
        suppressWarnings(as.numeric(ss[["N"]])), na.rm = TRUE))
      if (is.na(TN)) TN <- nrow(ss)
      BRIER::BRIERs.selection(object = fit, criteria = "Cp", TN = TN)
    }
    used_val <- FALSE
    sel <- if (has_val) {
      Xv_raw <- .geno_matrix(.read_role(data_dir, roles, "external_X_val"))
      # Standardize on the val's OWN moments: a summary external has no training X to
      # take moments from, and the summary coefficients live on the standardized scale.
      # Scale BEFORE the panel fill, so a predictor the val lacks stays exactly 0 (the
      # mean) instead of becoming NaN from a zero-variance column.
      Xv <- .align_split_to_panel(scale(Xv_raw), ext_panel, fill = 0)
      cov <- .check_coverage(Xv, ext_panel, "external validation")
      if (!is.null(cov$note)) .EXT_DIAG$notes <- c(.EXT_DIAG$notes, cov$note)
      if (!cov$ok) {
        # Coverage policy: a val this thin scores a DIFFERENT model than the one being
        # selected, so refuse it and use an IC instead of quietly biasing lambda.
        .EXT_DIAG$notes <- c(.EXT_DIAG$notes,
          "external val REFUSED (below the coverage threshold); selecting by Cp instead")
        ic_fallback()
      } else {
        used_val <- TRUE
        yv <- .pheno_vector(.read_role(data_dir, roles, "external_y_val"))
        if (family == "gaussian") yv <- as.numeric(scale(yv))
        BRIER::BRIERs.selection(object = fit, criteria = "gaussian.mspe",
                                X.val = Xv, y.val = yv)
      }
    } else {
      ic_fallback()
    }
    cf <- as.numeric(coef(sel, which.eta = sel$eta.min.index,
                          which.lambda = sel$lambda.min.index))
    if (.external_is_degenerate(cf)) {
      stop(sprintf(paste0(
        "the external model fit from SUMMARY data selected the NULL model: all %d ",
        "coefficients are zero (selected by %s), so this external carries no ",
        "information and any 'transfer' result from it would be meaningless. This is ",
        "a sample-size signal, not a tuning quirk. Supply external_X_val + ",
        "external_y_val so the external fit is tuned on a held-out split, or use a ",
        "larger external."),
        length(cf), if (used_val) "external-val MSPE" else "Cp"), call. = FALSE)
    }
    coord_cols <- intersect(c("varnames", "CHR", "BP", "REF", "ALT"),
                            colnames(ss_keep))
    out <- ss_keep[, coord_cols, drop = FALSE]
    out$coef <- cf
    crit <- if (used_val) "external-val MSPE (gaussian.mspe)" else "Cp"
    .record_external_fit("summary", NA_integer_, nrow(out), crit, cf)
    attr(out, "fit_note") <- sprintf(
      "summary external: BRIERs(eta=0) on %d SNPs, selected by %s -> %d nonzero",
      nrow(out), crit, sum(abs(cf) > 1e-12))
    return(out)
  }

  if (!is.null(roles[["external_X"]]) && !is.null(roles[["external_y"]])) {
    # ---- INDIVIDUAL external -> BRIERi(eta=0), standardized ----
    Xtr <- .geno_matrix(.read_role(data_dir, roles, "external_X"))
    # SUBSET THEN FIT: restrict to the target's predictors BEFORE fitting, so every
    # coefficient is estimated conditional on the predictors the transfer will actually use.
    # Standardize AFTER the subset: the moments must come from the columns being fitted.
    if (!is.null(target_snp)) {
      j <- match(ext_vars, colnames(Xtr))
      j <- j[!is.na(j)]
      if (!length(j)) {
        stop(paste("the external genotype matrix shares no columns with the target's panel;",
                   "check that both use the same variant naming convention"), call. = FALSE)
      }
      Xtr <- Xtr[, j, drop = FALSE]
    }
    ytr <- .pheno_vector(.read_role(data_dir, roles, "external_y"))
    st <- .fit_standardizer(Xtr, standardize_method)
    Xtr_s <- .apply_standardizer(Xtr, st)
    ymu <- 0; ysd <- 1
    if (family == "gaussian") {
      ymu <- mean(ytr); ysd <- stats::sd(ytr); if (ysd == 0) ysd <- 1
      ytr <- (ytr - ymu) / ysd
    }
    be0 <- matrix(0, ncol(Xtr_s) + 1L, 1L)
    fit <- BRIER::BRIERi(X = Xtr_s, y = ytr, beta.external = be0, eta.list = c(0))
    tr_panel <- colnames(Xtr_s)
    used_val <- FALSE
    sel <- if (has_val) {
      # Align the val to the TRAINING panel FIRST: the standardizer's center/scale are
      # indexed by training column, so an unaligned val silently shifts every predictor.
      # Fill a missing predictor with the training CENTER, which standardizes to exactly
      # 0 and therefore contributes nothing. (NOT a literal 0 in the raw scale: that is a
      # real genotype, not "no information".)
      Xv_raw <- .align_split_to_panel(
        .geno_matrix(.read_role(data_dir, roles, "external_X_val")),
        tr_panel, fill = st$center)
      cov <- .check_coverage(Xv_raw, tr_panel, "external validation")
      if (!is.null(cov$note)) .EXT_DIAG$notes <- c(.EXT_DIAG$notes, cov$note)
      if (!cov$ok) {
        .EXT_DIAG$notes <- c(.EXT_DIAG$notes,
          "external val REFUSED (below the coverage threshold); selecting by BIC instead")
        BRIER::BRIERi.selection(object = fit, criteria = "BIC")
      } else {
        used_val <- TRUE
        Xv <- .apply_standardizer(Xv_raw, st)
        yv <- .pheno_vector(.read_role(data_dir, roles, "external_y_val"))
        if (family == "gaussian") yv <- (yv - ymu) / ysd
        BRIER::BRIERi.selection(object = fit, criteria = "gaussian.mspe",
                                X.val = Xv, y.val = yv)
      }
    } else {
      BRIER::BRIERi.selection(object = fit, criteria = "BIC")
    }
    cf <- as.numeric(coef(sel, which.eta = sel$eta.min.index,
                          which.lambda = sel$lambda.min.index))
    cf <- cf[-1]  # drop intercept; the target fit prepends its own
    if (.external_is_degenerate(cf)) {
      stop(sprintf(paste0(
        "the external model fit from INDIVIDUAL data (%d samples x %d predictors) ",
        "selected the NULL model: all coefficients are zero (selected by %s), so this ",
        "external carries no information and any 'transfer' result from it would be ",
        "meaningless. This is a sample-size signal, not a tuning quirk: at these ",
        "dimensions an information criterion shrinks everything to zero. Supply ",
        "external_X_val + external_y_val so the external fit is tuned on a held-out ",
        "split, or use a larger external cohort."),
        nrow(Xtr_s), ncol(Xtr_s),
        if (used_val) "external-val MSPE" else "BIC"), call. = FALSE)
    }
    vn <- colnames(Xtr_s)
    out <- data.frame(varnames = vn, coef = cf, stringsAsFactors = FALSE)
    if (!is.null(ext_snp) &&
        all(c("varnames", "CHR", "BP", "REF", "ALT") %in% colnames(ext_snp))) {
      idx <- match(vn, as.character(ext_snp$varnames))
      out$CHR <- ext_snp$CHR[idx]; out$BP <- ext_snp$BP[idx]
      out$REF <- ext_snp$REF[idx]; out$ALT <- ext_snp$ALT[idx]
    }
    crit <- if (used_val) "external-val MSPE (gaussian.mspe)" else "BIC"
    .record_external_fit("individual", nrow(Xtr_s), ncol(Xtr_s), crit, cf)
    attr(out, "fit_note") <- sprintf(
      "individual external: BRIERi(eta=0) on standardized %dx%d, selected by %s -> %d nonzero",
      nrow(Xtr_s), ncol(Xtr_s), crit, sum(abs(cf) > 1e-12))
    return(out)
  }
  NULL
}


# ---- external-shape predicates ---------------------------------------------
# Both guard the same failure: a RAW external named under external_coef, which
# expects a PRETRAINED per-variant coefficient table. Kept as standalone predicates
# (not inline in .load_externals) so they are unit-testable.

# TRUE if a table looks like GWAS SUMMARY STATISTICS rather than a fitted model's
# coefficients. The tell is a marginal-effect vocabulary (P / N / SE / STAT / Z /
# BETA / CORR) with NO explicit coefficient column. A real pretrained file here is
# `varnames + coef`, so it never trips; a GWAS (varnames, SNP, CHR, BP, REF, ALT, N,
# BETA, P, corr) always does. This matters because a GWAS otherwise sails straight
# through: it HAS coordinates and a numeric BETA, so nothing errors and the fitter
# silently receives LD-confounded marginal effects as if they were joint coefficients.
.looks_like_sumstats <- function(df) {
  cn <- colnames(df)
  if (any(tolower(cn) %in% c("coef", "weight", "effect"))) return(FALSE)
  markers <- c("P", "PVAL", "P_VALUE", "N", "SE", "STAT", "Z", "BETA", "CORR")
  sum(toupper(cn) %in% markers) >= 2L
}

# TRUE if a table looks like an individual-level GENOTYPE MATRIX (samples x variants)
# rather than a per-variant coefficient table: wide, and most of its COLUMN NAMES are
# variants on the target panel (a coefficient table has variants down the ROWS).
.looks_like_genotype_matrix <- function(df, snp_info) {
  if (ncol(df) <= 50L) return(FALSE)
  if (is.null(snp_info) || !"varnames" %in% colnames(snp_info)) return(FALSE)
  hits <- sum(colnames(df) %in% as.character(snp_info$varnames))
  hits > (0.5 * ncol(df))
}


# A role must point at the KIND of file it names.
#
# On T2_afr-summary_eur-2ind (two INDIVIDUAL-level EUR cohorts) the agent named every
# external role wrongly at once:
#
#     external_sumstats_1 = height_EUR1_pheno_training.txt.gz   # a PHENOTYPE
#     external_ld_panel   = height_EUR_SNP_info.txt.gz          # a VARIANT MAP
#     external_snp_info_1 = height_EUR1_SNP_info.txt.gz         # does not exist
#
# and then looped. The errors it got back were about the missing file and an empty
# variant intersection: true, but downstream of the actual mistake, so they steered it
# nowhere. Name the mistake instead. A phenotype is two columns (an id and a value); it
# is not summary statistics, and a cohort that ships X + phenotype is INDIVIDUAL-level,
# which is a different pair of roles entirely.
.check_raw_external_roles <- function(data_dir, roles) {
  peek <- function(role) {
    tryCatch(.read_role(data_dir, roles, role, required = FALSE),
             error = function(e) NULL)
  }
  for (k in c("", paste0("_", 1:20))) {
    ssr <- paste0("external_sumstats", k)
    if (is.null(roles[[ssr]])) next
    df <- peek(ssr)
    if (is.null(df) || .looks_like_sumstats(df)) next
    f <- basename(as.character(roles[[ssr]])[1])
    cn <- paste(utils::head(colnames(df), 6L), collapse = ", ")
    xr <- paste0("external_X", k)
    yr <- paste0("external_y", k)

    # SAY WHAT THE FILE ACTUALLY IS. The first version of this guard asserted "it looks
    # like a PHENOTYPE" for every non-sumstats file -- including a GENOTYPE MATRIX -- and
    # then told the model to pass that matrix as external_y. That is nonsense, and it
    # oscillated: the model flipped to brier_i (where its external roles were RIGHT), got
    # steered back to brier_s by the summary-target guard, renamed the roles to sumstats
    # again, and looped. The model had the right answer and my error message talked it
    # out of it.
    wide <- ncol(df) > 50L
    kind <- if (wide) "an individual-level GENOTYPE MATRIX (samples x variants)"
            else "a PHENOTYPE (an id column and an outcome)"
    howto <- if (wide) {
      paste0("pass it as ", xr, " = '", f, "' and ", yr,
             " = <that cohort's phenotype file>")
    } else {
      paste0("pass it as ", yr, " = '", f, "' and ", xr,
             " = <that cohort's genotype file>")
    }
    stop(paste0(
      "'", f, "' was passed as ", ssr, ", but it is not GWAS summary statistics (its ",
      "first columns are: ", cn, "; summary statistics carry at least two of ",
      "P / N / SE / STAT / Z / BETA / CORR). It is ", kind, ".\n",
      "This external cohort is INDIVIDUAL-LEVEL, so ", howto, ". prep_auto FITS the ",
      "external model from the individual data itself.\n",
      "KEEP THE SHAPE YOU HAVE. An individual-level EXTERNAL does not change the ",
      "TARGET's shape: a summary target stays shape='brier_s', and its externals are ",
      "still external_X_k + external_y_k. Do NOT rename them to external_sumstats, and ",
      "do NOT re-route the target to brier_i."
    ), call. = FALSE)
  }
  invisible(NULL)
}


# TRUE if the roles map carries any RAW external to fit (Bucket B): a summary
# GWAS (external_sumstats[_k]) or an individual cohort (external_X[_k] +
# external_y[_k]), in the unnumbered OR the numbered (M>1) spelling.
.has_raw_external <- function(roles) {
  if (!is.null(roles[["external_sumstats"]])) return(TRUE)
  if (!is.null(roles[["external_X"]]) && !is.null(roles[["external_y"]])) return(TRUE)
  k <- 1
  repeat {
    sk <- roles[[sprintf("external_sumstats_%d", k)]]
    Xk <- roles[[sprintf("external_X_%d", k)]]
    yk <- roles[[sprintf("external_y_%d", k)]]
    if (!is.null(sk) || (!is.null(Xk) && !is.null(yk))) return(TRUE)
    if (is.null(sk) && is.null(Xk) && is.null(yk)) break
    k <- k + 1
  }
  FALSE
}

# Enumerate the RAW external instances (M >= 1) as a list of per-external roles
# subsets, each with the CANONICAL unnumbered keys .fit_external_model reads
# plus its own ancestry/build. Handles three spellings a small model may use:
#   * unnumbered scalars (external_sumstats / external_X + external_y) -> 1 instance
#   * a packed list under one unnumbered key (external_sumstats = [A, B]) -> one
#     instance per element, sharing the unnumbered ld_panel/snp_info/ancestry/build
#   * numbered (external_sumstats_1/_2, external_X_1/_2 + external_y_1/_2) with
#     per-external external_ld_panel_k / external_snp_info_k / external_*_val_k /
#     external_ld_ancestry_k / external_ld_build_k
# Deduped by the primary source value (same GWAS/X named twice counts once).
.raw_external_instances <- function(roles, ext_ld_ancestry, ext_ld_build) {
  instances <- list()
  seen <- character(0)
  pick <- function(x, i) {
    if (is.null(x)) return(NULL)
    if (length(x) == 1) return(x[[1]])
    if (length(x) >= i) return(x[[i]])
    NULL
  }
  add_summary <- function(ss, panel, snp, valX, valY, anc, bld) {
    key <- paste0("S:", as.character(ss))
    if (is.null(ss) || key %in% seen) return(invisible())
    seen <<- c(seen, key)
    r <- Filter(Negate(is.null), list(
      external_sumstats = ss, external_ld_panel = panel,
      external_snp_info = snp, external_X_val = valX, external_y_val = valY))
    instances[[length(instances) + 1]] <<- list(
      roles = r,
      ancestry = if (!is.null(anc)) anc else ext_ld_ancestry,
      build    = if (!is.null(bld)) bld else ext_ld_build)
  }
  add_indiv <- function(X, y, snp, valX, valY) {
    key <- paste0("I:", as.character(X))
    if (is.null(X) || is.null(y) || key %in% seen) return(invisible())
    seen <<- c(seen, key)
    r <- Filter(Negate(is.null), list(
      external_X = X, external_y = y, external_snp_info = snp,
      external_X_val = valX, external_y_val = valY))
    instances[[length(instances) + 1]] <<- list(
      roles = r, ancestry = ext_ld_ancestry, build = ext_ld_build)
  }

  # unnumbered (scalar or packed list on the primary key)
  ss <- roles[["external_sumstats"]]
  if (!is.null(ss)) {
    for (i in seq_len(length(ss))) {
      add_summary(
        pick(ss, i), pick(roles[["external_ld_panel"]], i),
        pick(roles[["external_snp_info"]], i), pick(roles[["external_X_val"]], i),
        pick(roles[["external_y_val"]], i), pick(roles[["external_ld_ancestry"]], i),
        pick(roles[["external_ld_build"]], i))
    }
  }
  Xu <- roles[["external_X"]]; yu <- roles[["external_y"]]
  if (!is.null(Xu) && !is.null(yu)) {
    for (i in seq_len(max(length(Xu), length(yu)))) {
      add_indiv(
        pick(Xu, i), pick(yu, i), pick(roles[["external_snp_info"]], i),
        pick(roles[["external_X_val"]], i), pick(roles[["external_y_val"]], i))
    }
  }
  # numbered
  k <- 1
  repeat {
    sk <- roles[[sprintf("external_sumstats_%d", k)]]
    Xk <- roles[[sprintf("external_X_%d", k)]]
    yk <- roles[[sprintf("external_y_%d", k)]]
    if (!is.null(sk)) {
      add_summary(
        sk, roles[[sprintf("external_ld_panel_%d", k)]],
        roles[[sprintf("external_snp_info_%d", k)]],
        roles[[sprintf("external_X_val_%d", k)]],
        roles[[sprintf("external_y_val_%d", k)]],
        roles[[sprintf("external_ld_ancestry_%d", k)]],
        roles[[sprintf("external_ld_build_%d", k)]])
    } else if (!is.null(Xk) && !is.null(yk)) {
      add_indiv(
        Xk, yk, roles[[sprintf("external_snp_info_%d", k)]],
        roles[[sprintf("external_X_val_%d", k)]],
        roles[[sprintf("external_y_val_%d", k)]])
    } else break
    k <- k + 1
  }
  instances
}


# `target_panel_map` is the TARGET'S OWN surviving panel, and it is NOT the same thing as
# `snp_info`. snp_info is the variant MAP (for brier_s it is the LD's map, which is usually
# LARGER than the target: the LD ships on every variant, the GWAS covers only some). A raw
# external is SUBSET THEN FIT against the target's panel, so it needs the panel, not the map.
# They are passed separately because snp_info still does coordinate lookup for PRETRAINED
# externals, where no fitting happens and the restriction does not apply.
.load_externals <- function(data_dir, roles, snp_info = NULL, require_coords = TRUE,
                            family = "gaussian", standardize_method = "sd",
                            ext_ld_ancestry = NULL, ext_ld_build = NULL,
                            target_panel_map = NULL, allow_name_merge = FALSE) {
  if (is.null(target_panel_map)) target_panel_map <- snp_info
  # Name a mis-typed external role BEFORE anything downstream complains about a missing
  # file or an empty variant intersection: those errors are true but they are downstream
  # of the actual mistake, so they steer the model nowhere and it loops.
  .check_raw_external_roles(data_dir, roles)
  # Bucket B: RAW external(s) (summary GWAS, or individual X/y) are FIT to produce
  # coefficients before the usual coordinate-alignment pipeline runs. Each raw
  # external instance (M >= 1) is fit independently, then merged as a p x M
  # beta.external by the shared mergeExternals path below.
  if (.has_raw_external(roles)) {
    insts <- .raw_external_instances(roles, ext_ld_ancestry, ext_ld_build)
    singles <- list()
    for (inst in insts) {
      fitted <- .fit_external_model(
        data_dir, inst$roles, target_snp = target_panel_map, family = family,
        standardize_method = standardize_method,
        ext_ld_ancestry = inst$ancestry, ext_ld_build = inst$build)
      if (!is.null(fitted)) singles[[length(singles) + 1]] <- fitted
    }
    if (length(singles) == 0) return(NULL)
  } else {
  # Collect external file SPECS (a roles map + the role key to read) from BOTH the
  # unnumbered single alias AND the numbered aliases, then dedup by resolved path
  # and read. A small model mixes spellings freely: external_coef (alias), a
  # length>1 list under one alias, external_coef_1/_2 (numbered), or -- observed on
  # a real run -- external_coef=model1 AND external_coef_1=model2 for two DISTINCT
  # models. Merging every external role (not either/or) keeps all of them.
  single_aliases <- c("external_coef", "beta_external", "external", "external_beta")
  ext_specs <- list()
  hit <- NULL
  for (a in single_aliases) if (!is.null(roles[[a]])) { hit <- a; break }
  if (!is.null(hit)) {
    vals <- roles[[hit]]
    if (length(vals) > 1) {
      # A list/vector packed into one role (e.g. external_coef = ["m1","m2"]).
      for (i in seq_along(vals)) {
        tmp <- roles
        tmp[[hit]] <- vals[[i]]
        ext_specs[[length(ext_specs) + 1]] <- list(roles = tmp, role = hit)
      }
    } else {
      ext_specs[[length(ext_specs) + 1]] <- list(roles = roles, role = hit)
    }
  }
  # Numbered externals external_coef_1/_2/..., APPENDED to whatever the unnumbered
  # alias contributed (so a mixed external_coef + external_coef_1 keeps both).
  k <- 1
  repeat {
    r <- NULL
    for (base in c("external_coef_%d", "beta_external_%d", "external_%d")) {
      cand <- sprintf(base, k)
      if (!is.null(roles[[cand]])) { r <- cand; break }
    }
    if (is.null(r)) break
    ext_specs[[length(ext_specs) + 1]] <- list(roles = roles, role = r)
    k <- k + 1
  }
  if (length(ext_specs) == 0) return(NULL)
  # Read each spec, deduping by resolved path (guards against the same file named
  # under two spellings, e.g. external_coef=m1 AND external_coef_1=m1).
  singles <- list()
  seen <- character(0)
  for (sp in ext_specs) {
    p <- .role_path(data_dir, sp$roles, sp$role)
    if (p %in% seen) next
    seen <- c(seen, p)
    singles[[length(singles) + 1]] <- .ensure_external_varnames(
      .read_role(data_dir, sp$roles, sp$role))
  }
  if (length(singles) == 0) return(NULL)
  }  # end else (file-based externals)

  # Attach CHR/BP/REF/ALT from snp_info to any external that lacks them but has
  # a varnames key. Skipped when require_coords is FALSE (varnames fallback).
  attach_coords <- function(df) {
    df <- as.data.frame(df)
    if (!require_coords) return(df)
    # A RAW GWAS named as external_coef. This one is dangerous because it SILENTLY
    # "works": a sumstats table has CHR/BP/REF/ALT and a numeric BETA, so it sails
    # through alignment and .guess_coef_col picks BETA -- handing the fitter RAW
    # MARGINAL effect sizes as if they were a fitted model's coefficients. Marginal
    # GWAS betas are confounded by LD and are NOT joint-model coefficients, so the
    # "external" is wrong and nothing errors. Steer to the raw-summary roles, which
    # make prep_auto FIT a joint model from the GWAS + an LD panel.
    cn <- colnames(df)
    if (.looks_like_sumstats(df)) {
      stop(paste0(
        "the external role points at what looks like GWAS SUMMARY STATISTICS ",
        "(columns: ", paste(utils::head(cn, 12), collapse = ", "),
        "), not a fitted model's coefficient table. Its BETA column holds RAW ",
        "MARGINAL effect sizes, which are confounded by LD and are NOT joint-model ",
        "coefficients: using them as beta.external would silently give a wrong ",
        "external. external_coef is for a PRETRAINED coefficient file (varnames + ",
        "coef). To use raw summary data, name it as such and prep_auto will FIT the ",
        "external model for you: pass external_sumstats = <the GWAS file> + ",
        "external_ld_panel = <a reference genotype panel of the SAME ancestry> + ",
        "external_snp_info = <its variant map>, with external_ld_ancestry + ",
        "external_ld_build."), call. = FALSE)
    }
    if (all(c("CHR","BP","REF","ALT") %in% colnames(df))) return(df)
    if (!"varnames" %in% colnames(df)) {
      # A frequent small-model slip: naming a RAW individual-level genotype matrix
      # (samples x variants) under external_coef, which expects a per-variant
      # COEFFICIENT TABLE (one row per variant). Steer to the raw external roles
      # instead of just reporting the missing key.
      if (.looks_like_genotype_matrix(df, snp_info)) {
        stop(paste0(
          "the external role points at what looks like an individual-level GENOTYPE ",
          "MATRIX (", nrow(df), " samples x ", ncol(df), " columns, whose names are ",
          "variants), not a per-variant coefficient table. external_coef is for a ",
          "PRETRAINED coefficient file. To use a RAW external cohort, name it as raw ",
          "data and prep_auto will FIT the external model for you: pass ",
          "external_X = <the genotype matrix> AND external_y = <its phenotype file> ",
          "(optionally external_X_val / external_y_val to tune it, and ",
          "external_snp_info = <its variant map>). Do NOT pass it as external_coef."),
          call. = FALSE)
      }
      stop("external lacks CHR/BP/REF/ALT and has no varnames key to join on",
           call. = FALSE)
    }
    if (is.null(snp_info) || !all(c("varnames","CHR","BP","REF","ALT") %in% colnames(snp_info))) {
      stop("snp_info with varnames+CHR/BP/REF/ALT is required to attach coordinates to the external",
           call. = FALSE)
    }
    idx <- match(as.character(df$varnames), as.character(snp_info$varnames))
    df$CHR <- snp_info$CHR[idx]
    df$BP  <- snp_info$BP[idx]
    df$REF <- snp_info$REF[idx]
    df$ALT <- snp_info$ALT[idx]
    # Drop external variants that are NOT on the target panel (unmapped varnames
    # -> NA coords). This INTERSECTS an off-panel external (e.g. a large published
    # model on its own ~163k panel) down to the cohort panel: the correct
    # harmonization, and required because mergeExternals rejects NA coordinates.
    unmapped <- is.na(idx)
    if (any(unmapped)) {
      df <- df[!unmapped, , drop = FALSE]
    }
    df
  }
  singles <- lapply(singles, attach_coords)

  if (length(singles) == 1) {
    df <- singles[[1]]
    if (!any(grepl("^coef", colnames(df)))) {
      cc <- .guess_coef_col(df)
      df$coef1 <- df[[cc]]
    }
    return(df)
  }
  if (!require_coords) {
    if (!allow_name_merge) {
      # GENOTYPE varnames fallback: merging several externals safely needs allele
      # harmonization across them (BRIER::mergeExternals), which needs coordinates.
      # Without coordinates, a shared varname might encode opposite alleles in two
      # externals, so refuse rather than merge on the name and orient nothing.
      stop("multiple externals in varnames-fallback mode are not supported; provide coordinates",
           call. = FALSE)
    }
    # GENERIC predictors have no alleles to orient, so several externals merge by NAME
    # alone (no coordinates, no mergeExternals). Build one table varnames + coef1..coefM;
    # a variant an external does not cover contributes 0 to that external's column (no
    # transfer), exactly the single-external impute-0. .align_predictors then aligns the
    # whole table to the target panel, imputing 0 for target predictors no external covers.
    all_names <- unique(unlist(lapply(singles, function(df) as.character(df$varnames))))
    merged <- data.frame(varnames = all_names, stringsAsFactors = FALSE)
    for (k in seq_along(singles)) {
      df <- singles[[k]]
      cc <- if (any(grepl("^coef", colnames(df)))) grep("^coef", colnames(df), value = TRUE)[1]
            else .guess_coef_col(df)
      v <- rep(0.0, length(all_names))
      idx <- match(all_names, as.character(df$varnames))
      got <- !is.na(idx)
      v[got] <- as.numeric(df[[cc]][idx[got]])
      merged[[sprintf("coef%d", k)]] <- v
    }
    return(merged)
  }
  prepped <- lapply(singles, function(df) {
    if (!"coef" %in% colnames(df)) df$coef <- df[[.guess_coef_col(df)]]
    df[, c("CHR","BP","REF","ALT","coef")]
  })
  BRIER::mergeExternals(prepped, verbose = FALSE)
}


# ---- alignment resolver ----------------------------------------------------
# One place that decides HOW to align target SNPs + externals, returning a
# uniform result the recipes consume. Preference order:
#   1. coordinate (preferred): external has CHR/BP/REF/ALT, or they are
#      inferable by LOOKUP against snp_info (varnames -> CHR/BP/REF/ALT). Uses
#      BRIER::preprocessI for robust allele-aware alignment.
#   2. varnames (fallback): coordinates absent AND not inferable. Match target
#      and externals directly on the shared varnames string, skipping
#      preprocessI. Safe ONLY when the allele is encoded in the varnames (e.g.
#      rsID_ALLELE), so a string match implies allele agreement; no silent flip.
# align_method: "auto" (default) | "coordinate" | "varnames".
#
# Returns list(
#   surv_varnames  = surviving SNPs in the ORIGINAL varnames format (match X),
#   beta           = aligned coefficient matrix (p x M) in surv order, or NULL,
#   method_used    = "coordinate" | "varnames",
#   note           = human-readable description
# )
.align_target_externals <- function(data_dir, roles, snp, align_method,
                                    family = "gaussian", standardize_method = "sd",
                                    ext_ld_ancestry = NULL, ext_ld_build = NULL,
                                    predictor_type = "genotype",
                                    ambiguous = "resolve", af_margin = 0.1) {
  if (!"varnames" %in% colnames(snp)) {
    stop("the predictor map must have a 'varnames' column", call. = FALSE)
  }
  has_coords_snp <- all(c("CHR","BP","REF","ALT") %in% colnames(snp))

  # GENERIC predictors: the identity IS the name. A gene has no coordinate to match on and no
  # allele to orient, so the whole coordinate/flip apparatus is not merely skipped, it is
  # MEANINGLESS. Match by name through the same aligner (.match_to_ref falls back to name
  # identity), which keeps the impute-0 and drop-external-only semantics identical to the
  # genotype path. This is the case BRIER's preprocessors could not express at all.
  if (identical(predictor_type, "generic")) {
    ext_tab <- .load_externals(data_dir, roles, snp_info = NULL, require_coords = FALSE,
                               family = family, standardize_method = standardize_method,
                               ext_ld_ancestry = ext_ld_ancestry,
                               ext_ld_build = ext_ld_build,
                               target_panel_map = snp, allow_name_merge = TRUE)
    .check_external_overlap(ext_tab, snp$varnames)
    al <- .align_predictors(ref = snp, ext_tab = ext_tab, predictor_type = "generic")
    surv <- as.character(snp$varnames)[al$keep]
    return(list(surv_varnames = surv, beta = al$beta, method_used = "varnames",
                note = c(sprintf(paste0("generic predictors: matched by NAME, %d kept ",
                                        "(no coordinates, no alleles, no orientation)"),
                                 length(surv)),
                         .align_counts(al))))
  }

  # can we use the coordinate path? need coords on snp_info (as the canonical
  # panel and as the lookup table for externals).
  coordinate_ok <- has_coords_snp
  use_method <- align_method
  if (identical(align_method, "auto")) {
    use_method <- if (coordinate_ok) "coordinate" else "varnames"
  }
  if (identical(use_method, "coordinate") && !coordinate_ok) {
    stop("align_method='coordinate' but snp_info lacks CHR/BP/REF/ALT", call. = FALSE)
  }

  if (identical(use_method, "coordinate")) {
    ext_tab <- .load_externals(data_dir, roles, snp_info = snp, family = family,
                               standardize_method = standardize_method,
                               ext_ld_ancestry = ext_ld_ancestry,
                               ext_ld_build = ext_ld_build)
    # OUR aligner, replacing BRIER::preprocessI (see .align_predictors). Verified BITWISE
    # identical to it on genotype data by mcp/tests/test_aligner_differential.R, which keeps
    # preprocessI as a verification ORACLE rather than a runtime dependency.
    al <- .align_predictors(ref = snp, ext_tab = ext_tab,
                            predictor_type = predictor_type,
                            ambiguous = ambiguous, af_margin = af_margin)
    surv <- as.character(snp$varnames)[al$keep]   # original varnames (match X)
    return(list(surv_varnames = surv, beta = al$beta, method_used = "coordinate",
                note = c(sprintf("coordinate alignment: %d predictors kept", length(surv)),
                         .align_counts(al))))
  }

  # varnames fallback: surviving set = snp_info varnames (canonical panel);
  # externals matched by varnames string. Allele assumed encoded in varnames.
  # This branch is reached PRECISELY BECAUSE no coordinates are available, so several
  # externals can only be merged by NAME here, exactly like the single-external case just
  # below. allow_name_merge = TRUE keeps the multi-external path consistent with the single
  # one instead of demanding coordinates that, by construction, do not exist (a genotype
  # matrix the model mislabelled, or a coordinate-free panel). mergeExternals is impossible
  # without coordinates anyway; the same "allele encoded in varnames" caveat carries over.
  surv <- as.character(snp$varnames)
  ext_tab <- .load_externals(data_dir, roles, snp_info = NULL, require_coords = FALSE,
                             family = family, standardize_method = standardize_method,
                             ext_ld_ancestry = ext_ld_ancestry, ext_ld_build = ext_ld_build,
                             allow_name_merge = TRUE)
  .check_external_overlap(ext_tab, surv)
  beta <- NULL
  n_ext <- 0L
  if (!is.null(ext_tab)) {
    coef_cols <- grep("^coef", colnames(ext_tab), value = TRUE)
    if (length(coef_cols) == 0) coef_cols <- setdiff(colnames(ext_tab), "varnames")
    n_ext <- length(coef_cols)
    idx <- match(surv, as.character(ext_tab$varnames))
    beta <- matrix(0, nrow = length(surv), ncol = length(coef_cols))
    present <- !is.na(idx)
    if (any(present)) {
      beta[present, ] <- as.matrix(ext_tab[idx[present], coef_cols, drop = FALSE])
    }
  }
  list(surv_varnames = surv, beta = beta, method_used = "varnames",
       note = sprintf("varnames-string alignment (no coordinates): %d predictors, %d external model(s) merged by name",
                      length(surv), n_ext))
}


# When a small model omits the target val/test roles but the fixture ships the
# sibling split (the common case: the fit/select chain then can't select on a
# held-out set and falls back to an IC), discover them from the TRAINING role's
# filename by renaming training->validation / testing (and _train->_val / _test)
# in the SAME data dir, same ancestry + panel. Only fills a role that is ABSENT,
# and only when BOTH the X and the y sibling exist -- so a genuine no-val case
# (no _validation file shipped) still correctly finds nothing and selects by IC,
# and a split is never fabricated from another split. Returns the augmented roles
# plus report notes.
# Drop OPTIONAL target roles that point at a file which does not exist. A summary
# target has no individual training data, but a small model routinely invents a
# target_y_train ("height_AFR_pheno_training.txt.gz") to sit alongside the GWAS --
# and brier_s reads that role as a standardization reference, so the run died on a
# bare "file for role target_y_train not found" and looped. These roles are optional
# by construction (brier_s falls back to each split's own moments), so a missing file
# means "not shipped", not "fatal". Pruning also runs BEFORE .discover_target_splits,
# so a mistyped val/test path is cleared and then re-discovered from the real sibling
# instead of silently disabling selection/scoring. REQUIRED roles (sumstats, snp_info,
# LD, externals) are never pruned: those must still fail loudly.
.prune_missing_optional_roles <- function(data_dir, roles) {
  optional <- c("target_X_train", "target_y_train", "target_X_val", "target_y_val",
                "target_X_test", "target_y_test")
  notes <- character(0)
  for (k in optional) {
    if (is.null(roles[[k]])) next
    p <- tryCatch(.role_path(data_dir, roles, k), error = function(e) NULL)
    if (is.null(p) || !file.exists(p)) {
      notes <- c(notes, sprintf(
        "dropped role %s: no such file (%s); a summary target ships no individual training data",
        k, as.character(roles[[k]])[1]))
      roles[[k]] <- NULL
    }
  }
  list(roles = roles, notes = notes)
}


.discover_target_splits <- function(data_dir, roles) {
  notes <- character(0)
  sib <- function(fname, from, to) {
    out <- fname
    for (i in seq_along(from)) out <- gsub(from[i], to[i], out, ignore.case = TRUE)
    out
  }
  # The genotype file the target's splits are named after. An INDIVIDUAL target has
  # target_X_train; a SUMMARY target has no training genotypes at all, but its LD
  # reference panel (target_ld_panel, e.g. height_AFR_X_training) follows the same
  # naming, so the val/test genotype siblings derive from it. Without this a summary
  # case never fills its held-out splits and silently selects by an IC.
  x_anchor <- if (!is.null(roles[["target_X_train"]])) {
    roles[["target_X_train"]]
  } else roles[["target_ld_panel"]]
  # The phenotype file. An individual target names it directly; a summary target has
  # none, so derive it from the genotype anchor by the X -> pheno naming convention
  # (height_AFR_X_training -> height_AFR_pheno_training).
  y_anchor <- if (!is.null(roles[["target_y_train"]])) {
    roles[["target_y_train"]]
  } else if (!is.null(x_anchor)) {
    gsub("_X_", "_pheno_", x_anchor, fixed = TRUE)
  } else NULL

  fill <- function(xr, yr, from, to, label) {
    if (!is.null(roles[[xr]]) || !is.null(roles[[yr]])) return(invisible())
    if (is.null(x_anchor) || is.null(y_anchor)) return(invisible())
    xc <- sib(x_anchor, from, to); yc <- sib(y_anchor, from, to)
    if (identical(xc, x_anchor) || identical(yc, y_anchor)) return(invisible())
    if (.exists_in_dir(data_dir, xc) && .exists_in_dir(data_dir, yc)) {
      roles[[xr]] <<- xc; roles[[yr]] <<- yc
      notes <<- c(notes, sprintf(
        "auto-discovered target %s split (%s, %s) from the training filename",
        label, xc, yc))
    }
  }
  fill("target_X_val", "target_y_val", c("training", "_train"),
       c("validation", "_val"), "validation")
  fill("target_X_test", "target_y_test", c("training", "_train"),
       c("testing", "_test"), "test")
  list(roles = roles, notes = notes)
}


# Guard against the summary-vs-individual mis-route. A small model picks an
# INDIVIDUAL-target shape for a SUMMARY target (fabricating a target_y_train from
# the GWAS panel's naming) and then dead-ends on "pheno file not found" and loops.
# It picks brier_i when there is one external, and brier_full when there are several
# (two external cohorts read as "pool the cohorts"), so BOTH shapes need the steer:
# without it, brier_full gives a bare file-not-found with no route forward.
# If the individual outcome is absent but a GWAS summary file sits in the data dir,
# this is a summary case -> steer to brier_s.
.steer_if_summary_target <- function(data_dir, roles, shape) {
  ytr_path <- tryCatch(.role_path(data_dir, roles, "target_y_train"),
                       error = function(e) NULL)
  if (!is.null(ytr_path) && file.exists(ytr_path)) return(invisible(NULL))
  gwas <- list.files(data_dir, pattern = "gwas|sumstat|summary",
                     ignore.case = TRUE, full.names = FALSE)
  if (length(gwas) == 0) return(invisible(NULL))
  stop(paste0(
    "target_y_train not found, but this data dir has a GWAS summary file (",
    gwas[1], "): the target is SUMMARY-level, so it has no individual-level ",
    "genotypes/phenotype to train on. Re-route to shape='brier_s' (NOT '", shape,
    "') with target_sumstats (the GWAS file) + target_ld_panel (the target's ",
    "X_training genotype panel, used as an LD reference) + ld_ancestry + ld_build. ",
    "Keep the external roles as they are: several external cohorts do NOT mean ",
    "brier_full -- each external is FIT into a coefficient column, and brier_s ",
    "takes them all as one multi-column beta.external."
  ), call. = FALSE)
}


# The MIRROR steer: shape='brier_s' on a target that is INDIVIDUAL-level.
#
# Observed on a real run of T2_afr-ind_eur-summary (an AFR X+pheno target with a EUR
# SUMMARY external). The agent could not find a route for the summary external under
# brier_i, so it flipped the whole case to brier_s and passed:
#
#     target_sumstats   = height_EUR_GWAS_training.txt.gz
#     external_sumstats = height_EUR_GWAS_training.txt.gz
#
# The SAME FILE in both roles. That is not a near-miss, it is incoherent: a target
# cannot be its own external, and the EUR GWAS is not the AFR target's data at all. Had
# the run got past its other error, this would have FIT THE EXTERNAL AS THE TARGET and
# reported a number -- a wrong analysis that scores, which is far worse than a stop.
#
# So refuse it on two grounds, both structural:
#   (a) the target's summary statistics ARE the external's (same file), or
#   (b) the target has individual-level X + y, which brier_s cannot use, and the data
#       dir has no GWAS for the TARGET's own cohort.
.steer_if_individual_target <- function(data_dir, roles, shape) {
  same_file <- function(a, b) {
    pa <- tryCatch(.role_path(data_dir, roles, a), error = function(e) NULL)
    pb <- tryCatch(.role_path(data_dir, roles, b), error = function(e) NULL)
    !is.null(pa) && !is.null(pb) && identical(normalizePath(pa, mustWork = FALSE),
                                              normalizePath(pb, mustWork = FALSE))
  }
  if (same_file("target_sumstats", "external_sumstats")) {
    stop(paste0(
      "target_sumstats and external_sumstats are THE SAME FILE. A target cannot be ",
      "its own external: the external is a DIFFERENT study, and fitting the external ",
      "as the target would report a number for an analysis nobody asked for. If the ",
      "target has individual-level genotypes + a phenotype, this is shape='brier_i' ",
      "(target_X_train + target_y_train), and the summary external goes in as ",
      "external_sumstats + external_ld_panel + external_ld_ancestry + ",
      "external_ld_build -- prep_auto FITS it for you. Do not route the target to ",
      "brier_s just because the EXTERNAL is summary-level."
    ), call. = FALSE)
  }

  xtr <- tryCatch(.role_path(data_dir, roles, "target_X_train"),
                  error = function(e) NULL)
  ytr <- tryCatch(.role_path(data_dir, roles, "target_y_train"),
                  error = function(e) NULL)
  if (is.null(xtr) || is.null(ytr) ||
      !file.exists(xtr) || !file.exists(ytr)) {
    return(invisible(NULL))
  }
  stop(paste0(
    "shape='", shape, "' was chosen, but the target has INDIVIDUAL-LEVEL data ",
    "(target_X_train + target_y_train both exist). BRIERs takes a SUMMARY target ",
    "(GWAS statistics); it cannot use individual genotypes. Re-route to ",
    "shape='brier_i'. A SUMMARY EXTERNAL does not change the TARGET's shape: pass it ",
    "as external_sumstats + external_ld_panel (a reference genotype panel for the ",
    "EXTERNAL's ancestry) + external_ld_ancestry + external_ld_build, and prep_auto ",
    "will FIT the external model for you and integrate it with BRIERi."
  ), call. = FALSE)
}


# ---- recipe: brier_i -------------------------------------------------------
.recipe_brier_i <- function(data_dir, roles, standardize, standardize_method,
                            outcome_family, align_method, report,
                            ext_ld_ancestry = NULL, ext_ld_build = NULL,
                            coverage_min = NULL, predictor_type = "genotype",
                            ambiguous = "resolve", af_margin = 0.1) {
  .steer_if_summary_target(data_dir, roles, "brier_i")
  # Fill omitted val/test roles from the training filename's siblings (a frequent
  # small-model slip is to pass only train + external and drop the held-out splits).
  disc <- .discover_target_splits(data_dir, roles)
  roles <- disc$roles
  report <- c(report, disc$notes)

  Xtr <- .geno_matrix(.read_role(data_dir, roles, "target_X_train"))
  ytr <- .pheno_vector(.read_role(data_dir, roles, "target_y_train"))
  snp <- .read_role(data_dir, roles, "snp_info", required = FALSE)
  if (is.null(snp)) {
    # snp_info is a GENOTYPE map: varnames PLUS CHR/BP/REF/ALT, for coordinate
    # alignment and allele-flip correction. A non-genetic predictor (gene
    # expression, protein, ...) has no such map: its identity IS the column name.
    # Rather than force the caller to supply a meaningless variant map, derive a
    # names-only panel from the training matrix. With no coordinates the predictor
    # type resolves to generic and alignment is by name (see .align_target_externals).
    snp <- data.frame(varnames = colnames(Xtr), stringsAsFactors = FALSE)
    report <- c(report, sprintf(paste0(
      "snp_info omitted; derived the predictor panel from the training matrix's ",
      "column names (%d predictors, matched by name)."), ncol(Xtr)))
  }
  predictor_type <- .resolve_predictor_type(predictor_type, snp)
  report <- c(report, sprintf("loaded target train X %dx%d, y %d; predictor_type = %s",
                              nrow(Xtr), ncol(Xtr), length(ytr), predictor_type))

  al <- .align_target_externals(data_dir, roles, snp, align_method,
                                family = outcome_family,
                                standardize_method = standardize_method,
                                ext_ld_ancestry = ext_ld_ancestry,
                                ext_ld_build = ext_ld_build,
                                predictor_type = predictor_type,
                                ambiguous = ambiguous, af_margin = af_margin)
  surv_orig_varnames <- al$surv_varnames
  report <- c(report, al$note)

  xcols <- match(surv_orig_varnames, colnames(Xtr))
  if (any(is.na(xcols))) {
    stop(sprintf("%d surviving SNPs not found among X columns by varnames",
                 sum(is.na(xcols))), call. = FALSE)
  }
  Xtr <- Xtr[, xcols, drop = FALSE]

  has_val <- !is.null(roles[["target_X_val"]])
  has_test <- !is.null(roles[["target_X_test"]])
  Xva <- Xte <- yva <- yte <- NULL

  # A predictor the split does not carry is imputed with the TRAINING COLUMN MEAN, in the
  # RAW scale. NOT a literal 0: BRIERi standardization is OPTIONAL, and on the raw scale 0
  # is a REAL GENOTYPE (homozygous reference), not "no information". The raw mean is right
  # in both worlds: if standardize=TRUE it maps to exactly 0 after standardizing anyway.
  # Xtr is still RAW here (standardization happens below), so its column means are what we
  # want.
  tr_mean <- colMeans(Xtr, na.rm = TRUE)
  tr_mean[!is.finite(tr_mean)] <- 0

  # Coverage policy (PREP_AUTO_DESIGN.md 3.1): a split below the threshold scores a model
  # it cannot fully see, biasing the metric while looking healthy. VAL -> refuse and let
  # selection fall back to an IC. TEST -> abort: there is no IC substitute for evaluation.
  .take_split <- function(role_X, role_y, which) {
    m <- .align_split_to_panel(
      .geno_matrix(.read_role(data_dir, roles, role_X)),
      surv_orig_varnames, fill = tr_mean)
    cov <- .check_coverage(m, surv_orig_varnames, which, coverage_min)
    if (!is.null(cov$note)) report <<- c(report, paste0("_notice_coverage: ", cov$note))
    if (!cov$ok) {
      if (identical(which, "testing")) {
        stop(sprintf(paste0(
          "%s There is no substitute for a test split, so this evaluation cannot be ",
          "reported. Supply a testing set that covers the model's predictors, or lower ",
          "coverage_min if you accept a partial score."), cov$note), call. = FALSE)
      }
      return(NULL)   # val refused -> caller falls back to an information criterion
    }
    list(X = m, y = .pheno_vector(.read_role(data_dir, roles, role_y)))
  }

  if (has_val) {
    got <- .take_split("target_X_val", "target_y_val", "validation")
    if (is.null(got)) {
      has_val <- FALSE
      report <- c(report, paste0("validation split REFUSED (below the coverage ",
                                 "threshold); select by an information criterion (BIC)"))
    } else { Xva <- got$X; yva <- got$y }
  }
  if (has_test) {
    got <- .take_split("target_X_test", "target_y_test", "testing")
    Xte <- got$X; yte <- got$y
  }

  if (isTRUE(standardize)) {
    st <- .fit_standardizer(Xtr, standardize_method)
    Xtr <- .apply_standardizer(Xtr, st)
    if (has_val) Xva <- .apply_standardizer(Xva, st)
    if (has_test) Xte <- .apply_standardizer(Xte, st)
    report <- c(report, sprintf("standardized X (method=%s, training constants)",
                                standardize_method))
    if (identical(outcome_family, "gaussian")) {
      ymu <- mean(ytr); ysd <- stats::sd(ytr); if (ysd == 0) ysd <- 1
      ytr <- (ytr - ymu) / ysd
      if (has_val) yva <- (yva - ymu) / ysd
      if (has_test) yte <- (yte - ymu) / ysd
      report <- c(report, "standardized Gaussian y (training mean/sd)")
    }
  } else {
    report <- c(report, "left X (and y) on raw scale (standardize=FALSE)")
  }

  # beta.external: aligned coefs, PREPEND intercept row (BRIERi convention).
  beta <- if (!is.null(al$beta)) al$beta else matrix(0, nrow = length(surv_orig_varnames), ncol = 1)
  beta <- rbind(matrix(0, nrow = 1, ncol = ncol(beta)), beta)
  # NAME the rows. The alignment above is correct, but until now the object carried no
  # way to PROVE it: the fitter compared row COUNTS, so a beta in a different ORDER
  # passed and every coefficient landed on the wrong predictor, silently. Names are the
  # only evidence of alignment, so the contract check demands them (_common.R).
  rownames(beta) <- c("(Intercept)", colnames(Xtr))
  report <- c(report, sprintf("beta.external with intercept row: %dx%d",
                              nrow(beta), ncol(beta)))

  prepared <- list(X = Xtr, y = ytr, beta_external = beta,
                   surv_varnames = surv_orig_varnames)
  if (has_val) { prepared$X_val <- Xva; prepared$y_val <- yva }
  if (has_test) { prepared$X_test <- Xte; prepared$y_test <- yte }

  # expr_hints expose which splits were assembled, so the agent (and the
  # continuation hooks) can branch: validation-set selection when X_val exists,
  # IC-based (BIC) selection when it does not; test scoring when X_test exists.
  hints <- list(X_expr = "prepared$X", y_expr = "prepared$y",
                beta_external_expr = "prepared$beta_external")
  if (has_val) {
    hints$X_val_expr <- "prepared$X_val"
    hints$y_val_expr <- "prepared$y_val"
  }
  if (has_test) {
    hints$X_test_expr <- "prepared$X_test"
    hints$y_test_expr <- "prepared$y_test"
  }

  list(prepared = prepared, report = report, expr_hints = hints)
}


# ---- recipe: brier_s -------------------------------------------------------
# Build an LD from a reference-panel predictor matrix (for a summary target that
# ships a reference panel but no prebuilt LD -- the common practical case). Two
# modes: GENOTYPE predictors (ancestry + build given) use Berisa LD blocks
# (getLDB) for a block-wise sparse LD; NON-GENOTYPE predictors (expression,
# protein, ...; no ancestry/build) get the plain correlation matrix from the
# panel, since Berisa blocks are genotype/genome-specific and do not apply.
.build_ld_from_panel <- function(panel_df, snp_info, ancestry = NULL,
                                 build = NULL, keep_vars = NULL) {
  X <- .geno_matrix(panel_df)
  # SUBSET THEN FIT: when the caller has already restricted the variant map (an external
  # restricted to the target's panel), the LD must be built over exactly those predictors,
  # not the panel's full set. calLD works on X's COLUMNS, so the restriction has to happen
  # here or the LD would carry predictors the model will never see.
  if (!is.null(keep_vars)) {
    j <- match(as.character(keep_vars), colnames(X))
    j <- j[!is.na(j)]
    if (!length(j)) {
      stop("none of the model's predictors appear in the LD reference panel", call. = FALSE)
    }
    X <- X[, j, drop = FALSE]
  }
  vnames <- colnames(X)
  have_blocks <- !is.null(ancestry) && nzchar(ancestry) &&
                 !is.null(build) && nzchar(build)
  # A GENOTYPE reference panel MUST specify ancestry + build: a full correlation
  # that ignores LD block structure is not a valid genotype LD. Guide the caller
  # (a small model tends to omit them) rather than silently build a wrong LD.
  looks_genotype <- !is.null(snp_info) &&
                    all(c("CHR", "BP") %in% colnames(snp_info))
  if (!have_blocks && looks_genotype) {
    stop(paste(
      "Building an LD from a GENOTYPE reference panel needs ld_ancestry",
      "(AFR/EUR/EAS) and ld_build (hg19/hg38) for the Berisa LD blocks -- pass",
      "BOTH to prep_auto. Omit them only for non-genotype predictors (gene",
      "expression, proteins, ...), where prep_auto builds a plain correlation."
    ), call. = FALSE)
  }
  call_args <- list(X = X)
  mode <- "plain correlation (no LD blocks)"
  if (have_blocks) {
    bed_path <- BRIER::getLDB(ancestry = ancestry, build = build)
    bed <- if (requireNamespace("data.table", quietly = TRUE)) {
      as.data.frame(data.table::fread(bed_path, data.table = FALSE))
    } else {
      utils::read.table(bed_path, header = TRUE, sep = "\t",
                        stringsAsFactors = FALSE, check.names = FALSE)
    }
    call_args$LDB <- .normalize_ldb(bed)
    call_args$SNP.info <- snp_info
    mode <- sprintf("Berisa %s %s LD blocks", ancestry, build)
  }
  ld <- do.call(BRIER::calLD, call_args)
  XtX <- ld$XtX
  if (is.null(rownames(XtX)) && !is.null(vnames)) {
    retained <- if (!is.null(ld$nz)) ld$nz else seq_len(ncol(XtX))
    v <- vnames[retained]
    if (length(v) == ncol(XtX)) dimnames(XtX) <- list(v, v)
  }
  list(XtX = XtX, mode = mode)
}


# Find a reference genotype panel in the data dir to build the LD from, for the
# case where a small model asks for a genotype LD (ld_ancestry + ld_build given)
# but forgets to name target_ld_panel. Match a genotype TRAINING/reference matrix
# (x_train / reference / panel; never a pheno or val/test split) and PREFER one
# whose ancestry token matches ld_ancestry; if an ancestry is given but no file
# matches it, return NULL (do NOT guess a wrong-ancestry panel).
.discover_ld_panel <- function(data_dir, ancestry = NULL) {
  files <- list.files(
    data_dir, pattern = "\\.(txt|tsv|csv|dat|gz|bgz)$", ignore.case = TRUE
  )
  if (length(files) == 0) return(NULL)
  cand <- files[grepl("x_train|x_ref|reference|panel|ld_panel|geno",
                      files, ignore.case = TRUE)]
  # never mistake a phenotype or val/test file for the reference panel
  cand <- cand[!grepl("pheno|_val|valid|_test", cand, ignore.case = TRUE)]
  if (length(cand) == 0) return(NULL)
  if (!is.null(ancestry) && nzchar(ancestry)) {
    anc <- cand[grepl(ancestry, cand, ignore.case = TRUE)]
    if (length(anc) >= 1) return(file.path(data_dir, anc[1]))
    return(NULL)
  }
  file.path(data_dir, cand[1])
}


.recipe_brier_s <- function(data_dir, roles, standardize, standardize_method,
                            outcome_family, report, ld_ancestry = NULL,
                            ld_build = NULL, ext_ld_ancestry = NULL,
                            ext_ld_build = NULL, coverage_min = NULL,
                            predictor_type = "genotype", ambiguous = "resolve", af_margin = 0.1) {
  # A SUMMARY external does not make the TARGET summary-level. A real run flipped the
  # whole case to brier_s and passed the EXTERNAL's GWAS as target_sumstats -- the same
  # file in both roles -- which would have fit the external AS the target and reported a
  # number for an analysis nobody asked for. Refuse it structurally.
  .steer_if_individual_target(data_dir, roles, "brier_s")
  # NOTE: the standardize override for brier_s happens in the DISPATCH, not here, so that
  # the value echoed back in the result agrees with what was actually used. Doing it here
  # would leave the result reporting standardize=FALSE while the run standardized anyway.
  # Drop hallucinated optional roles (a summary target has no individual training
  # data, but the model invents target_y_train next to the GWAS), THEN fill omitted
  # val/test roles from the training filename's siblings. Prune first so a bad path
  # is cleared and re-discovered rather than left broken.
  pr <- .prune_missing_optional_roles(data_dir, roles)
  roles <- pr$roles
  report <- c(report, pr$notes)
  disc <- .discover_target_splits(data_dir, roles)
  roles <- disc$roles
  report <- c(report, disc$notes)

  sumstats <- .read_role(data_dir, roles, "target_sumstats")
  # snp_info is optional: it is a GENOTYPE map (varnames + CHR/BP/REF/ALT). Non-genetic
  # predictors have none, so when it is absent the canonical panel is derived from the LD
  # matrix's own names further down (the LD IS the variant map for the summary shape).
  snp      <- .read_role(data_dir, roles, "snp_info", required = FALSE)
  snp_derived <- is.null(snp)
  predictor_type <- .resolve_predictor_type(predictor_type, snp)
  report <- c(report, sprintf("predictor_type = %s", predictor_type))
  # The LD matrix: prefer a prebuilt target_ld (a matrix or a cal_ld "LD" object);
  # otherwise BUILD it from a reference panel (target_ld_panel), the common
  # practical case where the user ships a reference X, not a precomputed LD.
  if (!is.null(roles[["target_ld"]])) {
    ld <- .read_role(data_dir, roles, "target_ld")
    if (is.list(ld) && !is.null(ld$XtX)) {
      ld <- ld$XtX
    }
  } else if (!is.null(roles[["target_ld_panel"]])) {
    panel_obj <- .read_role(data_dir, roles, "target_ld_panel")
    if (is.list(panel_obj) && !is.null(panel_obj$XtX)) panel_obj <- panel_obj$XtX
    if (.looks_like_ld_matrix(panel_obj)) {
      # The caller named a PREBUILT LD as the reference PANEL. Both roles are "the
      # LD thing", so a small model mixes them up; left alone, prep_auto would try
      # to BUILD an LD from an LD and demand ld_ancestry/ld_build to do it. The
      # object itself says what it is (square, symmetric, rownames == colnames),
      # so just use it.
      ld <- panel_obj
      report <- c(report, sprintf(
        paste0("target_ld_panel is already an LD MATRIX (%dx%d), not a reference ",
               "panel: using it as target_ld directly"),
        nrow(ld), ncol(ld)))
    } else {
      built <- .build_ld_from_panel(
        panel_obj, snp, ancestry = ld_ancestry, build = ld_build
      )
      ld <- built$XtX
      report <- c(report, sprintf("built LD from reference panel (%s), %dx%d",
                                  built$mode, nrow(ld), ncol(ld)))
    }
  } else if (!is.null(ld_ancestry) && nzchar(ld_ancestry) &&
             !is.null(ld_build) && nzchar(ld_build) &&
             !is.null(.discover_ld_panel(data_dir, ld_ancestry))) {
    # LD role omitted but the caller asked for a genotype LD (ancestry + build):
    # a small model often forgets to name target_ld_panel. Auto-discover the
    # ancestry-matching reference panel in the data dir and build from it.
    panel_file <- .discover_ld_panel(data_dir, ld_ancestry)
    tmp_roles <- list(target_ld_panel = basename(panel_file))
    built <- .build_ld_from_panel(
      .read_role(data_dir, tmp_roles, "target_ld_panel"), snp,
      ancestry = ld_ancestry, build = ld_build
    )
    ld <- built$XtX
    report <- c(report, sprintf(
      "target_ld_panel omitted; auto-discovered reference panel '%s' and built LD (%s), %dx%d",
      basename(panel_file), built$mode, nrow(ld), ncol(ld)
    ))
  } else {
    stop(paste(
      "brier_s needs an LD matrix. Pass target_ld (a prebuilt LD matrix or a",
      "cal_ld object) OR target_ld_panel (a reference predictor panel to build",
      "the LD from). For GENOTYPE predictors also pass ld_ancestry + ld_build",
      "(Berisa blocks); for non-genotype predictors omit them (plain correlation)."
    ), call. = FALSE)
  }
  # snp_info was not supplied (non-genetic predictors): the LD is the canonical variant
  # map, so derive a names-only panel from its row names now that it exists.
  if (snp_derived) {
    if (is.null(rownames(ld))) {
      stop(paste0(
        "snp_info was omitted and the LD matrix carries no names to derive the predictor ",
        "panel from. Supply snp_info (a varnames column), or an LD / reference panel whose ",
        "predictors are named."), call. = FALSE)
    }
    snp <- data.frame(varnames = rownames(ld), stringsAsFactors = FALSE)
    report <- c(report, sprintf(paste0(
      "snp_info omitted; derived the predictor panel from the LD matrix's names ",
      "(%d predictors, matched by name)."), nrow(ld)))
  }
  # target.ind tells preprocessS how the sumstats encodes the marginal signal.
  # Honor an explicit choice; otherwise AUTO-DETECT: if the sumstats already
  # ships a `corr` column use "corr" (no derivation needed), else "gwas"
  # (derive corr from p-value / beta). Auto-detection spares the agent a call
  # it would otherwise have to reason out from the column names.
  target_ind <- if (!is.null(roles[["target_ind"]])) {
    roles[["target_ind"]]
  } else if ("corr" %in% tolower(colnames(sumstats))) {
    "corr"
  } else {
    "gwas"
  }
  report <- c(report, sprintf("target.ind = '%s'", target_ind))

  # SUBSET THEN FIT needs the TARGET'S PANEL, and for a summary target that is NOT `snp`.
  # `snp` is the LD's variant map, which ships every variant; the target's GWAS covers only
  # some of them, so the panel the transfer actually runs on is what SURVIVES the target's
  # own alignment. Compute it first (target only, no external), and hand THAT to the
  # external fitter. Restricting against `snp` instead would restrict against a superset and
  # silently do nothing, which is exactly the bug this ordering exists to prevent.
  al_t <- .align_predictors(ref = snp, target_ss = sumstats, target_ind = target_ind,
                            predictor_type = predictor_type,
                            ambiguous = ambiguous, af_margin = af_margin)
  tgt_panel_map <- snp[al_t$keep, , drop = FALSE]

  # GENERIC predictors have no coordinates to attach, so the external is matched by NAME
  # (see .align_target_externals for the same branch on the individual path).
  is_generic <- identical(predictor_type, "generic")
  ext_tab <- .load_externals(data_dir, roles,
                             snp_info = if (is_generic) NULL else snp,
                             require_coords = !is_generic,
                             family = outcome_family,
                             standardize_method = standardize_method,
                             ext_ld_ancestry = ext_ld_ancestry,
                             ext_ld_build = ext_ld_build,
                             target_panel_map = tgt_panel_map,
                             allow_name_merge = is_generic)
  if (is.null(ext_tab)) {
    # List BOTH external families. Naming only external_coef here used to push a
    # small model straight back to the pretrained role when the case actually ships
    # RAW external data, producing an error <-> drop-the-external oscillation.
    stop(paste0(
      "brier_s needs an external but none was found in roles (",
      paste(names(roles), collapse = ", "), "). Name it by WHAT IT IS:\n",
      "  * PRETRAINED coefficients (a per-variant coef table): external_coef ",
      "(or external_coef_1, external_coef_2, ... for several).\n",
      "  * RAW INDIVIDUAL data (a genotype matrix + its phenotype): external_X + ",
      "external_y (+ optional external_X_val / external_y_val, external_snp_info). ",
      "prep_auto FITS the external model for you.\n",
      "  * RAW SUMMARY data (a GWAS + a reference panel): external_sumstats + ",
      "external_ld_panel + external_snp_info, with external_ld_ancestry + ",
      "external_ld_build. prep_auto FITS the external model for you.\n",
      "For several raw externals, number them: external_X_1/external_y_1, ",
      "external_X_2/external_y_2, ..."),
      call. = FALSE)
  }
  # Name-matched externals (generic) must share predictor names with the panel; a zero
  # overlap is a mis-read identifier column, not a valid all-zero external. The coordinate
  # path matches by CHR/BP, so this name-overlap check would false-positive there.
  if (is_generic) .check_external_overlap(ext_tab, snp$varnames)
  coef_cols <- if (!is.null(ext_tab)) grep("^coef", colnames(ext_tab), value = TRUE) else NULL

  # OUR aligner, replacing BRIER::preprocessS (see .align_predictors). It aligns the
  # target's sumstats AND every external to the LD's variant map in one pass: QC, coordinate
  # match, sign-correct the swapped alleles, derive corr when the sumstats ships none, and
  # impute 0 where an external does not cover a target predictor. Verified BITWISE identical
  # to preprocessS on genotype data (mcp/tests/test_aligner_differential.R), which keeps the
  # package as a verification ORACLE rather than a runtime dependency.
  al <- .align_predictors(ref = snp, target_ss = sumstats, target_ind = target_ind,
                          ext_tab = ext_tab,
                          predictor_type = predictor_type,
                          ambiguous = ambiguous, af_margin = af_margin)
  keep <- al$keep
  report <- c(report, sprintf(
    "aligned the sumstats to the LD panel: %d predictors kept; corr %s",
    length(keep),
    if (identical(target_ind, "gwas")) "DERIVED from p/n/sign(beta) via p2cor"
    else "taken from the sumstats' corr column"))
  report <- c(report, .align_counts(al))

  # The surviving predictors, in the map's ORIGINAL varnames: the LD rownames and the
  # genotype columns use those. keep = indices into snp.
  if (!"varnames" %in% colnames(snp)) {
    stop("snp_info must have a 'varnames' column", call. = FALSE)
  }
  surv_orig_varnames <- as.character(snp$varnames)[keep]

  # Subset the LD matrix by its rownames (robust to LD-vs-snp_info order),
  # falling back to positional keep only if the LD matrix has no names.
  if (!is.null(rownames(ld))) {
    li <- match(surv_orig_varnames, rownames(ld))
    if (any(is.na(li))) {
      stop(sprintf("%d surviving SNPs not found in LD matrix rownames",
                   sum(is.na(li))), call. = FALSE)
    }
    ld_sub <- ld[li, li, drop = FALSE]
  } else {
    ld_sub <- ld[keep, keep, drop = FALSE]
  }
  # BRIERs only accepts a SPARSE LD matrix; coerce if a dense one slipped through
  # (a cal_ld / sparse XtX stays sparse under subsetting and is a no-op here).
  if (!methods::is(ld_sub, "sparseMatrix")) {
    ld_sub <- Matrix::Matrix(ld_sub, sparse = TRUE)
  }
  report <- c(report, sprintf("subset LD matrix to %dx%d", nrow(ld_sub), ncol(ld_sub)))

  beta <- al$beta

  has_val <- !is.null(roles[["target_X_val"]])
  has_test <- !is.null(roles[["target_X_test"]])
  Xva <- Xte <- yva <- yte <- NULL
  st <- NULL
  if ((has_val || has_test) && !is.null(roles[["target_X_train"]])) {
    Xtr_ref <- .align_split_to_panel(
      .geno_matrix(.read_role(data_dir, roles, "target_X_train")),
      surv_orig_varnames, fill = 0)
    st <- .fit_standardizer(Xtr_ref, standardize_method)
  }
  # For a Gaussian outcome on standardized data, y must be standardized too so the
  # metric matches the model's standardized-scale (mean-less) predictions; never
  # for binary/Poisson. BRIERs has no training y in the fit, so use target_y_train
  # constants when shipped, else each split's own moments (R^2 and the MSPE argmin
  # are invariant to the choice; this just makes MSPE meaningful, not y-mean-dominated).
  std_y <- isTRUE(standardize) && identical(outcome_family, "gaussian")
  y_mu <- NULL; y_sd <- NULL
  if (std_y && !is.null(roles[["target_y_train"]])) {
    ytr_ref <- .pheno_vector(.read_role(data_dir, roles, "target_y_train"))
    y_mu <- mean(ytr_ref); y_sd <- stats::sd(ytr_ref); if (y_sd == 0) y_sd <- 1
  }
  .std_split_y <- function(y) {
    if (!std_y) return(y)
    mu <- if (!is.null(y_mu)) y_mu else mean(y)
    sdv <- if (!is.null(y_sd)) y_sd else stats::sd(y); if (sdv == 0) sdv <- 1
    (y - mu) / sdv
  }
  # BRIERs coefficients live on the STANDARDIZED scale (the fit is built from `corr` and
  # `XtX`), so a split must be standardized too: `standardize` is ENFORCED for brier_s.
  # A missing predictor is then imputed to 0, which IS the mean on that scale.
  #
  # Two standardization routes, and the fill differs between them:
  #   * a training reference X was shipped -> `st` holds its moments. Align the RAW split
  #     with fill = st$center, then apply st; the filled columns land on exactly 0.
  #   * no reference -> standardize the split's PRESENT columns on their own moments FIRST,
  #     then align with fill = 0. (Filling first would corrupt the moments with constants.)
  .take_split_s <- function(role_X, role_y, which) {
    raw <- .geno_matrix(.read_role(data_dir, roles, role_X))
    m <- if (!is.null(st)) {
      .apply_standardizer(
        .align_split_to_panel(raw, surv_orig_varnames, fill = st$center), st)
    } else {
      .align_split_to_panel(scale(raw), surv_orig_varnames, fill = 0)
    }
    cov <- .check_coverage(m, surv_orig_varnames, which, coverage_min)
    if (!is.null(cov$note)) report <<- c(report, paste0("_notice_coverage: ", cov$note))
    if (!cov$ok) {
      if (identical(which, "testing")) {
        stop(sprintf(paste0(
          "%s There is no substitute for a test split, so this evaluation cannot be ",
          "reported. Supply a testing set that covers the model's predictors, or lower ",
          "coverage_min if you accept a partial score."), cov$note), call. = FALSE)
      }
      return(NULL)   # val refused -> selection falls back to an information criterion
    }
    list(X = m, y = .std_split_y(.pheno_vector(.read_role(data_dir, roles, role_y))))
  }

  if (has_val) {
    got <- .take_split_s("target_X_val", "target_y_val", "validation")
    if (is.null(got)) {
      has_val <- FALSE
      report <- c(report, paste0("validation split REFUSED (below the coverage ",
                                 "threshold); select by an information criterion ",
                                 "(Cp / GIC)"))
    } else { Xva <- got$X; yva <- got$y }
  }
  if (has_test) {
    got <- .take_split_s("target_X_test", "target_y_test", "testing")
    Xte <- got$X; yte <- got$y
  }
  if (isTRUE(standardize) && (has_val || has_test)) {
    report <- c(report, sprintf("standardized val/test genotypes (method=%s)",
                                standardize_method))
    if (std_y) {
      report <- c(report, sprintf("standardized Gaussian val/test y (%s)",
                                  if (!is.null(y_mu)) "target training mean/sd" else "each split's own mean/sd"))
    }
  }

  # Carry the GWAS training N (median) so brier_s can cache it and
  # brier_s_selection can DEFAULT TN for an IC criterion. Take it from the RAW
  # sumstats, which always has it if the case ships one.
  n_train <- if ("N" %in% colnames(sumstats)) {
    suppressWarnings(as.numeric(stats::median(sumstats$N, na.rm = TRUE)))
  } else {
    NA_real_
  }
  # NAME the rows against the LD panel, for the same reason as brier_i above: alignment
  # must be PROVABLE, not merely true. BRIERs takes p rows and NO intercept.
  if (!is.null(beta) && !is.null(rownames(ld_sub)) &&
      nrow(beta) == nrow(ld_sub)) {
    rownames(beta) <- rownames(ld_sub)
  }
  prepared <- list(sumstats = al$sumstats, XtX = ld_sub, beta_external = beta,
                   snp_info = snp[keep, , drop = FALSE], ld_keep = keep,
                   n_train = n_train)
  if (has_val) { prepared$X_val <- Xva; prepared$y_val <- yva }
  if (has_test) { prepared$X_test <- Xte; prepared$y_test <- yte }

  hints <- list(sumstats_expr = "prepared$sumstats",
                XtX_expr = "prepared$XtX",
                beta_external_expr = "prepared$beta_external")
  if (has_val) {
    hints$X_val_expr <- "prepared$X_val"
    hints$y_val_expr <- "prepared$y_val"
  }
  if (has_test) {
    hints$X_test_expr <- "prepared$X_test"
    hints$y_test_expr <- "prepared$y_test"
  }

  list(prepared = prepared, report = report, expr_hints = hints)
}


# ---- recipe: brier_full ----------------------------------------------------
.recipe_brier_full <- function(data_dir, roles, standardize, standardize_method,
                               outcome_family, report, coverage_min = NULL) {
  # A small model reads "several external cohorts" as "pool them with BRIERfull",
  # even when the TARGET is summary-level (which BRIERfull cannot use at all).
  .steer_if_summary_target(data_dir, roles, "brier_full")
  # Each external-only comparator is a single-cohort fit that must be selected on ITS
  # OWN held-out data, never the target's. The agent omits the roles; fill them from the
  # cohort's training filename when the siblings are actually there.
  roles <- .discover_external_cohort_vals(data_dir, roles)
  # snp_info is optional here too: when supplied it also FIXES the panel ORDER; when
  # absent (non-genetic cohorts) the shared panel is derived from the cohort headers below.
  snp <- .read_role(data_dir, roles, "snp_info", required = FALSE)
  panel <- if (is.null(snp)) NULL
           else if ("varnames" %in% colnames(snp)) as.character(snp$varnames)
           else paste0(snp$SNP, "_", snp$ALT)

  # Resolve genotype PATHS (not data) and read the small phenotype vectors up
  # front: their lengths are the per-cohort row counts, so the stacked matrix is
  # preallocated once and no genotype block is held longer than it takes to copy
  # it into place. Nothing wide is materialized twice.
  Xt_path <- .role_path(data_dir, roles, "target_X_train")
  yt <- .pheno_vector(.read_role(data_dir, roles, "target_y_train"))

  ext_paths <- list(); ext_y <- list()
  k <- 1
  repeat {
    xr <- sprintf("external_X_%d", k); yr <- sprintf("external_y_%d", k)
    if (is.null(roles[[xr]])) break
    ext_paths[[k]] <- .role_path(data_dir, roles, xr)
    ext_y[[k]] <- .pheno_vector(.read_role(data_dir, roles, yr))
    k <- k + 1
  }
  n_ext <- length(ext_paths)
  if (n_ext < 1) stop("brier_full requires at least one external cohort", call. = FALSE)

  # Shared SNP panel from HEADERS alone (no genotype data read yet). When snp_info fixed a
  # panel order, keep only its variants in that order; otherwise the shared panel IS the
  # header intersection (in the target cohort's header order).
  hdrs <- c(list(.geno_header(Xt_path)), lapply(ext_paths, .geno_header))
  hdr_common <- Reduce(intersect, hdrs)
  if (is.null(panel)) {
    common <- hdr_common
    report <- c(report, sprintf(paste0(
      "snp_info omitted; derived the shared predictor panel from the cohort headers ",
      "(%d predictors, matched by name)."), length(common)))
  } else {
    common <- panel[panel %in% hdr_common]
  }
  if (length(common) < 1) {
    stop("brier_full: no SNPs shared across all cohorts", call. = FALSE)
  }
  report <- c(report, sprintf("aligned %d cohorts to %d shared SNPs (headers only)",
                              1 + n_ext, length(common)))

  # COVERAGE (PREP_AUTO_DESIGN.md 3.5). brier_full can only INTERSECT: it pools RAW
  # genotypes, and a genotype cannot be imputed, so a variant absent from one cohort has no
  # data in that cohort at all. There is therefore no coefficient vector to impute 0 into
  # and nothing to degrade gracefully to. A cohort that shares too little with the target
  # is UNUSABLE, so this ERRORS rather than falling back.
  # The target's PREDICTOR count, not its column count: .geno_header includes the sample-ID
  # column, so counting it directly would inflate p by one and shift the coverage fraction.
  tgt_panel <- panel[panel %in% hdrs[[1]]]
  tgt_p <- length(tgt_panel)
  thr <- .coverage_min(coverage_min)
  frac <- if (tgt_p > 0) length(common) / tgt_p else 1
  if (frac <= thr) {
    stop(sprintf(paste0(
      "brier_full: the cohorts share only %d of the target's %d predictors (%.1f%%), at ",
      "or below the %.0f%% coverage threshold. Pooling uses RAW genotypes, which cannot ",
      "be imputed, so only the SHARED predictors can be pooled and this much of the ",
      "target would be discarded. Use a cohort with better overlap, route to brier_i ",
      "(which imputes 0 for predictors an external does not cover), or lower ",
      "coverage_min if you accept the loss."),
      length(common), tgt_p, 100 * frac, 100 * thr), call. = FALSE)
  }
  if (length(common) < tgt_p) {
    report <- c(report, sprintf(
      paste0("_notice_coverage: pooling keeps %d of the target's %d predictors (%.1f%%); ",
             "the other %d are absent from at least one cohort and CANNOT be imputed ",
             "(raw genotypes), so they are dropped"),
      length(common), tgt_p, 100 * frac, tgt_p - length(common)))
  }

  # Preallocate the stacked matrix, then fill it cohort by cohort, reading only
  # the shared-panel columns and freeing each block before the next.
  nt <- length(yt)
  ne <- vapply(ext_y, length, integer(1))
  total <- nt + sum(ne)
  X <- matrix(0.0, nrow = total, ncol = length(common),
              dimnames = list(NULL, common))
  cohort <- integer(total)

  # Target block first (cohort 0). The standardizer is fit on the RAW target and
  # then applied to every cohort as it streams in.
  Xt <- .read_geno_cols(Xt_path, common)
  if (nrow(Xt) != nt) {
    stop(sprintf("target_X_train has %d rows but target_y_train has %d",
                 nrow(Xt), nt), call. = FALSE)
  }
  st <- NULL
  if (isTRUE(standardize)) {
    st <- .fit_standardizer(Xt, standardize_method)
    Xt <- .apply_standardizer(Xt, st)
  }
  X[seq_len(nt), ] <- Xt
  rm(Xt)
  gc(verbose = FALSE)

  off <- nt
  for (i in seq_len(n_ext)) {
    Xe <- .read_geno_cols(ext_paths[[i]], common)
    if (nrow(Xe) != ne[i]) {
      stop(sprintf("external_X_%d has %d rows but external_y_%d has %d",
                   i, nrow(Xe), i, ne[i]), call. = FALSE)
    }
    if (isTRUE(standardize) && !is.null(st)) Xe <- .apply_standardizer(Xe, st)
    X[(off + 1):(off + ne[i]), ] <- Xe
    cohort[(off + 1):(off + ne[i])] <- i
    off <- off + ne[i]
    rm(Xe)
    gc(verbose = FALSE)
  }
  # For a Gaussian outcome on standardized data, y must be standardized too (the
  # model / evaluation are on the standardized scale); never for binary/Poisson.
  # Use the TARGET training y (cohort 0) mean/sd as the reference, mirroring
  # brier_i, and reuse it for the val/test y below.
  y_mu <- NULL; y_sd <- NULL
  if (isTRUE(standardize) && identical(outcome_family, "gaussian")) {
    y_mu <- mean(yt); y_sd <- stats::sd(yt); if (y_sd == 0) y_sd <- 1
  }
  y <- do.call(c, c(list(yt), ext_y))
  if (!is.null(y_mu)) {
    y <- (y - y_mu) / y_sd
  }
  if (isTRUE(standardize)) {
    report <- c(report, sprintf("standardized all cohorts (target constants, method=%s)",
                                standardize_method))
    if (!is.null(y_mu)) {
      report <- c(report, "standardized Gaussian y (target training mean/sd)")
    }
  }
  report <- c(report, sprintf("stacked X %dx%d, cohort vector (0=target,1..%d)",
                              nrow(X), ncol(X), n_ext))

  # A zero (p+1) x 1 external beta so a per-cohort EXTERNAL-ONLY comparator can
  # be fit with brier_i (eta=0) directly off this object: there are no pretrained
  # external coefficients in the brier_full shape (raw cohorts), so external-only
  # performance must be FIT, not scored. See the comparison workflow in prompts.
  prepared <- list(X = X, y = y, cohort = cohort, snp_info = common,
                   beta_zero = matrix(0, nrow = ncol(X) + 1L, ncol = 1L))

  # Optional AFR val/test splits for held-out selection / scoring, subset to the
  # shared panel and standardized to the TRAINING scale when standardize=TRUE.
  .split <- function(xr, yr) {
    Xs <- .read_geno_cols(.role_path(data_dir, roles, xr), common)
    if (isTRUE(standardize) && !is.null(st)) Xs <- .apply_standardizer(Xs, st)
    ys <- .pheno_vector(.read_role(data_dir, roles, yr))
    if (!is.null(y_mu)) ys <- (ys - y_mu) / y_sd   # Gaussian y -> training scale
    list(X = Xs, y = ys)
  }
  has_val <- !is.null(roles[["target_X_val"]]) && !is.null(roles[["target_y_val"]])
  has_test <- !is.null(roles[["target_X_test"]]) && !is.null(roles[["target_y_test"]])
  if (has_val) {
    s <- .split("target_X_val", "target_y_val")
    prepared$X_val <- s$X; prepared$y_val <- s$y
  }
  if (has_test) {
    s <- .split("target_X_test", "target_y_test")
    prepared$X_test <- s$X; prepared$y_test <- s$y
  }

  # Optional PER-COHORT external validation splits. If external cohort k ships its
  # own validation data (external_X_k_val / external_y_k_val), expose it, subset to
  # the shared panel and standardized to the SAME training constants as the stacked
  # X. This lets the EXTERNAL-ONLY comparator for cohort k be selected on its OWN
  # held-out data (gaussian.mspe) instead of by BIC. Note: selecting an external-only
  # model on the TARGET val would leak target information into a comparator meant to
  # be purely external, so each external val is used only for its own cohort.
  ext_has_val <- logical(n_ext)
  for (k in seq_len(n_ext)) {
    xr <- sprintf("external_X_%d_val", k); yr <- sprintf("external_y_%d_val", k)
    if (!is.null(roles[[xr]]) && !is.null(roles[[yr]])) {
      s <- .split(xr, yr)
      prepared[[sprintf("X_ext_%d_val", k)]] <- s$X
      prepared[[sprintf("y_ext_%d_val", k)]] <- s$y
      ext_has_val[k] <- TRUE
    }
  }

  hints <- list(X_expr = "prepared$X", y_expr = "prepared$y",
                cohort_expr = "prepared$cohort")
  if (has_val) {
    hints$X_val_expr <- "prepared$X_val"; hints$y_val_expr <- "prepared$y_val"
  }
  if (has_test) {
    hints$X_test_expr <- "prepared$X_test"; hints$y_test_expr <- "prepared$y_test"
  }

  # Per-cohort EXTERNAL-ONLY comparator hints. Each external cohort k gets a
  # (X, y) pair carved from the stacked object by its cohort id (a subset
  # expression, no data duplication) so the agent can fit a standalone model on
  # cohort k at eta=0 (brier_i with beta_zero) and evaluate it on the target
  # test set. beta_zero is the required zero external coefficient for that fit.
  hints$beta_zero_expr <- "prepared$beta_zero"
  # Target-cohort (cohort 0) rows, for the AFR-only baseline: a single-cohort
  # brier_i(eta=0) fit. BRIERfull needs >= 2 pooled cohorts, so it cannot fit a
  # target-only baseline itself.
  hints$X_target_expr <- "prepared$X[prepared$cohort == 0L, , drop = FALSE]"
  hints$y_target_expr <- "prepared$y[prepared$cohort == 0L]"
  for (k in seq_len(n_ext)) {
    hints[[sprintf("X_ext_%d_expr", k)]] <-
      sprintf("prepared$X[prepared$cohort == %dL, , drop = FALSE]", k)
    hints[[sprintf("y_ext_%d_expr", k)]] <-
      sprintf("prepared$y[prepared$cohort == %dL]", k)
    if (ext_has_val[k]) {
      hints[[sprintf("X_ext_%d_val_expr", k)]] <- sprintf("prepared$X_ext_%d_val", k)
      hints[[sprintf("y_ext_%d_val_expr", k)]] <- sprintf("prepared$y_ext_%d_val", k)
    }
  }
  report <- c(report, sprintf(
    "exposed %d per-cohort external-only comparator split(s) + beta_zero", n_ext))
  n_ext_val <- sum(ext_has_val)
  if (n_ext_val > 0) {
    report <- c(report, sprintf(
      "exposed %d per-cohort external validation split(s) for held-out comparator selection",
      n_ext_val))
  }

  list(prepared = prepared, report = report, expr_hints = hints)
}


# ---- dispatch --------------------------------------------------------------

args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input
  shape <- inp$shape
  data_dir <- inp$data_dir
  roles <- .normalize_roles(inp$roles)

  # A small model frequently nests the top-level PARAMS inside the roles map (roles
  # is a free-form dict, so nothing stops it). Left alone, they are silently ignored
  # AND their top-level defaults win -- most damagingly `standardize`, which then
  # defaults to FALSE and leaves the target on the raw scale while the fitted
  # external is on the standardized one: a silent scale mismatch, not an error. Lift
  # any nested param out of roles, preferring an explicit top-level value, then drop
  # it from roles so it is never mistaken for a file path.
  .nested <- function(key) if (!is.null(roles[[key]])) roles[[key]] else NULL
  standardize <- if (!is.null(inp$standardize)) isTRUE(inp$standardize)
                 else isTRUE(.nested("standardize"))
  standardize_method <- if (!is.null(inp$standardize_method)) inp$standardize_method
                        else if (!is.null(.nested("standardize_method"))) .nested("standardize_method")
                        else "sd"
  align_method <- if (!is.null(inp$align_method)) inp$align_method
                  else if (!is.null(.nested("align_method"))) .nested("align_method")
                  else "auto"
  outcome_family <- if (!is.null(inp$outcome_family)) inp$outcome_family
                    else if (!is.null(.nested("outcome_family"))) .nested("outcome_family")
                    else "gaussian"
  # The coverage threshold (PREP_AUTO_DESIGN.md 3.1). A PARAMETER, never hard-coded: the
  # default lives in .COVERAGE_MIN_DEFAULT and BRIER_MCP_COVERAGE_MIN overrides it for the
  # operator. Nested-in-roles is lifted like the other params.
  coverage_min <- if (!is.null(inp$coverage_min)) inp$coverage_min
                  else if (!is.null(.nested("coverage_min"))) .nested("coverage_min")
                  else NULL
  # What KIND of predictor (PREP_AUTO_DESIGN.md 5.0). "auto" DETECTS it from the variant map
  # (CHR + BP means a genome; nothing else can be one), so the caller never has to say. An
  # explicit "genotype" / "generic" (or an alias: snp, gene_expression, protein, ...) wins.
  predictor_type <- if (!is.null(inp$predictor_type)) inp$predictor_type
                    else if (!is.null(.nested("predictor_type"))) .nested("predictor_type")
                    else "auto"
  for (k in c("standardize", "standardize_method", "align_method", "outcome_family",
              "persist", "out_dir", "timeout_s", "shape", "data_dir", "coverage_min",
              "predictor_type")) {
    roles[[k]] <- NULL
  }


  ld_ancestry <- inp$ld_ancestry
  ld_build <- inp$ld_build
  # A small model tends to nest ld_ancestry/ld_build INSIDE the roles map instead
  # of passing them as top-level params. Lift them out when that happens (they are
  # scalars, not role paths), so the genotype-LD build gets its ancestry/build.
  if ((is.null(ld_ancestry) || !nzchar(ld_ancestry)) && !is.null(roles[["ld_ancestry"]])) {
    ld_ancestry <- roles[["ld_ancestry"]]
  }
  if ((is.null(ld_build) || !nzchar(ld_build)) && !is.null(roles[["ld_build"]])) {
    ld_build <- roles[["ld_build"]]
  }
  roles[["ld_ancestry"]] <- NULL
  roles[["ld_build"]] <- NULL
  # Ancestry/build for a RAW summary external's OWN LD (Bucket B). Defaults to the
  # target ld_ancestry/ld_build if omitted (a small model tends to give only one),
  # and is lifted out of the roles map the same way.
  ext_ld_ancestry <- inp$external_ld_ancestry
  ext_ld_build <- inp$external_ld_build
  if ((is.null(ext_ld_ancestry) || !nzchar(ext_ld_ancestry)) &&
      !is.null(roles[["external_ld_ancestry"]])) {
    ext_ld_ancestry <- roles[["external_ld_ancestry"]]
  }
  if ((is.null(ext_ld_build) || !nzchar(ext_ld_build)) &&
      !is.null(roles[["external_ld_build"]])) {
    ext_ld_build <- roles[["external_ld_build"]]
  }
  roles[["external_ld_ancestry"]] <- NULL
  roles[["external_ld_build"]] <- NULL
  if (is.null(ext_ld_ancestry) || !nzchar(ext_ld_ancestry)) ext_ld_ancestry <- ld_ancestry
  if (is.null(ext_ld_build) || !nzchar(ext_ld_build)) ext_ld_build <- ld_build
  persist <- is.null(inp$persist) || isTRUE(inp$persist)
  # Where to persist the assembled object. Explicit out_dir wins; otherwise use
  # BRIER_MCP_OUT_DIR when set (so a caller can keep a read-only data_dir clean),
  # falling back to data_dir.
  env_out <- Sys.getenv("BRIER_MCP_OUT_DIR", unset = "")
  out_dir <- if (!is.null(inp$out_dir)) {
    inp$out_dir
  } else if (nzchar(env_out)) {
    env_out
  } else {
    data_dir
  }

  if (is.null(shape) || !nzchar(shape)) stop("shape is required", call. = FALSE)
  if (is.null(data_dir) || !nzchar(data_dir)) stop("data_dir is required", call. = FALSE)
  if (is.null(roles) || length(roles) == 0) stop("roles is required", call. = FALSE)
  if (!standardize_method %in% c("sd", "maf")) {
    stop("standardize_method must be 'sd' or 'maf'", call. = FALSE)
  }
  if (!align_method %in% c("auto", "coordinate", "varnames")) {
    stop("align_method must be 'auto', 'coordinate', or 'varnames'", call. = FALSE)
  }

  report <- character(0)

  # BRIERs is INHERENTLY on the standardized scale: the fit is built from `corr` (a
  # standardized marginal correlation) and `XtX`, so its coefficients are standardized-scale
  # and scoring them against RAW genotypes is silently wrong. prep_auto used to standardize
  # the val/test splits only inside `if (isTRUE(standardize))`, so a `standardize = FALSE`
  # brier_s call produced garbage with NO ERROR. Override it, loudly, rather than erroring:
  # a small model that fumbles a knob it should never have touched should not burn a run.
  #
  # This lives in the DISPATCH, not in the recipe, so the value ECHOED BACK in the result
  # agrees with what was actually used. Overriding inside the recipe would leave the result
  # reporting standardize=FALSE while the run standardized anyway, which is exactly the kind
  # of quiet mismatch a scorer trips on.
  if (identical(shape, "brier_s") && !isTRUE(standardize)) {
    report <- c(report, paste0(
      "_notice_standardize_forced: brier_s requires standardization (BRIERs coefficients ",
      "live on the standardized scale, so scoring them against raw genotypes is wrong). ",
      "standardize was FALSE; overriding to TRUE."))
    standardize <- TRUE
  }
  rec <- switch(
    shape,
    brier_i    = .recipe_brier_i(data_dir, roles, standardize, standardize_method, outcome_family, align_method, report, ext_ld_ancestry, ext_ld_build, coverage_min, predictor_type),
    brier_s    = .recipe_brier_s(data_dir, roles, standardize, standardize_method, outcome_family, report, ld_ancestry, ld_build, ext_ld_ancestry, ext_ld_build, coverage_min, predictor_type),
    brier_full = .recipe_brier_full(data_dir, roles, standardize, standardize_method, outcome_family, report, coverage_min),
    stop(sprintf("unknown shape: %s", shape), call. = FALSE)
  )

  prepared_path <- NULL
  if (persist) {
    dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
    prepared_path <- file.path(out_dir, sprintf("prep_auto_%s.rds", shape))
    saveRDS(rec$prepared, prepared_path)
    rec$report <- c(rec$report, sprintf("persisted to %s", prepared_path))
    # The fitter loads this .rds through load_data_files, which binds the
    # saved object under a variable named after the FILE BASENAME (not
    # "prepared"). Rewrite the expr_hints to reference that actual variable so
    # X_expr / y_expr / beta_external_expr resolve inside the fit call. Replace
    # EVERY `prepared` token, not just the leading one: the per-cohort comparator
    # hints reference the object twice, e.g. `prepared$X[prepared$cohort == 1L]`,
    # and a leftover inner `prepared` would be undefined at fit time.
    obj_var <- tools::file_path_sans_ext(basename(prepared_path))
    rec$expr_hints <- lapply(rec$expr_hints, function(h) {
      gsub("\\bprepared\\b", obj_var, h)
    })
  }

  # Surface every internally-fit external so the fit is AUDITABLE from the tool
  # result: how it was selected and whether it is actually non-degenerate. Without
  # this the only visible evidence is "prep_auto returned ok", which once let a
  # numerically-zero external score full marks.
  ext_fits <- .EXT_DIAG$fits
  for (f in ext_fits) {
    rec$report <- c(rec$report, sprintf(
      "%s %s external: %d predictors, selected by %s -> %d nonzero coefs (max |coef| %s)",
      if (isTRUE(f$cached)) "reused CACHED" else "fitted",
      f$kind, f$n_predictors, f$selected_by, f$nonzero_coefs, format(f$max_abs_coef)))
  }
  # Warnings about the external fit that are not per-fit facts, e.g. a validation
  # split covering only part of the model's predictor panel (the missing predictors
  # contribute 0 to the val score, so lambda is chosen on a partial signal).
  if (length(.EXT_DIAG$notes)) {
    rec$report <- c(rec$report, paste0("_notice_external_val: ", .EXT_DIAG$notes))
  }

  list(status = "ok", shape = shape,
       prepared_path = if (is.null(prepared_path)) NA_character_ else prepared_path,
       standardize = standardize, standardize_method = standardize_method,
       expr_hints = rec$expr_hints, report = rec$report,
       external_fits = if (length(ext_fits)) ext_fits else NULL,
       n_external_fits = length(ext_fits))
}, error = function(e) {
  make_error(conditionMessage(e), where = "prep_auto.R", class = "PrepAutoError")
})

write_output(result, io$output_path)
