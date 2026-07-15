"""Tests for v0.10.2 multi-file data_paths support.

The headline scenario: the height_data case, where the target sumstats +
LD live in one .RData file and the external PRS coefficients live in a
SEPARATE .RData file. Before v0.10.2 this required the user to manually
run an R script to merge the two files. Now it is one brier_s call with
data_paths.

Covers:
  1. load_data_files convention: each file wrapped under basename
  2. Single-file .RData backward compat (legacy bare expressions work)
  3. height_data end-to-end: brier_s -> selection -> predict, multi-file
  4. summarize_fit on a multi-file fit
  5. data_paths threads through every fit/predict/plot tool

Run:
  cd mcp/
  uv run tests/test_v102.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def _rscript():
    return server._find_rscript()


def _make_height_files(workdir, p=150, n=120):
    """Create synthetic height_AFR.RData + height_EUR.RData mimicking the
    real data structure:
      AFR `out`: sumstats (with corr), XtX, X.val, y.val, X.testing,
                 y.testing, keep
      EUR `out`: beta.external (a bare vector), keep
    """
    afr = os.path.join(workdir, "height_AFR.RData")
    eur = os.path.join(workdir, "height_EUR.RData")
    r_code = f"""
    set.seed(7)
    p <- {p}; n <- {n}
    sumstats <- data.frame(
        variable = paste0("rs", 1:p),
        corr  = rnorm(p, 0, 0.05),
        stats = rnorm(p),
        df    = rep(n, p),
        pval  = runif(p),
        n     = rep(n, p)
    )
    XtX <- diag(p)
    X.val     <- matrix(rnorm(n * p), n, p)
    y.val     <- as.matrix(rnorm(n))
    X.testing <- matrix(rnorm(n * p), n, p)
    y.testing <- as.matrix(rnorm(n))
    keep <- 1:p
    out <- list(sumstats=sumstats, XtX=XtX, X.val=X.val, y.val=y.val,
                X.testing=X.testing, y.testing=y.testing, keep=keep)
    save(out, file="{afr}")

    out <- list(beta.external = rnorm(p, 0, 0.1), keep = 1:p)
    save(out, file="{eur}")
    """
    subprocess.run(
        [_rscript(), "--no-save", "--no-restore", "--no-init-file",
         "-e", r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return afr, eur


# --------------------------------------------------------------------------
# Test 1: load_data_files basename convention
# --------------------------------------------------------------------------

def test_basename_convention():
    print("\n--- Test 1: multi-file basename wrapping convention ---")
    workdir = tempfile.mkdtemp(prefix="v102_t1_")
    try:
        afr, eur = _make_height_files(workdir)
        # Use a tiny R snippet that sources _common.R and checks names
        common = os.path.join(str(HERE.parent), "r_scripts", "_common.R")
        r_code = f"""
        source("{common}")
        env <- load_data_files(c("{afr}", "{eur}"))
        cat("has_height_AFR:", exists("height_AFR", envir=env), "\\n")
        cat("has_height_EUR:", exists("height_EUR", envir=env), "\\n")
        afr_obj <- get("height_AFR", envir=env)
        eur_obj <- get("height_EUR", envir=env)
        cat("afr_has_sumstats:", !is.null(afr_obj$sumstats), "\\n")
        cat("eur_has_beta:", !is.null(eur_obj$beta.external), "\\n")
        """
        res = subprocess.run(
            [_rscript(), "--no-save", "--no-restore", "--no-init-file",
             "-e", r_code],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        out = res.stdout
        ok = []
        ok.append(_check("height_AFR wrapped under basename",
                          "has_height_AFR: TRUE" in out))
        ok.append(_check("height_EUR wrapped under basename",
                          "has_height_EUR: TRUE" in out))
        ok.append(_check("AFR$sumstats accessible",
                          "afr_has_sumstats: TRUE" in out))
        ok.append(_check("EUR$beta.external accessible",
                          "eur_has_beta: TRUE" in out))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 2: single-file .RData backward compat
# --------------------------------------------------------------------------

def test_single_file_legacy_compat():
    print("\n--- Test 2: single-file .RData legacy bare-name compat ---")
    workdir = tempfile.mkdtemp(prefix="v102_t2_")
    try:
        rda = os.path.join(workdir, "legacy.RData")
        r_code = f"""
        set.seed(1); X <- matrix(rnorm(100), 10, 10); y <- rnorm(10)
        save(X, y, file="{rda}")
        """
        subprocess.run([_rscript(), "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
        common = os.path.join(str(HERE.parent), "r_scripts", "_common.R")
        r_check = f"""
        source("{common}")
        env <- load_data_files("{rda}")
        cat("bare_X:", exists("X", envir=env), "\\n")
        cat("bare_y:", exists("y", envir=env), "\\n")
        cat("wrapped:", exists("legacy", envir=env), "\\n")
        """
        res = subprocess.run([_rscript(), "--no-save", "--no-restore",
                              "--no-init-file", "-e", r_check],
                             capture_output=True, text=True,
                             stdin=subprocess.DEVNULL)
        out = res.stdout
        ok = []
        ok.append(_check("bare X still accessible (legacy)",
                          "bare_X: TRUE" in out))
        ok.append(_check("bare y still accessible (legacy)",
                          "bare_y: TRUE" in out))
        ok.append(_check("also wrapped under basename",
                          "wrapped: TRUE" in out))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 3: height_data end-to-end, multi-file
# --------------------------------------------------------------------------

def test_height_data_pipeline():
    print("\n--- Test 3: height_data end-to-end (brier_s -> select -> predict) ---")
    workdir = tempfile.mkdtemp(prefix="v102_t3_")
    try:
        afr, eur = _make_height_files(workdir)
        ok = []

        # FIT: one call, two files. This is the line that used to require
        # a manual merge script.
        fit = server.brier_s(
            data_paths=[afr, eur],
            sumstats_expr="height_AFR$sumstats",
            XtX_expr="height_AFR$XtX",
            beta_external_expr="matrix(height_EUR$beta.external, ncol=1)",
            family="gaussian",
            eta_list=[0, 0.5, 1, 2],
        )
        ok.append(_check("brier_s multi-file fit ok",
                          fit.get("status") == "ok",
                          detail=str(fit.get("message", ""))))
        if fit.get("status") != "ok":
            return False
        fit_id = fit["fit_id"]
        ok.append(_check("p detected correctly (150)",
                          fit.get("p") == 150))

        # SELECT: IC-based (Cp) needs TN
        sel = server.brier_s_selection(
            fit_id=fit_id, criteria="Cp", TN=120,
        )
        ok.append(_check("brier_s_selection ok",
                          sel.get("status") == "ok",
                          detail=str(sel.get("message", ""))))
        if sel.get("status") != "ok":
            return all(ok)
        sel_id = sel["selection_id"]

        # PREDICT: on AFR testing set, multi-file (reference both files)
        pred = server.brier_predict(
            selection_id=sel_id,
            data_paths=[afr, eur],
            newx_expr="height_AFR$X.testing",
        )
        ok.append(_check("brier_predict multi-file ok",
                          pred.get("status") == "ok",
                          detail=str(pred.get("message", ""))))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 4: summarize_fit on a multi-file fit
# --------------------------------------------------------------------------

def test_summarize_multifile():
    print("\n--- Test 4: summarize_fit on a multi-file BRIERs fit ---")
    workdir = tempfile.mkdtemp(prefix="v102_t4_")
    try:
        afr, eur = _make_height_files(workdir)
        fit = server.brier_s(
            data_paths=[afr, eur],
            sumstats_expr="height_AFR$sumstats",
            XtX_expr="height_AFR$XtX",
            beta_external_expr="matrix(height_EUR$beta.external, ncol=1)",
            family="gaussian",
            eta_list=[0, 1],
        )
        if fit.get("status") != "ok":
            return _check("fit ok", False, detail=str(fit.get("message")))
        sel = server.brier_s_selection(fit_id=fit["fit_id"],
                                        criteria="Cp", TN=120)
        if sel.get("status") != "ok":
            return _check("selection ok", False,
                          detail=str(sel.get("message")))

        # summarize without test set (just metadata + reproduce.R)
        rep = server.summarize_fit(selection_id=sel["selection_id"])
        ok = []
        ok.append(_check("summarize_fit ok",
                          rep.get("status") == "ok",
                          detail=str(rep.get("message", ""))))
        if rep.get("status") != "ok":
            return False
        ok.append(_check("report HTML exists",
                          os.path.exists(rep.get("report_html_path", ""))))
        ok.append(_check("reproduce.R exists",
                          os.path.exists(rep.get("reproduce_r_path", ""))))
        ok.append(_check("summary tool == brier_s",
                          rep["summary"]["tool"] == "brier_s"))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 5: every fit/predict/plot tool accepts data_paths in its signature
# --------------------------------------------------------------------------

def test_all_tools_accept_data_paths():
    print("\n--- Test 5: data_paths in signature for all data-loading tools ---")
    import inspect
    tools = ["brier_i", "brier_i_cv", "brier_i_selection", "brier_full",
             "brier_full_selection", "brier_s", "brier_s_selection",
             "brier_predict", "brier_evaluate", "cal_ld", "preprocess_i",
             "preprocess_s", "brier_plot_eta", "brier_plot_box",
             "brier_plot_importance", "summarize_fit"]
    ok = []
    for name in tools:
        fn = getattr(server, name)
        # unwrap the FastMCP tool wrapper if present
        target = getattr(fn, "__wrapped__", fn)
        try:
            sig = inspect.signature(target)
            has = "data_paths" in sig.parameters
        except (ValueError, TypeError):
            # Some MCP wrappers aren't introspectable; check source instead
            src = inspect.getsource(target) if hasattr(target, "__code__") else ""
            has = "data_paths" in src
        ok.append(_check(f"{name} accepts data_paths", has))
    return all(ok)


def test_multifile_reproduce_runs():
    print("\n--- Test 6: multi-file reproduce.R loads both files and runs ---")
    workdir = tempfile.mkdtemp(prefix="v102_t6_")
    try:
        afr, eur = _make_height_files(workdir, p=80, n=80)
        fit = server.brier_s(
            data_paths=[afr, eur],
            sumstats_expr="height_AFR$sumstats",
            XtX_expr="height_AFR$XtX",
            beta_external_expr="matrix(height_EUR$beta.external, ncol=1)",
            family="gaussian", eta_list=[0, 1],
        )
        if fit.get("status") != "ok":
            return _check("fit ok", False, detail=str(fit.get("message")))
        sel = server.brier_s_selection(fit_id=fit["fit_id"],
                                        criteria="Cp", TN=80)
        if sel.get("status") != "ok":
            return _check("selection ok", False,
                          detail=str(sel.get("message")))
        rep = server.summarize_fit(selection_id=sel["selection_id"])
        ok = []
        ok.append(_check("summarize_fit ok", rep.get("status") == "ok"))
        if rep.get("status") != "ok":
            return False
        repro = rep["reproduce_r_path"]
        with open(repro) as f:
            content = f.read()
        ok.append(_check("reproduce.R lists both files",
                          "height_AFR" in content and "height_EUR" in content))
        ok.append(_check("reproduce.R uses data_paths loop",
                          "data_paths <- c(" in content))
        ok.append(_check("reproduce.R recovers XtX expr (not placeholder)",
                          "XtX <- height_AFR$XtX" in content))
        ok.append(_check("reproduce.R has no empty data_path",
                          'data_path <- ""' not in content))
        # Actually run it
        res = subprocess.run(
            [_rscript(), "--no-save", "--no-restore", "--no-init-file", repro],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=120,
        )
        ok.append(_check("reproduce.R runs end-to-end (rc=0)",
                          res.returncode == 0,
                          detail=f"stderr={res.stderr[-200:]}"))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_split_file_guidance_present():
    print("\n--- Test 7: wizard + docstrings advertise multi-file for split data ---")
    ok = []
    # Wizard ai_instructions
    r = server.start_analysis()
    ai = r.get("ai_instructions", "")
    ok.append(_check("ai_instructions has SPLIT-FILE section",
                      "SPLIT-FILE DATA" in ai))
    ok.append(_check("ai_instructions discourages merge script",
                      "merge script" in ai.lower()))
    ok.append(_check("ai_instructions shows data_paths example",
                      "data_paths=" in ai))
    # brier_s docstring
    import inspect
    bs = inspect.getsource(getattr(server.brier_s, "__wrapped__",
                                    server.brier_s))
    ok.append(_check("brier_s docstring advertises data_paths for split",
                      "SPLIT ACROSS FILES" in bs))
    bi = inspect.getsource(getattr(server.brier_i, "__wrapped__",
                                    server.brier_i))
    ok.append(_check("brier_i docstring advertises data_paths for split",
                      "SPLIT ACROSS FILES" in bi))
    return all(ok)


def test_plot_tools_multifile():
    print("\n--- Test 8: plot tools accept multi-file data_paths ---")
    workdir = tempfile.mkdtemp(prefix="v102_t8_")
    try:
        afr, eur = _make_height_files(workdir, p=100, n=90)
        fit = server.brier_s(
            data_paths=[afr, eur],
            sumstats_expr="height_AFR$sumstats",
            XtX_expr="height_AFR$XtX",
            beta_external_expr="matrix(height_EUR$beta.external, ncol=1)",
            family="gaussian", eta_list=[0, 1],
        )
        sel = server.brier_s_selection(fit_id=fit["fit_id"],
                                        criteria="Cp", TN=90)
        ok = []
        # plot_eta via multi-file (this used to error: "data_path is required")
        r = server.brier_plot_eta(
            selection_id=sel["selection_id"],
            data_paths=[afr, eur],
            newx_expr="height_AFR$X.testing",
            newy_expr="height_AFR$y.testing",
            criteria="gaussian.mspe",
        )
        ok.append(_check("plot_eta multi-file ok",
                          r.get("status") == "ok",
                          detail=str(r.get("message", ""))))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_highp_guard():
    print("\n--- Test 9: summarize_fit high-p guard skips bootstrap plots ---")
    workdir = tempfile.mkdtemp(prefix="v102_t9_")
    try:
        afr, eur = _make_height_files(workdir, p=300, n=90)
        fit = server.brier_s(
            data_paths=[afr, eur],
            sumstats_expr="height_AFR$sumstats",
            XtX_expr="height_AFR$XtX",
            beta_external_expr="matrix(height_EUR$beta.external, ncol=1)",
            family="gaussian", eta_list=[0, 1],
        )
        sel = server.brier_s_selection(fit_id=fit["fit_id"],
                                        criteria="Cp", TN=90)
        # p=300; set threshold to 200 so guard fires, keep bootstrap_n tiny
        r = server.summarize_fit(
            selection_id=sel["selection_id"],
            data_paths=[afr, eur],
            newx_expr="height_AFR$X.testing",
            newy_expr="height_AFR$y.testing",
            criteria="gaussian.mspe",
            bootstrap_plot_max_p=200,
            bootstrap_n=10,
        )
        ok = []
        ok.append(_check("summarize_fit ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        # Selection + eta plots stay (cheap); box/importance skipped under
        # the high-p guard. Selection plot is always-on as of v0.10.3.
        ok.append(_check("selection + eta plots included (2 plots)",
                          r["summary"]["plots_included"] == 2,
                          detail=f"got {r['summary']['plots_included']}"))
        ok.append(_check("high-p notice emitted",
                          "_notice" in r and "exceeds" in r["_notice"]))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    print("BRIER MCP v0.10.2 multi-file support test suite")
    all_pass = True
    all_pass &= test_basename_convention()
    all_pass &= test_single_file_legacy_compat()
    all_pass &= test_height_data_pipeline()
    all_pass &= test_summarize_multifile()
    all_pass &= test_all_tools_accept_data_paths()
    all_pass &= test_multifile_reproduce_runs()
    all_pass &= test_split_file_guidance_present()
    all_pass &= test_plot_tools_multifile()
    all_pass &= test_highp_guard()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
