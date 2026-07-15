"""Tests for v0.8.1 fixes and additions.

Covers:
  1. brier_s no longer accepts y_train_expr (auto-stash reverted)
  2. brier_predict KEEPS explicit y_center / y_scale at predict time
     (mathematically correct, no auto-magic, no auto-stash)
  3. Wizard cross_family_comparison guidance lists all three sourcing
     options (train y, test y, external scalars)
  4. preprocess_i wraps BRIER::preprocessI correctly
  5. preprocess_s wraps BRIER::preprocessS correctly

Run:
  cd mcp/
  uv run tests/test_v081.py
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


# --------------------------------------------------------------------------
# Test 1: brier_s no longer accepts y_train_expr / y_center / y_scale
# --------------------------------------------------------------------------

def test_brier_s_y_args_removed() -> bool:
    print("\n--- Test 1: brier_s y_train_expr / y_center / y_scale removed ---")
    ok = []
    import inspect
    sig = inspect.signature(server.brier_s)
    ok.append(_check(
        "brier_s.y_train_expr NOT in signature",
        "y_train_expr" not in sig.parameters,
    ))
    ok.append(_check(
        "brier_s.y_center NOT in signature",
        "y_center" not in sig.parameters,
    ))
    ok.append(_check(
        "brier_s.y_scale NOT in signature",
        "y_scale" not in sig.parameters,
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 2: brier_predict KEEPS explicit y_center / y_scale
# --------------------------------------------------------------------------

def test_brier_predict_y_args_kept() -> bool:
    print("\n--- Test 2: brier_predict.y_center / .y_scale kept as predict-time args ---")
    import inspect
    sig = inspect.signature(server.brier_predict)
    ok = []
    ok.append(_check(
        "brier_predict.y_center in signature",
        "y_center" in sig.parameters,
    ))
    ok.append(_check(
        "brier_predict.y_scale in signature",
        "y_scale" in sig.parameters,
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 3: brier_predict does NOT auto-apply from cache (no stashing)
# --------------------------------------------------------------------------

def test_brier_predict_no_auto_apply() -> bool:
    print("\n--- Test 3: brier_predict no longer auto-applies y_center/y_scale from cache ---")
    workdir = tempfile.mkdtemp(prefix="brier_v081_t3_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        r_code = (
            "set.seed(20);\n"
            "n <- 60; p <- 12;\n"
            "X <- matrix(rnorm(n*p), n, p);\n"
            "y <- rnorm(n);\n"
            "beta_zero <- matrix(0, nrow = p+1, ncol = 1);\n"
            "X_test <- matrix(rnorm(20*p), 20, p);\n"
            f"saveRDS(list(X=X, y=y, beta_zero=beta_zero, X_test=X_test), '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)

        fit = server.brier_i(
            data_path=rds_path,
            X_expr="synth$X",
            y_expr="synth$y",
            beta_external_expr="synth$beta_zero",
            family="gaussian",
            eta_list=[0, 0.5, 1],
        )
        sel = server.brier_i_selection(
            fit_id=fit["fit_id"], criteria="BIC",
        )

        # Predict without y_center/y_scale: should leave predictions
        # on the model's native scale, NO _notice_unstandardize_applied
        p = server.brier_predict(
            data_path=rds_path,
            newx_expr="synth$X_test",
            selection_id=sel["selection_id"],
        )
        ok = []
        ok.append(_check(
            "predict ok",
            p.get("status") == "ok",
            detail=f"msg={p.get('message')}",
        ))
        if p.get("status") != "ok":
            return False
        ok.append(_check(
            "no _notice_unstandardize_applied (no auto-apply)",
            "_notice_unstandardize_applied" not in p,
        ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 4: wizard cross_family_comparison guidance
# --------------------------------------------------------------------------

def test_wizard_cross_family_guidance() -> bool:
    print("\n--- Test 4: wizard cross_family_comparison lists three sourcing options ---")
    paths = server._PATHS
    brier_s_path = paths.get("BRIERs", {})
    cfc = brier_s_path.get("cross_family_comparison", "")
    ok = []
    ok.append(_check(
        "cross_family_comparison field present",
        bool(cfc),
    ))
    if not cfc:
        return False
    # All three sourcing options mentioned
    ok.append(_check(
        "mentions training y as unbiased option",
        "train" in cfc.lower() and "unbiased" in cfc.lower(),
    ))
    ok.append(_check(
        "mentions test y as biased approximation",
        "test" in cfc.lower() and "bias" in cfc.lower(),
    ))
    ok.append(_check(
        "mentions external scalars from literature",
        "external" in cfc.lower() and "literature" in cfc.lower(),
    ))
    ok.append(_check(
        "mentions leaving predictions standardized as no-source case",
        "standardized" in cfc.lower(),
    ))
    # Cross-family standardization advice still present
    ok.append(_check(
        "mentions standardize_X for cross-family comparison",
        "standardize_X" in cfc,
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 5: preprocess_i smoke test on Data_preprocessI
# --------------------------------------------------------------------------

def test_preprocess_i_smoke() -> bool:
    print("\n--- Test 5: preprocess_i on Data_preprocessI ---")
    workdir = tempfile.mkdtemp(prefix="brier_v081_t5_")
    try:
        rds_path = os.path.join(workdir, "pi.rds")
        rscript = server._find_rscript()
        r_code = (
            "suppressPackageStartupMessages(library(BRIER));\n"
            "data(Data_preprocessI);\n"
            f"saveRDS(Data_preprocessI, '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)

        r = server.preprocess_i(
            data_path=rds_path,
            target_info_expr="pi$target.info",
            external_coef_exprs=["pi$external.coef1"],
            target_info_cols={"chr": "CHR", "bp": "BP",
                              "ref": "A2", "alt": "A1"},
            external_coef_cols=["coef"],
            drop_ambiguous=True,
        )
        ok = []
        ok.append(_check(
            "status ok",
            r.get("status") == "ok",
            detail=f"msg={r.get('message')}",
        ))
        if r.get("status") != "ok":
            return False
        ok.append(_check(
            "preprocess_id returned",
            bool(r.get("preprocess_id")) and
            r["preprocess_id"].startswith("preproc_i_"),
        ))
        ok.append(_check(
            "preprocess_path exists",
            r.get("preprocess_path") and os.path.exists(r["preprocess_path"]),
        ))
        summary = r.get("summary", {})
        ok.append(_check(
            "summary.n_target_in == 10000",
            summary.get("n_target_in") == 10000,
        ))
        ok.append(_check(
            "summary.M_external == 1",
            summary.get("M_external") == 1,
        ))
        ok.append(_check(
            "summary.n_aligned_out > 0",
            summary.get("n_aligned_out") and summary.get("n_aligned_out") > 0,
        ))

        # Reject empty external list
        r2 = server.preprocess_i(
            data_path=rds_path,
            target_info_expr="pi$target.info",
            external_coef_exprs=[],
        )
        ok.append(_check(
            "empty external list -> status error",
            r2.get("status") == "error",
        ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 6: preprocess_s smoke test on Data_preprocessS
# --------------------------------------------------------------------------

def test_preprocess_s_smoke() -> bool:
    print("\n--- Test 6: preprocess_s on Data_preprocessS ---")
    workdir = tempfile.mkdtemp(prefix="brier_v081_t6_")
    try:
        rds_path = os.path.join(workdir, "ps.rds")
        rscript = server._find_rscript()
        r_code = (
            "suppressPackageStartupMessages(library(BRIER));\n"
            "data(Data_preprocessS);\n"
            f"saveRDS(Data_preprocessS, '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)

        r = server.preprocess_s(
            data_path=rds_path,
            target_ss_expr="ps$target.ss",
            target_ld_expr="ps$target.ld",
            target_ld_mat_expr="ps$target.ld.mat",
            external_coef_exprs=["ps$external.coef1"],
            target_ss_cols={"chr": "CHR", "bp": "BP", "ref": "A2",
                            "alt": "A1", "p": "P", "n": "NMISS",
                            "sgn": "STAT", "beta": "BETA",
                            "corr": "BETA"},
            target_ld_cols={"chr": "CHR", "bp": "BP", "ref": "A2",
                            "alt": "A1"},
            external_coef_cols=["coef"],
            target_ind="gwas",
            drop_ambiguous=True,
        )
        ok = []
        ok.append(_check(
            "status ok",
            r.get("status") == "ok",
            detail=f"msg={r.get('message')}",
        ))
        if r.get("status") != "ok":
            # preprocessS may have format expectations the example data
            # doesn't quite match; record but don't hard-fail if it's a
            # known shape issue rather than a tool wrapper bug.
            print(f"    note: preprocessS error from BRIER: {r.get('message')}")
            return False
        ok.append(_check(
            "preprocess_id starts with preproc_s_",
            r.get("preprocess_id", "").startswith("preproc_s_"),
        ))
        ok.append(_check(
            "summary.M_external == 1",
            r.get("summary", {}).get("M_external") == 1,
        ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    print("BRIER MCP v0.8.1 end-to-end smoke test")
    all_pass = True
    all_pass &= test_brier_s_y_args_removed()
    all_pass &= test_brier_predict_y_args_kept()
    all_pass &= test_brier_predict_no_auto_apply()
    all_pass &= test_wizard_cross_family_guidance()
    all_pass &= test_preprocess_i_smoke()
    all_pass &= test_preprocess_s_smoke()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
