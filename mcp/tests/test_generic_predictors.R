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

# A SECOND pretrained external, over a partly-overlapping gene set, for the multi-external
# name-merge check. Generic predictors have no allele to orient, so several externals merge
# by NAME with no variant map at all.
set.seed(7005)
ext_genes2 <- c(genes[50:120], sprintf("ENSG8%04d", 1:10))
ext_coef2 <- c(rep(0.3, 6), rep(0, 65), runif(10))
write.table(data.frame(varnames = ext_genes2, coef = ext_coef2),
            file.path(D, "external_model2.txt"), sep = "\t", quote = FALSE, row.names = FALSE)

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

cat("\n[6] snp_info is OPTIONAL for a non-genetic predictor: the panel is DERIVED\n")
# A gene-expression cohort has no variant map, so requiring one is genotype vocabulary
# leaking into the generic path. With snp_info omitted the panel comes from the data itself:
# the training matrix's columns (brier_i) / the LD's names (brier_s).
r5 <- run(list(
  shape = "brier_i", data_dir = D, out_dir = OUT, persist = TRUE, standardize = TRUE,
  roles = list(target_X_train = "target_X_training.txt",
               target_y_train = "target_pheno_training.txt",
               external_coef = "external_model.txt",
               target_X_val = "target_X_validation.txt",
               target_y_val = "target_pheno_validation.txt")
), "gi_nosnp")
ok(identical(r5$status, "ok"),
   paste("brier_i prepares with NO snp_info:", substr(paste(r5$message, collapse=" "), 1, 80)))
rep5 <- paste(r5$report, collapse = " | ")
ok(grepl("snp_info omitted; derived the predictor panel from the training matrix", rep5),
   "the panel is derived from the training matrix's column names, and it is REPORTED")
if (identical(r5$status, "ok")) {
  prep5 <- readRDS(r5$prepared_path)
  ok(ncol(prep5$X) == p && nrow(prep5$beta_external) == p + 1L,
     "the derived-panel object is identical in shape to the snp_info one (p genes, p+1 beta)")
}
r6 <- run(list(
  shape = "brier_s", data_dir = D, out_dir = OUT, persist = TRUE, standardize = TRUE,
  roles = list(target_sumstats = "target_sumstats.txt",
               target_ld_panel = "reference_panel.txt",
               external_coef = "external_model.txt",
               target_X_val = "target_X_validation.txt",
               target_y_val = "target_pheno_validation.txt")
), "gs_nosnp")
ok(identical(r6$status, "ok"),
   paste("brier_s prepares with NO snp_info:", substr(paste(r6$message, collapse=" "), 1, 80)))
ok(grepl("snp_info omitted; derived the predictor panel from the LD matrix", paste(r6$report, collapse=" | ")),
   "the summary panel is derived from the LD matrix's names, and it is REPORTED")

cat("\n[7] MULTIPLE externals merge by NAME for a generic predictor, no snp_info, no coords\n")
# For genotypes, several externals need coordinates (allele harmonization across them). A
# gene has no allele, so name identity is enough: two externals become a p x 2 beta with no
# variant map. A variant an external does not cover contributes 0 to its column.
r7 <- run(list(
  shape = "brier_i", data_dir = D, out_dir = OUT, persist = TRUE, standardize = TRUE,
  roles = list(target_X_train = "target_X_training.txt",
               target_y_train = "target_pheno_training.txt",
               external_coef_1 = "external_model.txt",
               external_coef_2 = "external_model2.txt",
               target_X_val = "target_X_validation.txt",
               target_y_val = "target_pheno_validation.txt")
), "gi_multi")
ok(identical(r7$status, "ok"),
   paste("brier_i merges TWO generic externals with no snp_info:",
         substr(paste(r7$message, collapse=" "), 1, 70)))
if (identical(r7$status, "ok")) {
  prep7 <- readRDS(r7$prepared_path)
  ok(nrow(prep7$beta_external) == p + 1L && ncol(prep7$beta_external) == 2L,
     sprintf("beta.external is (p+1) x 2 (got %dx%d): one column per external, merged by name",
             nrow(prep7$beta_external), ncol(prep7$beta_external)))
  fit7 <- tryCatch(
    BRIER::BRIERi(X = prep7$X, y = prep7$y, beta.external = prep7$beta_external,
                  eta.list = c(0, 1)),
    error = function(e) e)
  ok(!inherits(fit7, "error"), "the two-external generic object FITS in real BRIERi")
}

cat("\n[8] a mislabelled predictor_type=genotype (no coordinates) still name-merges\n")
# A real 7B slip: it set predictor_type=genotype on coordinate-free data, taking the
# NON-generic varnames-fallback path. That path is reached PRECISELY because no
# coordinates exist, so multiple externals must merge by name there too (mergeExternals
# is impossible without coordinates). It used to error "provide coordinates", which sent
# the model chasing align_method=coordinate in a loop.
r8 <- run(list(
  shape = "brier_i", data_dir = D, out_dir = OUT, persist = FALSE, standardize = TRUE,
  predictor_type = "genotype",
  roles = list(target_X_train = "target_X_training.txt",
               target_y_train = "target_pheno_training.txt",
               external_coef_1 = "external_model.txt",
               external_coef_2 = "external_model2.txt")
), "gi_geno_multi")
ok(identical(r8$status, "ok"),
   paste("predictor_type=genotype + 2 externals + no coordinates no longer errors:",
         substr(paste(r8$message, collapse=" "), 1, 60)))
ok(grepl("merged by name", paste(r8$report, collapse = " | ")),
   "the report says the externals were merged by name (no coordinates available)")

cat("\n", strrep("-", 62), "\n", sep = "")
if (.fails == 0L) {
  cat(sprintf("generic predictors: ALL %d CHECKS PASS\n", .checks))
  cat("  BRIER's own preprocessors CANNOT run this case (they hard-require CHR/BP/REF/ALT).\n")
} else {
  cat(sprintf("generic predictors: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
