"""Tests for v0.10 summarize_fit comprehensive report tool.

Covers:
  1. summarize_fit on BRIERi (M=1) WITHOUT test set: 7 sections present,
     reproduce.R generated, no embedded plots
  2. summarize_fit on BRIERi WITH test set: 3 embedded plots, all sections
  3. reproduce.R is actually runnable (sources cleanly in R)
  4. summarize_fit on BRIERfull: template handles cohort_expr, selection
     block emitted as commented-out
  5. Missing selection_id -> clean error
  6. HTML structure: all 7 sections, base64 PNGs, code block visible

Run:
  cd mcp/
  uv run tests/test_v100.py
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


def _stage_brier_i(workdir):
    """Stage Data_BRIERi and make a fit+selection. Returns selection_id."""
    rds_path = os.path.join(workdir, "bi.rds")
    rscript = server._find_rscript()
    subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e",
         "suppressPackageStartupMessages(library(BRIER)); "
         f"data(Data_BRIERi); saveRDS(Data_BRIERi, '{rds_path}')"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    fit = server.brier_i(
        data_path=rds_path,
        X_expr="bi$target$train$X",
        y_expr="bi$target$train$y",
        beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
        family="gaussian",
        eta_list=[0, 0.5, 1, 2, 5],
    )
    assert fit["status"] == "ok", fit
    sel = server.brier_i_selection(fit_id=fit["fit_id"], criteria="BIC")
    assert sel["status"] == "ok", sel
    return rds_path, sel["selection_id"]


def _stage_brier_full(workdir):
    rds_path = os.path.join(workdir, "bf.rds")
    rscript = server._find_rscript()
    subprocess.run([
        rscript, "--no-save", "--no-restore", "--no-init-file", "-e",
        "suppressPackageStartupMessages(library(BRIER)); "
        "data(Data_BRIERfull); d <- Data_BRIERfull; "
        "X_full <- rbind(d$target$train$X, d$external1$train$X); "
        "y_full <- c(d$target$train$y, d$external1$train$y); "
        "cohort <- c(rep(0L, nrow(d$target$train$X)), "
        "            rep(1L, nrow(d$external1$train$X))); "
        "d$X_full <- X_full; d$y_full <- y_full; d$cohort <- cohort; "
        f"saveRDS(d, '{rds_path}')"
    ], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    fit = server.brier_full(
        data_path=rds_path,
        X_expr="bf$X_full", y_expr="bf$y_full",
        cohort_expr="bf$cohort",
        family="gaussian", eta_list=[0, 0.5, 1, 2],
    )
    assert fit["status"] == "ok", fit
    sel = server.brier_full_selection(
        fit_id=fit["fit_id"], criteria="gaussian.mspe",
        data_path=rds_path,
        X_val_expr="bf$target$validation$X",
        y_val_expr="bf$target$validation$y",
    )
    assert sel["status"] == "ok", sel
    return rds_path, sel["selection_id"]


# --------------------------------------------------------------------------
# Test 1: BRIERi without test set
# --------------------------------------------------------------------------

def test_summarize_brier_i_no_test_set():
    print("\n--- Test 1: summarize_fit BRIERi without test set ---")
    workdir = tempfile.mkdtemp(prefix="brier_v100_t1_")
    try:
        _, sel_id = _stage_brier_i(workdir)
        r = server.summarize_fit(selection_id=sel_id)
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok",
                          detail=str(r.get("message", ""))))
        if r.get("status") != "ok":
            return False
        ok.append(_check("report_id present", bool(r.get("report_id"))))
        ok.append(_check("HTML file exists",
                          os.path.exists(r.get("report_html_path", ""))))
        ok.append(_check("reproduce.R exists",
                          os.path.exists(r.get("reproduce_r_path", ""))))
        ok.append(_check("plots_included == 1 (selection plot always-on, no test set)",
                          r["summary"]["plots_included"] == 1))
        ok.append(_check("_notice_no_plots present",
                          "_notice_no_plots" in r))
        ok.append(_check("summary.tool == brier_i",
                          r["summary"]["tool"] == "brier_i"))
        ok.append(_check("summary.best_eta populated",
                          r["summary"]["best_eta"] is not None))
        ok.append(_check("summary.criteria == BIC",
                          r["summary"]["criteria"] == "BIC"))
        # HTML structure: all 7 sections
        with open(r["report_html_path"]) as f:
            html = f.read()
        for section in ["Header", "Data context", "Fitting summary",
                         "Selection summary", "Plots", "Reproducibility",
                         "MCP metadata"]:
            ok.append(_check(f"HTML has section: {section}",
                              f"<h2>{section}</h2>" in html))
        ok.append(_check("HTML has 1 plot image (selection plot, no test set)",
                          html.count("data:image/png;base64") == 1))
        ok.append(_check("HTML mentions 'not available' for missing data",
                          "not available" in html.lower()))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 2: BRIERi with test set (embeds 3 plots)
# --------------------------------------------------------------------------

def test_summarize_brier_i_with_plots():
    print("\n--- Test 2: summarize_fit BRIERi with test set (embeds plots) ---")
    workdir = tempfile.mkdtemp(prefix="brier_v100_t2_")
    try:
        rds_path, sel_id = _stage_brier_i(workdir)
        r = server.summarize_fit(
            selection_id=sel_id,
            data_path=rds_path,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="gaussian.mspe",
            bootstrap_n=20,
        )
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("plots_included == 4 (selection + eta + box + importance)",
                          r["summary"]["plots_included"] == 4))
        with open(r["report_html_path"]) as f:
            html = f.read()
        ok.append(_check("HTML has 4 base64 PNG embeds (selection + eta + box + importance)",
                          html.count("data:image/png;base64") == 4))
        # Verify content of the 3 figures
        ok.append(_check("HTML mentions 'Eta performance'",
                          "Eta performance" in html))
        ok.append(_check("HTML mentions 'Bootstrap comparison'",
                          "Bootstrap comparison" in html))
        ok.append(_check("HTML mentions 'Variable importance'",
                          "Variable importance" in html))
        # No partial-args notice
        ok.append(_check("no _notice_no_plots in response",
                          "_notice_no_plots" not in r))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 3: reproduce.R is actually runnable
# --------------------------------------------------------------------------

def test_reproduce_script_runnable():
    print("\n--- Test 3: reproduce.R is actually runnable ---")
    workdir = tempfile.mkdtemp(prefix="brier_v100_t3_")
    try:
        _, sel_id = _stage_brier_i(workdir)
        r = server.summarize_fit(selection_id=sel_id)
        ok = []
        repro_path = r["reproduce_r_path"]
        ok.append(_check("reproduce.R exists",
                          os.path.exists(repro_path)))
        if not os.path.exists(repro_path):
            return False
        # Verify script structure
        with open(repro_path) as f:
            script = f.read()
        ok.append(_check("script uses library(BRIER)",
                          "library(BRIER)" in script))
        ok.append(_check("script uses assign() for data convention",
                          "assign(.top_name" in script or
                          "assign(.nm" in script))
        ok.append(_check("script calls BRIERi()", "BRIERi(" in script))
        ok.append(_check("script calls BRIERi.selection()",
                          "BRIERi.selection(" in script))
        # Actually run it
        rscript = server._find_rscript()
        result = subprocess.run(
            [rscript, "--no-save", "--no-restore",
             "--no-init-file", repro_path],
            capture_output=True, text=True, timeout=120,
            stdin=subprocess.DEVNULL,
        )
        ok.append(_check("script runs successfully (rc=0)",
                          result.returncode == 0,
                          detail=f"stderr={result.stderr[-200:]}"))
        ok.append(_check("script output contains 'Selected eta:'",
                          "Selected eta:" in result.stdout))
        ok.append(_check("script output contains 'Selected lambda:'",
                          "Selected lambda:" in result.stdout))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 4: BRIERfull family template
# --------------------------------------------------------------------------

def test_summarize_brier_full():
    print("\n--- Test 4: summarize_fit BRIERfull (commented-out selection block) ---")
    workdir = tempfile.mkdtemp(prefix="brier_v100_t4_")
    try:
        _, sel_id = _stage_brier_full(workdir)
        r = server.summarize_fit(selection_id=sel_id)
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok",
                          detail=str(r.get("message", ""))))
        if r.get("status") != "ok":
            return False
        ok.append(_check("summary.tool == brier_full",
                          r["summary"]["tool"] == "brier_full"))
        ok.append(_check("summary.best_eta populated",
                          r["summary"]["best_eta"] is not None))
        # reproduce.R should have BRIERfull-specific structure
        with open(r["reproduce_r_path"]) as f:
            script = f.read()
        ok.append(_check("script calls BRIERfull(",
                          "BRIERfull(" in script))
        ok.append(_check("script handles cohort vector",
                          "cohort" in script))
        ok.append(_check("script comments out selection (no X.val recoverable)",
                          "# selection <- BRIERfull.selection" in script))
        # Verify script runs (selection is commented out, but the fit
        # itself should still complete)
        rscript = server._find_rscript()
        result = subprocess.run(
            [rscript, "--no-save", "--no-restore",
             "--no-init-file", r["reproduce_r_path"]],
            capture_output=True, text=True, timeout=300,
            stdin=subprocess.DEVNULL,
        )
        ok.append(_check("BRIERfull script runs (rc=0)",
                          result.returncode == 0,
                          detail=f"stderr={result.stderr[-200:]}"))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 5: missing selection_id returns clean error
# --------------------------------------------------------------------------

def test_missing_selection_id():
    print("\n--- Test 5: missing selection_id -> clean error ---")
    r = server.summarize_fit(selection_id="does_not_exist_abc")
    ok = []
    ok.append(_check("returns error status",
                      r.get("status") == "error"))
    ok.append(_check("error message mentions selection_id",
                      "selection_id" in r.get("message", "").lower() or
                      "not found" in r.get("message", "").lower()))
    return all(ok)


# --------------------------------------------------------------------------
# Test 6: partial test-set args -> notice in response
# --------------------------------------------------------------------------

def test_partial_test_set_args():
    print("\n--- Test 6: partial test-set args -> notice ---")
    workdir = tempfile.mkdtemp(prefix="brier_v100_t6_")
    try:
        rds_path, sel_id = _stage_brier_i(workdir)
        # Only some test-set args provided
        r = server.summarize_fit(
            selection_id=sel_id,
            data_path=rds_path,
            # newx_expr, newy_expr, criteria missing
        )
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("_notice present (partial args)",
                          "_notice" in r,
                          detail=f"notice={r.get('_notice')}"))
        ok.append(_check("plots_included == 1 (selection plot always-on)",
                          r["summary"]["plots_included"] == 1))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    print("BRIER MCP v0.10 summarize_fit report tool test suite")
    all_pass = True
    all_pass &= test_summarize_brier_i_no_test_set()
    all_pass &= test_summarize_brier_i_with_plots()
    all_pass &= test_reproduce_script_runnable()
    all_pass &= test_summarize_brier_full()
    all_pass &= test_missing_selection_id()
    all_pass &= test_partial_test_set_args()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
