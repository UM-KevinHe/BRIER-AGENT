"""End-to-end smoke test for the BRIER MCP v0.4.0 surface.

Exercises brier_full + brier_full_selection on the canonical
Data_BRIERfull example shipped with BRIER. Runs the full transfer-
learning loop with raw external data:

  inspect_data -> brier_full -> brier_full_selection
              -> brier_predict -> brier_evaluate

Stacks target + 3 externals (150 + 3*300 = 1050 rows total, p=200).
Compares against a target-only baseline via brier_full(eta.list=0)
to validate the eta_list grid mechanism.

Prerequisites:
  * R >= 4.0 with: jsonlite, BRIER (>= 1.0.2).

Run:
  cd mcp/
  uv run tests/test_v04.py
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
    """Save BRIER::Data_BRIERfull (plus stacked X/y/cohort) to a temp .rds.

    The stacked objects make the dispatcher's job simpler: one expression
    string per argument, no R-side composition. This mirrors how a real
    user would prepare data in R before calling the MCP tool.
    """
    workdir = tempfile.mkdtemp(prefix="brier_v04_test_")
    rds_path = os.path.join(workdir, "Data_BRIERfull_stacked.rds")

    r_code = (
        "suppressPackageStartupMessages(library(BRIER))\n"
        "data(Data_BRIERfull)\n"
        "X.full <- rbind(\n"
        "  Data_BRIERfull$target$train$X,\n"
        "  Data_BRIERfull$external1$train$X,\n"
        "  Data_BRIERfull$external2$train$X,\n"
        "  Data_BRIERfull$external3$train$X\n"
        ")\n"
        "y.full <- c(\n"
        "  Data_BRIERfull$target$train$y,\n"
        "  Data_BRIERfull$external1$train$y,\n"
        "  Data_BRIERfull$external2$train$y,\n"
        "  Data_BRIERfull$external3$train$y\n"
        ")\n"
        "cohort.full <- c(\n"
        "  rep(0L, nrow(Data_BRIERfull$target$train$X)),\n"
        "  rep(1L, nrow(Data_BRIERfull$external1$train$X)),\n"
        "  rep(2L, nrow(Data_BRIERfull$external2$train$X)),\n"
        "  rep(3L, nrow(Data_BRIERfull$external3$train$X))\n"
        ")\n"
        "# Also save validation and testing splits at the top level for easy access\n"
        "X.val <- Data_BRIERfull$target$validation$X\n"
        "y.val <- Data_BRIERfull$target$validation$y\n"
        "X.test <- Data_BRIERfull$target$testing$X\n"
        "y.test <- Data_BRIERfull$target$testing$y\n"
        "saveRDS(\n"
        "  list(X.full = X.full, y.full = y.full, cohort.full = cohort.full,\n"
        "       X.val = X.val, y.val = y.val,\n"
        "       X.test = X.test, y.test = y.test),\n"
        f"  file = '{rds_path.replace(chr(92), '/')}'\n"
        ")\n"
    )
    rscript = server._find_rscript()
    proc = subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e", r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to stage Data_BRIERfull:\n{proc.stderr}")
    return workdir, rds_path


# Top-level object name (readRDS-loaded files get named by the basename).
# The .rds file basename is "Data_BRIERfull_stacked", so the env binding is
# `Data_BRIERfull_stacked`.
TOP = "Data_BRIERfull_stacked"


def test_brier_full_happy_path(rds_path) -> dict:
    print("\n--- Test 1: brier_full happy path on stacked Data_BRIERfull ---")
    r = server.brier_full(
        data_path=rds_path,
        X_expr=f"{TOP}$X.full",
        y_expr=f"{TOP}$y.full",
        cohort_expr=f"{TOP}$cohort.full",
        family="gaussian",
        eta_list=[0, 0.5, 1, 5],  # short grid for test speed
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"msg={r.get('message')}"))
    if r.get("status") != "ok":
        return {}
    ok.append(_check("returned a fit_id", bool(r.get("fit_id"))))
    ok.append(_check("fit file exists",
                     r.get("fit_path") and Path(r["fit_path"]).exists()))
    ok.append(_check("n_target = 150",
                     r.get("n_target") == 150,
                     detail=f"got n_target={r.get('n_target')}"))
    ok.append(_check("n_total = 1050",
                     r.get("n_total") == 1050,
                     detail=f"got n_total={r.get('n_total')}"))
    ok.append(_check("M_external = 3",
                     r.get("M_external") == 3,
                     detail=f"got M={r.get('M_external')}"))
    ok.append(_check("p = 200",
                     r.get("p") == 200))
    ok.append(_check("_followup_offer_selection present",
                     "_followup_offer_selection" in r))
    ok.append(_check("_notice_external_dominance fires (1050 vs 150 is >5x)",
                     "_notice_external_dominance" in r))
    return r if all(ok) else {}


def test_brier_full_rejects_cohort_all_zero(rds_path) -> bool:
    print("\n--- Test 2: brier_full rejects all-zero cohort (target-only) ---")
    # Build a quick all-zero cohort to test the error path.
    r = server.brier_full(
        data_path=rds_path,
        X_expr=f"{TOP}$X.full",
        y_expr=f"{TOP}$y.full",
        cohort_expr="rep(0L, length(" + TOP + "$y.full))",
        family="gaussian",
        eta_list=[0.1, 1],
    )
    ok = []
    # This expression has rep() which is a function call, but no deny-listed
    # symbols, so it should pass the deny-list and run.
    ok.append(_check("status error", r.get("status") == "error"))
    ok.append(_check("error mentions positive integer / external",
                     any(w in (r.get("message") or "").lower()
                         for w in ("positive", "external", "target-only")),
                     detail=f"msg={r.get('message')!r}"))
    return all(ok)


def test_brier_full_selection_rejects_BIC(rds_path, fit_id) -> bool:
    print("\n--- Test 3: brier_full_selection rejects BIC criterion ---")
    r = server.brier_full_selection(
        fit_id=fit_id,
        criteria="BIC",
        X_val_expr=f"{TOP}$X.val",
        y_val_expr=f"{TOP}$y.val",
        data_path=rds_path,
    )
    ok = []
    ok.append(_check("status error", r.get("status") == "error"))
    msg = (r.get("message") or "").lower()
    ok.append(_check("error mentions brier_i_selection as alternative",
                     "brier_i_selection" in msg or "ic criteria" in msg,
                     detail=f"msg={r.get('message')!r}"))
    return all(ok)


def test_brier_full_selection_mspe(rds_path, fit_id) -> dict:
    print("\n--- Test 4: brier_full_selection with gaussian.mspe ---")
    r = server.brier_full_selection(
        fit_id=fit_id,
        criteria="gaussian.mspe",
        X_val_expr=f"{TOP}$X.val",
        y_val_expr=f"{TOP}$y.val",
        data_path=rds_path,
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"msg={r.get('message')}"))
    if r.get("status") != "ok":
        return {}
    ok.append(_check("returned selection_id", bool(r.get("selection_id"))))
    ok.append(_check("selection_id has brier_full_sel prefix",
                     (r.get("selection_id") or "").startswith("brier_full_sel_"),
                     detail=f"got {r.get('selection_id')!r}"))
    ok.append(_check("selected_eta is a number or per-external list",
                     isinstance(r.get("selected_eta"), (int, float, list))
                     and (isinstance(r["selected_eta"], (int, float))
                          or (isinstance(r["selected_eta"], list)
                              and len(r["selected_eta"]) > 0
                              and all(isinstance(x, (int, float))
                                      for x in r["selected_eta"]))),
                     detail=f"got {r.get('selected_eta')!r}"))
    ok.append(_check("selected_lambda is a number",
                     isinstance(r.get("selected_lambda"), (int, float))))
    ok.append(_check("selected_metric (validation MSPE) is positive",
                     isinstance(r.get("selected_metric"), (int, float))
                     and r["selected_metric"] >= 0))
    return r if all(ok) else {}


def test_full_loop_predict_evaluate(rds_path, selection_id) -> bool:
    print("\n--- Test 5: brier_predict + brier_evaluate on testing split ---")
    ok = []

    # 5a: predict
    pred = server.brier_predict(
        data_path=rds_path,
        newx_expr=f"{TOP}$X.test",
        selection_id=selection_id,
    )
    ok.append(_check("predict status ok",
                     pred.get("status") == "ok",
                     detail=f"msg={pred.get('message')}"))
    if pred.get("status") != "ok":
        return False
    ok.append(_check("n_predicted = 150 (target test split)",
                     pred.get("n_predicted") == 150))
    ok.append(_check("predictions CSV exists",
                     Path(pred["predictions_path"]).exists()))

    # 5b: evaluate
    ev = server.brier_evaluate(
        data_path=rds_path,
        newx_expr=f"{TOP}$X.test",
        newy_expr=f"{TOP}$y.test",
        criteria="gaussian.mspe",
        selection_id=selection_id,
    )
    ok.append(_check("evaluate status ok",
                     ev.get("status") == "ok",
                     detail=f"msg={ev.get('message')}"))
    if ev.get("status") != "ok":
        return False
    ok.append(_check("metric_value is positive",
                     isinstance(ev.get("metric_value"), (int, float))
                     and ev["metric_value"] > 0,
                     detail=f"got MSPE={ev.get('metric_value')}"))

    # Print the real result for human inspection
    print(f"\n  Test-set MSPE (BRIERfull with validation-selected eta/lambda): "
          f"{ev.get('metric_value'):.3f}")
    return all(ok)


def test_brier_full_denylist(rds_path) -> bool:
    print("\n--- Test 6: deny-list blocks malicious cohort_expr ---")
    r = server.brier_full(
        data_path=rds_path,
        X_expr=f"{TOP}$X.full",
        y_expr=f"{TOP}$y.full",
        cohort_expr='system("echo pwned")',
        family="gaussian",
    )
    return all([
        _check("status error", r.get("status") == "error"),
        _check("class DenylistViolation",
               r.get("class") == "DenylistViolation"),
    ])


def main() -> int:
    print("BRIER MCP v0.4.0 end-to-end smoke test")
    print(f"  Rscript: {server._find_rscript()}")

    workdir, rds_path = _stage_canonical_dataset()
    try:
        all_pass = True

        fit_result = test_brier_full_happy_path(rds_path)
        all_pass &= bool(fit_result)
        if not fit_result:
            print("Cannot run downstream tests without a fit; bailing.")
            return 1
        fit_id = fit_result["fit_id"]

        all_pass &= test_brier_full_rejects_cohort_all_zero(rds_path)
        all_pass &= test_brier_full_selection_rejects_BIC(rds_path, fit_id)

        sel_result = test_brier_full_selection_mspe(rds_path, fit_id)
        all_pass &= bool(sel_result)
        if not sel_result:
            return 1

        all_pass &= test_full_loop_predict_evaluate(
            rds_path, sel_result["selection_id"])

        all_pass &= test_brier_full_denylist(rds_path)

        print()
        print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
        return 0 if all_pass else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
