#!/usr/bin/env Rscript
# list_data_directory.R - list R data files in a local directory.
#
# Called by mcp/server.py as:
#   Rscript list_data_directory.R <input.json> <output.json>
#
# input.json : {"dir_path": "...", "recursive": false}
# output.json: {"status": "ok", "dir_path": "...", "files": [...]}
#          or {"status": "error", ...}
#
# Returns only file paths + basenames + sizes; does not load any file
# contents. Filters to data extensions: R objects (.rda/.RData/.rds),
# tabular (.csv/.tsv/.txt/.xlsx/.xls), and genotype binaries
# (.pgen/.bed/.bgen). The intent is to help a user say "what data files
# are in my folder?" without leaving the Claude conversation.

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


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  dir_path <- io$input$dir_path
  recursive <- isTRUE(io$input$recursive)

  if (is.null(dir_path) || !nzchar(dir_path)) {
    stop("dir_path is required", call. = FALSE)
  }
  if (!dir.exists(dir_path)) {
    stop(sprintf("Directory not found: %s", dir_path), call. = FALSE)
  }

  paths <- list.files(
    dir_path,
    pattern = "\\.(rda|RData|rds|csv|tsv|txt|xlsx|xls|pgen|bed|bgen)$",
    recursive = recursive,
    full.names = TRUE,
    ignore.case = TRUE
  )

  files <- lapply(paths, function(p) {
    info <- file.info(p)
    list(
      path = p,
      name = basename(p),
      size_bytes = as.numeric(info$size),
      modified = format(info$mtime, "%Y-%m-%d %H:%M:%S")
    )
  })

  list(
    status = "ok",
    dir_path = dir_path,
    recursive = recursive,
    n_files = length(files),
    files = files
  )
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "list_data_directory.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
