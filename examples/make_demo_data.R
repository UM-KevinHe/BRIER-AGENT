#!/usr/bin/env Rscript
# Generate the bundled demo datasets from BRIER's own simulated example data.
#
# BRIER ships Data_BRIERi / Data_BRIERs (simulated, generic predictors named X1..X200, no
# genome coordinates). This script writes them out as the flat, gzipped, tab-separated
# files the agent consumes by role, into examples/data/. Re-run to regenerate.
#
#   Rscript examples/make_demo_data.R
#
# The predictors are generic features (not SNPs with CHR/BP), so the agent takes its
# non-genotype path: it aligns the external to the target by feature NAME, and for the
# summary shape builds the dependence matrix as a plain correlation (no LD blocks).

suppressMessages(library(BRIER))

root <- file.path("examples", "data")
ind_dir <- file.path(root, "individual")
sum_dir <- file.path(root, "summary")
dir.create(ind_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(sum_dir, recursive = TRUE, showWarnings = FALSE)

.gz_write <- function(df, path) {
  con <- gzfile(path, "w")
  on.exit(close(con))
  write.table(df, con, sep = "\t", quote = FALSE, row.names = FALSE)
}

# A feature matrix -> id column + one column per feature (X1..X200). The id is a
# non-numeric string, so the agent treats it as the sample id, not a predictor.
write_X <- function(X, path, prefix) {
  df <- data.frame(id = paste0(prefix, "_", seq_len(nrow(X))),
                   X, check.names = FALSE)
  .gz_write(df, path)
}
# The outcome -> id column (matching the X ids of the same split) + trait.
write_y <- function(y, path, prefix) {
  .gz_write(data.frame(id = paste0(prefix, "_", seq_along(y)), trait = y), path)
}
# An external coefficient vector -> varnames + coef (BRIERs convention: no intercept row).
write_coef <- function(names_, coef, path) {
  .gz_write(data.frame(varnames = names_, coef = coef), path)
}
# No predictor-map ("snp_info") writer: these are non-genetic predictors, so there is no
# variant map to supply. prep_auto derives the predictor panel from the data itself, and
# merges several external models by feature name (genotypes would need coordinates for
# allele orientation, but a gene or protein has no allele to orient).

# =========================== individual-level (BRIERi) ===========================
data(Data_BRIERi)
di <- Data_BRIERi
write_X(di$target$train$X,      file.path(ind_dir, "X_train.tsv.gz"),  "train")
write_y(di$target$train$y,      file.path(ind_dir, "y_train.tsv.gz"),  "train")
write_X(di$target$validation$X, file.path(ind_dir, "X_val.tsv.gz"),    "val")
write_y(di$target$validation$y, file.path(ind_dir, "y_val.tsv.gz"),    "val")
write_X(di$target$testing$X,    file.path(ind_dir, "X_test.tsv.gz"),   "test")
write_y(di$target$testing$y,    file.path(ind_dir, "y_test.tsv.gz"),   "test")
# beta.external for BRIERi carries an intercept row; the external COEF file drops it (the
# fitter re-adds the intercept). Export all three external models: one primary file plus
# numbered files for the multi-source demo.
bi <- di$beta.external
keep <- rownames(bi) != "Intercept"
vn <- rownames(bi)[keep]
write_coef(vn, bi[keep, 1], file.path(ind_dir, "external_model.tsv.gz"))
for (k in 1:3) write_coef(vn, bi[keep, k], file.path(ind_dir, sprintf("external_model%d.tsv.gz", k)))

# ============================ summary-statistics (BRIERs) =========================
data(Data_BRIERs)
ds <- Data_BRIERs
# GWAS summary statistics: the agent's summary path reads a per-variant table with a
# correlation column. Rename "variable" -> "varnames" so the variant id is unambiguous.
ss <- ds$target$train$sumstats
names(ss)[names(ss) == "variable"] <- "varnames"
.gz_write(ss, file.path(sum_dir, "sumstats.tsv.gz"))
# A reference panel to build the dependence (LD) matrix from, when none is shipped.
write_X(ds$target$train$X, file.path(sum_dir, "reference_panel.tsv.gz"), "ref")
# External coefficients (p rows, no intercept for BRIERs).
bs <- ds$beta.external
write_coef(rownames(bs), bs[, 1], file.path(sum_dir, "external_model.tsv.gz"))
# No snp_info here on purpose: these are non-genetic predictors, so there is no variant
# map to supply. prep_auto derives the predictor panel from the LD's names (built from the
# reference panel). snp_info is only needed when predictors are genotypes (for coordinate
# alignment and allele flips) or when aligning several external models (the individual demo).
# Held-out individual data, for validation-set selection and test-set evaluation.
write_X(ds$target$validation$X, file.path(sum_dir, "X_val.tsv.gz"),  "val")
write_y(ds$target$validation$y, file.path(sum_dir, "y_val.tsv.gz"),  "val")
write_X(ds$target$testing$X,    file.path(sum_dir, "X_test.tsv.gz"), "test")
write_y(ds$target$testing$y,    file.path(sum_dir, "y_test.tsv.gz"), "test")

cat("Wrote demo data under", root, "\n")
cat("  individual/:", paste(list.files(ind_dir), collapse = ", "), "\n")
cat("  summary/:   ", paste(list.files(sum_dir), collapse = ", "), "\n")
