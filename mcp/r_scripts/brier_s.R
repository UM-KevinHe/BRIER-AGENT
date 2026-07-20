#!/usr/bin/env Rscript
# brier_s.R - fit BRIERs() with summary-statistics target data.
#
# Called by mcp/server.py as:
#   Rscript brier_s.R <input.json> <output.json>
#
# Distinct from brier_i / brier_full:
#   * Target is summary statistics (corr + LD matrix), NOT individual X/y.
#   * beta.external has NO intercept row (asymmetric with BRIERi which
#     requires one); shape is p x M, not (p+1) x M.
#   * Returns coefficients on the STANDARDIZED scale.
#
# input.json: {
#   data_path:           "/path/to/data.rds",     # required
#   sumstats_expr:       "sumstats",              # required (must have $corr column)
#   beta_external_expr:  "beta.external",         # required (p x M)
#   family:              "gaussian" | ...,         # required
#
#   # LD matrix: PREFERRED to pass ld_id (auto-subset by $nz)
#   ld_id:               "ld_xxx",                # OR
#   XtX_expr:            "ld$XtX",                # explicit (caller subsets manually)
#
#   # Other args
#   multi_method:        "stacking" | "PCA" | "ind",
#   eta_list:            [...],
#   penalty_factor_expr: "...",           # optional (length p = post-LD-subset)
#   alpha:               0.5,             # optional; (0,1], default 1
#   penalty:             "MCP",           # optional; LASSO|SCAD|MCP
#   gamma:               3,               # optional; SCAD/MCP concavity
#   trace:               false
# }
#
# output.json: {
#   status: "ok",
#   fit_id, fit_path,
#   family, p, M_external, eta_list_used,
#   timing, ld_id_used (if any),
#   _notice_*, _followup_*
# } or {status: "error", ...}

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

suppressPackageStartupMessages({
  library(BRIER)
  library(Matrix)
})


.cache_root_fits <- function() {
  base <- Sys.getenv("XDG_CACHE_HOME", unset = NA)
  if (is.na(base) || !nzchar(base)) {
    base <- if (.Platform$OS.type == "windows") {
      Sys.getenv("LOCALAPPDATA",
                 unset = file.path(Sys.getenv("HOME"), "AppData", "Local"))
    } else {
      file.path(Sys.getenv("HOME"), ".cache")
    }
  }
  d <- file.path(base, "brier-mcp", "fits")
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

.cache_root_ld <- function() {
  base <- Sys.getenv("XDG_CACHE_HOME", unset = NA)
  if (is.na(base) || !nzchar(base)) {
    base <- if (.Platform$OS.type == "windows") {
      Sys.getenv("LOCALAPPDATA",
                 unset = file.path(Sys.getenv("HOME"), "AppData", "Local"))
    } else {
      file.path(Sys.getenv("HOME"), ".cache")
    }
  }
  file.path(base, "brier-mcp", "ld")
}

.generate_fit_id <- function() {
  ts <- format(Sys.time(), "%Y%m%d_%H%M%S")
  suffix <- paste(sample(c(0:9, letters), 6, replace = TRUE), collapse = "")
  paste0("brier_s_", ts, "_", suffix)
}


args <- commandArgs(trailingOnly = TRUE)
io <- read_input(args)

result <- tryCatch({
  inp <- io$input

  if (is.null(inp$data_paths) && is.null(inp$data_path)) {
    stop("either data_paths or data_path is required", call. = FALSE)
  }
  if (is.null(inp$sumstats_expr)) {
    stop("sumstats_expr is required", call. = FALSE)
  }
  if (is.null(inp$beta_external_expr)) {
    stop("beta_external_expr is required", call. = FALSE)
  }

  family_was_supplied <- !is.null(inp$family) && nzchar(inp$family)

  # The REQUEST; resolved below once beta_external is loaded and M is known.
  multi_method_requested <- inp$multi_method

  # Resolve XtX. Two paths: ld_id (preferred, auto-subset) or XtX_expr (raw).
  # v0.11: multi-file support via load_data_files()
  resolved_paths <- resolve_data_paths_input(inp)
  env <- load_data_files(resolved_paths)

  # Family: explicit arg wins; else recover from the prepared object (prep_auto records
  # prepared$family), so an auto-detected binomial outcome is fit as logistic; else gaussian.
  family <- if (family_was_supplied) inp$family else family_from_prepared(env)
  family_source <- if (family_was_supplied) "supplied" else if (!is.null(family)) "prepared" else "default"
  if (is.null(family)) family <- "gaussian"
  sumstats <- safe_eval(inp$sumstats_expr, env)
  beta_external <- safe_eval(inp$beta_external_expr, env)

  if (is.null(sumstats)) stop("sumstats_expr resolved to NULL", call. = FALSE)
  if (is.null(beta_external)) {
    stop("beta_external_expr resolved to NULL", call. = FALSE)
  }
  if (!is.matrix(beta_external)) beta_external <- as.matrix(beta_external)
  if (!"corr" %in% colnames(sumstats)) {
    stop(paste(
      "sumstats must have a 'corr' column. Build with p2cor(pval, n,",
      "sign = sign(stats)) if you only have p-values."
    ), call. = FALSE)
  }

  XtX <- NULL
  ld_id_used <- NULL
  used_ld_subset <- FALSE

  xtx_autofilled <- NULL
  ld_id_ignored <- NULL
  # A bogus ld_id must NOT be fatal when a real LD is already in hand. On a real run
  # the 7B invented an ld_id -- literally the string "get_ldb(ancestry='AFR',
  # build='hg38')" -- while the prepared object it had just built carried a perfectly
  # good $XtX. Hard-stopping there dead-ended the run (and the model then repeated the
  # identical call until the guard aborted). Treat an unresolvable ld_id as ABSENT and
  # fall through to XtX_expr / the prepared object's $XtX; only fail if no LD can be
  # found by ANY route.
  ld_id_ok <- FALSE
  if (!is.null(inp$ld_id) && nzchar(inp$ld_id)) {
    ld_path <- file.path(.cache_root_ld(), paste0(inp$ld_id, ".rds"))
    ld_id_ok <- file.exists(ld_path)
    if (!ld_id_ok) {
      ld_id_ignored <- sprintf(
        paste0("ld_id '%s' does not name a cached LD; ignoring it and using the LD ",
               "carried by the prepared object instead"), inp$ld_id)
    }
  }
  if (ld_id_ok) {
    ld <- readRDS(ld_path)
    XtX <- ld$XtX
    ld_id_used <- inp$ld_id

    # Auto-subset by $nz. This is the silent-failure trap from llms.txt
    # that we systematically remove via the ld_id workflow.
    if (!is.null(ld$nz)) {
      if (nrow(sumstats) > length(ld$nz)) {
        sumstats <- sumstats[ld$nz, , drop = FALSE]
        used_ld_subset <- TRUE
      }
      if (nrow(beta_external) > length(ld$nz)) {
        beta_external <- beta_external[ld$nz, , drop = FALSE]
        used_ld_subset <- TRUE
      }
    }
  } else if (!is.null(inp$XtX_expr) && nzchar(inp$XtX_expr)) {
    XtX <- safe_eval(inp$XtX_expr, env)
    if (is.null(XtX)) stop("XtX_expr resolved to NULL", call. = FALSE)
  } else {
    # Fallback for the standard prep_auto path: the persisted prepared object
    # already carries the LD as $XtX. A small model sometimes omits XtX_expr;
    # rather than fail, recover $XtX from the loaded object (bound from data_path)
    # so the fit still runs. Only reached when neither ld_id nor XtX_expr is given.
    XtX <- NULL
    for (v in ls(env)) {
      obj <- tryCatch(get(v, envir = env), error = function(e) NULL)
      if (is.list(obj) && !is.null(obj[["XtX"]])) {
        XtX <- obj[["XtX"]]
        xtx_autofilled <- sprintf("%s$XtX", v)
        break
      }
    }
    if (is.null(XtX)) {
      stop(paste(
        "brier_s needs the LD matrix and none was passed. If you assembled with",
        "prep_auto, pass XtX_expr from the returned expr_hints (the prepared",
        "object already contains it, e.g. XtX_expr='prepared$XtX') -- do NOT drop",
        "that hint. Otherwise pass ld_id from a prior cal_ld call. One of XtX_expr",
        "or ld_id is required."
      ), call. = FALSE)
    }
  }

  # Shape sanity: sumstats rows == XtX rows == beta.external rows.
  if (nrow(sumstats) != nrow(XtX) || nrow(beta_external) != nrow(XtX)) {
    stop(sprintf(paste(
      "Row count mismatch after any LD-subset: sumstats has %d rows,",
      "XtX has %d rows, beta.external has %d rows. All three must match."
    ), nrow(sumstats), nrow(XtX), nrow(beta_external)), call. = FALSE)
  }

  # THE PREPARED-OBJECT CONTRACT. The count check above is necessary and NOT
  # sufficient: three objects with matching row counts in DIFFERENT ORDER pass it, and
  # every coefficient then sits on the wrong predictor, fits, and reports a number. Only
  # NAMES can prove alignment, so the contract demands them. See _common.R.
  stop_on_contract_violations(
    validate_fit_inputs("brier_s", sumstats = sumstats, XtX = XtX,
                        beta_external = beta_external, family = family),
    "brier_s"
  )

  # BRIERs takes XtX as Matrix-like; pass through as-is (BRIERs internally
  # coerces to sparse).

  # Optional penalty.factor. For BRIERs it is length p = the post-LD-subset
  # variant count (nrow(sumstats)); validate so a stale full-length vector fails
  # loudly instead of silently misaligning.
  penalty_factor <- safe_eval(inp$penalty_factor_expr, env)
  # An all-1 penalty.factor is the DEFAULT (every predictor penalized); treat it
  # as omitted regardless of length, so a small model that passes rep(1, k) with a
  # wrong k (a common hallucination) does not trip the length check.
  if (!is.null(penalty_factor) && all(penalty_factor == 1)) {
    penalty_factor <- NULL
  }
  if (!is.null(penalty_factor) && length(penalty_factor) != nrow(sumstats)) {
    stop(sprintf(paste(
      "penalty.factor has length %d but BRIERs needs length p = %d (the",
      "variant count after any LD $nz subset). Size it to the fitted variants."
    ), length(penalty_factor), nrow(sumstats)), call. = FALSE)
  }

  # multi.method: M is known now. ind up to M=2 (weights each source separately, and
  # wins on val AND test), stacking from M=3 (ind's grid is n^M). Explicit always wins.
  multi_method <- resolve_multi_method(multi_method_requested, ncol(beta_external))

  fit_args <- list(
    sumstats = sumstats,
    XtX = XtX,
    family = family,
    beta.external = beta_external,
    multi.method = multi_method,
    trace = isTRUE(inp$trace),
    parallel = FALSE,
    ncores = 1
  )
  if (!is.null(inp$eta_list)) {
    fit_args$eta.list <- as.numeric(unlist(inp$eta_list))
  } else {
    fit_args$eta.list <- c(0, exp(seq(log(0.1), log(10), length.out = 20)))
  }

  # Optional penalty knobs (alpha / penalty / gamma / penalty.factor); each
  # defaults to BRIER's own default when the caller omits it.
  fit_args <- add_penalty_args(fit_args, inp, penalty_factor)

  # M=1 auto-substitute. With one external, stacking and PCA collapse
  # to ind mathematically. Substitute silently for robustness.
  m_one_auto_ind_applied <- FALSE
  if (ncol(beta_external) == 1L &&
      (identical(multi_method, "stacking") ||
       identical(multi_method, "PCA"))) {
    fit_args$multi.method <- "ind"
    m_one_auto_ind_applied <- TRUE
  }

  t0 <- Sys.time()
  fit <- do.call(BRIER::BRIERs, fit_args)
  t1 <- Sys.time()
  fit_seconds <- as.numeric(difftime(t1, t0, units = "secs"))

  # Training N for IC-selection TN defaulting: prefer an N column on the sumstats,
  # else the prep_auto-carried prepared$n_train (preprocessS drops N).
  n_train_meta <- if ("N" %in% colnames(sumstats)) {
    suppressWarnings(as.numeric(stats::median(sumstats$N, na.rm = TRUE)))
  } else {
    NA_real_
  }
  if (!is.finite(n_train_meta)) {
    for (.v in ls(env)) {
      .obj <- tryCatch(get(.v, envir = env), error = function(e) NULL)
      .nt <- if (is.list(.obj)) suppressWarnings(as.numeric(.obj[["n_train"]])) else NULL
      if (length(.nt) >= 1L && is.finite(.nt[1L]) && .nt[1L] > 0) {
        n_train_meta <- .nt[1L]
        break
      }
    }
  }

  cache_dir <- .cache_root_fits()
  fit_id <- .generate_fit_id()
  fit_path <- file.path(cache_dir, paste0(fit_id, ".rds"))

  saveRDS(
    list(
      fit = fit,
      meta = list(
        family = family,
        # Training sample size, so brier_s_selection can DEFAULT TN for an IC
        # criterion (Cp/GIC) when the caller omits it. preprocessS drops the N
        # column, so prep_auto carries it as prepared$n_train; fall back to that.
        n_train = n_train_meta,
        multi_method = multi_method,
        data_path = inp$data_path,
        data_paths = resolved_paths,
        sumstats_expr = inp$sumstats_expr,
        beta_external_expr = inp$beta_external_expr,
        XtX_expr = inp$XtX_expr,
        ld_id_used = ld_id_used,
        eta_list = inp$eta_list,
        tool = "brier_s",
        # BRIERs works from correlations and an LD matrix, so its coefficients are ALWAYS
        # on the STANDARDIZED scale: there is no raw-scale BRIERs fit. That has been the
        # subject of an always-on warning that fired whether or not anything was wrong.
        # Recording it as the fit's regime turns that warning into a real check: a raw
        # X_val or a raw gaussian y_val is now REFUSED at selection/evaluate instead of
        # quietly producing an MSPE of ~mean(y^2).
        x_scale_regime = "standardized",
        y_scale_regime = if (identical(family, "gaussian")) "standardized" else NULL,
        prep_session_ids = extract_prep_session_ids(resolved_paths)
      )
    ),
    file = fit_path
  )

  eta_used <- tryCatch(fit$eta.list, error = function(e) NULL)
  eta_serialized <- if (is.null(eta_used)) {
    NULL
  } else if (is.list(eta_used) && length(eta_used) == 1) {
    as.numeric(eta_used[[1]])
  } else if (is.list(eta_used)) {
    lapply(eta_used, as.numeric)
  } else {
    as.numeric(eta_used)
  }

  M_external <- ncol(beta_external)

  out <- list(
    status = "ok",
    fit_id = fit_id,
    fit_path = fit_path,
    family = family,
    p = nrow(XtX),
    M_external = M_external,
    eta_list_used = eta_serialized,
    multi_method_used = multi_method,
    ld_id_used = ld_id_used,
    timing = list(fit_seconds = round(fit_seconds, 3))
  )
  out <- add_penalty_echo(out, inp, penalty_factor)

  if (identical(family_source, "prepared")) {
    out$`_notice_family_from_prepared` <- sprintf(paste(
      "Family was not supplied; recovered family='%s' from the prepared object.",
      "Select and evaluate on the matching %s metrics."), family,
      if (identical(family, "binomial")) "binomial (binomial.dev / binomial.auc)"
      else if (identical(family, "poisson")) "poisson (poisson.dev)" else "gaussian")
  } else if (identical(family_source, "default")) {
    out$`_notice_family_default` <- paste(
      "Family was not explicitly supplied; BRIERs used the gaussian default.",
      "If the outcome is binary or count, refit with family='binomial' or",
      "'poisson'."
    )
  }

  # Always-on standardization warning. This is THE big BRIERs pitfall.
  out$`_notice_brier_s_standardize` <- paste(
    "BRIERs returns coefficients on the STANDARDIZED scale. When you call",
    "brier_s_selection with a validation-set criterion (gaussian.mspe,",
    "binomial.dev, etc.), the X.val matrix you pass MUST be column-",
    "standardized (e.g., via standardize_X). Pass y.val standardized for",
    "family='gaussian' only; for family='binomial' or 'poisson' pass raw",
    "y.val. See llms.txt 'BRIERs() returns standardized coefficients'."
  )

  if (!is.null(xtx_autofilled)) {
    out$`_notice_xtx_autofilled` <- paste(
      "XtX_expr was not supplied; recovered the LD matrix from the prepared",
      sprintf("object as %s.", xtx_autofilled),
      "For clarity, pass XtX_expr from prep_auto's expr_hints next time."
    )
  }

  if (!is.null(ld_id_ignored)) {
    out$`_notice_ld_id_ignored` <- paste(
      ld_id_ignored,
      "Do not invent an ld_id: pass XtX_expr from prep_auto's expr_hints, or an",
      "ld_id returned by an actual cal_ld call."
    )
  }

  if (used_ld_subset) {
    out$`_notice_ld_subset_applied` <- paste(
      "ld_id was used; sumstats and beta.external were automatically",
      "subset by the LD matrix's $nz indices before fitting. The fit's",
      "p =", nrow(XtX), "reflects the retained variants, which may be",
      "smaller than the input sumstats / beta.external row counts."
    )
  }

  if (identical(multi_method, "ind") && M_external >= 5) {
    out$`_notice_multi_ind_slow` <- sprintf(
      paste(
        "multi.method='ind' tunes an independent eta per external model.",
        "With M=%d this grid is multiplicative; expect very slow fits.",
        "Consider multi.method='stacking'."
      ), M_external)
  }

  if (m_one_auto_ind_applied) {
    out$`_notice_m_one_auto_ind` <- paste(
      "Detected M=1 (a single external) with multi_method=",
      sprintf("'%s'.", multi_method),
      "With one external, stacking and PCA collapse mathematically to",
      "ind. Silently switched to multi_method='ind'; the result is",
      "identical. Pass multi_method='ind' explicitly to skip this notice."
    )
    out$multi_method_used <- "ind"
  }

  out$`_followup_offer_selection` <- paste(
    "This is the raw fit across the (eta, lambda) grid. To select the",
    sprintf("optimal hyperparameters, call brier_s_selection with fit_id='%s'", fit_id),
    "and one of: (a) an IC criterion like 'Cp', 'GIC', 'pseu.val';",
    "(b) a family-specific validation metric ('gaussian.mspe', etc.)",
    "plus X_val_expr / y_val_expr / data_path with STANDARDIZED X.val and",
    "(for gaussian only) standardized y.val."
  )

  out
}, error = function(err) {
  make_error(
    msg = conditionMessage(err),
    where = "brier_s.R",
    class = class(err)[1]
  )
})

write_output(result, io$output_path)
