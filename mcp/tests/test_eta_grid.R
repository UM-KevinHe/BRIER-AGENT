# =============================================================================
# eta_grid_values_of(): the eta grid a selection searched, PER SOURCE.
#
# `sel$eta.lambda` has one ROW per grid point and one `eta_k` COLUMN per external
# model. With M > 1 and multi.method = "ind" the grid is a PRODUCT (two sources and
# 3 rungs is 3 x 3 = 9 rows), and each source's eta has its OWN axis.
#
# The old code `unlist`ed every eta_k column into one flat pool, which threw the axes
# away. The Python boundary check then compared each selected component against the
# largest eta ANYWHERE in the grid, so a source pinned at the top of its own, SHORTER
# axis looked interior and the notice never fired: a truncated model reported as
# converged, which is the one thing that check exists to prevent.
#
#   Rscript mcp/tests/test_eta_grid.R                   (from the repo root)
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

# --- M = 1: a single axis, so a flat numeric vector ---------------------------
sel1 <- list(eta.lambda = data.frame(eta_1 = c(0, 0.1, 1, 10), lambda = 1:4))
g1 <- eta_grid_values_of(sel1)
ok(is.numeric(g1) && !is.list(g1), "M=1: returns a flat numeric vector")
ok(identical(as.numeric(g1), c(0, 0.1, 1, 10)), "M=1: the values are the grid")

# --- M = 2 (ind): one axis PER SOURCE, recovered from the product grid --------
# Deliberately DIFFERENT tops (10 and 100). This is the case the flattened pool
# destroyed: source 2 pinning at 100 is fine, but source 1 pinning at 10 was invisible
# against a global maximum of 100.
sel2 <- list(eta.lambda = data.frame(
  eta_1 = rep(c(0, 1, 10), each = 3),
  eta_2 = rep(c(0, 1, 100), times = 3)
))
g2 <- eta_grid_values_of(sel2)
ok(is.list(g2) && length(g2) == 2, "M=2: returns one grid per external source")
ok(identical(g2[[1]], c(0, 1, 10)), "M=2: source 1's axis is recovered (top 10)")
ok(identical(g2[[2]], c(0, 1, 100)), "M=2: source 2's axis is recovered (top 100)")
ok(!identical(max(g2[[1]]), max(g2[[2]])),
   "M=2: the per-source tops DIFFER, which a flattened pool cannot express")

# --- degenerate inputs --------------------------------------------------------
ok(is.null(eta_grid_values_of(list(eta.lambda = data.frame(lambda = 1)))),
   "no eta_ columns -> NULL")
ok(is.null(eta_grid_values_of(list())), "no eta.lambda at all -> NULL (no error)")

if (.fails == 0L) {
  cat(sprintf("eta grid: ALL %d CHECKS PASS\n", .checks))
} else {
  cat(sprintf("eta grid: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
