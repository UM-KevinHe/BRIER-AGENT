# =============================================================================
# add_penalty_args / add_penalty_echo: the penalty knobs must be read with EXACT
# key matching, never R's `$` partial matching.
#
# The bug this guards: a fit call that carries `penalty_factor_expr` but NO explicit
# `penalty` key (e.g. a replayed reproduce script, which omits the server's penalty
# default) made `inp$penalty` PARTIAL-MATCH `inp$penalty_factor_expr`, so the penalty
# NAME was read as the penalty.factor EXPRESSION string ("...$penalty_factor") and the
# fit died with "penalty must be one of LASSO, SCAD, MCP". This is the same $-partial
# -match footgun as $X matching $XtX. A brier_full reproduce re-run hit it (row #9).
#
#   Rscript mcp/tests/test_penalty_args.R                (from the repo root)
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

# --- THE BUG: penalty_factor_expr present, penalty absent -> must NOT partial-match --
inp_pf <- list(penalty_factor_expr = "prep_auto_brier_full$penalty_factor")
fa <- tryCatch(add_penalty_args(list(), inp_pf, penalty_factor = NULL),
               error = function(e) conditionMessage(e))
ok(is.list(fa), "penalty_factor_expr + no penalty key -> add_penalty_args does NOT error")
ok(is.list(fa) && is.null(fa$penalty),
   "penalty stays unset (not read from penalty_factor_expr via partial match)")
ech <- add_penalty_echo(list(), inp_pf, NULL)
ok(identical(ech$penalty_used, "LASSO"),
   "penalty_used echoes the LASSO default, not the penalty.factor expression")

# --- an explicit penalty is still honored (exact key present) -----------------------
fa2 <- add_penalty_args(list(), list(penalty = "mcp"), penalty_factor = NULL)
ok(identical(fa2$penalty, "MCP"), "explicit penalty='mcp' normalizes to MCP")
ech2 <- add_penalty_echo(list(), list(penalty = "scad"), NULL)
ok(identical(ech2$penalty_used, "SCAD"), "explicit penalty='scad' echoed as SCAD")

# --- an invalid penalty still errors ------------------------------------------------
err <- tryCatch(add_penalty_args(list(), list(penalty = "ridge"), penalty_factor = NULL),
                error = function(e) conditionMessage(e))
ok(is.character(err) && grepl("LASSO, SCAD, MCP", err),
   "invalid penalty='ridge' still errors with the valid-set message")

# --- alpha / gamma likewise read by exact key ---------------------------------------
fa3 <- add_penalty_args(list(), list(alpha = 0.5, gamma = 3), penalty_factor = NULL)
ok(identical(fa3$alpha, 0.5) && identical(fa3$gamma, 3),
   "explicit alpha / gamma are applied")
# a stray *_expr sibling must not be picked up as alpha/gamma
fa4 <- add_penalty_args(list(), list(alpha_expr = "x", gamma_expr = "y"),
                        penalty_factor = NULL)
ok(is.null(fa4$alpha) && is.null(fa4$gamma),
   "alpha_expr / gamma_expr siblings do NOT partial-match alpha / gamma")

if (.fails == 0L) {
  cat(sprintf("penalty args: ALL %d CHECKS PASS\n", .checks))
} else {
  cat(sprintf("penalty args: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
