#!/usr/bin/env Rscript
# =============================================================================
# DIFFERENTIAL TEST: our aligner vs BRIER::preprocessI / preprocessS.
#
# This is the safety net for the single riskiest change in the redesign. Allele orientation
# is the subtlest code in the pipeline and its failures are SILENT: a wrongly-flipped panel
# still matches, still aligns, still fits, and every coefficient just has the wrong sign.
#
# preprocessI/preprocessS are VERIFIED CORRECT (900 target-sumstats flips and 2000
# external-coefficient flips, max|dev| = 0). We are replacing them because they hard-require
# CHR/BP/REF/ALT and therefore cannot express the non-genotype path. But we keep them as a
# VERIFICATION ORACLE: this asserts BITWISE agreement on genotype data, so their correctness
# is inherited as a TEST rather than as a runtime dependency.
#
# Uses the real benchmark data (skips if absent).
#
#   Rscript mcp/tests/test_aligner_differential.R
# =============================================================================

.fails <- 0L
.checks <- 0L
ok <- function(cond, what) {
  .checks <<- .checks + 1L
  if (!isTRUE(cond)) { .fails <<- .fails + 1L; cat("  FAIL:", what, "\n") }
  else cat("  ok:", what, "\n")
}

D <- "/Volumes/zrayw/Dissertation/BRIER_software/example_data/height_data2"
if (!dir.exists(D) || !requireNamespace("BRIER", quietly = TRUE)) {
  cat("SKIP: benchmark data or BRIER not available\n"); quit(status = 0L)
}
suppressWarnings(suppressMessages({ library(data.table); library(BRIER) }))

# Load prep_auto's DEFINITIONS only (everything above the dispatch), as the helper tests do.
src <- readLines("mcp/r_scripts/prep_auto.R", warn = FALSE)
cut <- grep("^# ---- dispatch", src)[1]
defs <- src[seq_len(cut - 1L)]
defs <- defs[!grepl("^source\\(", defs)]
eval(parse(text = paste(defs, collapse = "\n")), envir = globalenv())

snp  <- as.data.frame(fread(file.path(D, "height_AFR_SNP_info.txt.gz")))
gwas <- as.data.frame(fread(file.path(D, "height_AFR_GWAS_training.txt.gz")))
m1   <- as.data.frame(fread(file.path(D, "height_EUR_model1.txt.gz")))

# The external as prep_auto builds it: coordinates + a coef1 column.
ext <- data.frame(varnames = snp$varnames, CHR = snp$CHR, BP = snp$BP,
                  REF = snp$REF, ALT = snp$ALT,
                  coef1 = m1$coef[match(snp$varnames, m1$varnames)],
                  stringsAsFactors = FALSE)

cat("\n[1] preprocessI (no target sumstats): the external aligned to the target map\n")
pi_ <- BRIER::preprocessI(target.info = snp, external.ss = ext,
                          external.coef.cols = "coef1", verbose = FALSE)
mine <- .align_predictors(ref = snp, ext_tab = ext, predictor_type = "genotype")
ok(identical(as.integer(pi_$target.info.keep), as.integer(mine$keep)),
   "preprocessI: the surviving keep-indices are IDENTICAL")
b_theirs <- as.numeric(as.matrix(pi_$external.ss[, "coef1", drop = FALSE]))
b_mine   <- as.numeric(mine$beta[, 1])
ok(length(b_theirs) == length(b_mine) && max(abs(b_theirs - b_mine)) == 0,
   sprintf("preprocessI: beta.external is BITWISE identical (max|dev| = %g)",
           if (length(b_theirs) == length(b_mine)) max(abs(b_theirs - b_mine)) else NA))

cat("\n[2] preprocessS with a corr column (target.ind = 'corr')\n")
ps <- BRIER::preprocessS(target.ss = gwas, target.ind = "corr", target.ld = snp,
                         target.ss.cols = .ss_col_map(gwas),
                         external.ss = ext, external.coef.cols = "coef1", verbose = FALSE)
mine <- .align_predictors(ref = snp, target_ss = gwas, target_ind = "corr",
                          ext_tab = ext, predictor_type = "genotype")
ok(identical(as.integer(ps$target.ld.keep), as.integer(mine$keep)),
   "preprocessS/corr: the surviving keep-indices are IDENTICAL")
ok(max(abs(as.numeric(ps$target.ss$corr) - as.numeric(mine$sumstats$corr))) == 0,
   "preprocessS/corr: the aligned corr is BITWISE identical")
ok(max(abs(as.numeric(as.matrix(ps$external.ss[, "coef1"])) -
           as.numeric(mine$beta[, 1]))) == 0,
   "preprocessS/corr: beta.external is BITWISE identical")

cat("\n[3] preprocessS DERIVING corr from p/N/sign(beta) (target.ind = 'gwas')\n")
gwas_nocorr <- gwas[, setdiff(colnames(gwas), c("corr", "STAT"))]
ps <- BRIER::preprocessS(target.ss = gwas_nocorr, target.ind = "gwas", target.ld = snp,
                         target.ss.cols = .ss_col_map(gwas_nocorr),
                         external.ss = ext, external.coef.cols = "coef1", verbose = FALSE)
mine <- .align_predictors(ref = snp, target_ss = gwas_nocorr, target_ind = "gwas",
                          ext_tab = ext, predictor_type = "genotype")
ok(max(abs(as.numeric(ps$target.ss$corr) - as.numeric(mine$sumstats$corr))) == 0,
   "preprocessS/gwas: the DERIVED corr (p2cor) is BITWISE identical")

cat("\n[4] ALLELE FLIPS: the thing that must not silently break\n")
# Flip a seeded 20% of the EXTERNAL's alleles and negate its coefficients. Correcting them
# must reproduce the ORIGINAL model exactly, which is independent ground truth.
set.seed(4242)
fl <- sort(sample.int(nrow(ext), 2000))
ext_f <- ext
ext_f$REF[fl] <- ext$ALT[fl]; ext_f$ALT[fl] <- ext$REF[fl]
ext_f$coef1[fl] <- -ext$coef1[fl]
mine <- .align_predictors(ref = snp, ext_tab = ext_f, predictor_type = "genotype")
ok(max(abs(as.numeric(mine$beta[, 1]) - ext$coef1[mine$keep])) == 0,
   "external flips: correcting them reproduces the ORIGINAL coefficients exactly")
ok(mine$n_flipped_external == 2000L,
   sprintf("external flips: all 2000 are DETECTED and counted (got %d)",
           mine$n_flipped_external))
pi_f <- BRIER::preprocessI(target.info = snp, external.ss = ext_f,
                           external.coef.cols = "coef1", verbose = FALSE)
ok(max(abs(as.numeric(as.matrix(pi_f$external.ss[, "coef1"])) -
           as.numeric(mine$beta[, 1]))) == 0,
   "external flips: BITWISE identical to preprocessI's correction")

# Now flip the TARGET's sumstats against the LD, which is the T3_briers-flip-align case.
set.seed(909)
fl2 <- sort(sample.int(nrow(gwas), 900))
g_f <- gwas
g_f$REF[fl2] <- gwas$ALT[fl2]; g_f$ALT[fl2] <- gwas$REF[fl2]
g_f$corr[fl2] <- -gwas$corr[fl2]
mine <- .align_predictors(ref = snp, target_ss = g_f, target_ind = "corr",
                          predictor_type = "genotype")
ok(max(abs(as.numeric(mine$sumstats$corr) - gwas$corr[mine$keep])) == 0,
   "target flips: correcting them reproduces the UNCORRUPTED corr exactly")
ok(mine$n_flipped_target == 900L,
   sprintf("target flips: all 900 are DETECTED and counted (got %d)",
           mine$n_flipped_target))

cat("\n[5] IMPUTE 0, do NOT intersect (the convention the T3 keys pin)\n")
ext_part <- ext[1:6000, ]      # the external covers only 6000 of the 10000 target variants
mine <- .align_predictors(ref = snp, ext_tab = ext_part, predictor_type = "genotype")
ok(length(mine$keep) == nrow(snp),
   sprintf("impute-0: the FULL target panel survives (%d), not the 6000 intersection",
           length(mine$keep)))
ok(all(mine$beta[6001:10000, 1] == 0),
   "impute-0: the 4000 target variants the external lacks carry coefficient EXACTLY 0")
ok(sum(mine$beta[, 1] != 0) == sum(ext_part$coef1 != 0),
   "impute-0: every nonzero coefficient the external DID carry is preserved")

cat("\n[6] GENERIC predictors: no alleles, no flips, name identity only\n")
gmap <- data.frame(varnames = paste0("gene", 1:6), stringsAsFactors = FALSE)
gext <- data.frame(varnames = c("gene3", "gene1", "gene9"), coef1 = c(0.3, 0.1, 9.9),
                   stringsAsFactors = FALSE)
mine <- .align_predictors(ref = gmap, ext_tab = gext, predictor_type = "generic")
ok(length(mine$keep) == 6L, "generic: the full target panel survives")
ok(identical(as.numeric(mine$beta[, 1]), c(0.1, 0, 0.3, 0, 0, 0)),
   "generic: matched BY NAME, unmatched imputed 0, external-only (gene9) dropped")
ok(mine$n_flipped_external == 0L, "generic: no flips exist (orientation is undefined)")
gdup <- data.frame(varnames = c("g1", "g1", "g2"), stringsAsFactors = FALSE)
q <- .qc_variants(gdup, "generic")
ok(sum(q$keep) == 1L && q$n_multiallelic == 2L,
   "generic QC: duplicate NAMES are dropped (the identity is the name)")

cat("\n", strrep("-", 62), "\n", sep = "")
if (.fails == 0L) {
  cat(sprintf("aligner differential: ALL %d CHECKS PASS\n", .checks))
  cat("  our aligner is BITWISE identical to preprocessI/preprocessS on genotype data,\n")
  cat("  and additionally supports the generic (non-genotype) path they cannot express.\n")
} else {
  cat(sprintf("aligner differential: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
