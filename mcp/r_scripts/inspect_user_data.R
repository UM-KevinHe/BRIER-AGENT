#!/usr/bin/env Rscript
# inspect_user_data.R
#
# Heuristic inspection of one or more user-provided data files. Runs the
# same structural inspection as inspect_data.R, then adds:
#   * Outcome family heuristic (gaussian / binomial / poisson / time-to-event)
#   * Predictor type heuristic (SNP / gene expression / protein / other)
#   * Data shape heuristic (individual-level vs sumstats vs pretrained coefs)
#   * Train/val/test split detection
#   * Detection of R expression paths to important variables
#
# Each heuristic returns {value, confidence, evidence, alternatives}.
# Confidence levels: high, medium, low, unknown.
#
# Called by mcp/server.py as:
#   Rscript inspect_user_data.R <input.json> <output.json>
#
# input.json: {
#   data_paths: ["path1", "path2", ...],   # one or more files
#   csv_options: {                          # optional, for .csv/.tsv/.txt
#     header: true,
#     row_labels: false,
#     sep: "auto"  # "auto" / "," / "\t" / " "
#   }
# }
#
# output.json: {
#   status: "ok",
#   inspection_id: "insp_yyyymmdd_hhmmss_xxxx",
#   inspection_path: "/path/to/cache.rds",
#   files: [
#     {
#       path: "...",
#       format: "rds" | "rda" | "csv" | "tsv",
#       structure: { ... },   # like inspect_data output
#       heuristics: {
#         data_shape: { value, confidence, evidence, alternatives },
#         outcome_family: { ... },
#         predictor_type: { ... },
#         splits: { ... },
#         missingness: { ... },
#         time_to_event: { ... },
#       },
#       suggested_exprs: {
#         target_X_expr: "...",
#         target_y_expr: "...",
#         external_beta_expr: "...",
#         sumstats_expr: "...",
#       }
#     }
#   ],
#   combined_assessment: {  # across all files
#     target_shape: "individual" | "sumstats" | "unknown",
#     external_shape: "coefficients" | "individual" | "sumstats" | "none" | "unknown",
#     n_target: int | null,
#     n_external_total: int | null,
#     p: int | null,
#     M: int | null,
#     outcome_family: "gaussian" | "binomial" | "poisson" | "time-to-event" | "unknown",
#     predictor_type: "SNP" | "gene_expression" | "protein" | "mixed" | "unknown",
#     has_validation_set: bool | null,
#   }
# }

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


# -- cache infrastructure --------------------------------------------------

.cache_root_inspections <- function() {
  base <- Sys.getenv("XDG_CACHE_HOME", unset = NA)
  if (is.na(base) || !nzchar(base)) {
    base <- if (.Platform$OS.type == "windows") {
      Sys.getenv("LOCALAPPDATA",
                 unset = file.path(Sys.getenv("HOME"), "AppData", "Local"))
    } else {
      file.path(Sys.getenv("HOME"), ".cache")
    }
  }
  d <- file.path(base, "brier-mcp", "inspections")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.generate_inspection_id <- function() {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0("insp_", ts, "_", suffix)
}

# -- format detection ------------------------------------------------------

.detect_format <- function(path) {
  # A gzipped delimited file (e.g. sumstats.txt.gz) reports its extension as
  # "gz" via tools::file_ext, which would fall through to "unknown". Strip a
  # trailing .gz/.bgz first so the INNER extension (txt/csv/tsv) is detected;
  # read.table / read.csv decompress gzip transparently, so no other change is
  # needed to actually read the file.
  path <- sub("\\.(gz|bgz)$", "", path, ignore.case = TRUE)
  ext <- tolower(tools::file_ext(path))
  switch(
    ext,
    "rds" = "rds",
    "rda" = "rda",
    "rdata" = "rda",
    "csv" = "csv",
    "tsv" = "tsv",
    "txt" = "txt",
    "xlsx" = "xlsx",
    "xls" = "xlsx",
    "pgen" = "pgen",
    "bed" = "bed",
    "bgen" = "bgen",
    "unknown"
  )
}

.read_delimited <- function(path, csv_options) {
  if (is.null(csv_options)) csv_options <- list()

  header <- if (is.null(csv_options$header)) TRUE
            else isTRUE(csv_options$header)
  row_labels <- if (is.null(csv_options$row_labels)) FALSE
                else isTRUE(csv_options$row_labels)
  sep <- if (is.null(csv_options$sep)) "auto" else csv_options$sep

  if (sep == "auto") {
    ext_path <- sub("\\.(gz|bgz)$", "", path, ignore.case = TRUE)
    ext <- tolower(tools::file_ext(ext_path))
    sep <- switch(ext, "csv" = ",", "tsv" = "\t", "\t")
  }

  utils::read.table(
    path,
    header = header,
    sep = sep,
    row.names = if (row_labels) 1 else NULL,
    stringsAsFactors = FALSE,
    check.names = FALSE
  )
}

# -- v2.0.0: extended-format helpers ---------------------------------------
# Genetic binary formats (.pgen/.bed/.bgen) are NEVER loaded into memory.
# Instead we read their companion text files to report dimensions, the same
# metadata-only philosophy inspect_data.R uses for large R objects. xlsx is
# read via readxl if available, degrading gracefully if not (R packages are
# user-installed; locked-down servers may lack readxl).

.count_lines <- function(path) {
  # Fast line count without loading the whole file into one object.
  if (!file.exists(path)) return(NA_integer_)
  n <- 0L
  con <- file(path, open = "r")
  on.exit(close(con))
  repeat {
    chunk <- readLines(con, n = 65536L, warn = FALSE)
    if (length(chunk) == 0L) break
    n <- n + length(chunk)
  }
  n
}

.read_header_cols <- function(path, sep = "\t", comment_prefixes = character(0)) {
  # Return the column names from the first non-comment line of a text file.
  con <- file(path, open = "r")
  on.exit(close(con))
  repeat {
    line <- readLines(con, n = 1L, warn = FALSE)
    if (length(line) == 0L) return(character(0))
    skip <- FALSE
    for (pre in comment_prefixes) {
      if (startsWith(line, pre)) { skip <- TRUE; break }
    }
    if (!skip && nzchar(line)) {
      return(strsplit(line, sep, fixed = TRUE)[[1]])
    }
  }
}

.inspect_plink2 <- function(pgen_path) {
  # PLINK2 trio: .pgen (binary genotypes), .pvar (variants), .psam (samples).
  # We read only the .pvar/.psam companions, never the .pgen binary.
  base <- tools::file_path_sans_ext(pgen_path)
  pvar <- paste0(base, ".pvar")
  psam <- paste0(base, ".psam")
  .pvar_header_lines <- function(p) {
    # Count leading ## meta lines plus the single #CHROM column-header line.
    hdr <- 0L
    con <- file(p, open = "r")
    on.exit(close(con))
    repeat {
      line <- readLines(con, n = 1L, warn = FALSE)
      if (length(line) == 0L) break
      if (startsWith(line, "##")) { hdr <- hdr + 1L; next }
      if (startsWith(line, "#")) { hdr <- hdr + 1L; break }
      break
    }
    hdr
  }
  n_variants <- if (file.exists(pvar)) {
    nl <- .count_lines(pvar)
    hdr <- .pvar_header_lines(pvar)
    if (is.na(nl)) NA_integer_ else nl - hdr
  } else NA_integer_
  psam_info <- list(n_samples = NA_integer_, has_phenotype = NA, columns = NULL)
  if (file.exists(psam)) {
    nl <- .count_lines(psam)
    cols <- .read_header_cols(psam, sep = "\t")
    # .psam header line starts with #FID or #IID; sample lines follow.
    has_header <- length(cols) > 0L && startsWith(cols[1], "#")
    psam_info$n_samples <- if (is.na(nl)) NA_integer_
                           else if (has_header) nl - 1L else nl
    psam_info$columns <- cols
    pheno_cols <- grep("PHENO|^P[0-9]|PHENOTYPE", cols, ignore.case = TRUE)
    psam_info$has_phenotype <- length(pheno_cols) > 0L
  }
  list(
    format = "pgen",
    kind = "PLINK2 genotype data (binary; companions read for metadata)",
    pgen_path = pgen_path,
    companions = list(
      pvar = if (file.exists(pvar)) pvar else NA_character_,
      psam = if (file.exists(psam)) psam else NA_character_
    ),
    n_variants = n_variants,
    n_samples = psam_info$n_samples,
    has_phenotype = psam_info$has_phenotype,
    psam_columns = psam_info$columns,
    note = if (!file.exists(pvar) || !file.exists(psam))
      "one or more companion files (.pvar/.psam) missing; dimensions partial"
      else NULL
  )
}

.inspect_plink1 <- function(bed_path) {
  # PLINK1 trio: .bed (binary), .bim (variants), .fam (samples).
  base <- tools::file_path_sans_ext(bed_path)
  bim <- paste0(base, ".bim")
  fam <- paste0(base, ".fam")
  n_variants <- if (file.exists(bim)) .count_lines(bim) else NA_integer_
  n_samples  <- if (file.exists(fam)) .count_lines(fam) else NA_integer_
  # .fam column 6 is phenotype; -9 / 0 conventionally means missing.
  has_phenotype <- NA
  if (file.exists(fam)) {
    first <- .read_header_cols(fam, sep = " ")
    if (length(first) < 6L) {
      # .fam may be whitespace (not single-space) delimited
      con <- file(fam, open = "r"); on.exit(close(con), add = TRUE)
      line <- readLines(con, n = 1L, warn = FALSE)
      if (length(line) > 0L) first <- strsplit(trimws(line), "\\s+")[[1]]
    }
    if (length(first) >= 6L) {
      pheno_val <- suppressWarnings(as.numeric(first[6]))
      has_phenotype <- !is.na(pheno_val) && !(pheno_val %in% c(-9, 0))
    }
  }
  list(
    format = "bed",
    kind = "PLINK1 genotype data (binary; companions read for metadata)",
    bed_path = bed_path,
    companions = list(
      bim = if (file.exists(bim)) bim else NA_character_,
      fam = if (file.exists(fam)) fam else NA_character_
    ),
    n_variants = n_variants,
    n_samples = n_samples,
    has_phenotype = has_phenotype,
    note = if (!file.exists(bim) || !file.exists(fam))
      "one or more companion files (.bim/.fam) missing; dimensions partial"
      else NULL
  )
}

.inspect_bgen <- function(bgen_path) {
  # BGEN: binary, no simple text companion for variants. A .sample file may
  # accompany it for sample IDs. We report what is cheaply knowable without
  # a bgen parser (which would be a heavy dependency); this is intentionally
  # shallower than the PLINK formats.
  base <- tools::file_path_sans_ext(bgen_path)
  sample_file <- paste0(base, ".sample")
  n_samples <- NA_integer_
  if (file.exists(sample_file)) {
    nl <- .count_lines(sample_file)
    # .sample has 2 header lines (names + types) then one row per sample.
    n_samples <- if (is.na(nl)) NA_integer_ else max(nl - 2L, 0L)
  }
  list(
    format = "bgen",
    kind = "BGEN genotype data (binary; shallow inspection only)",
    bgen_path = bgen_path,
    companions = list(
      sample = if (file.exists(sample_file)) sample_file else NA_character_
    ),
    n_samples = n_samples,
    n_variants = NA_integer_,
    has_phenotype = NA,
    note = paste(
      "BGEN variant count requires a bgen parser (not read here to avoid a",
      "heavy dependency). Only sample count from the .sample companion is",
      "reported, if present."
    )
  )
}

.read_xlsx <- function(path, csv_options) {
  # Read the first sheet of an .xlsx via readxl, if installed. Returns a
  # data.frame so the normal tabular heuristics apply. If readxl is absent,
  # signals graceful degradation via an attribute the caller checks.
  if (!requireNamespace("readxl", quietly = TRUE)) {
    out <- data.frame()
    attr(out, ".xlsx_unavailable") <- TRUE
    return(out)
  }
  sheet <- if (!is.null(csv_options) && !is.null(csv_options$sheet))
    csv_options$sheet else 1
  as.data.frame(
    readxl::read_excel(path, sheet = sheet),
    stringsAsFactors = FALSE,
    check.names = FALSE
  )
}

# -- structural inspection (like inspect_data) -----------------------------

.describe_object <- function(obj, max_depth = 3, depth = 0) {
  if (depth >= max_depth) return(list(type = class(obj)[1], note = "truncated"))

  if (is.null(obj)) return(list(type = "NULL"))

  if (is.matrix(obj)) {
    return(list(
      type = "matrix",
      dim = dim(obj),
      mode = storage.mode(obj),
      colnames_sample = if (!is.null(colnames(obj)))
        head(colnames(obj), 10) else NULL
    ))
  }

  if (is.data.frame(obj)) {
    return(list(
      type = "data.frame",
      dim = dim(obj),
      colnames = colnames(obj),
      col_classes = vapply(obj, function(c) class(c)[1], character(1))
    ))
  }

  if (is.list(obj) && !is.null(names(obj))) {
    children <- lapply(obj, .describe_object,
                       max_depth = max_depth, depth = depth + 1)
    return(list(
      type = "list",
      names = names(obj),
      children = children
    ))
  }

  if (is.vector(obj) || is.factor(obj)) {
    n <- length(obj)
    uvals <- if (is.numeric(obj)) {
      sort(unique(stats::na.omit(obj)))
    } else {
      unique(obj)
    }
    n_unique <- length(uvals)
    return(list(
      type = class(obj)[1],
      length = n,
      n_unique = n_unique,
      sample_values = if (n_unique <= 10) uvals else head(uvals, 5),
      n_na = sum(is.na(obj)),
      range = if (is.numeric(obj) && n_unique > 0)
        as.numeric(range(obj, na.rm = TRUE)) else NULL
    ))
  }

  list(type = class(obj)[1])
}

# -- heuristics ------------------------------------------------------------

.heuristic_outcome_family <- function(y) {
  if (is.null(y)) {
    return(list(value = "unknown", confidence = "unknown",
                evidence = "no outcome vector identified", alternatives = c()))
  }

  if (!is.numeric(y) && !is.factor(y) && !is.logical(y)) {
    return(list(
      value = "unknown",
      confidence = "low",
      evidence = sprintf("y is class %s; not obviously numeric",
                          class(y)[1]),
      alternatives = c("gaussian", "binomial")
    ))
  }

  yv <- if (is.factor(y)) as.numeric(y) - 1 else as.numeric(y)
  yv <- yv[!is.na(yv)]
  if (length(yv) == 0) {
    return(list(value = "unknown", confidence = "unknown",
                evidence = "y is all NA", alternatives = c()))
  }

  uvals <- sort(unique(yv))
  n_unique <- length(uvals)

  # Binary
  if (n_unique == 2 && all(uvals %in% c(0, 1))) {
    return(list(
      value = "binomial",
      confidence = "high",
      evidence = sprintf("y has 2 unique values {0, 1} across %d obs",
                          length(yv)),
      alternatives = c()
    ))
  }
  if (n_unique == 2) {
    return(list(
      value = "binomial",
      confidence = "medium",
      evidence = sprintf("y has 2 unique values {%s} (not standard 0/1 coding)",
                          paste(uvals, collapse = ", ")),
      alternatives = c("gaussian")
    ))
  }

  # Count: integer-valued, non-negative, more than 2 unique values
  is_integer_valued <- all(yv == round(yv))
  if (is_integer_valued && all(yv >= 0) && n_unique >= 3 && n_unique <= 50) {
    return(list(
      value = "poisson",
      confidence = "medium",
      evidence = sprintf(
        "y is integer-valued, non-negative, with %d unique values; range [%g, %g]",
        n_unique, min(yv), max(yv)
      ),
      alternatives = c("gaussian", "binomial")
    ))
  }

  # Continuous
  return(list(
    value = "gaussian",
    confidence = if (n_unique > 50) "high" else "medium",
    evidence = sprintf(
      "y is continuous with %d unique values; range [%g, %g]",
      n_unique, min(yv), max(yv)
    ),
    alternatives = c()
  ))
}

.heuristic_predictor_type <- function(X) {
  if (is.null(X) || (is.null(colnames(X)) && !is.data.frame(X))) {
    return(list(value = "unknown", confidence = "unknown",
                evidence = "no predictor matrix or column names",
                alternatives = c()))
  }
  cn <- colnames(X)
  if (is.null(cn) || length(cn) == 0) {
    return(list(value = "unknown", confidence = "unknown",
                evidence = "predictor matrix has no column names",
                alternatives = c()))
  }

  n_total <- length(cn)
  n_sample <- min(n_total, 200)
  sample_cols <- cn[seq_len(n_sample)]

  # rs IDs
  n_rsid <- sum(grepl("^rs[0-9]+$", sample_cols, ignore.case = TRUE))
  # Chromosomal positions (1:12345, chr1:12345)
  n_chrpos <- sum(grepl("^(chr)?[0-9XYM]+[:_-][0-9]+", sample_cols,
                         ignore.case = TRUE))
  # Gene symbols: short uppercase strings, often with digits
  n_gene_symbol <- sum(grepl("^[A-Z][A-Z0-9]{1,15}$", sample_cols))
  # Ensembl gene IDs
  n_ensembl <- sum(grepl("^ENSG[0-9]+", sample_cols, ignore.case = TRUE))
  # Uniprot
  n_uniprot <- sum(grepl("^[OPQ][0-9][A-Z0-9]{3}[0-9]$",
                          sample_cols, ignore.case = TRUE) |
                   grepl("^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$",
                          sample_cols, ignore.case = TRUE))

  snp_frac <- (n_rsid + n_chrpos) / n_sample
  gene_frac <- (n_gene_symbol + n_ensembl) / n_sample
  prot_frac <- n_uniprot / n_sample

  if (snp_frac > 0.7) {
    return(list(
      value = "SNP",
      confidence = if (snp_frac > 0.9) "high" else "medium",
      evidence = sprintf(
        "%.0f%% of sampled column names look like SNP identifiers (rsID or chr:pos)",
        100 * snp_frac
      ),
      alternatives = c()
    ))
  }
  if (gene_frac > 0.5) {
    return(list(
      value = "gene_expression",
      confidence = if (n_ensembl > 0) "high" else "medium",
      evidence = sprintf(
        "%.0f%% of column names look like gene symbols or Ensembl IDs",
        100 * gene_frac
      ),
      alternatives = c("protein")
    ))
  }
  if (prot_frac > 0.5) {
    return(list(
      value = "protein",
      confidence = "medium",
      evidence = sprintf(
        "%.0f%% of column names look like Uniprot IDs", 100 * prot_frac
      ),
      alternatives = c()
    ))
  }

  return(list(
    value = "mixed",
    confidence = "low",
    evidence = sprintf(
      "column-name heuristics did not match a single predictor type "
      %+% "(SNP=%.0f%%, gene=%.0f%%, protein=%.0f%%)",
      100 * snp_frac, 100 * gene_frac, 100 * prot_frac
    ),
    alternatives = c("SNP", "gene_expression", "protein")
  ))
}

`%+%` <- function(a, b) paste0(a, b)

.heuristic_data_shape <- function(obj) {
  # Returns: "individual" | "sumstats" | "coefficients" | "unknown"
  if (is.null(obj)) {
    return(list(value = "unknown", confidence = "unknown",
                evidence = "no object", alternatives = c()))
  }

  if (is.data.frame(obj)) {
    cn_lower <- tolower(colnames(obj))
    sumstats_signals <- c("corr", "pval", "p.value", "p_value", "p", "stats",
                          "beta", "se", "n", "df", "z", "or", "log_or",
                          "effect", "effect_allele", "ref", "alt")
    matches <- intersect(cn_lower, sumstats_signals)
    if (length(matches) >= 3) {
      return(list(
        value = "sumstats",
        confidence = "high",
        evidence = sprintf(
          "data.frame with sumstats-like columns: %s",
          paste(matches, collapse = ", ")
        ),
        alternatives = c()
      ))
    }
  }

  # Object that looks like individual-level data
  if (is.list(obj) && !is.null(names(obj))) {
    nm_lower <- tolower(names(obj))
    if (any(c("x") %in% nm_lower) && any(c("y") %in% nm_lower)) {
      return(list(
        value = "individual",
        confidence = "high",
        evidence = "object contains both X and y at the top level",
        alternatives = c()
      ))
    }
    if ("sumstats" %in% nm_lower) {
      return(list(
        value = "sumstats",
        confidence = "high",
        evidence = "object has a 'sumstats' field",
        alternatives = c()
      ))
    }
    if (any(c("beta.external", "beta_external", "betaext", "beta") %in% nm_lower)) {
      # Could be either: pretrained coefs alongside an individual target,
      # or could be standalone pretrained external info. Look for X/y too.
      if (any(c("x") %in% nm_lower) && any(c("y") %in% nm_lower)) {
        # Individual + coefs
        return(list(
          value = "individual",
          confidence = "high",
          evidence = "object has X, y, AND beta.external (individual target + pretrained external)",
          alternatives = c()
        ))
      }
    }
  }

  if (is.matrix(obj) || (is.data.frame(obj) && !is.list(obj))) {
    # Bare matrix or data frame; could be X-only or pretrained-coefs
    return(list(
      value = "unknown",
      confidence = "low",
      evidence = sprintf(
        "bare matrix/data.frame of shape %dx%d; could be X-only or pretrained coefficients",
        nrow(obj), ncol(obj)
      ),
      alternatives = c("individual", "coefficients", "sumstats")
    ))
  }

  return(list(value = "unknown", confidence = "low",
              evidence = "could not match any known shape",
              alternatives = c("individual", "sumstats", "coefficients")))
}

.detect_splits <- function(obj) {
  if (!is.list(obj) || is.null(names(obj))) {
    return(list(value = FALSE, confidence = "high",
                evidence = "object is not a named list",
                alternatives = c()))
  }
  nm <- tolower(names(obj))
  has_train <- any(c("train", "training") %in% nm)
  has_val <- any(c("val", "validation", "valid") %in% nm)
  has_test <- any(c("test", "testing") %in% nm)

  if (has_train && has_val) {
    return(list(
      value = TRUE,
      confidence = "high",
      evidence = sprintf(
        "found train/val splits at top level (%s)",
        paste(intersect(nm, c("train", "training", "val", "validation",
                              "valid", "test", "testing")), collapse = ", ")
      ),
      alternatives = c()
    ))
  }

  # Look one level deeper for nested target$train, target$validation etc.
  for (key in names(obj)) {
    sub <- obj[[key]]
    if (is.list(sub) && !is.null(names(sub))) {
      subnm <- tolower(names(sub))
      if (any(c("train", "training") %in% subnm) &&
          any(c("val", "validation") %in% subnm)) {
        return(list(
          value = TRUE,
          confidence = "high",
          evidence = sprintf(
            "found train/val splits nested under '%s'", key
          ),
          alternatives = c()
        ))
      }
    }
  }

  return(list(value = FALSE, confidence = "medium",
              evidence = "no train/val/test field names detected",
              alternatives = c()))
}

.detect_time_to_event <- function(obj) {
  # Look for 'time' + 'event' or 'status' columns, or Surv objects
  if (inherits(obj, "Surv")) {
    return(list(value = TRUE, confidence = "high",
                evidence = "object is a survival::Surv object",
                alternatives = c()))
  }
  if (is.data.frame(obj)) {
    cn_lower <- tolower(colnames(obj))
    has_time <- any(c("time", "tte", "follow_up", "followup",
                      "time_to_event") %in% cn_lower)
    has_event <- any(c("event", "status", "censor", "censored",
                       "death") %in% cn_lower)
    if (has_time && has_event) {
      return(list(
        value = TRUE,
        confidence = "medium",
        evidence = "data.frame has time + event/status columns",
        alternatives = c()
      ))
    }
  }
  return(list(value = FALSE, confidence = "high",
              evidence = "no time/event signature detected",
              alternatives = c()))
}

.compute_missingness <- function(obj) {
  if (is.matrix(obj) || is.data.frame(obj)) {
    n_na <- sum(is.na(obj))
    n_total <- length(obj)
    return(list(
      n_na = n_na,
      n_total = n_total,
      fraction = if (n_total > 0) n_na / n_total else 0
    ))
  }
  if (is.vector(obj) || is.factor(obj)) {
    n_na <- sum(is.na(obj))
    return(list(
      n_na = n_na,
      n_total = length(obj),
      fraction = if (length(obj) > 0) n_na / length(obj) else 0
    ))
  }
  list(n_na = NA_integer_, n_total = NA_integer_, fraction = NA_real_)
}

# -- expression suggestion --------------------------------------------------

.suggest_exprs <- function(obj, top_name) {
  # Returns suggested R expression strings for common BRIER inputs.
  # top_name is the variable name the inspection loaded the file as
  # (i.e., the basename of the .rds without extension, for .rds files;
  # or the actual object name from .rda).
  out <- list()

  if (!is.list(obj) || is.null(names(obj))) {
    # Flat object: probably the X matrix itself
    out$target_X_expr <- top_name
    return(out)
  }

  nm <- names(obj)
  nm_lower <- tolower(nm)

  # Direct top-level X/y
  if ("X" %in% nm) out$target_X_expr <- paste0(top_name, "$X")
  else if ("x" %in% nm) out$target_X_expr <- paste0(top_name, "$x")

  if ("y" %in% nm) out$target_y_expr <- paste0(top_name, "$y")
  else if ("Y" %in% nm) out$target_y_expr <- paste0(top_name, "$Y")

  if ("sumstats" %in% nm) out$sumstats_expr <- paste0(top_name, "$sumstats")
  if ("beta.external" %in% nm) {
    out$external_beta_expr <- paste0(top_name, "$beta.external")
  } else if ("beta_external" %in% nm) {
    out$external_beta_expr <- paste0(top_name, "$beta_external")
  }

  # Look under target / training nested keys
  target_key <- nm[match("target", nm_lower)]
  if (!is.na(target_key)) {
    sub <- obj[[target_key]]
    if (is.list(sub) && !is.null(names(sub))) {
      subnm <- names(sub)
      subnm_lower <- tolower(subnm)
      train_key <- subnm[match("train", subnm_lower)]
      if (is.na(train_key)) train_key <- subnm[match("training", subnm_lower)]
      if (!is.na(train_key)) {
        train_sub <- sub[[train_key]]
        if (is.list(train_sub) && !is.null(names(train_sub))) {
          if ("X" %in% names(train_sub)) {
            out$target_X_expr <-
              paste0(top_name, "$", target_key, "$", train_key, "$X")
          }
          if ("y" %in% names(train_sub)) {
            out$target_y_expr <-
              paste0(top_name, "$", target_key, "$", train_key, "$y")
          }
          if ("sumstats" %in% names(train_sub)) {
            out$sumstats_expr <-
              paste0(top_name, "$", target_key, "$", train_key, "$sumstats")
          }
        }
      }
      val_key <- subnm[match("validation", subnm_lower)]
      if (is.na(val_key)) val_key <- subnm[match("val", subnm_lower)]
      if (!is.na(val_key)) {
        val_sub <- sub[[val_key]]
        if (is.list(val_sub) && !is.null(names(val_sub))) {
          if ("X" %in% names(val_sub)) {
            out$X_val_expr <-
              paste0(top_name, "$", target_key, "$", val_key, "$X")
          }
          if ("y" %in% names(val_sub)) {
            out$y_val_expr <-
              paste0(top_name, "$", target_key, "$", val_key, "$y")
          }
        }
      }
      test_key <- subnm[match("testing", subnm_lower)]
      if (is.na(test_key)) test_key <- subnm[match("test", subnm_lower)]
      if (!is.na(test_key)) {
        test_sub <- sub[[test_key]]
        if (is.list(test_sub) && !is.null(names(test_sub))) {
          if ("X" %in% names(test_sub)) {
            out$X_test_expr <-
              paste0(top_name, "$", target_key, "$", test_key, "$X")
          }
          if ("y" %in% names(test_sub)) {
            out$y_test_expr <-
              paste0(top_name, "$", target_key, "$", test_key, "$y")
          }
        }
      }
    }
  }

  out
}

# -- per-file processing ---------------------------------------------------

.find_target_X_y <- function(obj) {
  # Best-effort: find what looks like the "main" X and y for heuristics.
  # Returns list(X = ..., y = ...) where either may be NULL.
  if (!is.list(obj)) {
    if (is.matrix(obj) || is.data.frame(obj)) {
      return(list(X = obj, y = NULL))
    }
    return(list(X = NULL, y = NULL))
  }

  nm_lower <- tolower(names(obj))
  X <- NULL; y <- NULL

  # Direct
  if ("x" %in% nm_lower) X <- obj[[which(nm_lower == "x")[1]]]
  if ("y" %in% nm_lower) y <- obj[[which(nm_lower == "y")[1]]]
  if (!is.null(X) || !is.null(y)) return(list(X = X, y = y))

  # Nested under target$train$
  if ("target" %in% nm_lower) {
    sub <- obj[[which(nm_lower == "target")[1]]]
    if (is.list(sub)) {
      subnm <- tolower(names(sub))
      if ("train" %in% subnm || "training" %in% subnm) {
        train <- sub[[which(subnm %in% c("train", "training"))[1]]]
        if (is.list(train)) {
          tnm <- tolower(names(train))
          if ("x" %in% tnm) X <- train[[which(tnm == "x")[1]]]
          if ("y" %in% tnm) y <- train[[which(tnm == "y")[1]]]
        }
      }
    }
  }

  list(X = X, y = y)
}

.detect_nested_externals <- function(obj) {
  # Look for sibling-named external cohorts at the top level of a single
  # .rds file: external1, external2, external3, ... OR ext1, ext2, ...
  # Each should itself be a list containing X (and ideally y).
  #
  # Returns a list:
  #   M: integer count (0 if none detected)
  #   names: character vector of detected external keys
  #   shape: "individual" | "coefficients" | "sumstats" | "mixed" | "unknown"
  #   n_total: sum of nrow(X) across detected externals (if individual)
  #   X_exprs / y_exprs: lists keyed by external name, with R expression strings
  if (!is.list(obj) || is.null(names(obj))) {
    return(list(M = 0L, names = character(0), shape = "unknown",
                n_total = NULL))
  }
  top_nm <- names(obj)
  # Match common patterns: external1, external2, ..., ext1, ext2, ...,
  # ext_1, ext_2, ..., external_eur, external_afr, etc.
  ext_pattern <- "^(external|ext)(_?)([0-9]+|[a-zA-Z]+)$"
  ext_keys <- top_nm[grepl(ext_pattern, top_nm, ignore.case = TRUE)]
  if (length(ext_keys) == 0L) {
    return(list(M = 0L, names = character(0), shape = "unknown",
                n_total = NULL))
  }

  # For each detected external, inspect its shape.
  shapes <- character(length(ext_keys))
  n_per <- integer(length(ext_keys))
  X_exprs <- character(length(ext_keys))
  y_exprs <- character(length(ext_keys))
  for (i in seq_along(ext_keys)) {
    key <- ext_keys[i]
    sub <- obj[[key]]
    shape_i <- "unknown"
    n_i <- 0L
    X_expr_i <- ""
    y_expr_i <- ""
    if (is.list(sub) && !is.null(names(sub))) {
      sub_nm <- tolower(names(sub))
      # Check for train/X structure first
      train_idx <- which(sub_nm %in% c("train", "training"))
      if (length(train_idx) > 0L) {
        train <- sub[[train_idx[1]]]
        if (is.list(train) && !is.null(names(train))) {
          t_nm <- tolower(names(train))
          if ("x" %in% t_nm && "y" %in% t_nm) {
            shape_i <- "individual"
            X_obj <- train[[which(t_nm == "x")[1]]]
            if (is.matrix(X_obj) || is.data.frame(X_obj)) n_i <- nrow(X_obj)
            X_expr_i <- paste0("$", key, "$", names(sub)[train_idx[1]],
                                "$", names(train)[which(t_nm == "x")[1]])
            y_expr_i <- paste0("$", key, "$", names(sub)[train_idx[1]],
                                "$", names(train)[which(t_nm == "y")[1]])
          }
        }
      }
      # Fall back to top-level X/y inside the external
      if (shape_i == "unknown") {
        if ("x" %in% sub_nm && "y" %in% sub_nm) {
          shape_i <- "individual"
          X_obj <- sub[[which(sub_nm == "x")[1]]]
          if (is.matrix(X_obj) || is.data.frame(X_obj)) n_i <- nrow(X_obj)
          X_expr_i <- paste0("$", key, "$",
                              names(sub)[which(sub_nm == "x")[1]])
          y_expr_i <- paste0("$", key, "$",
                              names(sub)[which(sub_nm == "y")[1]])
        } else if ("sumstats" %in% sub_nm) {
          shape_i <- "sumstats"
        }
      }
    } else if (is.matrix(sub) || (is.data.frame(sub) && !is.null(ncol(sub)))) {
      # bare matrix or data.frame: could be a coefficient vector
      shape_i <- "coefficients"
    }
    shapes[i] <- shape_i
    n_per[i] <- n_i
    X_exprs[i] <- X_expr_i
    y_exprs[i] <- y_expr_i
  }

  unique_shapes <- unique(shapes[nzchar(shapes) & shapes != "unknown"])
  combined_shape <- if (length(unique_shapes) == 1L) unique_shapes[1]
                    else if (length(unique_shapes) > 1L) "mixed"
                    else "unknown"
  n_total <- sum(n_per)
  if (n_total == 0L) n_total <- NULL

  list(
    M = length(ext_keys),
    names = ext_keys,
    shape = combined_shape,
    n_total = n_total,
    n_per_external = if (any(n_per > 0L)) as.list(setNames(n_per, ext_keys))
                     else NULL,
    X_exprs = setNames(X_exprs, ext_keys),
    y_exprs = setNames(y_exprs, ext_keys)
  )
}


.inspect_one_file <- function(path, csv_options) {
  if (!file.exists(path)) {
    return(list(
      path = path,
      error = sprintf("File not found: %s", path)
    ))
  }

  format <- .detect_format(path)
  if (format == "unknown") {
    return(list(
      path = path,
      error = sprintf("Unsupported file extension: %s",
                       tools::file_ext(path))
    ))
  }

  # Genetic binary formats are inspected via their companion text files
  # only; the binary itself is never loaded. They return a metadata-only
  # result and skip the in-memory heuristics (which assume an R object).
  if (format %in% c("pgen", "bed", "bgen")) {
    geno <- switch(
      format,
      "pgen" = .inspect_plink2(path),
      "bed"  = .inspect_plink1(path),
      "bgen" = .inspect_bgen(path)
    )
    return(list(
      path = path,
      format = format,
      top_name = tools::file_path_sans_ext(basename(path)),
      structure = geno,
      heuristics = list(
        data_shape = list(
          value = "genotype_panel",
          confidence = "high",
          evidence = geno$kind,
          alternatives = c()
        ),
        predictor_type = list(
          value = "SNP",
          confidence = "high",
          evidence = "genotype binary format (PLINK/BGEN)",
          alternatives = c()
        )
      ),
      suggested_exprs = list(),
      derived = list(n = geno$n_samples, p = geno$n_variants)
    ))
  }

  # Load the object(s)
  obj <- NULL
  top_name <- NULL
  if (format == "rds") {
    obj <- readRDS(path)
    top_name <- tools::file_path_sans_ext(basename(path))
  } else if (format == "rda") {
    env <- new.env()
    loaded <- load(path, envir = env)
    if (length(loaded) == 1) {
      obj <- get(loaded[1], envir = env)
      top_name <- loaded[1]
    } else {
      # Multiple objects in .rda - wrap as a named list
      obj <- as.list(env)
      top_name <- tools::file_path_sans_ext(basename(path))
    }
  } else if (format == "xlsx") {
    obj <- .read_xlsx(path, csv_options)
    top_name <- tools::file_path_sans_ext(basename(path))
    if (isTRUE(attr(obj, ".xlsx_unavailable"))) {
      return(list(
        path = path,
        format = format,
        error = paste(
          "xlsx detected but the 'readxl' R package is not installed in",
          "this R environment. Install it (install.packages('readxl')) or",
          "export the sheet to .csv and inspect that instead."
        )
      ))
    }
  } else {
    # CSV / TSV / TXT
    obj <- .read_delimited(path, csv_options)
    top_name <- tools::file_path_sans_ext(basename(path))
  }

  # Structural inspection
  structure_desc <- .describe_object(obj)

  # Find candidate X / y for heuristics
  Xy <- .find_target_X_y(obj)

  # Detect nested external cohorts (external1, external2, ... at top level)
  nested_ext <- .detect_nested_externals(obj)
  # Prefix the X_exprs / y_exprs with the top-level variable name
  if (nested_ext$M > 0L) {
    nested_ext$X_exprs <- setNames(
      vapply(nested_ext$X_exprs,
              function(s) if (nzchar(s)) paste0(top_name, s) else "",
              character(1)),
      names(nested_ext$X_exprs)
    )
    nested_ext$y_exprs <- setNames(
      vapply(nested_ext$y_exprs,
              function(s) if (nzchar(s)) paste0(top_name, s) else "",
              character(1)),
      names(nested_ext$y_exprs)
    )
  }

  heuristics <- list(
    data_shape = .heuristic_data_shape(obj),
    outcome_family = .heuristic_outcome_family(Xy$y),
    predictor_type = .heuristic_predictor_type(Xy$X),
    splits = .detect_splits(obj),
    time_to_event = .detect_time_to_event(obj),
    missingness_X = if (!is.null(Xy$X)) .compute_missingness(Xy$X) else NULL,
    missingness_y = if (!is.null(Xy$y)) .compute_missingness(Xy$y) else NULL,
    nested_externals = nested_ext
  )

  suggested <- .suggest_exprs(obj, top_name)

  # Extract sample sizes
  n_target <- if (!is.null(Xy$X)) nrow(Xy$X) else NULL
  p <- if (!is.null(Xy$X)) ncol(Xy$X) else NULL

  list(
    path = path,
    format = format,
    top_name = top_name,
    structure = structure_desc,
    heuristics = heuristics,
    suggested_exprs = suggested,
    derived = list(n = n_target, p = p)
  )
}

# -- combined assessment ---------------------------------------------------

.combine_assessment <- function(file_results) {
  # Combine per-file inspection into a single assessment.
  # Heuristic: first file is target, additional files are external sources.

  if (length(file_results) == 0 ||
      !is.null(file_results[[1]]$error)) {
    return(list(
      target_shape = "unknown",
      external_shape = "unknown",
      n_target = NULL,
      n_external_total = NULL,
      p = NULL,
      M = NULL,
      outcome_family = "unknown",
      predictor_type = "unknown",
      has_validation_set = NULL,
      has_time_to_event = FALSE
    ))
  }

  primary <- file_results[[1]]
  external_files <- if (length(file_results) >= 2) {
    file_results[2:length(file_results)]
  } else {
    list()
  }

  target_shape <- primary$heuristics$data_shape$value
  outcome_family <- primary$heuristics$outcome_family$value
  predictor_type <- primary$heuristics$predictor_type$value
  has_validation_set <- primary$heuristics$splits$value
  has_time_to_event <- primary$heuristics$time_to_event$value
  n_target <- primary$derived$n
  p_target <- primary$derived$p

  # If the primary file is a single .rds that ALSO contains external info
  # (e.g. beta.external), we can detect this without separate external files.
  primary_has_internal_external <- FALSE
  if (!is.null(primary$suggested_exprs$external_beta_expr)) {
    primary_has_internal_external <- TRUE
  }

  # Detect nested external cohorts (external1, external2, ... siblings of
  # `target` in the primary file).
  nested_ext <- primary$heuristics$nested_externals
  primary_has_nested_externals <- !is.null(nested_ext) && nested_ext$M > 0L

  # External shape determination
  external_shape <- "unknown"
  n_external_total <- NULL
  M <- NULL

  if (length(external_files) > 0) {
    # Multiple files: derive from external shapes
    n_external_total <- 0L
    M <- 0L
    shapes <- character(length(external_files))
    for (i in seq_along(external_files)) {
      ef <- external_files[[i]]
      if (!is.null(ef$error)) next
      shapes[i] <- ef$heuristics$data_shape$value
      if (!is.null(ef$derived$n)) {
        n_external_total <- n_external_total + ef$derived$n
      }
      M <- M + 1L
    }
    unique_shapes <- unique(shapes[nzchar(shapes)])
    if (length(unique_shapes) == 1) {
      external_shape <- unique_shapes[1]
    } else {
      external_shape <- "mixed"
    }
    if (n_external_total == 0L) n_external_total <- NULL
    if (M == 0L) M <- NULL
  } else if (primary_has_nested_externals) {
    # Single .rds file with sibling-named external cohorts inside.
    # This is the Data_BRIERfull case: target / external1 / external2 / ...
    external_shape <- nested_ext$shape
    M <- nested_ext$M
    n_external_total <- nested_ext$n_total
  } else if (primary_has_internal_external) {
    external_shape <- "coefficients"
    M <- NULL  # Could potentially derive from ncol(beta.external)
                # but that needs another R round-trip
  } else {
    external_shape <- "none"
  }

  list(
    target_shape = target_shape,
    external_shape = external_shape,
    n_target = n_target,
    n_external_total = n_external_total,
    p = p_target,
    M = M,
    outcome_family = outcome_family,
    predictor_type = predictor_type,
    has_validation_set = has_validation_set,
    has_time_to_event = has_time_to_event
  )
}

# -- main ------------------------------------------------------------------

args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$data_paths) || length(inp$data_paths) == 0) {
    stop("data_paths is required (one or more file paths)", call. = FALSE)
  }
  paths <- as.character(unlist(inp$data_paths))
  csv_options <- inp$csv_options

  file_results <- lapply(paths, .inspect_one_file, csv_options = csv_options)
  combined <- .combine_assessment(file_results)

  # Cache the inspection so start_analysis can reference it.
  inspection_id <- .generate_inspection_id()
  inspection_path <- file.path(.cache_root_inspections(),
                                paste0(inspection_id, ".rds"))
  cache_payload <- list(
    files = file_results,
    combined = combined,
    paths = paths,
    timestamp = Sys.time()
  )
  saveRDS(cache_payload, inspection_path)

  list(
    status = "ok",
    inspection_id = inspection_id,
    inspection_path = inspection_path,
    files = file_results,
    combined_assessment = combined
  )
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "inspect_user_data.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
