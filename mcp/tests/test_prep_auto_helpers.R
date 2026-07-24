#!/usr/bin/env Rscript
# =============================================================================
# Unit tests for prep_auto.R's pure-logic helpers.
#
# prep_auto is by far the most-churned file in the repo (role normalization, three
# auto-discovery paths, a prune step, four error steers, a degeneracy guard,
# multi-external enumeration, a diagnostics echo). These tests pin the behaviour of
# every helper that does NOT need BRIER or the benchmark data, so a regression is
# caught in seconds instead of by a 12-minute run against the real 7B.
#
# Loads prep_auto.R's DEFINITIONS only: everything above the dispatch block is
# eval'd, and the `source(_common.R)` line is stripped, so nothing executes.
#
#   Rscript mcp/tests/test_prep_auto_helpers.R          (from the repo root)
# =============================================================================

.fails <- 0L
.checks <- 0L
ok <- function(cond, what) {
  .checks <<- .checks + 1L
  if (!isTRUE(cond)) {
    .fails <<- .fails + 1L
    cat("  FAIL:", what, "\n")
  }
}
# A helper is expected to stop() with a message matching `pattern`.
errs <- function(expr, pattern, what) {
  m <- tryCatch({ force(expr); NULL }, error = function(e) conditionMessage(e))
  ok(!is.null(m) && grepl(pattern, m), what)
}
section <- function(s) cat("\n[", s, "]\n", sep = "")

# ---- load the definitions ---------------------------------------------------
here <- function(p) {
  f <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  root <- if (length(f) == 1L && nzchar(f)) {
    normalizePath(file.path(dirname(f), "..", ".."), mustWork = FALSE)
  } else getwd()
  file.path(root, p)
}
src <- readLines(here("mcp/r_scripts/prep_auto.R"), warn = FALSE)
cut <- grep("^# ---- dispatch", src)[1]
code <- src[seq_len(cut - 1L)]
code <- code[!grepl("source[(]", code)]
eval(parse(text = paste(code, collapse = "\n")))

# A scratch data dir with empty placeholder files, so the file.exists()-based
# discovery/prune logic can be exercised without any real data.
tmp <- file.path(tempdir(), "prep_auto_tests")
unlink(tmp, recursive = TRUE); dir.create(tmp, recursive = TRUE)
touch <- function(...) for (f in c(...)) file.create(file.path(tmp, f), showWarnings = FALSE)
touch("height_AFR_X_training.txt.gz", "height_AFR_pheno_training.txt.gz",
      "height_AFR_X_validation.txt.gz", "height_AFR_pheno_validation.txt.gz",
      "height_AFR_X_testing.txt.gz", "height_AFR_pheno_testing.txt.gz",
      "height_EUR_X_training.txt.gz", "height_EUR_pheno_training.txt.gz",
      "height_EUR_X_validation.txt.gz", "height_EUR_pheno_validation.txt.gz")

# =============================================================================
section("role normalization")
# A small model uses the FITTER's expr vocabulary as role keys, or short forms.
r <- .normalize_roles(list(sumstats_expr = "g.gz", xtx_expr = "ld.rds",
                           beta_external_expr = "m.gz", snp = "s.gz"))
ok(identical(r[["target_sumstats"]], "g.gz"), "sumstats_expr -> target_sumstats")
ok(identical(r[["target_ld"]], "ld.rds"),     "xtx_expr -> target_ld")
ok(identical(r[["external_coef"]], "m.gz"),   "beta_external_expr -> external_coef")
ok(identical(r[["snp_info"]], "s.gz"),        "snp -> snp_info")
# numbered external aliases
r2 <- .normalize_roles(list(external_1 = "a.gz", external_2 = "b.gz"))
ok(identical(r2[["external_coef_1"]], "a.gz"), "external_1 -> external_coef_1")
ok(identical(r2[["external_coef_2"]], "b.gz"), "external_2 -> external_coef_2")
# an already-canonical role is never overwritten
r3 <- .normalize_roles(list(target_sumstats = "real.gz", gwas = "other.gz"))
ok(identical(r3[["target_sumstats"]], "real.gz"), "canonical role not overwritten")

# .role_path recovers a BARE filename (no extension) -- a common small-model slip. The
# manuscript fixtures are .tsv.gz, which was missing from the retry list, so bare
# "sumstats" for "sumstats.tsv.gz" errored and gpt-4o-mini looped.
rp_dir <- file.path(tempdir(), "rolepath"); unlink(rp_dir, recursive = TRUE); dir.create(rp_dir)
invisible(file.create(file.path(rp_dir, "sumstats.tsv.gz"), showWarnings = FALSE))
invisible(file.create(file.path(rp_dir, "panel.csv.gz"),    showWarnings = FALSE))
got_tsv <- .role_path(rp_dir, list(target_sumstats = "sumstats"), "target_sumstats")
ok(basename(got_tsv) == "sumstats.tsv.gz", "bare name resolves to .tsv.gz")
got_csv <- .role_path(rp_dir, list(x = "panel"), "x")
ok(basename(got_csv) == "panel.csv.gz", "bare name resolves to .csv.gz")
# a bare name with NO matching file still errors (never silently mis-resolves)
errs(.role_path(rp_dir, list(x = "nope"), "x"), "not found", "bare name with no match errors")

# NULL / empty-valued roles must be dropped (a small model nests non-role keys like ld_id:null
# here; left in, they crash prep_auto with "argument is of length zero"). Mirrors the dispatch
# backstop `roles <- Filter(function(v) !is.null(v) && length(v) > 0L, roles)`.
rnull <- Filter(function(v) !is.null(v) && length(v) > 0L,
                list(target_sumstats = "s.gz", ld_id = NULL, empty = character(0), snp_info = "i.gz"))
ok(!("ld_id" %in% names(rnull)) && !("empty" %in% names(rnull)),
   "NULL and length-0 role values are dropped by the dispatch backstop")
ok(identical(rnull[["target_sumstats"]], "s.gz") && identical(rnull[["snp_info"]], "i.gz"),
   "real role paths survive the NULL-drop")

# =============================================================================
section("raw-external detection + enumeration (Bucket B, M >= 1)")
ok(.has_raw_external(list(external_sumstats = "a.gz")),                 "raw: unnumbered summary")
ok(.has_raw_external(list(external_X = "x.gz", external_y = "y.gz")),   "raw: unnumbered individual")
ok(.has_raw_external(list(external_sumstats_1 = "a.gz")),               "raw: numbered summary")
ok(.has_raw_external(list(external_X_1 = "x.gz", external_y_1 = "y.gz")), "raw: numbered individual")
ok(!.has_raw_external(list(external_coef = "m.gz")),  "pretrained coef is NOT a raw external")
ok(!.has_raw_external(list(target_X_train = "x.gz")), "no external at all")
# external_X WITHOUT external_y is not a fittable cohort
ok(!.has_raw_external(list(external_X = "x.gz")), "external_X alone is not raw-fittable")

# unnumbered scalar -> exactly one instance
a <- .raw_external_instances(list(external_sumstats = "s.gz", external_ld_panel = "p.gz"),
                             "EUR", "hg38")
ok(length(a) == 1L, "unnumbered summary -> 1 instance")
ok(identical(a[[1]]$ancestry, "EUR"), "global ancestry applied")

# numbered M=2, each with its OWN panel and ancestry
b <- .raw_external_instances(list(
  external_sumstats_1 = "s1.gz", external_ld_panel_1 = "p1.gz", external_ld_ancestry_1 = "EUR",
  external_sumstats_2 = "s2.gz", external_ld_panel_2 = "p2.gz", external_ld_ancestry_2 = "EAS"),
  NULL, "hg38")
ok(length(b) == 2L, "numbered summary -> 2 instances")
ok(identical(b[[1]]$ancestry, "EUR") && identical(b[[2]]$ancestry, "EAS"), "per-instance ancestry")
ok(identical(b[[1]]$build, "hg38") && identical(b[[2]]$build, "hg38"), "build falls back to global")
ok(identical(b[[2]]$roles$external_sumstats, "s2.gz"), "instance 2 keeps its own sumstats")

# numbered summaries sharing ONE global external_ld_panel / external_snp_info (the common
# Bucket B case: several EUR GWAS built off one EUR reference). The global role must fall
# back to each numbered external, or the summary fit errors "needs external_ld_panel".
bg <- .raw_external_instances(list(
  external_sumstats_1 = "s1.gz", external_sumstats_2 = "s2.gz",
  external_ld_panel = "shared_panel.gz", external_snp_info = "shared_snp.gz"),
  "EUR", "hg38")
ok(length(bg) == 2L, "numbered summaries + shared global panel -> 2 instances")
ok(identical(bg[[1]]$roles$external_ld_panel, "shared_panel.gz") &&
   identical(bg[[2]]$roles$external_ld_panel, "shared_panel.gz"),
   "global external_ld_panel falls back to each numbered external")
ok(identical(bg[[1]]$roles$external_snp_info, "shared_snp.gz") &&
   identical(bg[[2]]$roles$external_snp_info, "shared_snp.gz"),
   "global external_snp_info falls back to each numbered external")
# a per-external panel still WINS over the global
bw <- .raw_external_instances(list(
  external_sumstats_1 = "s1.gz", external_ld_panel_1 = "own1.gz",
  external_sumstats_2 = "s2.gz", external_ld_panel = "shared.gz"), "EUR", "hg38")
ok(identical(bw[[1]]$roles$external_ld_panel, "own1.gz") &&
   identical(bw[[2]]$roles$external_ld_panel, "shared.gz"),
   "per-external panel wins; the other falls back to global")

# a packed list under ONE key -> one instance per element, sharing the panel
cc <- .raw_external_instances(list(external_sumstats = list("s1.gz", "s2.gz"),
                                   external_ld_panel = "p.gz"), "EUR", "hg38")
ok(length(cc) == 2L, "packed list -> 2 instances")
ok(identical(cc[[1]]$roles$external_ld_panel, cc[[2]]$roles$external_ld_panel),
   "packed list shares the one panel")

# numbered INDIVIDUAL cohorts, per-cohort val
d <- .raw_external_instances(list(
  external_X_1 = "x1.gz", external_y_1 = "y1.gz",
  external_X_val_1 = "xv1.gz", external_y_val_1 = "yv1.gz",
  external_X_2 = "x2.gz", external_y_2 = "y2.gz"), "EUR", "hg38")
ok(length(d) == 2L, "numbered individual -> 2 instances")
ok("external_X_val" %in% names(d[[1]]$roles), "cohort 1 keeps its own val")
ok(!("external_X_val" %in% names(d[[2]]$roles)), "cohort 2 has no val (not borrowed)")

# the SAME file named twice must count once
e <- .raw_external_instances(list(external_sumstats = "s.gz", external_sumstats_1 = "s.gz"),
                             "EUR", "hg38")
ok(length(e) == 1L, "duplicate source deduped")
# ...but two DISTINCT files under mixed spellings must both survive
e2 <- .raw_external_instances(list(external_sumstats = "sA.gz", external_sumstats_1 = "sB.gz"),
                              "EUR", "hg38")
ok(length(e2) == 2L, "mixed spellings, distinct files -> both kept")

# =============================================================================
section("external-shape guards (a RAW external named as external_coef)")
# a real PRETRAINED file: varnames + coef -> must NOT trip either guard
pre <- data.frame(varnames = c("rs1_A", "rs2_T"), coef = c(0.1, -0.2))
ok(!.looks_like_sumstats(pre), "pretrained (varnames+coef) is not sumstats")
# a GWAS: marginal-effect vocabulary, no coef column -> MUST trip
gwas <- data.frame(varnames = "rs1_A", SNP = "rs1", CHR = 1, BP = 10,
                   REF = "A", ALT = "T", N = 1000, BETA = 0.01, P = 0.4, corr = 0.02)
ok(.looks_like_sumstats(gwas), "GWAS sumstats detected")
# a coef table that happens to name its column `beta` is still NOT sumstats
# (no P/N/SE markers alongside it)
ok(!.looks_like_sumstats(data.frame(varnames = "rs1_A", coef = 0.1)),
   "single coef column is not sumstats")

snp <- data.frame(varnames = paste0("rs", 1:100, "_A"), CHR = 1, BP = 1:100,
                  REF = "A", ALT = "T")
geno <- as.data.frame(matrix(0, nrow = 5, ncol = 100))
colnames(geno) <- snp$varnames                     # columns ARE variants
ok(.looks_like_genotype_matrix(geno, snp), "genotype matrix detected")
ok(!.looks_like_genotype_matrix(pre, snp), "narrow coef table is not a genotype matrix")
ok(!.looks_like_genotype_matrix(geno, NULL), "no snp_info -> cannot claim genotype")

# =============================================================================
section("degeneracy guard (a NULL external must never pass silently)")
ok(.external_is_degenerate(rep(0, 10)), "all-exact-zero is degenerate")
# the real failure: an IC-selected null model leaves floating-point dust, which
# `all(cf == 0)` would MISS -- this is the 5.9e-17 external that once scored 70/70.
ok(.external_is_degenerate(c(5.9e-17, 0, 0)), "floating-point dust is degenerate")
ok(!.external_is_degenerate(c(0, 0, 0.015)), "a real coefficient is not degenerate")

# =============================================================================
section("target split discovery")
# INDIVIDUAL target: siblings derived from target_X_train / target_y_train
d1 <- .discover_target_splits(tmp, list(target_X_train = "height_AFR_X_training.txt.gz",
                                        target_y_train = "height_AFR_pheno_training.txt.gz"))
ok(identical(d1$roles[["target_X_val"]],  "height_AFR_X_validation.txt.gz"),  "individual: val X")
ok(identical(d1$roles[["target_y_val"]],  "height_AFR_pheno_validation.txt.gz"), "individual: val y")
ok(identical(d1$roles[["target_X_test"]], "height_AFR_X_testing.txt.gz"),     "individual: test X")

# SUMMARY target: no target_X_train at all -> anchor on the LD panel, and derive the
# phenotype by the X -> pheno convention. (Missing this is why brier_s silently fell
# back to an IC and reported a test MSPE of 4.9.)
d2 <- .discover_target_splits(tmp, list(target_sumstats = "gwas.gz",
                                        target_ld_panel = "height_AFR_X_training.txt.gz"))
ok(identical(d2$roles[["target_X_val"]], "height_AFR_X_validation.txt.gz"), "summary: val X")
ok(identical(d2$roles[["target_y_val"]], "height_AFR_pheno_validation.txt.gz"), "summary: val y")
ok(identical(d2$roles[["target_y_test"]], "height_AFR_pheno_testing.txt.gz"), "summary: test y")

# an explicitly-passed role is never overridden
d3 <- .discover_target_splits(tmp, list(target_X_train = "height_AFR_X_training.txt.gz",
                                        target_y_train = "height_AFR_pheno_training.txt.gz",
                                        target_X_val = "MINE.gz", target_y_val = "MINEY.gz"))
ok(identical(d3$roles[["target_X_val"]], "MINE.gz"), "explicit val not overridden")

# a case that genuinely ships NO val must discover nothing, so selection correctly
# falls back to an IC -- and a split is NEVER fabricated from another split.
bare <- file.path(tempdir(), "noval"); unlink(bare, recursive = TRUE); dir.create(bare)
invisible(file.create(file.path(bare, "height_AFR_X_training.txt.gz"), showWarnings = FALSE))
invisible(file.create(file.path(bare, "height_AFR_pheno_training.txt.gz"), showWarnings = FALSE))
d4 <- .discover_target_splits(bare, list(target_X_train = "height_AFR_X_training.txt.gz",
                                         target_y_train = "height_AFR_pheno_training.txt.gz"))
ok(is.null(d4$roles[["target_X_val"]]), "no val shipped -> nothing discovered")
ok(length(d4$notes) == 0L, "no val shipped -> no note")

# LITERAL-FILENAME fallback: a summary case whose LD panel carries no "training" token
# and whose splits are named plainly (X_test.tsv.gz / y_test.tsv.gz). The anchor
# substitution finds nothing, so the fallback must fill the splits by filename. (This is
# the gpt-4o-mini summary-case loop: no X_test_expr hint -> invented `prepared$X_test`.)
lit <- file.path(tempdir(), "litsplit"); unlink(lit, recursive = TRUE); dir.create(lit)
for (f in c("sumstats.tsv.gz", "reference_panel.tsv.gz", "X_val.tsv.gz", "y_val.tsv.gz",
            "X_test.tsv.gz", "y_test.tsv.gz"))
  invisible(file.create(file.path(lit, f), showWarnings = FALSE))
d5 <- .discover_target_splits(lit, list(target_sumstats = "sumstats.tsv.gz",
                                        target_ld_panel = "reference_panel.tsv.gz"))
ok(identical(d5$roles[["target_X_test"]], "X_test.tsv.gz"), "literal fallback: test X")
ok(identical(d5$roles[["target_y_test"]], "y_test.tsv.gz"), "literal fallback: test y")
ok(identical(d5$roles[["target_X_val"]],  "X_val.tsv.gz"),  "literal fallback: val X")

# the fallback must NOT override an explicitly-passed role
d6 <- .discover_target_splits(lit, list(target_sumstats = "sumstats.tsv.gz",
                                        target_ld_panel = "reference_panel.tsv.gz",
                                        target_X_test = "MINE.gz", target_y_test = "MINEY.gz"))
ok(identical(d6$roles[["target_X_test"]], "MINE.gz"), "literal fallback: explicit test not overridden")

# an AMBIGUOUS dir (two X_test candidates) must fill NOTHING, not guess
amb <- file.path(tempdir(), "ambsplit"); unlink(amb, recursive = TRUE); dir.create(amb)
for (f in c("X_test.tsv.gz", "cohort2_X_test.tsv.gz", "y_test.tsv.gz"))
  invisible(file.create(file.path(amb, f), showWarnings = FALSE))
d7 <- .discover_target_splits(amb, list(target_sumstats = "sumstats.tsv.gz"))
ok(is.null(d7$roles[["target_X_test"]]), "ambiguous dir -> nothing filled")

# =============================================================================
section("external val discovery")
# Omitting the external val drops the fit onto an IC, which at realistic n/p selects
# the NULL model -- this is the fix that turned a numerically-zero external into a
# real 285-coefficient one.
v1 <- .discover_external_val(tmp, list(external_X = "height_EUR_X_training.txt.gz",
                                       external_y = "height_EUR_pheno_training.txt.gz"))
ok(identical(v1[["external_X_val"]], "height_EUR_X_validation.txt.gz"), "external val X found")
ok(identical(v1[["external_y_val"]], "height_EUR_pheno_validation.txt.gz"), "external val y found")
# explicit external val is not overridden
v2 <- .discover_external_val(tmp, list(external_X = "height_EUR_X_training.txt.gz",
                                       external_y = "height_EUR_pheno_training.txt.gz",
                                       external_X_val = "MINE.gz", external_y_val = "MINEY.gz"))
ok(identical(v2[["external_X_val"]], "MINE.gz"), "explicit external val kept")
# no sibling on disk -> nothing invented
v3 <- .discover_external_val(bare, list(external_X = "height_AFR_X_training.txt.gz",
                                        external_y = "height_AFR_pheno_training.txt.gz"))
ok(is.null(v3[["external_X_val"]]), "no external val shipped -> nothing invented")

# =============================================================================
section("pruning hallucinated optional roles")
# A SUMMARY target ships no pheno_training, but the model invents one next to the
# GWAS; brier_s reads it as a standardization reference and used to die on a bare
# "file not found".
p1 <- .prune_missing_optional_roles(bare, list(
  target_sumstats = "gwas.gz",                       # required, untouched
  target_X_train  = "height_AFR_X_training.txt.gz",  # exists -> kept
  target_y_train  = "height_AFR_pheno_training.txt.gz"))
ok(!is.null(p1$roles[["target_X_train"]]), "existing optional role kept")
ok(!is.null(p1$roles[["target_y_train"]]), "existing optional role kept (y)")

p2 <- .prune_missing_optional_roles(tmp, list(
  target_sumstats = "gwas.gz",
  target_y_train  = "does_not_exist.txt.gz",
  target_X_test   = "height_AFR_X_testing.txt.gz"))
ok(is.null(p2$roles[["target_y_train"]]), "missing optional role pruned")
ok(!is.null(p2$roles[["target_X_test"]]), "present optional role survives pruning")
ok(identical(p2$roles[["target_sumstats"]], "gwas.gz"), "REQUIRED role never pruned")
ok(length(p2$notes) == 1L, "prune emits exactly one note")

# prune-then-discover: a mistyped val path is cleared AND re-discovered from the real
# sibling, rather than silently disabling selection.
pr <- .prune_missing_optional_roles(tmp, list(
  target_X_train = "height_AFR_X_training.txt.gz",
  target_y_train = "height_AFR_pheno_training.txt.gz",
  target_X_val   = "TYPO_validation.txt.gz",
  target_y_val   = "TYPO_pheno.txt.gz"))
dd <- .discover_target_splits(tmp, pr$roles)
ok(identical(dd$roles[["target_X_val"]], "height_AFR_X_validation.txt.gz"),
   "mistyped val is pruned then RE-discovered")

# =============================================================================
section("external-fit cache")
# Fitting a raw external is prep_auto's most expensive step (~7 min on a 20k x 10k
# matrix), and the agent's self-correction re-ran it on every retry. The key must
# depend on the external's inputs + fit config -- and on NOTHING about the target,
# since the external fit does not use the target at all.
Sys.setenv(BRIER_MCP_CACHE_DIR = file.path(tempdir(), "cachetest"))
ck <- function(roles, family = "gaussian", sm = "sd", anc = "EUR", bld = "hg38")
  .ext_fit_cache_key(tmp, roles, family, sm, anc, bld)

base_roles <- list(external_X = "height_EUR_X_training.txt.gz",
                   external_y = "height_EUR_pheno_training.txt.gz")
k1 <- ck(base_roles)
ok(!is.null(k1) && nzchar(k1), "key computed for a resolvable external")
ok(identical(k1, ck(base_roles)), "key is STABLE for identical inputs (-> cache hit)")

# the target must not enter the key: the same external fit is reusable across targets
ok(identical(k1, ck(c(base_roles, list(target_X_train = "height_AFR_X_training.txt.gz",
                                       target_sumstats = "gwas.gz")))),
   "target roles do NOT change the key")

# anything that changes the FIT must change the key
ok(!identical(k1, ck(base_roles, family = "binomial")), "family changes the key")
ok(!identical(k1, ck(base_roles, sm = "maf")),           "standardize_method changes the key")
ok(!identical(k1, ck(base_roles, anc = "EAS")),          "external ancestry changes the key")
ok(!identical(k1, ck(base_roles, bld = "hg19")),         "external build changes the key")
# adding the external's val split changes the SELECTION criterion, so it must miss
ok(!identical(k1, ck(c(base_roles,
                       list(external_X_val = "height_EUR_X_validation.txt.gz",
                            external_y_val = "height_EUR_pheno_validation.txt.gz")))),
   "external val split changes the key")
# a DIFFERENT external file must miss
ok(!identical(k1, ck(list(external_X = "height_AFR_X_training.txt.gz",
                          external_y = "height_AFR_pheno_training.txt.gz"))),
   "a different external file changes the key")
# no resolvable external inputs -> no key (nothing to cache)
ok(is.null(ck(list(target_X_train = "height_AFR_X_training.txt.gz"))),
   "no external inputs -> NULL key")

# EDITING an input invalidates the key (content identity, not just the path)
Sys.sleep(0.02)
writeLines("changed", file.path(tmp, "height_EUR_pheno_training.txt.gz"))
ok(!identical(k1, ck(base_roles)), "editing an input file invalidates the key")

# round-trip: put then get
key <- "unit_test_key"
payload_out <- data.frame(varnames = "rs1_A", coef = 0.5)
payload_diag <- list(kind = "individual", n_predictors = 10L,
                     selected_by = "external-val MSPE", nonzero_coefs = 1L)
.ext_fit_cache_put(key, payload_out, payload_diag)
hit <- .ext_fit_cache_get(key)
ok(!is.null(hit), "cache put -> get round-trips")
ok(identical(hit$out$coef, 0.5), "cached coefficients survive the round-trip")
ok(identical(hit$diag$nonzero_coefs, 1L), "cached DIAGNOSTICS survive (a cached fit stays auditable)")
ok(is.null(.ext_fit_cache_get("no_such_key")), "missing key -> NULL (miss)")

# a corrupt entry must degrade to a MISS, never an error: a cache is an optimization
# and must not be able to break a run.
writeLines("not an rds", file.path(.ext_fit_cache_dir(), "corrupt.rds"))
ok(is.null(.ext_fit_cache_get("corrupt")), "corrupt entry -> miss, not an error")
Sys.unsetenv("BRIER_MCP_CACHE_DIR")

# ---- .looks_like_ld_matrix (target_ld vs target_ld_panel) --------------------
# `target_ld` (a prebuilt LD) and `target_ld_panel` (a reference panel to BUILD one
# from) are both "the LD thing", so a small model mixes them up. On a real run the 7B
# handed a prebuilt sparse LD as target_ld_panel; prep_auto then tried to BUILD an LD
# from an LD, demanded ld_ancestry/ld_build to do it, and looped to a guard abort.
# The object itself says what it is, so detect it rather than trusting the role name.
suppressWarnings(suppressMessages(library(Matrix)))

vn <- paste0("rs", 1:5, "_A")
ld_named <- matrix(0, 5, 5, dimnames = list(vn, vn))
ok(.looks_like_ld_matrix(ld_named), "LD: square, rownames == colnames")

ld_sparse <- Matrix::Matrix(diag(5), sparse = TRUE)   # square, unnamed, sparse
ok(.looks_like_ld_matrix(ld_sparse), "LD: square sparse Matrix, unnamed")

ld_sparse_named <- Matrix::Matrix(diag(5), sparse = TRUE)
dimnames(ld_sparse_named) <- list(vn, vn)
ok(.looks_like_ld_matrix(ld_sparse_named), "LD: square sparse Matrix, named")

# A genotype reference panel: samples x variants. Read from a text file it is a
# data.frame, and it is rectangular either way.
panel_df <- data.frame(DeID_PatientID = 1:4, rs1_A = 0:3, rs2_G = c(1, 0, 2, 1))
ok(!.looks_like_ld_matrix(panel_df), "panel: a data.frame is never an LD")

panel_m <- matrix(0, nrow = 100, ncol = 5,
                  dimnames = list(paste0("s", 1:100), vn))
ok(!.looks_like_ld_matrix(panel_m), "panel: rectangular samples x variants is not an LD")

# Square but the margins disagree: not a variant x variant matrix.
mismatched <- matrix(0, 5, 5, dimnames = list(paste0("s", 1:5), vn))
ok(!.looks_like_ld_matrix(mismatched), "not an LD: square but rownames != colnames")

ok(!.looks_like_ld_matrix(NULL), "not an LD: NULL")

# ---- the coverage policy (PREP_AUTO_DESIGN.md 3.1) --------------------------
# A split below the threshold scores a model it cannot fully see: the missing predictors
# contribute nothing, so the metric is computed against a DIFFERENT model than the one being
# selected, and the chosen lambda is biased WHILE LOOKING HEALTHY. Refuse it rather than
# quietly scoring the overlap. VAL -> fall back to an IC. TEST -> abort (no IC substitute).

# .coverage_min: a PARAMETER, never hard-coded.
ok(identical(.coverage_min(), 0.8),        "coverage: default is 0.8")
ok(identical(.coverage_min(0.5), 0.5),     "coverage: an explicit value wins")
ok(identical(.coverage_min(0), 0.8),       "coverage: 0 is invalid -> default")
ok(identical(.coverage_min(1.5), 0.8),     "coverage: > 1 is invalid -> default")
ok(identical(.coverage_min("nonsense"), 0.8), "coverage: junk -> default")
Sys.setenv(BRIER_MCP_COVERAGE_MIN = "0.6")
ok(identical(.coverage_min(), 0.6),        "coverage: env var overrides the default")
ok(identical(.coverage_min(0.9), 0.9),     "coverage: an explicit value beats the env var")
Sys.unsetenv("BRIER_MCP_COVERAGE_MIN")

# .align_split_to_panel: name-matched, reordered, missing predictors FILLED.
panel <- c("a", "b", "c", "d")
X <- matrix(c(1, 2,
              3, 4,
              5, 6), nrow = 2,
            dimnames = list(c("s1", "s2"), c("c", "a", "b")))   # SHUFFLED, missing "d"
al <- .align_split_to_panel(X, panel, fill = 0)
ok(identical(colnames(al), panel),   "align: columns are reordered to the panel")
ok(identical(as.numeric(al[, "a"]), c(3, 4)), "align: 'a' follows its NAME, not its position")
ok(identical(as.numeric(al[, "c"]), c(1, 2)), "align: 'c' follows its name")
ok(all(al[, "d"] == 0),              "align: a predictor the split lacks is FILLED")
ok(identical(attr(al, "coverage"), 3L), "align: coverage counts the predictors carried")

# The FILL is per-shape and is NOT always 0: on the raw scale a literal 0 is a REAL
# GENOTYPE (homozygous reference), not "no information". A per-column fill must be honoured.
al2 <- .align_split_to_panel(X, panel, fill = c(10, 20, 30, 40))
ok(all(al2[, "d"] == 40), "align: a per-column fill vector is applied positionally")
ok(identical(as.numeric(al2[, "a"]), c(3, 4)), "align: a carried predictor is NOT overwritten by the fill")

# A split sharing NOTHING with the panel: all fill, coverage 0. It must not error here;
# the coverage check is what refuses it (so val and test can diverge).
al3 <- .align_split_to_panel(
  matrix(1:2, nrow = 2, dimnames = list(NULL, "zz")), panel, fill = 7)
ok(identical(attr(al3, "coverage"), 0L), "align: zero overlap -> coverage 0, no error here")

# .check_coverage: the decision itself.
hi <- .check_coverage(al, panel, "validation")                    # 3/4 = 75%
ok(isFALSE(hi$ok),                     "coverage: 75% is BELOW the 0.8 threshold -> refuse")
ok(grepl("75", hi$note),               "coverage: the note states the actual percentage")
lo <- .check_coverage(al, panel, "validation", coverage_min = 0.5)
ok(isTRUE(lo$ok),                      "coverage: the same split PASSES at coverage_min=0.5")
ok(grepl("imputed", lo$note),          "coverage: an accepted-but-partial split SAYS SO")
full <- .align_split_to_panel(
  matrix(1:8, nrow = 2, dimnames = list(NULL, panel)), panel, fill = 0)
fc <- .check_coverage(full, panel, "testing")
ok(isTRUE(fc$ok),                      "coverage: full coverage passes")
ok(is.null(fc$note),                   "coverage: full coverage is SILENT (no noise)")

# ---- .align_counts (surface everything the aligner did) ----------------------
# The aligner's counts are the only evidence a run has that variants were dropped to QC or
# that hundreds of allele flips were silently corrected. BRIER computed these and prep_auto
# threw them away (verbose = FALSE, counts never read), so a run could lose predictors and
# nothing in the report, the trace or the scorer would ever say so.
q <- .align_counts(list(n_multiallelic = 3L, n_ambiguous = 5L, n_flipped_target = 7L,
                        n_flipped_external = 11L, n_unmatched_target = 2L,
                        n_external_missing = 4L, n_external_only = 6L))
ok(length(q) == 7L,                        "counts: every nonzero count is surfaced")
ok(any(grepl("MULTI-ALLELIC", q)),         "counts: names the multi-allelic drop")
ok(any(grepl("STRAND-AMBIGUOUS", q)),      "counts: names the strand-ambiguous drop")
ok(any(grepl("TARGET effect", q)),         "counts: names the corrected target flips")
ok(any(grepl("EXTERNAL coefficient", q)),  "counts: names the corrected external flips")
ok(any(grepl("imputed to 0", q)),          "counts: names the imputed external coefficients")
ok(length(.align_counts(list(n_multiallelic = 0L, n_flipped_target = 0L))) == 0L,
   "counts: nothing happened -> SILENT (no noise on a clean run)")
# A diagnostic must never be able to break the run it reports on: an absent or NA count is
# silence, not an error. (A bare `if (!is.na(x))` on integer(0) dies with "missing value
# where TRUE/FALSE needed", which it did, taking out all 7 T3 cases on one run.)
ok(length(.align_counts(list())) == 0L,    "counts: ABSENT counts -> silent, not an error")
ok(length(.align_counts(list(n_multiallelic = NA_integer_))) == 0L,
   "counts: NA count -> silent, not an error")

# ---- .ss_col_map (the sumstats column map for preprocessS) ------------------
# preprocessS maps KEY -> COLUMN NAME and its defaults are lowercase
# (p = "pval", n = "n", beta = "beta"). Real GWAS files ship P / N / BETA, so the
# defaults silently do not match. This went unnoticed for a long time because
# target.ind = "corr" only needs `corr` (which DOES match), so every case shipping
# a corr column worked, while the target.ind = "gwas" branch -- the one that
# DERIVES corr via p2cor(p, n, sgn) -- could never run: it died on
# "Column 'pval' (mapped from key 'p') not found in target.ss".
ss <- data.frame(varnames = "v", SNP = "rs1", CHR = 1, BP = 2, REF = "A",
                 ALT = "G", N = 100, BETA = 0.1, P = 0.05)
m <- .ss_col_map(ss)
ok(identical(unname(m[["p"]]), "P"),       "col map: key p -> the shipped 'P' (not 'pval')")
ok(identical(unname(m[["n"]]), "N"),       "col map: key n -> the shipped 'N'")
ok(identical(unname(m[["beta"]]), "BETA"), "col map: key beta -> the shipped 'BETA'")
ok(identical(unname(m[["chr"]]), "CHR"),   "col map: key chr -> CHR")
ok(identical(unname(m[["alt"]]), "ALT"),   "col map: key alt -> ALT")

# A key with no matching column keeps BRIER's default, so a genuinely missing
# column still raises BRIER's own error instead of being papered over.
ok(identical(unname(m[["corr"]]), "corr"), "col map: absent corr keeps the default")
ok(identical(unname(m[["sgn"]]), "sgn"),   "col map: absent sgn keeps the default (preprocessS derives it from beta)")

# Case-insensitive, and the usual aliases resolve.
ss2 <- data.frame(chrom = 1, pos = 2, A2 = "A", A1 = "G", pval = 0.05,
                  sample_size = 100, effect = 0.1, corr = 0.3)
m2 <- .ss_col_map(ss2)
ok(identical(unname(m2[["chr"]]), "chrom"),        "col map: alias chrom -> chr")
ok(identical(unname(m2[["bp"]]), "pos"),           "col map: alias pos -> bp")
ok(identical(unname(m2[["ref"]]), "A2"),           "col map: alias A2 -> ref")
ok(identical(unname(m2[["alt"]]), "A1"),           "col map: alias A1 -> alt")
ok(identical(unname(m2[["p"]]), "pval"),           "col map: alias pval -> p")
ok(identical(unname(m2[["n"]]), "sample_size"),    "col map: alias sample_size -> n")
ok(identical(unname(m2[["beta"]]), "effect"),      "col map: alias effect -> beta")
ok(identical(unname(m2[["corr"]]), "corr"),        "col map: corr found when shipped")

# =============================================================================
cat("\n", strrep("-", 60), "\n", sep = "")
# ---------------------------------------------------------------------------
# .discover_external_cohort_vals: fill an omitted per-cohort external val (brier_full).
#
# Each external-only comparator is a single-cohort fit, and it MUST be selected on its
# own held-out data: selecting it on the TARGET's validation set leaks target data into
# a comparator whose entire point is to be purely external, and the leak is invisible in
# the metric. T1_brierfull ships a EUR validation split precisely so the comparator can
# be tuned on it -- and the agent omitted the roles, so it silently fell back to BIC.
local({
  d <- file.path(tempdir(), "bfval"); dir.create(d, showWarnings = FALSE)
  for (f in c("height_EUR_X_training.txt.gz", "height_EUR_pheno_training.txt.gz",
              "height_EUR_X_validation.txt.gz", "height_EUR_pheno_validation.txt.gz",
              "height_EUR2_X_training.txt.gz", "height_EUR2_pheno_training.txt.gz")) {
    writeLines("x", file.path(d, f))
  }

  # cohort 1 ships a val -> filled from the training filename's sibling
  r <- .discover_external_cohort_vals(d, list(
    external_X_1 = "height_EUR_X_training.txt.gz",
    external_y_1 = "height_EUR_pheno_training.txt.gz"))
  ok(identical(r[["external_X_1_val"]], "height_EUR_X_validation.txt.gz"),
     "external cohort val: filled from the training sibling")
  ok(identical(r[["external_y_1_val"]], "height_EUR_pheno_validation.txt.gz"),
     "external cohort val: the phenotype sibling too")

  # cohort 2 ships NO val -> nothing is fabricated, it falls through to an IC
  r2 <- .discover_external_cohort_vals(d, list(
    external_X_2 = "height_EUR2_X_training.txt.gz",
    external_y_2 = "height_EUR2_pheno_training.txt.gz"))
  ok(is.null(r2[["external_X_2_val"]]),
     "external cohort val: a cohort with no val split gets none invented")

  # an explicitly-named val is never overwritten
  r3 <- .discover_external_cohort_vals(d, list(
    external_X_1 = "height_EUR_X_training.txt.gz",
    external_y_1 = "height_EUR_pheno_training.txt.gz",
    external_X_1_val = "mine.txt.gz", external_y_1_val = "mine_y.txt.gz"))
  ok(identical(r3[["external_X_1_val"]], "mine.txt.gz"),
     "external cohort val: an explicit role is respected, not overwritten")
})


# ---------------------------------------------------------------------------
# .steer_if_individual_target / .discover_external_ld_panel
#
# T2_afr-ind_eur-summary is an INDIVIDUAL AFR target (X + pheno) with a SUMMARY EUR
# external. The agent could not route the summary external under brier_i, so it flipped
# the whole CASE to brier_s and passed the EXTERNAL's GWAS as target_sumstats -- the SAME
# FILE in both roles. A target cannot be its own external. Had the run got past its other
# error it would have fit the external AS the target and reported a number: a wrong
# analysis that scores, which is far worse than a stop.
local({
  d <- file.path(tempdir(), "steerind"); dir.create(d, showWarnings = FALSE)
  for (f in c("height_AFR_X_training.txt.gz", "height_AFR_pheno_training.txt.gz",
              "height_EUR_GWAS_training.txt.gz", "height_EUR_X_training.txt.gz",
              "height_AFR_GWAS_training.txt.gz")) writeLines("x", file.path(d, f))

  # (a) the same file as target AND external -> refused, and the message says why
  e <- tryCatch(.steer_if_individual_target(d, list(
    target_sumstats   = "height_EUR_GWAS_training.txt.gz",
    external_sumstats = "height_EUR_GWAS_training.txt.gz"), "brier_s"),
    error = function(e) conditionMessage(e))
  ok(is.character(e) && grepl("SAME FILE", e),
     "steer: a target that is its own external is refused")
  ok(is.character(e) && grepl("brier_i", e),
     "steer: ... and it names the shape to re-route to")

  # (b) an INDIVIDUAL-level target routed to brier_s -> refused
  e2 <- tryCatch(.steer_if_individual_target(d, list(
    target_X_train = "height_AFR_X_training.txt.gz",
    target_y_train = "height_AFR_pheno_training.txt.gz"), "brier_s"),
    error = function(e) conditionMessage(e))
  ok(is.character(e2) && grepl("INDIVIDUAL-LEVEL", e2),
     "steer: an individual-level target cannot be brier_s")
  ok(is.character(e2) && grepl("external_sumstats", e2),
     "steer: ... and it says the summary EXTERNAL still has a home")

  # (c) a GENUINE summary target must pass straight through, or every brier_s case breaks
  ok(is.null(.steer_if_individual_target(d, list(
    target_sumstats = "height_AFR_GWAS_training.txt.gz",
    external_sumstats = "height_EUR_GWAS_training.txt.gz"), "brier_s")),
     "steer: a genuine summary target (different external) is untouched")
})

# The agent named external_sumstats + the ancestry and omitted the PANEL the external's
# LD must be built from, then looped on the error. Discover it -- but ONLY by ancestry
# match, so a EUR external can never have its LD built from the AFR panel (which would be
# quietly wrong rather than loudly broken).
local({
  d <- file.path(tempdir(), "extpanel"); dir.create(d, showWarnings = FALSE)
  for (f in c("height_EUR_GWAS_training.txt.gz", "height_EUR_X_training.txt.gz",
              "height_AFR_X_training.txt.gz", "height_AFR_pheno_training.txt.gz"))
    writeLines("x", file.path(d, f))

  r <- .discover_external_ld_panel(d, list(
    external_sumstats = "height_EUR_GWAS_training.txt.gz"), "EUR")
  ok(identical(r[["external_ld_panel"]], "height_EUR_X_training.txt.gz"),
     "external panel: discovered by ancestry match")

  # a wrong-ancestry request must find NOTHING rather than grab the panel that is there
  r2 <- .discover_external_ld_panel(d, list(
    external_sumstats = "height_EUR_GWAS_training.txt.gz"), "EAS")
  ok(is.null(r2[["external_ld_panel"]]),
     "external panel: an ancestry with no panel gets none invented")

  # no ancestry -> no guess (the build is not inferable, so neither is the panel)
  r3 <- .discover_external_ld_panel(d, list(
    external_sumstats = "height_EUR_GWAS_training.txt.gz"), NULL)
  ok(is.null(r3[["external_ld_panel"]]),
     "external panel: no ancestry means no discovery")

  # an explicitly-named panel is never overwritten
  r4 <- .discover_external_ld_panel(d, list(
    external_sumstats = "height_EUR_GWAS_training.txt.gz",
    external_ld_panel = "mine.txt.gz"), "EUR")
  ok(identical(r4[["external_ld_panel"]], "mine.txt.gz"),
     "external panel: an explicit role is respected")
})


# ---------------------------------------------------------------------------
# .check_raw_external_roles: a role must point at the KIND of file it names.
#
# On T2_afr-summary_eur-2ind (two INDIVIDUAL-level EUR cohorts) the agent passed the
# cohorts' PHENOTYPE files as external_sumstats_1/_2. The errors it got back were about a
# missing snp_info file and an empty variant intersection -- true, but downstream of the
# actual mistake, so they steered it nowhere and it looped to a guard abort.
local({
  d <- file.path(tempdir(), "extkind"); dir.create(d, showWarnings = FALSE)
  # a PHENOTYPE: an id and a value. Not summary statistics.
  write.table(data.frame(DeID_PatientID = 1:3, height = c(170, 165, 180)),
              file.path(d, "height_EUR1_pheno_training.txt"), sep = "\t",
              row.names = FALSE, quote = FALSE)
  # real GWAS summary statistics
  write.table(data.frame(varnames = c("a", "b"), CHR = 1, BP = 1:2, REF = "A",
                         ALT = "G", N = 100, BETA = 0.1, P = 0.01),
              file.path(d, "height_EUR_GWAS_training.txt"), sep = "\t",
              row.names = FALSE, quote = FALSE)

  e <- tryCatch(.check_raw_external_roles(d, list(
    external_sumstats_1 = "height_EUR1_pheno_training.txt")),
    error = function(e) conditionMessage(e))
  ok(is.character(e) && grepl("not GWAS summary statistics", e),
     "external kind: a phenotype passed as sumstats is refused")
  ok(is.character(e) && grepl("PHENOTYPE", e),
     "external kind: ... and it says the file is a PHENOTYPE")
  ok(is.character(e) && grepl("external_y_1 = ", e),
     "external kind: ... so the PHENOTYPE goes in external_y_k")

  # A GENOTYPE MATRIX passed as sumstats. The first guard called this a "phenotype" too
  # and told the model to pass the matrix as external_y -- nonsense, and it oscillated:
  # the model flipped to brier_i (where its roles were RIGHT), was steered back to
  # brier_s, renamed to sumstats again, and looped. Say what the file IS, and pin the
  # shape: an individual-level EXTERNAL does not make the TARGET individual-level.
  geno <- as.data.frame(matrix(0, nrow = 3, ncol = 60))
  colnames(geno) <- c("DeID_PatientID", paste0("rs", 1:59, "_A"))
  write.table(geno, file.path(d, "height_EUR1_X_training.txt"), sep = "\t",
              row.names = FALSE, quote = FALSE)
  g <- tryCatch(.check_raw_external_roles(d, list(
    external_sumstats_1 = "height_EUR1_X_training.txt")),
    error = function(e) conditionMessage(e))
  ok(is.character(g) && grepl("GENOTYPE MATRIX", g),
     "external kind: a genotype matrix is NOT called a phenotype")
  ok(is.character(g) && grepl("external_X_1 = ", g),
     "external kind: ... so the MATRIX goes in external_X_k")
  ok(is.character(g) && grepl("KEEP THE SHAPE", g) && grepl("brier_s", g),
     "external kind: ... and the summary TARGET keeps shape=brier_s")

  # a GENUINE summary external must pass straight through
  ok(is.null(.check_raw_external_roles(d, list(
    external_sumstats = "height_EUR_GWAS_training.txt"))),
     "external kind: real summary statistics are untouched")

  # nothing to check
  ok(is.null(.check_raw_external_roles(d, list(external_X_1 = "x.txt"))),
     "external kind: an individual external is not checked for sumstats-ness")
})


# ---------------------------------------------------------------------------
# Discovery must work with ABSOLUTE role paths.
#
# The benchmark runner injects absolute paths into the prompt ("do not use bare
# filenames"), so the agent passes absolute role values routinely. Every .discover_*
# helper built its sibling by string substitution and then checked
# file.exists(file.path(data_dir, candidate)) -- which for an ABSOLUTE candidate yields
# "<data_dir>//abs/path" and never exists. Discovery therefore did NOTHING, silently:
# T2_afr-summary_eur-summary assembled no test split, the agent invented
# `prepared$X_test`, and looped until the guard aborted the run. The earlier cases only
# worked because those agents happened to pass BARE filenames.
local({
  d <- file.path(tempdir(), "abspaths"); dir.create(d, showWarnings = FALSE)
  for (f in c("height_AFR_X_training.txt.gz", "height_AFR_X_testing.txt.gz",
              "height_AFR_pheno_testing.txt.gz", "height_AFR_X_validation.txt.gz",
              "height_AFR_pheno_validation.txt.gz")) writeLines("x", file.path(d, f))

  # the agent passes an ABSOLUTE path, exactly as the runner tells it to
  abs_panel <- file.path(d, "height_AFR_X_training.txt.gz")
  r <- .discover_target_splits(d, list(target_ld_panel = abs_panel))$roles
  ok(!is.null(r[["target_X_test"]]) && grepl("X_testing", r[["target_X_test"]]),
     "discovery: an ABSOLUTE anchor still finds the test split")
  ok(!is.null(r[["target_y_test"]]) && grepl("pheno_testing", r[["target_y_test"]]),
     "discovery: ... and its phenotype sibling")
  ok(!is.null(r[["target_X_val"]]) && grepl("X_validation", r[["target_X_val"]]),
     "discovery: ... and the validation split")

  # a BARE filename must keep working (it is how the earlier cases passed)
  r2 <- .discover_target_splits(d, list(
    target_ld_panel = "height_AFR_X_training.txt.gz"))$roles
  ok(!is.null(r2[["target_X_test"]]),
     "discovery: a bare filename still works (no regression)")

  # .exists_in_dir itself
  ok(.exists_in_dir(d, abs_panel), "exists_in_dir: absolute path")
  ok(.exists_in_dir(d, "height_AFR_X_training.txt.gz"), "exists_in_dir: bare filename")
  ok(!.exists_in_dir(d, "nope.txt.gz"), "exists_in_dir: a missing file is not invented")
})


section("external identifier normalization (.ensure_external_varnames)")
# The COEF column was always guessed; the IDENTIFIER column used to require the literal
# name `varnames`, so a `SNP`/`id`/... column silently matched nothing (all-zero external).
ok("varnames" %in% colnames(.ensure_external_varnames(
     data.frame(varnames = c("g1","g2"), coef = c(0.1, 0.2), stringsAsFactors = FALSE))),
   "an existing varnames column is kept")
ok(identical(colnames(.ensure_external_varnames(
     data.frame(SNP = c("g1","g2"), coef = c(0.1, 0.2), stringsAsFactors = FALSE)))[1], "varnames"),
   "a `SNP` identifier alias is renamed to varnames")
ok("varnames" %in% colnames(.ensure_external_varnames(
     data.frame(id = c("g1","g2"), weight = c(0.1, 0.2), stringsAsFactors = FALSE))),
   "an `id` alias is renamed (and the coef column may be `weight`)")
ok("varnames" %in% colnames(.ensure_external_varnames(
     data.frame(locus = c("g1","g2"), coef = c(0.1, 0.2), stringsAsFactors = FALSE))),
   "a lone non-numeric column (no alias) is taken as the identifier")
ok(!("varnames" %in% colnames(.ensure_external_varnames(
     data.frame(a = c("x","y"), b = c("p","q"), coef = c(1, 2), stringsAsFactors = FALSE)))),
   "two non-numeric columns and no alias: left unchanged (ambiguous, no guess)")

section("zero-overlap external guard (.check_external_overlap)")
ok(.check_external_overlap(
     data.frame(varnames = c("g1","g2"), coef = c(1, 2), stringsAsFactors = FALSE),
     c("g1","g3","g5")) == 1L,
   "an external that overlaps the panel returns the overlap count, no error")
errs(.check_external_overlap(
       data.frame(varnames = c("z1","z2"), coef = c(1, 2), stringsAsFactors = FALSE),
       c("g1","g2","g3")),
     "shares NO predictor names",
     "a disjoint external ERRORS loudly instead of returning a silent all-zero vector")
ok(.check_external_overlap(NULL, c("g1")) == 0L,
   "a NULL external is a no-op (the missing-external path handles it elsewhere)")

section("external scale detection (external_coef_scale='auto', brier_i)")
# STRUCTURAL detector: log|beta| vs log(sd) slope, ~0 standardized, ~-1 raw. sd is the
# CONVENTIONAL EMPIRICAL sd of the predictors (never AF-derived). Conservative: default
# standardized, flip to raw only on strong, well-populated evidence.
set.seed(11)
sdv <- runif(60, 0.2, 0.9)                       # predictor empirical sds
b_std <- rnorm(60, 0, 0.1)                       # standardized: coef sizes independent of sd
b_raw <- b_std / sdv                             # raw twin: beta_raw = beta_std / sd
ok(.detect_external_scale(b_std, sdv)$scale == "standardized",
   "a standardized coefficient vector detects as standardized (slope ~ 0)")
ok(.detect_external_scale(b_raw, sdv)$scale == "raw",
   "its raw twin detects as raw (slope ~ -1)")
# below the nonzero-count floor -> decline to the safe default rather than read noise
ok(.detect_external_scale(c(b_raw[1:4], rep(0, 56)), sdv)$scale == "standardized",
   "too few nonzero coefficients -> defaults to standardized (does not guess raw)")
# a borderline-negative slope (the AF/cross-ancestry confound band) must NOT be called raw
b_soft <- b_std / (sdv^0.4)                       # slope ~ -0.4, ambiguous
ok(.detect_external_scale(b_soft, sdv)$scale == "standardized",
   "a mildly-negative slope stays standardized (only strong evidence flips to raw)")
ok(is.finite(.detect_external_scale(b_raw, sdv)$slope) &&
     .detect_external_scale(b_raw, sdv)$slope < -0.9,
   "the reported slope for a raw vector is firmly negative")

# multi-external agreement: detect EACH column; helper is per-vector, so simulate the caller's
# aggregation. Two standardized columns agree; a standardized + a raw column disagree.
scl <- function(b) .detect_external_scale(b, sdv)$scale
b_std2 <- rnorm(60, 0, 0.1)
ok(length(unique(c(scl(b_std), scl(b_std2)))) == 1L && scl(b_std) == "standardized",
   "two standardized external columns agree on standardized")
ok(length(unique(c(scl(b_std), scl(b_raw)))) == 2L,
   "a standardized column and a raw column DISAGREE (caller then defaults to standardized + warns)")

section("outcome-family detection")
ok(.detect_family(c(0, 1, 1, 0, 1)) == "binomial",
   "a 0/1 two-level response detects as binomial")
ok(.detect_family(c(1.2, 3.4, 170.5, -2)) == "gaussian",
   "a continuous response detects as gaussian")
ok(.detect_family(c(0, 0, 0)) == "gaussian",
   "an all-zero (one-level) response is NOT binomial (needs two levels)")
ok(.detect_family(c(0, 1, NA, 1)) == "binomial",
   "NA values are ignored in detection")
ok(.detect_family(c(0, 1, 2, 1)) == "gaussian",
   "0/1/2 dosages are NOT binary (three levels) -> gaussian")
# .resolve_family: an explicit family always wins; only unspecified is detected.
ok(.resolve_family("gaussian", c(0, 1, 0)) == "gaussian",
   "an explicit gaussian is respected even on a 0/1 response")
ok(.resolve_family("binomial", c(1.5, 2.5)) == "binomial",
   "an explicit binomial is respected on a continuous response")
ok(.resolve_family("poisson", c(0, 1)) == "poisson",
   "an explicit poisson is respected")
ok(.resolve_family("auto", c(0, 1, 1)) == "binomial",
   "'auto' detects binomial from a 0/1 response")
ok(.resolve_family("unknown", c(2.3, 4.1)) == "gaussian",
   "an unrecognized family is treated as unspecified and detected (gaussian here)")
ok(.resolve_family(NULL, c(0, 1)) == "binomial",
   "a NULL family is detected")

section("empty-role guard (.role_path)")
# A model that emits an empty role value must get an ACTIONABLE message naming the role,
# not the cryptic "zero-length 'path' argument" from the downstream file readers.
errs(.role_path("/tmp", list(target_X_train = ""), "target_X_train"),
     "empty or invalid value",
     "an empty role value errors actionably, naming the role")
errs(.role_path("/tmp", list(target_X_train = character(0)), "target_X_train"),
     "empty or invalid value",
     "a zero-length role value errors actionably")
# A present, non-empty role is unaffected: it still errors on the MISSING FILE (not the
# empty-value guard), so the guard does not swallow the normal not-found path.
errs(.role_path("/tmp", list(target_X_train = "no_such_file_xyz.txt.gz"), "target_X_train"),
     "Files in|not", "a non-empty but missing file still reports file-not-found, not empty")

if (.fails == 0L) {
  cat(sprintf("prep_auto helpers: ALL %d CHECKS PASS\n", .checks))
} else {
  cat(sprintf("prep_auto helpers: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}

