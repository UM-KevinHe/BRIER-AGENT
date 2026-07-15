#!/usr/bin/env Rscript
# =============================================================================
# GOLDEN FIXTURE: which allele does a PLINK genotype dosage COUNT?
#
# This is the one error the allele harmonizer structurally CANNOT catch. A panel whose
# coding is uniformly inverted is perfectly SELF-CONSISTENT: it matches by name, it
# aligns by coordinate, the LD looks fine, coverage passes, the fit converges. And every
# single coefficient has the wrong sign. Nothing is inconsistent, so nothing complains.
#
# So the counted allele must be PINNED BY A TEST, not inferred from documentation. It was
# nearly got wrong that way: genio's help says its matrix is "encoded as reference allele
# dosages", which reads as though genio and BEDMatrix count OPPOSITE alleles and the same
# .bed would give X on one machine and 2 - X on another. This fixture proves they AGREE.
#
# GROUND TRUTH comes from a VCF, which states REF/ALT explicitly, so the ALT dosage of
# 0/0, 0/1, 1/1 is known with no interpretation:  0/0 -> 0,  0/1 -> 1,  1/1 -> 2.
#
# Requires plink2 on PATH plus at least one reader. Skips (does not fail) when they are
# absent, so the fast suite still runs on a machine without them.
#
#   Rscript mcp/tests/test_plink_counted_allele.R
# =============================================================================

.fails <- 0L
.checks <- 0L
ok <- function(cond, what) {
  .checks <<- .checks + 1L
  if (!isTRUE(cond)) {
    .fails <<- .fails + 1L
    cat("  FAIL:", what, "\n")
  } else {
    cat("  ok:", what, "\n")
  }
}

if (nchar(Sys.which("plink2")) == 0L) {
  cat("SKIP: plink2 not on PATH (cannot generate ground truth)\n")
  quit(status = 0L)
}

wd <- tempfile("plink_truth"); dir.create(wd)
old <- setwd(wd); on.exit(setwd(old), add = TRUE)

# The genotypes are deliberately ASYMMETRIC, so counting ALT and counting REF give
# DIFFERENT answers and a reader cannot accidentally look correct. And v2's ALT is the
# MAJOR allele (9 of 12 copies), so any tool that reassigns A1 to the MINOR allele would
# visibly swap it. plink2 --make-bed does NOT; plink 1.9 without --keep-allele-order does.
writeLines(c(
  "##fileformat=VCFv4.2",
  "##contig=<ID=1>",
  "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"GT\">",
  paste("#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT",
        "S1", "S2", "S3", "S4", "S5", "S6", sep = "\t"),
  paste("1", "100", "v1", "A", "G", ".", ".", ".", "GT",
        "0/0", "0/0", "0/0", "0/1", "1/1", "1/1", sep = "\t"),
  paste("1", "200", "v2", "C", "T", ".", ".", ".", "GT",
        "1/1", "1/1", "1/1", "1/1", "0/1", "0/0", sep = "\t")
), "t.vcf")

# The KNOWN ALT dosage, read straight off the VCF. variants x samples.
truth_alt <- rbind(v1 = c(0, 0, 0, 1, 2, 2),
                   v2 = c(2, 2, 2, 2, 1, 0))

system2("plink2", c("--vcf", "t.vcf", "--make-bed",  "--out", "t"),
        stdout = FALSE, stderr = FALSE)
system2("plink2", c("--vcf", "t.vcf", "--make-pgen", "--out", "p"),
        stdout = FALSE, stderr = FALSE)

cat("\n[.bim plink2 wrote: CHR SNP cM BP A1 A2]\n")
cat(paste0("  ", readLines("t.bim"), collapse = "\n"), "\n")

# plink2 PRESERVES allele order, so A1 must be the VCF's ALT even for v2, whose ALT is the
# MAJOR allele. If this ever fails, the writer changed and every assertion below is moot.
bim <- utils::read.table("t.bim", stringsAsFactors = FALSE)
ok(identical(bim[[5]], c("G", "T")),
   "plink2 --make-bed preserves allele order: .bim A1 == the VCF ALT")
ok(identical(bim[[6]], c("A", "C")), ".bim A2 == the VCF REF (so the LAST column is REF)")

cat("\n[.bed readers]\n")
if (requireNamespace("genio", quietly = TRUE)) {
  X <- genio::read_plink("t", verbose = FALSE)$X          # loci x indiv
  ok(all(unname(X) == truth_alt), "genio counts A1 (== ALT here), NOT the REF")
  # genio's own help says "reference allele dosages", which is misleading: it names .bim
  # column 5 `alt` and counts THAT. Pinning it here so the wording cannot mislead again.
} else cat("  SKIP genio (not installed)\n")

if (requireNamespace("BEDMatrix", quietly = TRUE)) {
  b <- BEDMatrix::BEDMatrix("t.bed")                      # DEFAULT simple_names = FALSE
  ok(all(unname(t(as.matrix(b))) == truth_alt), "BEDMatrix counts A1 (== ALT here)")
  # The DEFAULT colnames encode the counted allele as `<id>_<A1>`. load_data_files passes
  # simple_names = TRUE and THROWS THIS AWAY, discarding the one thing that makes the file
  # self-describing.
  ok(identical(colnames(b), c("v1_G", "v2_T")),
     "BEDMatrix colname suffix IS the counted allele (id_A1)")
} else cat("  SKIP BEDMatrix (not installed)\n")

cat("\n[.pgen reader]\n")
if (requireNamespace("pgenlibr", quietly = TRUE)) {
  pvar <- pgenlibr::NewPvar("p.pvar")
  pgen <- pgenlibr::NewPgen("p.pgen", pvar = pvar)
  M <- pgenlibr::ReadList(pgen, seq_len(pgenlibr::GetVariantCt(pgen)), meanimpute = FALSE)
  pgenlibr::ClosePgen(pgen); pgenlibr::ClosePvar(pvar)
  ok(all(unname(t(M)) == truth_alt), "pgenlibr counts the ALT (.pvar REF/ALT are explicit)")
} else cat("  SKIP pgenlibr (not installed)\n")

cat("\n", strrep("-", 60), "\n", sep = "")
if (.fails == 0L) {
  cat(sprintf("PLINK counted allele: ALL %d CHECKS PASS\n", .checks))
  cat("  .bed  -> the dosage counts A1 (.bim col 5), whatever A1 happens to be.\n")
  cat("          A1 == ALT only if the writer preserved allele order. NOT SAFE by itself.\n")
  cat("  .pgen -> the dosage counts the ALT, explicitly. SAFE by construction.\n")
} else {
  cat(sprintf("PLINK counted allele: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
