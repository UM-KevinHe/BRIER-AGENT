# =============================================================================
# as_bool: tolerant boolean coercion (_common.R).
#
# The bug this guards: a reproduce_*.R replays the model's RAW recorded args,
# bypassing the MCP server's type coercion. A small model that emits persist as
# the STRING "TRUE" (not a JSON boolean) reached prep_auto.R, where the old
# `isTRUE(inp$persist)` is FALSE for a string -- so prep_auto silently did NOT
# persist, returned no prepared_path, and the next step died with "either
# data_paths or data_path is required". The live run was fine (the server coerced
# "TRUE" -> TRUE); only the direct-to-R reproduce path broke. This pinned the two
# brier_i_noval cases at -10 (row: reproduce re-runs to the same numbers).
#
#   Rscript mcp/tests/test_as_bool.R                     (from the repo root)
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

# --- THE BUG: a string "TRUE"/"FALSE" from a replayed reproduce script ---------
ok(isTRUE(as_bool("TRUE")),  "string \"TRUE\"  -> TRUE  (the persist bug)")
ok(isTRUE(as_bool("true")),  "string \"true\"  -> TRUE")
ok(isTRUE(as_bool("T")),     "string \"T\"     -> TRUE")
ok(!as_bool("FALSE"),        "string \"FALSE\" -> FALSE")
ok(!as_bool("false"),        "string \"false\" -> FALSE")
ok(!as_bool("F"),            "string \"F\"     -> FALSE")

# --- real logicals pass through unchanged -------------------------------------
ok(isTRUE(as_bool(TRUE)),    "logical TRUE  -> TRUE")
ok(!as_bool(FALSE),          "logical FALSE -> FALSE")

# --- numeric / string-numeric forms -------------------------------------------
ok(isTRUE(as_bool(1)),       "numeric 1   -> TRUE")
ok(!as_bool(0),              "numeric 0   -> FALSE")
ok(isTRUE(as_bool("1")),     "string \"1\" -> TRUE")
ok(!as_bool("0"),            "string \"0\" -> FALSE")

# --- default semantics: NULL / empty / NA / unrecognized keep the caller's default
ok(!as_bool(NULL),                     "NULL        -> default FALSE")
ok(isTRUE(as_bool(NULL, default = TRUE)), "NULL, default TRUE -> TRUE (persist's default)")
ok(!as_bool(""),                       "empty \"\"   -> default FALSE")
ok(!as_bool(NA),                       "NA          -> default FALSE")
ok(!as_bool("maybe"),                  "unrecognized-> default FALSE")
ok(isTRUE(as_bool("maybe", default = TRUE)), "unrecognized, default TRUE -> TRUE")
# a length>1 value is not a scalar boolean -> default
ok(!as_bool(c("TRUE", "FALSE")),       "length-2 vector -> default FALSE")

# --- the exact prep_auto call sites: persist defaults TRUE, standardize FALSE --
persist_from <- function(v) is.null(v) || as_bool(v, default = TRUE)
ok(persist_from(NULL),        "persist: absent -> TRUE (default persist)")
ok(persist_from("TRUE"),      "persist: \"TRUE\" -> TRUE (was silently FALSE before)")
ok(!persist_from("FALSE"),    "persist: \"FALSE\" -> FALSE")
ok(persist_from(TRUE),        "persist: logical TRUE -> TRUE")

if (.fails == 0L) {
  cat(sprintf("as_bool: ALL %d CHECKS PASS\n", .checks))
} else {
  cat(sprintf("as_bool: %d/%d CHECKS FAILED\n", .fails, .checks))
  quit(status = 1L)
}
