#!/usr/bin/env Rscript
# inspect_data.R - describe the structure of an R data file.
#
# Called by mcp/server.py as:
#   Rscript inspect_data.R <input.json> <output.json>
#
# input.json : {"data_path": "..."}
# output.json: {"status": "ok", "data_path": "...",
#               "top_level_names": [...], "structure": {...}}
#          or {"status": "error", "message": "...", "class": "...", "where": "..."}
#
# Only metadata (class, dim, length) is reported - values are never read.
# Safe on very large files (whole-genome genotype matrices, LD matrices).

# Load shared utilities. The path computation matches every other
# dispatcher: file.path(dirname(this script), "_common.R").
.script_dir <- (function() {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) > 0) {
    dirname(normalizePath(sub("^--file=", "", file_arg[1])))
  } else {
    getwd()
  }
})()
source(file.path(.script_dir, "_common.R"))


# --------------------------------------------------------------------------
# Description helper
# --------------------------------------------------------------------------

describe <- function(obj) {
  if (is.matrix(obj)) {
    sprintf("matrix %dx%d (%s)", nrow(obj), ncol(obj), typeof(obj))
  } else if (is.data.frame(obj)) {
    cols <- vapply(obj, function(x) class(x)[1], character(1))
    list(
      ".kind"    = sprintf("data.frame %dx%d", nrow(obj), ncol(obj)),
      ".columns" = as.list(cols)
    )
  } else if (is.list(obj) && !is.null(names(obj)) && all(nzchar(names(obj)))) {
    lapply(obj, describe)
  } else if (is.list(obj)) {
    sprintf("unnamed list (length %d)", length(obj))
  } else if (is.factor(obj)) {
    sprintf("factor (length %d, %d levels)", length(obj), nlevels(obj))
  } else if (is.numeric(obj)) {
    sprintf("numeric (length %d)", length(obj))
  } else if (is.integer(obj)) {
    sprintf("integer (length %d)", length(obj))
  } else if (is.character(obj)) {
    sprintf("character (length %d)", length(obj))
  } else if (is.logical(obj)) {
    sprintf("logical (length %d)", length(obj))
  } else {
    class(obj)[1]
  }
}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  data_path <- io$input$data_path
  env <- load_data_file(data_path)
  structure_desc <- lapply(as.list(env), describe)

  list(
    status = "ok",
    data_path = data_path,
    top_level_names = names(structure_desc),
    structure = structure_desc
  )
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "inspect_data.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
