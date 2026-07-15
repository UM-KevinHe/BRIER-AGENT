#!/usr/bin/env Rscript
# =============================================================================
# STRAND AMBIGUITY: the one QC decision that was over-applied, and the one with no fixture.
#
# A PALINDROMIC pair (A/T, C/G) reads the SAME on either strand, so its allele letters carry
# NO orientation information. An apparent swap could be a genuine allele swap or a strand
# flip; an apparent MATCH could equally be either. BRIER's preprocessors resolved this by
# blanket-dropping every such variant, unconditionally, even with no external to be ambiguous
# against. On real data palindromic variants are ~15% of common SNPs, so that is a large,
# silent, and usually unnecessary loss.
#
# Allele frequency separates the two hypotheses:
#     same effect allele  ->  AF_tbl ~= AF_ref
#     opposite            ->  AF_tbl ~= 1 - AF_ref
# which collapses to ONE rule: NEGATE iff AF_tbl is closer to 1 - AF_ref than to AF_ref, and
# only by a clear MARGIN. The margin is what makes the undecidable band fall out for free:
# as AF_ref approaches 0.5 the two hypotheses converge on their own.
#
# THE BENCHMARK PANEL HAS ZERO PALINDROMIC VARIANTS, so none of this is reachable from the
# real data. This fixture is built to exercise it: same-strand, strand-flipped, genuinely
# allele-swapped, undecidable (MAF ~ 0.5), and no-AF-available, plus non-palindromic
# controls that must be completely unaffected.
#
#   Rscript mcp/tests/test_ambiguity.R
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
src <- readLines("mcp/r_scripts/prep_auto.R", warn = FALSE)
cut <- grep("^# ---- dispatch", src)[1]
defs <- src[seq_len(cut - 1L)]
defs <- defs[!grepl("^source\\(", defs)]
eval(parse(text = paste(defs, collapse = "\n")), envir = globalenv())

# -----------------------------------------------------------------------------
# The reference panel. Six variants, each probing one branch.
#
#   v1  A/T palindromic, AF 0.20  external SAME letters,     AF 0.22  -> same strand, keep
#   v2  C/G palindromic, AF 0.15  external SWAPPED letters,  AF 0.17  -> STRAND flip, keep
#                                 (letters say swap, AF says same allele: AF WINS)
#   v3  A/T palindromic, AF 0.30  external SAME letters,     AF 0.72  -> AF says OPPOSITE,
#                                 so the effect is negated even though the letters MATCH
#   v4  A/T palindromic, AF 0.50  external, AF 0.50                   -> UNDECIDABLE
#   v5  A/G, AF 0.25             external SWAPPED letters              -> plain flip, no
#                                 ambiguity (not palindromic)
#   v6  C/T, AF 0.40             external SAME letters                 -> plain match
# -----------------------------------------------------------------------------
ref <- data.frame(
  varnames = paste0("v", 1:6),
  CHR = c(1, 1, 1, 1, 2, 2),
  BP  = c(100, 200, 300, 400, 500, 600),
  REF = c("A", "C", "A", "A", "A", "C"),
  ALT = c("T", "G", "T", "T", "G", "T"),
  AF  = c(0.20, 0.15, 0.30, 0.50, 0.25, 0.40),
  stringsAsFactors = FALSE
)
ext <- data.frame(
  varnames = paste0("v", 1:6),
  CHR = c(1, 1, 1, 1, 2, 2),
  BP  = c(100, 200, 300, 400, 500, 600),
  REF = c("A", "G", "A", "A", "G", "C"),   # v2 and v5 have SWAPPED letters
  ALT = c("T", "C", "T", "T", "A", "T"),
  AF  = c(0.22, 0.17, 0.72, 0.50, 0.75, 0.41),
  coef1 = c(0.10, 0.20, 0.30, 0.40, 0.50, 0.60),
  stringsAsFactors = FALSE
)

cat("\n[1] ambiguous = 'resolve' (the default): AF decides, letters are noise\n")
al <- .align_predictors(ref = ref, ext_tab = ext, predictor_type = "genotype")
b <- as.numeric(al$beta[, 1])

ok(length(al$keep) == 6L,
   "every reference variant survives: ambiguity costs a COEFFICIENT, not a predictor")
ok(al$n_ambiguous == 4L,      "all 4 palindromic matches are counted as ambiguous")
ok(al$n_ambiguous_resolved == 3L,
   sprintf("3 of them are RESOLVED by AF (got %d)", al$n_ambiguous_resolved))
ok(al$n_ambiguous_dropped == 1L,
   sprintf("only the MAF ~ 0.5 one stays undecidable (got %d)", al$n_ambiguous_dropped))

ok(b[1] == 0.10,
   "v1 (palindromic, AF agrees, letters agree): kept as is")
ok(b[2] == 0.20,
   "v2 (palindromic, letters say SWAP but AF says same allele): a STRAND flip, NOT negated")
ok(b[3] == -0.30,
   "v3 (palindromic, letters MATCH but AF says opposite): NEGATED. The letters lied.")
ok(b[4] == 0,
   "v4 (palindromic, AF ~ 0.5, undecidable): coefficient imputed to 0, predictor kept")
ok(b[5] == -0.50,
   "v5 (NOT palindromic, letters swapped): plain flip, negated, no ambiguity involved")
ok(b[6] == 0.60,
   "v6 (NOT palindromic, letters match): untouched")
ok(al$n_flipped_external == 2L,
   sprintf("exactly 2 negations (v3 by AF, v5 by letters); got %d", al$n_flipped_external))

cat("\n[2] the letters ALONE would get v2 and v3 exactly BACKWARDS\n")
# This is the whole point. Without AF resolution a palindrome is oriented by its letters,
# which for a palindrome are meaningless: v2 would be negated (it should not be) and v3 would
# be kept (it should be negated). Both are SILENT sign errors.
al_keep <- .align_predictors(ref = ref, ext_tab = ext, predictor_type = "genotype",
                             ambiguous = "keep")
bk <- as.numeric(al_keep$beta[, 1])
ok(bk[2] == -0.20 && bk[3] == 0.30,
   "ambiguous = 'keep' trusts the letters and gets BOTH signs wrong (this is the bug)")
ok(b[2] == -bk[2] && b[3] == -bk[3],
   "'resolve' and 'keep' differ by exactly the two signs AF corrected")

cat("\n[3] ambiguous = 'drop': the old preprocessS behaviour, still available\n")
al_drop <- .align_predictors(ref = ref, ext_tab = ext, predictor_type = "genotype",
                             ambiguous = "drop")
bd <- as.numeric(al_drop$beta[, 1])
ok(all(bd[1:4] == 0),
   "'drop': every palindromic coefficient is refused (imputed 0), even the resolvable ones")
ok(bd[5] == -0.50 && bd[6] == 0.60,
   "'drop': the non-palindromic variants are untouched")
ok(length(al_drop$keep) == 6L,
   "'drop' still keeps the target's predictors: an external's refusal is not the target's loss")

cat("\n[4] NO allele frequency: refuse rather than guess\n")
ext_noaf <- ext[, setdiff(colnames(ext), "AF")]
al_noaf <- .align_predictors(ref = ref, ext_tab = ext_noaf, predictor_type = "genotype")
bn <- as.numeric(al_noaf$beta[, 1])
ok(al_noaf$n_ambiguous_no_af == 4L,
   "with no AF on one side, all 4 palindromic matches are flagged unresolvable")
ok(all(bn[1:4] == 0),
   "no AF -> the palindromic coefficients are imputed 0, NOT guessed from the letters")
ok(bn[5] == -0.50 && bn[6] == 0.60,
   "no AF -> the non-palindromic variants still align normally")

cat("\n[5] a bare MAF is NOT an allele frequency\n")
# MAF is the MINOR allele's frequency and does not say WHICH allele is minor, so it cannot
# orient anything. Accepting it as if it were an ALT frequency would silently produce
# confident, wrong flips.
ext_maf <- ext
colnames(ext_maf)[colnames(ext_maf) == "AF"] <- "MAF"
ok(is.null(.alt_freq(ext_maf)),
   "a MAF column is REFUSED as an orientation frequency")
al_maf <- .align_predictors(ref = ref, ext_tab = ext_maf, predictor_type = "genotype")
ok(al_maf$n_ambiguous_no_af == 4L,
   "a MAF-only external falls back to refusing the palindromes, not to guessing")

cat("\n[6] the TARGET side: an undecidable variant leaves the panel entirely\n")
# The asymmetry that runs through the whole aligner. A missing/unorientable EXTERNAL
# coefficient just means no transfer contribution, so impute 0 and keep the predictor. A
# target variant whose orientation is unknown has NO usable signal and nothing to impute
# from, so it must go.
ss <- data.frame(
  varnames = paste0("v", 1:6),
  CHR = ref$CHR, BP = ref$BP,
  REF = ref$REF, ALT = ref$ALT,
  AF = c(0.20, 0.15, 0.30, 0.50, 0.25, 0.40),
  corr = c(0.01, 0.02, 0.03, 0.04, 0.05, 0.06),
  stringsAsFactors = FALSE
)
al_t <- .align_predictors(ref = ref, target_ss = ss, target_ind = "corr",
                          predictor_type = "genotype")
ok(length(al_t$keep) == 5L,
   sprintf("the target panel DROPS the undecidable v4: 5 survive, not 6 (got %d)",
           length(al_t$keep)))
ok(!("v4" %in% as.character(al_t$sumstats$varnames)),
   "v4 is the one that left")
ok(identical(as.numeric(al_t$sumstats$corr), c(0.01, 0.02, 0.03, 0.05, 0.06)),
   "the surviving corr values are untouched (this target agrees with the reference)")

cat("\n[7] a GENERIC predictor has no strand, so none of this applies\n")
gref <- data.frame(varnames = c("gene1", "gene2"), stringsAsFactors = FALSE)
gext <- data.frame(varnames = c("gene1", "gene2"), coef1 = c(1.5, 2.5),
                   stringsAsFactors = FALSE)
al_g <- .align_predictors(ref = gref, ext_tab = gext, predictor_type = "generic")
ok(al_g$n_ambiguous == 0L,
   "generic: nothing is strand-ambiguous (there is no opposite allele of an expression level)")
ok(identical(as.numeric(al_g$beta[, 1]), c(1.5, 2.5)),
   "generic: the coefficients pass through unmodified")

cat("\n[8] the margin is what creates the undecidable band, not a hard-coded MAF window\n")
# As AF_ref approaches 0.5, AF_ref and 1 - AF_ref converge, so the two hypotheses converge
# with them and the variant declares itself undecidable. Widening the margin therefore
# widens the band: it is one knob, not two.
al_wide <- .align_predictors(ref = ref, ext_tab = ext, predictor_type = "genotype",
                             af_margin = 0.45)
ok(al_wide$n_ambiguous_dropped > al$n_ambiguous_dropped,
   sprintf("a wider margin refuses MORE palindromes (%d at 0.45 vs %d at 0.10)",
           al_wide$n_ambiguous_dropped, al$n_ambiguous_dropped))
al_narrow <- .align_predictors(ref = ref, ext_tab = ext, predictor_type = "genotype",
                               af_margin = 0.0)
ok(al_narrow$n_ambiguous_dropped == 1L,
   "even at margin 0 the exact AF ~ 0.5 tie is still undecidable (the distances are EQUAL)")

cat("\n[9] cross-ancestry AF drift must not flip a variant\n")
# The reason the margin exists. AF genuinely differs across ancestries (an AFR target against
# a EUR external is exactly our benchmark), so a palindrome whose frequency merely DRIFTED
# must not be read as an allele swap. Here AF_ref = 0.20, AF_ext = 0.34: drifted, but still
# far closer to 0.20 than to 0.80.
ref_d <- ref[1, , drop = FALSE]
ext_d <- ext[1, , drop = FALSE]; ext_d$AF <- 0.34
al_d <- .align_predictors(ref = ref_d, ext_tab = ext_d, predictor_type = "genotype")
ok(as.numeric(al_d$beta[, 1]) == 0.10 && al_d$n_flipped_external == 0L,
   "a drifted-but-unambiguous AF (0.20 vs 0.34) resolves to the SAME allele, not a flip")
ext_d$AF <- 0.55   # now genuinely between the hypotheses (|0.55-0.20| vs |0.55-0.80|)
al_d2 <- .align_predictors(ref = ref_d, ext_tab = ext_d, predictor_type = "genotype")
ok(al_d2$n_ambiguous_dropped == 1L,
   "an AF that sits BETWEEN the two hypotheses is refused, not guessed")

cat("\n", strrep("-", 62), "\n", sep = "")
if (.fails == 0L) {
  cat(sprintf("strand ambiguity: ALL %d CHECKS PASS\n", .checks))
} else {
  cat(sprintf("strand ambiguity: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
