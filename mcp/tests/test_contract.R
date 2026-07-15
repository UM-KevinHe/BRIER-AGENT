#!/usr/bin/env Rscript
# =============================================================================
# THE PREPARED-OBJECT CONTRACT
#
# Every clause here corresponds to a failure that produces a NUMBER rather than an
# error, which is why they need a check at all. Each test therefore comes in a pair:
# the CORRECT object must PASS (or the check is a false positive, which is worse than
# no check: it trains everyone to ignore it), and the CORRUPTED object must FAIL.
#
# The scale clauses in particular were nearly written wrong twice, and both wrong
# versions would have failed a case that is CORRECT:
#   * "X must be standardized" fails T1_brierfull, which legitimately pools raw cohorts.
#   * "standardized means column means ~ 0" fails the pooled cross-ancestry matrix,
#     whose external cohorts have large column means (different allele frequencies)
#     while being correctly standardized by the target's moments.
# Both regressions are pinned below.
# =============================================================================

.dir <- (function() {
  a <- commandArgs(trailingOnly = FALSE)
  f <- grep("^--file=", a, value = TRUE)
  if (length(f)) dirname(normalizePath(sub("^--file=", "", f[1]))) else getwd()
})()
source(file.path(.dir, "..", "r_scripts", "_common.R"))
suppressPackageStartupMessages(library(Matrix))

pass <- 0L
fail <- 0L
ok <- function(cond, what) {
  if (isTRUE(cond)) {
    pass <<- pass + 1L
  } else {
    fail <<- fail + 1L
    cat("  FAIL:", what, "\n")
  }
}
# A violation vector must mention the clause tag, so a failure names WHICH rule broke.
has <- function(v, tag) any(grepl(tag, v, fixed = TRUE))

set.seed(11)


# ---------------------------------------------------------------- fixtures
n <- 60L
p <- 12L
panel <- paste0("rs", seq_len(p), "_A")

raw_geno <- function(n, p) {
  m <- matrix(rbinom(n * p, 2, 0.3), n, p)
  colnames(m) <- panel
  m
}
standardize <- function(m) {
  s <- scale(m)
  s[, ] <- ifelse(is.finite(s), s, 0)
  colnames(s) <- colnames(m)
  as.matrix(s)
}

Xraw <- raw_geno(n, p)
Xstd <- standardize(Xraw)
y_std <- as.vector(scale(rnorm(n)))
y_raw <- 170 + 10 * rnorm(n)          # height, the real raw-scale outcome

beta_i <- matrix(rnorm(p + 1L) * 0.1, ncol = 1L)   # BRIERi: p+1 rows, intercept first
rownames(beta_i) <- c("(Intercept)", panel)

beta_s <- matrix(rnorm(p) * 0.1, ncol = 1L)        # BRIERs: p rows, NO intercept
rownames(beta_s) <- panel

xtx <- Matrix::Matrix(diag(p), sparse = TRUE)
dimnames(xtx) <- list(panel, panel)
ss <- data.frame(varnames = panel, corr = rnorm(p) * 0.01,
                 stringsAsFactors = FALSE)


# ---------------------------------------------------------------- scale regimes
cat("scale regimes\n")
ok(scale_regime_matrix(Xstd) == "standardized", "standardized X is 'standardized'")
ok(scale_regime_matrix(Xraw) == "raw", "raw genotype X is 'raw'")
ok(scale_regime_vector(y_std) == "standardized", "standardized y")
ok(scale_regime_vector(y_raw) == "raw", "raw height y")

# REGRESSION: a val split standardized by the TRAINING moments does NOT have mean 0 /
# sd 1 in itself. The old heuristic demanded |mean| < 0.05 on random columns and so
# false-positived on correctly standardized data. It must read as standardized.
mu <- colMeans(Xraw)
sd_ <- apply(Xraw, 2, sd)
Xval_by_train <- sweep(sweep(raw_geno(n, p), 2, mu, "-"), 2, sd_, "/")
colnames(Xval_by_train) <- panel
ok(scale_regime_matrix(Xval_by_train) == "standardized",
   "a val split standardized by TRAINING moments reads as standardized (no false positive)")

# REGRESSION: the pooled cross-ancestry matrix. Standardize by the TARGET's moments,
# then stack a cohort whose allele frequencies differ: its columns get large means
# while remaining correctly standardized. A mean-based check calls this raw.
ext <- matrix(rbinom(200L * p, 2, 0.65), 200L, p)   # a different allele frequency
pooled <- rbind(Xstd, sweep(sweep(ext, 2, mu, "-"), 2, sd_, "/"))
colnames(pooled) <- panel
ok(median(abs(colMeans(pooled))) > 0.3,
   "the pooled matrix DOES have large column means (the trap is real)")
ok(scale_regime_matrix(pooled) == "standardized",
   "the pooled cross-ancestry matrix still reads as standardized (mean-based check would fail)")


# ---------------------------------------------------------------- brier_i
cat("brier_i clauses\n")
ok(length(validate_fit_inputs("brier_i", X = Xstd, y = y_std,
                              beta_external = beta_i)) == 0,
   "a correct brier_i object passes")

b <- beta_i; rownames(b) <- NULL
ok(has(validate_fit_inputs("brier_i", X = Xstd, y = y_std, beta_external = b),
       "[alignment]"),
   "beta with NO rownames FAILS (alignment cannot be proved; a check that skips is not a check)")

# THE ONE THE COUNT CHECK COULD NEVER CATCH: right length, wrong order. Every
# coefficient lands on a different predictor, and the fit succeeds.
b <- beta_i; rownames(b) <- c("(Intercept)", rev(panel))
ok(has(validate_fit_inputs("brier_i", X = Xstd, y = y_std, beta_external = b),
       "[alignment]"),
   "beta with the RIGHT length but the WRONG ORDER fails")

b <- beta_i[-1, , drop = FALSE]        # BRIERs convention handed to BRIERi
ok(has(validate_fit_inputs("brier_i", X = Xstd, y = y_std, beta_external = b),
       "[shape]"),
   "beta missing the intercept row fails")

b <- beta_i; b[, 1] <- 0
ok(has(validate_fit_inputs("brier_i", X = Xstd, y = y_std, beta_external = b),
       "[degenerate]"),
   "an all-zero external fails")

# The hollow 70/70: ONE coefficient at 5.9e-17. all(cf == 0) misses it.
b <- beta_i; b[, 1] <- 0; b[3, 1] <- 5.9e-17
ok(has(validate_fit_inputs("brier_i", X = Xstd, y = y_std, beta_external = b),
       "[degenerate]"),
   "a numerically-zero external (one coef at 5.9e-17) fails")

b <- beta_i; b[, 1] <- 0; b[3, 1] <- 0.02
ok(!has(validate_fit_inputs("brier_i", X = Xstd, y = y_std, beta_external = b),
        "[degenerate]"),
   "a WEAK but real external (one coef at 0.02) passes: weak is not degenerate")

# The bug that produced a val MSPE of ~28964 for every model.
ok(has(validate_fit_inputs("brier_i", X = Xstd, y = y_raw, beta_external = beta_i),
       "[scale]"),
   "standardized X with a RAW gaussian y fails")

ok(!has(validate_fit_inputs("brier_i", X = Xraw, y = y_raw, beta_external = beta_i),
        "[scale]"),
   "raw X with a raw y does NOT trip the scale clause (self-consistent)")

# A binary outcome must NEVER be standardized, so the clause must not fire on it.
ok(length(validate_fit_inputs("brier_i", X = Xstd, y = rbinom(n, 1, 0.4),
                              beta_external = beta_i, family = "binomial")) == 0,
   "a binomial 0/1 y against standardized X passes (never standardize a binary y)")


# ---------------------------------------------------------------- brier_s
cat("brier_s clauses\n")
ok(length(validate_fit_inputs("brier_s", sumstats = ss, XtX = xtx,
                              beta_external = beta_s)) == 0,
   "a correct brier_s object passes")

b <- beta_s; rownames(b) <- rev(panel)
ok(has(validate_fit_inputs("brier_s", sumstats = ss, XtX = xtx, beta_external = b),
       "[alignment]"),
   "beta in the wrong ORDER vs the LD panel fails (row counts match)")

b <- rbind(matrix(0, 1, 1), beta_s)    # BRIERi convention handed to BRIERs
rownames(b) <- c("(Intercept)", panel)
ok(has(validate_fit_inputs("brier_s", sumstats = ss, XtX = xtx, beta_external = b),
       "[shape]"),
   "beta WITH an intercept row fails for brier_s (p rows, no intercept)")

x2 <- xtx; dimnames(x2) <- NULL
ok(has(validate_fit_inputs("brier_s", sumstats = ss, XtX = x2,
                           beta_external = beta_s), "[alignment]"),
   "an unnamed XtX fails")

dense <- as.matrix(diag(p)); dimnames(dense) <- list(panel, panel)
ok(has(validate_fit_inputs("brier_s", sumstats = ss, XtX = dense,
                           beta_external = beta_s), "[ld]"),
   "a DENSE LD fails (BRIERs requires sparse)")

ss2 <- ss; ss2$varnames <- rev(panel)
ok(has(validate_fit_inputs("brier_s", sumstats = ss2, XtX = xtx,
                           beta_external = beta_s), "[alignment]"),
   "sumstats in a different ORDER than the LD fails")


# ---------------------------------------------------------------- brier_full
cat("brier_full clauses\n")
# It pools RAW cohorts and has no external, so only the scale clause applies. Raw is
# LEGITIMATE here: T1_brierfull runs standardize=FALSE and scores 100.
ok(length(validate_fit_inputs("brier_full", X = Xraw, y = y_raw)) == 0,
   "a raw pooled brier_full object passes (raw is not a violation)")
ok(length(validate_fit_inputs("brier_full", X = pooled, y = y_std)) == 0,
   "a standardized pooled cross-ancestry object passes")
ok(has(validate_fit_inputs("brier_full", X = pooled, y = y_raw), "[scale]"),
   "a standardized pooled X with a RAW y fails")


# ---------------------------------------------------------------- held-out splits
cat("held-out split clauses\n")
ok(length(validate_eval_inputs(X = Xval_by_train, y = y_std,
                               fit_x_regime = "standardized",
                               fit_y_regime = "standardized")) == 0,
   "a correctly standardized val split passes")

ok(has(validate_eval_inputs(X = Xraw, y = y_std, fit_x_regime = "standardized",
                            fit_y_regime = "standardized"), "[scale]"),
   "a RAW val split against a model fit on standardized predictors fails")

ok(has(validate_eval_inputs(X = Xval_by_train, y = y_raw,
                            fit_x_regime = "standardized",
                            fit_y_regime = "standardized"), "[scale]"),
   "a raw gaussian y_val against a standardized fit fails (MSPE would be ~mean(y^2))")

ok(length(validate_eval_inputs(X = Xraw, y = y_raw, fit_x_regime = "raw",
                               fit_y_regime = "raw")) == 0,
   "a raw val split against a RAW fit passes (BRIERfull's legitimate case)")

# When the fit recorded nothing (an old cached fit), do not invent a verdict.
ok(length(validate_eval_inputs(X = Xraw, y = y_raw,
                               fit_x_regime = NULL, fit_y_regime = NULL)) == 0,
   "no recorded regime => no claim (an old fit is not retroactively wrong)")


# ---------------------------------------------------------------- the refusal
cat("the refusal\n")
e <- tryCatch({
  stop_on_contract_violations(c("[scale] boom"), "brier_i")
  NULL
}, error = function(e) conditionMessage(e))
ok(!is.null(e) && grepl("CONTRACT", e) && grepl("boom", e),
   "stop_on_contract_violations refuses and names the clause")
ok(length(stop_on_contract_violations(character(0), "brier_i")) == 0,
   "no violations => no refusal")

cat(sprintf("\ncontract: %d passed, %d failed\n", pass, fail))
if (fail > 0) quit(status = 1)
