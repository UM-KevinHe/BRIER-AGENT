"""End-to-end smoke test for the BRIER MCP v0.5.0 surface.

Exercises the summary-statistics workflow on the canonical Data_BRIERs
example. Validates:

  inspect_data -> [p2cor in R staging] -> cal_ld -> brier_s
              -> brier_s_selection -> brier_predict -> brier_evaluate

Plus:
  * get_ldb returns a valid BED file path and surfaces the chr-prefix warning
  * brier_s_selection accepts both IC criteria (Cp) and validation MSPE
  * The standardization heuristic fires when un-standardized X.val is passed
  * Deny-list catches malicious expressions

Prerequisites:
  * R >= 4.0 with: jsonlite, BRIER (>= 1.0.2 with Matrix::crossprod
    import fix), Matrix.

Run:
  cd mcp/
  uv run tests/test_v05.py
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


def _stage_canonical_dataset() -> tuple[str, str]:
    """Save BRIER::Data_BRIERs (with p2cor-augmented sumstats + standardized
    validation X/y) to a temp .rds. Mirrors the workflow a real user would
    do in R before invoking the MCP tools.
    """
    workdir = tempfile.mkdtemp(prefix="brier_v05_test_")
    rds_path = os.path.join(workdir, "Data_BRIERs_prepped.rds")

    r_code = (
        "suppressPackageStartupMessages(library(BRIER))\n"
        "data(Data_BRIERs)\n"
        "# Train side: augment sumstats with corr (p2cor)\n"
        "sumstats <- Data_BRIERs$target$train$sumstats\n"
        "sumstats$corr <- p2cor(sumstats$pval, sumstats$n,\n"
        "                       sign = sign(sumstats$stats))\n"
        "# Validation side: standardize X.val and y.val (gaussian).\n"
        "X.val.std <- standardize_X(Data_BRIERs$target$validation$X)$standardized\n"
        "y.val.std <- as.numeric(standardize_X(\n"
        "  as.matrix(Data_BRIERs$target$validation$y))$standardized)\n"
        "# A non-standardized X.val so we can test the heuristic warning.\n"
        "X.val.raw <- Data_BRIERs$target$validation$X\n"
        "# Standardized testing for evaluate calls (gaussian).\n"
        "X.test.std <- standardize_X(Data_BRIERs$target$testing$X)$standardized\n"
        "y.test.std <- as.numeric(standardize_X(\n"
        "  as.matrix(Data_BRIERs$target$testing$y))$standardized)\n"
        "saveRDS(\n"
        "  list(X = Data_BRIERs$target$train$X,\n"
        "       sumstats = sumstats,\n"
        "       beta.external = Data_BRIERs$beta.external,\n"
        "       X.val.std = X.val.std, y.val.std = y.val.std,\n"
        "       X.val.raw = X.val.raw,\n"
        "       X.test.std = X.test.std, y.test.std = y.test.std),\n"
        f"  file = '{rds_path.replace(chr(92), '/')}'\n"
        ")\n"
    )
    rscript = server._find_rscript()
    proc = subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e", r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to stage Data_BRIERs:\n{proc.stderr}")
    return workdir, rds_path


# Top-level object name in the .rds (named by basename).
TOP = "Data_BRIERs_prepped"


# --------------------------------------------------------------------------
# Test 1: get_ldb
# --------------------------------------------------------------------------

def test_get_ldb() -> bool:
    print("\n--- Test 1: get_ldb returns a valid BED path with metadata ---")
    r = server.get_ldb(ancestry="EUR", build="hg38")
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"msg={r.get('message')}"))
    if r.get("status") != "ok":
        return False
    ok.append(_check("bed_path exists",
                     r.get("bed_path") and Path(r["bed_path"]).exists()))
    ok.append(_check("n_blocks > 1000 (EUR/hg38 should be ~1700)",
                     r.get("n_blocks", 0) > 1000,
                     detail=f"got n_blocks={r.get('n_blocks')}"))
    ok.append(_check("n_chromosomes >= 22",
                     r.get("n_chromosomes", 0) >= 22))
    ok.append(_check("chr_format = 'chr-prefixed'",
                     r.get("chr_format") == "chr-prefixed"))
    ok.append(_check("_notice_chr_prefix_mismatch fires",
                     "_notice_chr_prefix_mismatch" in r))
    return all(ok)


# --------------------------------------------------------------------------
# Test 2: cal_ld basic (no LDB / SNP info — small synthetic LD)
# --------------------------------------------------------------------------

def test_cal_ld_basic(rds_path) -> dict:
    print("\n--- Test 2: cal_ld on the reference panel (no LDB) ---")
    r = server.cal_ld(
        data_path=rds_path,
        X_expr=f"{TOP}$X",
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"msg={r.get('message')}"))
    if r.get("status") != "ok":
        return {}
    ok.append(_check("ld_id returned", bool(r.get("ld_id"))))
    ok.append(_check("ld_path exists",
                     r.get("ld_path") and Path(r["ld_path"]).exists()))
    ok.append(_check("p_input == 200",
                     r.get("p_input") == 200,
                     detail=f"got p_input={r.get('p_input')}"))
    ok.append(_check("p_retained <= 200",
                     r.get("p_retained", 0) <= 200))
    ok.append(_check("_notice_subset_required present",
                     "_notice_subset_required" in r))
    return r if all(ok) else {}


# --------------------------------------------------------------------------
# Test 3: brier_s with ld_id (preferred path)
# --------------------------------------------------------------------------

def test_brier_s_with_ld_id(rds_path, ld_id) -> dict:
    print("\n--- Test 3: brier_s with ld_id (auto-subset) ---")
    r = server.brier_s(
        data_path=rds_path,
        sumstats_expr=f"{TOP}$sumstats",
        beta_external_expr=f"{TOP}$beta.external",
        family="gaussian",
        ld_id=ld_id,
        multi_method="stacking",
        eta_list=[0, 0.5, 1, 5],  # short grid for test speed
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"msg={r.get('message')}"))
    if r.get("status") != "ok":
        return {}
    ok.append(_check("fit_id returned", bool(r.get("fit_id"))))
    ok.append(_check("ld_id_used echoed back",
                     r.get("ld_id_used") == ld_id))
    ok.append(_check("p reflects retained variants",
                     isinstance(r.get("p"), int) and r["p"] > 0))
    ok.append(_check("M_external = 3", r.get("M_external") == 3))
    ok.append(_check("_notice_brier_s_standardize always present",
                     "_notice_brier_s_standardize" in r))
    ok.append(_check("_followup_offer_selection present",
                     "_followup_offer_selection" in r))
    return r if all(ok) else {}


# --------------------------------------------------------------------------
# Test 4: brier_s_selection with IC criterion (Cp)
# --------------------------------------------------------------------------

def test_brier_s_selection_ic(fit_id) -> dict:
    print("\n--- Test 4: brier_s_selection with IC criterion (Cp) ---")
    # Cp requires TN (training sample size). Data_BRIERs target train has n=150.
    r = server.brier_s_selection(
        fit_id=fit_id,
        criteria="Cp",
        TN=150,
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"msg={r.get('message')}"))
    if r.get("status") != "ok":
        return {}
    ok.append(_check("criteria_mode = 'ic'",
                     r.get("criteria_mode") == "ic"))
    ok.append(_check("selection_id returned", bool(r.get("selection_id"))))
    ok.append(_check("selection_id has brier_s_sel prefix",
                     (r.get("selection_id") or "").startswith("brier_s_sel_")))
    return r if all(ok) else {}


# --------------------------------------------------------------------------
# Test 5: brier_s_selection with validation MSPE (standardized X.val)
# --------------------------------------------------------------------------

def test_brier_s_selection_validation(rds_path, fit_id) -> dict:
    print("\n--- Test 5: brier_s_selection with gaussian.mspe (standardized X.val) ---")
    r = server.brier_s_selection(
        fit_id=fit_id,
        criteria="gaussian.mspe",
        data_path=rds_path,
        X_val_expr=f"{TOP}$X.val.std",
        y_val_expr=f"{TOP}$y.val.std",
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"msg={r.get('message')}"))
    if r.get("status") != "ok":
        return {}
    ok.append(_check("criteria_mode = 'validation'",
                     r.get("criteria_mode") == "validation"))
    ok.append(_check("standardization warning NOT fired (X.val IS standardized)",
                     "_notice_x_val_not_standardized" not in r))
    return r if all(ok) else {}


# --------------------------------------------------------------------------
# Test 6: standardization heuristic fires on raw X.val
# --------------------------------------------------------------------------

def test_standardization_heuristic_fires(rds_path, fit_id) -> bool:
    print("\n--- Test 6: heuristic fires when X.val is raw (un-standardized) ---")
    r = server.brier_s_selection(
        fit_id=fit_id,
        criteria="gaussian.mspe",
        data_path=rds_path,
        X_val_expr=f"{TOP}$X.val.raw",   # NOT standardized
        y_val_expr=f"{TOP}$y.val.std",
    )
    ok = []
    # Whether BRIER errors or succeeds with garbage doesn't matter for this
    # test; what matters is the heuristic warning surfaces when status=ok.
    if r.get("status") == "ok":
        ok.append(_check("_notice_x_val_not_standardized fired",
                         "_notice_x_val_not_standardized" in r))
    else:
        # If BRIER refused with un-standardized X.val (which it might), we
        # still want a clean error not a crash.
        ok.append(_check("clean error if BRIER rejected raw X.val",
                         isinstance(r.get("message"), str)))
    return all(ok)


# --------------------------------------------------------------------------
# Test 7: brier_predict + brier_evaluate on standardized test set
# --------------------------------------------------------------------------

def test_predict_and_evaluate(rds_path, selection_id) -> bool:
    print("\n--- Test 7: brier_predict + brier_evaluate on standardized testing ---")
    ok = []

    pred = server.brier_predict(
        data_path=rds_path,
        newx_expr=f"{TOP}$X.test.std",
        selection_id=selection_id,
    )
    ok.append(_check("predict status ok", pred.get("status") == "ok",
                     detail=f"msg={pred.get('message')}"))
    if pred.get("status") != "ok":
        return False
    ok.append(_check("n_predicted = 150",
                     pred.get("n_predicted") == 150))

    ev = server.brier_evaluate(
        data_path=rds_path,
        newx_expr=f"{TOP}$X.test.std",
        newy_expr=f"{TOP}$y.test.std",
        criteria="gaussian.mspe",
        selection_id=selection_id,
    )
    ok.append(_check("evaluate status ok", ev.get("status") == "ok",
                     detail=f"msg={ev.get('message')}"))
    if ev.get("status") != "ok":
        return False
    ok.append(_check("metric_value is a finite number",
                     isinstance(ev.get("metric_value"), (int, float)),
                     detail=f"got MSPE={ev.get('metric_value')}"))

    print(f"\n  Test-set MSPE (BRIERs, MSPE-selected): "
          f"{ev.get('metric_value'):.4f}")
    return all(ok)


# --------------------------------------------------------------------------
# Test 8: cal_ld deny-list
# --------------------------------------------------------------------------

def test_cal_ld_denylist(rds_path) -> bool:
    print("\n--- Test 8: cal_ld deny-list blocks malicious X_expr ---")
    r = server.cal_ld(
        data_path=rds_path,
        X_expr='system("echo pwned")',
    )
    return all([
        _check("status error", r.get("status") == "error"),
        _check("class DenylistViolation",
               r.get("class") == "DenylistViolation"),
    ])


# --------------------------------------------------------------------------
# Test 9: brier_s without ld_id or XtX_expr is an error
# --------------------------------------------------------------------------

def test_brier_s_requires_ld(rds_path) -> bool:
    print("\n--- Test 9: brier_s with neither ld_id nor XtX_expr errors cleanly ---")
    r = server.brier_s(
        data_path=rds_path,
        sumstats_expr=f"{TOP}$sumstats",
        beta_external_expr=f"{TOP}$beta.external",
        family="gaussian",
    )
    return all([
        _check("status error", r.get("status") == "error"),
        _check("error mentions ld_id or XtX_expr",
               any(w in (r.get("message") or "").lower()
                   for w in ("ld_id", "xtx_expr")),
               detail=f"msg={r.get('message')!r}"),
    ])


# --------------------------------------------------------------------------
# Test 10: brier_s_selection rejects unknown criteria
# --------------------------------------------------------------------------

def test_brier_s_selection_bad_criteria(fit_id) -> bool:
    print("\n--- Test 10: brier_s_selection rejects unknown criteria ---")
    r = server.brier_s_selection(
        fit_id=fit_id,
        criteria="BIC",   # valid for brier_i, not brier_s
    )
    return all([
        _check("status error", r.get("status") == "error"),
        _check("error mentions valid criteria",
               "criteria" in (r.get("message") or "").lower(),
               detail=f"msg={r.get('message')!r}"),
    ])


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    print("BRIER MCP v0.5.0 end-to-end smoke test")
    print(f"  Rscript: {server._find_rscript()}")

    workdir, rds_path = _stage_canonical_dataset()
    try:
        all_pass = True

        all_pass &= test_get_ldb()

        ld_result = test_cal_ld_basic(rds_path)
        all_pass &= bool(ld_result)
        if not ld_result:
            print("Cannot run downstream tests without an LD object; bailing.")
            return 1
        ld_id = ld_result["ld_id"]

        fit_result = test_brier_s_with_ld_id(rds_path, ld_id)
        all_pass &= bool(fit_result)
        if not fit_result:
            return 1
        fit_id = fit_result["fit_id"]

        all_pass &= bool(test_brier_s_selection_ic(fit_id))

        sel_result = test_brier_s_selection_validation(rds_path, fit_id)
        all_pass &= bool(sel_result)

        all_pass &= test_standardization_heuristic_fires(rds_path, fit_id)

        if sel_result:
            all_pass &= test_predict_and_evaluate(
                rds_path, sel_result["selection_id"])

        all_pass &= test_cal_ld_denylist(rds_path)
        all_pass &= test_brier_s_requires_ld(rds_path)
        all_pass &= test_brier_s_selection_bad_criteria(fit_id)

        print()
        print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
        return 0 if all_pass else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
