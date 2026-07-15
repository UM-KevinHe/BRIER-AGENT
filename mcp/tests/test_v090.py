"""Tests for v0.9 plot wrappers.

Covers the three new MCP tools:
  1. brier_plot_eta on M=1 (renders ggplot directly)
  2. brier_plot_eta on M=2 (auto-builds heatmap from summary.df)
  3. brier_plot_eta family-criterion validation
  4. brier_plot_box bootstrap performance comparison
  5. brier_plot_importance bootstrap variable importance
  6. Output artifacts: PNG + CSV both produced

Run:
  cd mcp/
  uv run tests/test_v090.py
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


def _setup_data():
    """Stage Data_BRIERi to a temp .rds and return (workdir, rds_path)."""
    workdir = tempfile.mkdtemp(prefix="brier_v090_")
    rds_path = os.path.join(workdir, "bi.rds")
    rscript = server._find_rscript()
    subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e",
         "suppressPackageStartupMessages(library(BRIER)); "
         f"data(Data_BRIERi); saveRDS(Data_BRIERi, '{rds_path}')"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return workdir, rds_path


def _make_m1_selection(rds_path):
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
    return sel["selection_id"]


def _make_m2_selection(rds_path):
    fit = server.brier_i(
        data_path=rds_path,
        X_expr="bi$target$train$X",
        y_expr="bi$target$train$y",
        beta_external_expr="bi$beta.external[, 1:2]",
        multi_method="ind",
        family="gaussian",
        eta_list=[[0, 0.5, 1, 2], [0, 0.5, 1, 2]],
    )
    assert fit["status"] == "ok", fit
    sel = server.brier_i_selection(fit_id=fit["fit_id"], criteria="BIC")
    assert sel["status"] == "ok", sel
    return sel["selection_id"]


# --------------------------------------------------------------------------
# Test 1: brier_plot_eta on M=1 (single external)
# --------------------------------------------------------------------------

def test_plot_eta_m1() -> bool:
    print("\n--- Test 1: brier_plot_eta M=1 (renders ggplot directly) ---")
    workdir, rds_path = _setup_data()
    try:
        sel_id = _make_m1_selection(rds_path)
        r = server.brier_plot_eta(
            selection_id=sel_id,
            data_path=rds_path,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="gaussian.mspe",
        )
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok",
                          detail=f"msg={r.get('message')}"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("M == 1", r.get("M") == 1))
        ok.append(_check("rendered_kind == M1_curve",
                          r.get("rendered_kind") == "M1_curve"))
        ok.append(_check("plot_id starts with plot_eta_",
                          r.get("plot_id", "").startswith("plot_eta_")))
        ok.append(_check("PNG exists",
                          r.get("plot_png_path") and
                          os.path.exists(r["plot_png_path"])))
        ok.append(_check("CSV exists",
                          r.get("plot_csv_path") and
                          os.path.exists(r["plot_csv_path"])))
        ok.append(_check("PNG nonzero size",
                          r.get("plot_png_path") and
                          os.path.getsize(r["plot_png_path"]) > 1000))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 2: brier_plot_eta on M=2 (auto-builds heatmap)
# --------------------------------------------------------------------------

def test_plot_eta_m2_heatmap() -> bool:
    print("\n--- Test 2: brier_plot_eta M=2 (auto-builds heatmap) ---")
    workdir, rds_path = _setup_data()
    try:
        sel_id = _make_m2_selection(rds_path)
        r = server.brier_plot_eta(
            selection_id=sel_id,
            data_path=rds_path,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="gaussian.mspe",
        )
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok",
                          detail=f"msg={r.get('message')}"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("M == 2", r.get("M") == 2))
        ok.append(_check("rendered_kind == M2_heatmap",
                          r.get("rendered_kind") == "M2_heatmap"))
        ok.append(_check("n_eta_points > 1",
                          r.get("n_eta_points", 0) > 1))
        ok.append(_check("PNG exists",
                          r.get("plot_png_path") and
                          os.path.exists(r["plot_png_path"])))
        ok.append(_check("CSV exists",
                          r.get("plot_csv_path") and
                          os.path.exists(r["plot_csv_path"])))
        # Verify CSV has eta_1 and eta_2 columns
        if r.get("plot_csv_path") and os.path.exists(r["plot_csv_path"]):
            with open(r["plot_csv_path"]) as f:
                header = f.readline()
            ok.append(_check("CSV has eta_1 and eta_2 columns",
                              "eta_1" in header and "eta_2" in header,
                              detail=f"header={header.strip()}"))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 3: criteria-family validation
# --------------------------------------------------------------------------

def test_criteria_family_validation() -> bool:
    print("\n--- Test 3: family-criterion validation ---")
    workdir, rds_path = _setup_data()
    try:
        sel_id = _make_m1_selection(rds_path)  # gaussian fit

        ok = []
        # Bad combo: binomial criterion on gaussian fit
        r1 = server.brier_plot_eta(
            selection_id=sel_id, data_path=rds_path,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="binomial.auc",
        )
        ok.append(_check(
            "binomial.auc on gaussian -> error",
            r1.get("status") == "error" and
            "not compatible" in r1.get("message", "").lower(),
        ))

        # Unknown criterion
        r2 = server.brier_plot_eta(
            selection_id=sel_id, data_path=rds_path,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="nonsense.metric",
        )
        ok.append(_check(
            "unknown criterion -> error",
            r2.get("status") == "error" and
            "not a recognized" in r2.get("message", "").lower(),
        ))

        # Good combo: gaussian criterion on gaussian fit (should NOT error
        # at the validation layer)
        r3 = server.brier_plot_eta(
            selection_id=sel_id, data_path=rds_path,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="gaussian.rsq",
        )
        ok.append(_check(
            "gaussian.rsq on gaussian fit passes validation",
            r3.get("status") == "ok",
            detail=f"msg={r3.get('message')}",
        ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 4: brier_plot_box bootstrap comparison
# --------------------------------------------------------------------------

def test_plot_box() -> bool:
    print("\n--- Test 4: brier_plot_box bootstrap comparison ---")
    workdir, rds_path = _setup_data()
    try:
        sel_id = _make_m1_selection(rds_path)
        r = server.brier_plot_box(
            selection_id=sel_id, data_path=rds_path,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="gaussian.mspe",
            bootstrap_n=30, seed=42,
        )
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok",
                          detail=f"msg={r.get('message')}"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("plot_id starts with plot_box_",
                          r.get("plot_id", "").startswith("plot_box_")))
        ok.append(_check("PNG exists",
                          os.path.exists(r.get("plot_png_path", ""))))
        ok.append(_check("CSV exists",
                          os.path.exists(r.get("plot_csv_path", ""))))
        ok.append(_check("n_bootstrap == 30",
                          r.get("n_bootstrap") == 30))
        # CSV should have 30 rows (one per replicate) plus header
        if r.get("plot_csv_path") and os.path.exists(r["plot_csv_path"]):
            with open(r["plot_csv_path"]) as f:
                lines = f.readlines()
            ok.append(_check(
                "CSV has ~30 bootstrap rows",
                len(lines) >= 31,  # header + 30
                detail=f"got {len(lines)} lines",
            ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 5: brier_plot_importance variable importance
# --------------------------------------------------------------------------

def test_plot_importance() -> bool:
    print("\n--- Test 5: brier_plot_importance ---")
    workdir, rds_path = _setup_data()
    try:
        sel_id = _make_m1_selection(rds_path)
        r = server.brier_plot_importance(
            selection_id=sel_id, data_path=rds_path,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="gaussian.mspe",
            n_top=10, replications=30, seed=42,
        )
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok",
                          detail=f"msg={r.get('message')}"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("plot_id starts with plot_importance_",
                          r.get("plot_id", "").startswith("plot_importance_")))
        ok.append(_check("PNG exists",
                          os.path.exists(r.get("plot_png_path", ""))))
        ok.append(_check("n_top == 10", r.get("n_top") == 10))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 6: cox criterion rejection (even if user passes a known criterion)
# --------------------------------------------------------------------------

def test_cox_rejection() -> bool:
    print("\n--- Test 6: family='cox' not yet supported in plot tools ---")
    # We can't fit a cox model since BRIER doesn't have it functional, but
    # we can verify the validator emits the right family check by passing
    # an invalid combination at the criterion level. The cox-specific
    # error is reachable through _lookup_family_from_selection returning
    # 'cox' from a hypothetical fit; we approximate by checking that the
    # validator function rejects cox directly.
    err = server._validate_plot_criteria("gaussian.mspe", family="cox")
    ok = [_check(
        "_validate_plot_criteria rejects family='cox'",
        err is not None and "cox" in err.lower(),
        detail=f"err={err!r}",
    )]
    return all(ok)


def main() -> int:
    print("BRIER MCP v0.9 plot wrappers test suite")
    all_pass = True
    all_pass &= test_plot_eta_m1()
    all_pass &= test_plot_eta_m2_heatmap()
    all_pass &= test_criteria_family_validation()
    all_pass &= test_plot_box()
    all_pass &= test_plot_importance()
    all_pass &= test_cox_rejection()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
