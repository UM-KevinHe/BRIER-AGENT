#!/usr/bin/env Rscript
# get_ldb.R - return Berisa-Pickrell LD block coordinates for a given
# ancestry + genome build.
#
# Called by mcp/server.py as:
#   Rscript get_ldb.R <input.json> <output.json>
#
# BRIER ships pre-computed LD block coordinates from Berisa and Pickrell (2016)
# for three ancestries (AFR, EAS, EUR) and two builds (hg19, hg38), as
# tab-separated BED files in inst/extdata. getLDB() returns the file path;
# the actual coordinates are loaded by calLD() when building an LD matrix.
#
# This dispatcher returns the path PLUS a small summary (number of blocks,
# per-chromosome counts) so the user can inspect what they got without
# reading the file themselves.
#
# input.json: {
#   ancestry: "AFR" | "EAS" | "EUR",   # required
#   build:    "hg19" | "hg38"           # required
# }
#
# output.json: {
#   status: "ok",
#   ancestry, build,
#   bed_path: "/path/to/Berisa.EUR.hg38.bed",
#   n_blocks: integer,
#   n_chromosomes: integer,
#   chr_format: "chr-prefixed" | "numeric",
#   _notice_chr_prefix_mismatch: "..."  # always-on warning about the
#                                        # "chr" prefix gotcha
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
})


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$ancestry) || !nzchar(inp$ancestry)) {
    stop("ancestry is required (one of AFR, EAS, EUR)", call. = FALSE)
  }
  if (is.null(inp$build) || !nzchar(inp$build)) {
    stop("build is required (one of hg19, hg38)", call. = FALSE)
  }

  bed_path <- BRIER::getLDB(ancestry = inp$ancestry, build = inp$build)

  # Read the BED file's header + a sample row to report structure.
  bed <- utils::read.table(bed_path, header = TRUE, sep = "\t",
                            stringsAsFactors = FALSE)

  # Determine chr column format. BRIER ships "chr1" style strings; users
  # commonly have numeric CHR columns in their sumstats, and the join
  # silently fails.
  chr_values <- bed[[1]]
  chr_prefixed <- all(grepl("^chr", chr_values))
  unique_chrs <- unique(chr_values)

  out <- list(
    status = "ok",
    ancestry = inp$ancestry,
    build = inp$build,
    bed_path = bed_path,
    n_blocks = nrow(bed),
    n_chromosomes = length(unique_chrs),
    chr_format = if (chr_prefixed) "chr-prefixed" else "numeric"
  )

  if (chr_prefixed) {
    out$`_notice_chr_prefix_mismatch` <- paste(
      "The LD block BED file uses chr-prefixed chromosome labels (e.g., 'chr1').",
      "Many sumstats / SNP info tables use numeric CHR columns (1, 2, ...).",
      "If you pass this LDB to calLD() against numeric-CHR sumstats, the",
      "join will silently fail (no blocks will match). Convert with:",
      "as.numeric(sub('chr', '', LDB[, 1])) before calling calLD()."
    )
  }

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "get_ldb.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
