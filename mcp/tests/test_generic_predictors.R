#!/usr/bin/env Rscript
# =============================================================================
# THE NON-GENOTYPE PATH, end to end. This is the whole reason the preprocessors were
# replaced (PREP_AUTO_DESIGN.md 1.4).
#
# BRIER is general: a predictor can be a SNP, a gene's expression level, a protein
# abundance. Its PREPROCESSORS are not: preprocessI/preprocessS hard-require CHR/BP/REF/ALT,
# so the non-genotype path could not even be EXPRESSED on top of them. It has never had a
# fixture, because until now it could not run.
#
# The generic path is the genotype path with the ORIENTATION MACHINERY REMOVED, and nothing
# else:
#              | genotype                     | generic
#   identity   | CHR:BP plus the allele pair  | a NAME
#   orientation| a flip is meaningful         | UNDEFINED. There is nothing to flip.
#   qc         | duplicate CHR:BP, palindromic| duplicate names
#   ld         | Berisa blocks -> block sparse| plain correlation
#
# This drives the REAL prep_auto (both shapes) on a synthetic gene-expression cohort:
# no coordinates, no alleles, no strand, no LD blocks.
#
#   Rscript mcp/tests/test_generic_predictors.R
# =============================================================================

.fails <- 0L
.checks <- 0L
ok <- function(cond, what) {
  .checks <<- .checks + 1L
  if (!isTRUE(cond)) { .fails <<- .fails + 1L; cat("  FAIL:", what, "\n") }
  else cat("  ok:", what, "\n")
}

if (!requireNamespace("BRIER", quietly = TRUE)) {
  cat("SKIP: BRIER not available\n"); quit(status = 0L)
}
suppressWarnings(suppressMessages({ library(jsonlite); library(Matrix) }))

PREP <- normalizePath("mcp/r_scripts/prep_auto.R")
D <- file.path(tempdir(), "generic_case")
dir.create(D, showWarnings = FALSE, recursive = TRUE)
OUT <- file.path(tempdir(), "generic_out")
dir.create(OUT, showWarnings = FALSE, recursive = TRUE)

# ---- a gene-expression cohort -----------------------------------------------
# 400 target samples, 120 genes. NO CHR, NO BP, NO REF/ALT: there is nothing to orient.
set.seed(7001)
n <- 400L; p <- 120L
genes <- sprintf("ENSG%05d", seq_len(p))
Xt <- matrix(rnorm(n * p), n, p, dimnames = list(NULL, genes))
beta_true <- c(rep(0.5, 8), rep(0, p - 8))
yt <- as.numeric(Xt %*% beta_true + rnorm(n))

wr <- function(m, f, idcol = "GEO_accession") {
  df <- data.frame(id = sprintf("S%04d", seq_len(nrow(m))), m, check.names = FALSE)
  colnames(df)[1] <- idcol
  write.table(df, file.path(D, f), sep = "\t", quote = FALSE, row.names = FALSE)
}
wr(Xt, "target_X_training.txt")
write.table(data.frame(GEO_accession = sprintf("GSM%04d", seq_len(n)), pheno = yt),
            file.path(D, "target_pheno_training.txt"),
            sep = "\t", quote = FALSE, row.names = FALSE)

# a validation split, on the same 120 genes
set.seed(7002)
nv <- 200L
Xv <- matrix(rnorm(nv * p), nv, p, dimnames = list(NULL, genes))
yv <- as.numeric(Xv %*% beta_true + rnorm(nv))
wr(Xv, "target_X_validation.txt")
write.table(data.frame(GEO_accession = sprintf("GSM%04d", seq_len(nv)), pheno = yv),
            file.path(D, "target_pheno_validation.txt"),
            sep = "\t", quote = FALSE, row.names = FALSE)

# The "variant" map: names ONLY. This is what makes it a generic predictor.
write.table(data.frame(varnames = genes),
            file.path(D, "gene_info.txt"), sep = "\t", quote = FALSE, row.names = FALSE)

# A PRETRAINED external model over 100 of the 120 genes, plus 15 genes the target
# does not have at all (these must be DROPPED, not imputed into the target).
set.seed(7003)
ext_genes <- c(genes[1:100], sprintf("ENSG9%04d", 1:15))
ext_coef <- c(rep(0.4, 8), rep(0, 92), runif(15))
write.table(data.frame(varnames = ext_genes, coef = ext_coef),
            file.path(D, "external_model.txt"), sep = "\t", quote = FALSE, row.names = FALSE)

run <- function(payload, tag) {
  ip <- file.path(OUT, paste0(tag, "_in.json"))
  op <- file.path(OUT, paste0(tag, "_out.json"))
  write(toJSON(payload, auto_unbox = TRUE, null = "null"), ip)
  system2("Rscript", c("--no-save", "--no-restore", "--no-init-file", PREP, ip, op),
          stdout = FALSE, stderr = FALSE)
  if (!file.exists(op)) return(list(status = "no output"))
  fromJSON(op, simplifyVector = TRUE)
}

# =============================================================================
cat("\n[1] brier_i on GENE EXPRESSION: no coordinates, no alleles, no strand\n")
r <- run(list(
  shape = "brier_i", data_dir = D, out_dir = OUT, persist = TRUE, standardize = TRUE,
  roles = list(target_X_train = "target_X_training.txt",
               target_y_train = "target_pheno_training.txt",
               snp_info = "gene_info.txt",
               external_coef = "external_model.txt",
               target_X_val = "target_X_validation.txt",
               target_y_val = "target_pheno_validation.txt")
), "gi")
ok(identical(r$status, "ok"),
   paste("brier_i prepares a gene-expression cohort:", substr(paste(r$message, collapse=" "), 1, 90)))
rep_txt <- paste(r$report, collapse = " | ")
ok(grepl("predictor_type = generic", rep_txt),
   "predictor_type is DETECTED as generic (the map has no CHR/BP, so it cannot be a genome)")

if (identical(r$status, "ok")) {
  prep <- readRDS(r$prepared_path)
  ok(ncol(prep$X) == p,
     sprintf("the FULL target panel survives: %d genes (align to the target, do not intersect)",
             ncol(prep$X)))
  ok(nrow(prep$beta_external) == p + 1L,
     "beta.external carries the BRIERi intercept row (p + 1), the same as for genotypes")
  b <- as.numeric(prep$beta_external)[-1]
  ok(all(b[101:120] == 0),
     "the 20 genes the external does not cover are imputed to coefficient EXACTLY 0")
  ok(sum(b != 0) == 8L,
     sprintf("the external's 8 nonzero coefficients survive (got %d)", sum(b != 0)))
  ok(!any(grepl("STRAND|palindrom|flip", rep_txt, ignore.case = TRUE)),
     "NO flip or strand machinery is reported: orientation is undefined for a gene")
  # The 15 external-only genes must be gone, not appended to the target's panel.
  ok(!any(grepl("^ENSG9", colnames(prep$X))),
     "the 15 external-only genes are DROPPED (the target has no data for them)")

  cat("\n[2] the prepared object FITS in real BRIERi\n")
  fit <- tryCatch(
    BRIER::BRIERi(X = prep$X, y = prep$y, beta.external = prep$beta_external,
                  eta.list = c(0, 1)),
    error = function(e) e)
  ok(!inherits(fit, "error"),
     paste("BRIERi fits gene-expression predictors:",
           if (inherits(fit, "error")) conditionMessage(fit) else "ok"))
}

# =============================================================================
cat("\n[3] brier_s on GENE EXPRESSION: the LD is a PLAIN CORRELATION\n")
# A gene has no genome position, so there are no Berisa LD blocks to assign it to. The
# "LD" is simply the predictors' correlation matrix, built from a reference panel.
# The genotype-panel guard must NOT fire here (it demands ancestry + build), because
# there is no ancestry and no build for a transcriptome.
set.seed(7004)
Xr <- matrix(rnorm(500 * p), 500, p, dimnames = list(NULL, genes))
wr(Xr, "reference_panel.txt")

# summary statistics over the same genes: a marginal correlation per gene
corr <- as.numeric(cor(Xt, yt))
write.table(data.frame(varnames = genes, corr = corr, N = n),
            file.path(D, "target_sumstats.txt"), sep = "\t", quote = FALSE,
            row.names = FALSE)

r2 <- run(list(
  shape = "brier_s", data_dir = D, out_dir = OUT, persist = TRUE, standardize = TRUE,
  roles = list(target_sumstats = "target_sumstats.txt",
               snp_info = "gene_info.txt",
               target_ld_panel = "reference_panel.txt",
               external_coef = "external_model.txt")
), "gs")
ok(identical(r2$status, "ok"),
   paste("brier_s prepares it with NO ancestry and NO build:",
         substr(paste(r2$message, collapse=" "), 1, 90)))
rep2 <- paste(r2$report, collapse = " | ")
ok(grepl("predictor_type = generic", rep2), "brier_s also detects generic")
ok(grepl("plain correlation", rep2),
   "the LD is built as a PLAIN CORRELATION (Berisa blocks are genome-specific)")

if (identical(r2$status, "ok")) {
  prep2 <- readRDS(r2$prepared_path)
  ok(nrow(prep2$XtX) == p && ncol(prep2$XtX) == p,
     sprintf("the LD is %dx%d over the gene panel", nrow(prep2$XtX), ncol(prep2$XtX)))
  ok(inherits(prep2$XtX, "sparseMatrix"), "the LD is sparse (BRIERs requires it)")
  ok(nrow(prep2$beta_external) == p,
     "beta.external has NO intercept row for BRIERs (p rows), the same as for genotypes")
  b2 <- as.numeric(prep2$beta_external)
  ok(all(b2[101:120] == 0), "the uncovered genes are imputed to 0 here too")

  cat("\n[4] the prepared object FITS in real BRIERs\n")
  fit2 <- tryCatch(
    BRIER::BRIERs(sumstats = prep2$sumstats, XtX = prep2$XtX,
                  beta.external = prep2$beta_external, eta.list = c(0, 1)),
    error = function(e) e)
  ok(!inherits(fit2, "error"),
     paste("BRIERs fits gene-expression predictors:",
           if (inherits(fit2, "error")) conditionMessage(fit2) else "ok"))
}

# =============================================================================
cat("\n[5] an explicit predictor_type OVERRIDES the detection\n")
# The detection is on the MAP, so a caller can still force the genotype path (or, more
# usefully, force `generic` for a map whose coordinates are not genomic).

r3 <- run(list(
  shape = "brier_i", data_dir = D, out_dir = OUT, persist = FALSE, standardize = TRUE,
  predictor_type = "gene_expression",
  roles = list(target_X_train = "target_X_training.txt",
               target_y_train = "target_pheno_training.txt",
               snp_info = "gene_info.txt", external_coef = "external_model.txt")
), "gx")
ok(identical(r3$status, "ok") &&
   grepl("predictor_type = generic", paste(r3$report, collapse = " | ")),
   "the alias 'gene_expression' resolves to generic")
r4 <- run(list(
  shape = "brier_i", data_dir = D, out_dir = OUT, persist = FALSE, standardize = TRUE,
  predictor_type = "nonsense",
  roles = list(target_X_train = "target_X_training.txt",
               target_y_train = "target_pheno_training.txt",
               snp_info = "gene_info.txt", external_coef = "external_model.txt")
), "gbad")
ok(identical(r4$status, "error") && grepl("predictor_type", r4$message),
   "an unknown predictor_type is REFUSED with a named error, not silently defaulted")

cat("\n", strrep("-", 62), "\n", sep = "")
if (.fails == 0L) {
  cat(sprintf("generic predictors: ALL %d CHECKS PASS\n", .checks))
  cat("  BRIER's own preprocessors CANNOT run this case (they hard-require CHR/BP/REF/ALT).\n")
} else {
  cat(sprintf("generic predictors: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
