# =============================================================================
# resolve_multi_method(): which multi.method, when the caller does not say.
#
# With M > 1 externals the two methods are STRUCTURALLY different:
#
#   ind       one eta PER SOURCE. eta is a VECTOR and the grid is a PRODUCT, so the fit
#             can lean on a strong external and ignore a weak one.
#   stacking  the sources are COLLAPSED into ONE combined predictor BEFORE transfer, so a
#             single scalar eta must cover them all. It CANNOT weight them differently.
#
# Measured on T2_afr-summary_eur-2ind, whose two externals are deliberately unequal
# (EUR1 = 37 nonzero coefficients, EUR2 = 2), each selected on the AFR validation split:
#
#     stacking  eta 10       val R^2 0.0035   test R^2 0.0054  MSPE 0.9946    25s
#     ind       eta (10,10)  val R^2 0.0073   test R^2 0.0076  MSPE 0.9919   550s
#
# ind wins on validation AND test -- it is not a test-set fluke, the selection preferred
# it. The same ordering was found on T2_multisource.
#
# But ind's grid is n^M and does not scale: at 21 points per axis, M=2 is 441 fits (the
# 550s above) and M=3 is 9261 (~3 hours). So the default is ind up to M=2 and stacking
# from M=3 -- the better method where it is affordable, the affordable one where it is not.
#
#   Rscript mcp/tests/test_multi_method.R          (from the repo root)
# =============================================================================
source("mcp/r_scripts/_common.R")

.checks <- 0L
.fails <- 0L
ok <- function(cond, what) {
  .checks <<- .checks + 1L
  if (isTRUE(cond)) {
    cat(sprintf("  ok  %s\n", what))
  } else {
    .fails <<- .fails + 1L
    cat(sprintf("  FAIL %s\n", what))
  }
}

# --- the M-dependent default ---------------------------------------------------
ok(identical(resolve_multi_method(NULL, 1L), "ind"),
   "M=1 -> ind (with one external the methods coincide anyway)")
ok(identical(resolve_multi_method(NULL, 2L), "ind"),
   "M=2 -> ind (it weights each source separately, and it WINS on val and test)")
ok(identical(resolve_multi_method(NULL, 3L), "stacking"),
   "M=3 -> stacking (ind's n^M grid is ~9000 fits here: not a default)")
ok(identical(resolve_multi_method(NULL, 8L), "stacking"),
   "large M -> stacking")

# "not supplied" has several spellings from a tool layer
ok(identical(resolve_multi_method("auto", 2L), "ind"),
   "an explicit 'auto' resolves by M")
ok(identical(resolve_multi_method("AUTO", 2L), "ind"),
   "'auto' is case-insensitive")
ok(identical(resolve_multi_method("", 2L), "ind"),
   "an empty string means NOT SUPPLIED, not a choice")

# --- THE CALLER ALWAYS WINS ----------------------------------------------------
# The default gets smarter; the knob stays the caller's. A user who has measured their
# own data and wants stacking at M=2 must get stacking, with no second-guessing.
ok(identical(resolve_multi_method("stacking", 2L), "stacking"),
   "an EXPLICIT stacking at M=2 is respected (the rule does not override the caller)")
ok(identical(resolve_multi_method("ind", 8L), "ind"),
   "an EXPLICIT ind at large M is respected, even though it is expensive")
ok(identical(resolve_multi_method("PCA", 4L), "PCA"),
   "any other explicit method passes through untouched")

if (.fails == 0L) {
  cat(sprintf("multi.method: ALL %d CHECKS PASS\n", .checks))
} else {
  cat(sprintf("multi.method: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
