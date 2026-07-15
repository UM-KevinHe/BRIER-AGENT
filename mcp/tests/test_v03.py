"""End-to-end smoke test for the BRIER MCP v0.3.0 surface.

Builds on test_v02.py by adding brier_predict and brier_evaluate.
Uses the canonical Data_BRIERi example shipped with the BRIER package
(saved to a temp .rds), to exercise the full transfer-learning loop:

  inspect_data -> brier_i -> brier_i_selection -> brier_predict
              -> brier_evaluate

Validates:
  * Selection now caches a selection object and returns selection_id.
  * brier_predict using selection_id (preferred path) returns
    predictions on a held-out X.
  * brier_evaluate using selection_id returns a metric value.
  * Both tools surface eta_used / lambda_used from the cached selection.
  * The predictions side-file CSV is created and has the right shape.

Prerequisites:
  * R >= 4.0 with: jsonlite, BRIER (>= 1.0.2).
  * Python: just the `mcp` package, handled by uv.

Run:
  cd mcp/
  uv run tests/test_v03.py
"""
from __future__ import annotations

import csv
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
    """Save BRIER::Data_BRIERi to a temp .rds. Returns (workdir, rds_path)."""
    workdir = tempfile.mkdtemp(prefix="brier_v03_test_")
    rds_path = os.path.join(workdir, "Data_BRIERi.rds")

    r_code = (
        "suppressPackageStartupMessages(library(BRIER))\n"
        "data(Data_BRIERi)\n"
        f"saveRDS(Data_BRIERi, file = '{rds_path.replace(chr(92), '/')}')\n"
    )
    rscript = server._find_rscript()
    proc = subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e", r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to stage Data_BRIERi:\n{proc.stderr}")
    return workdir, rds_path


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_full_workflow(rds_path) -> bool:
    """Fit -> select -> predict -> evaluate, on the canonical example.

    Mirrors the workflow a user would run through Claude Desktop.
    """
    print("\n--- Full workflow: brier_i -> selection -> predict -> evaluate ---")
    ok = []

    # 1. Fit on the training split.
    fit = server.brier_i(
        data_path=rds_path,
        X_expr="Data_BRIERi$target$train$X",
        y_expr="Data_BRIERi$target$train$y",
        beta_external_expr="Data_BRIERi$beta.external",
        family="gaussian",
        multi_method="stacking",
    )
    ok.append(_check("brier_i status ok", fit.get("status") == "ok",
                     detail=f"msg={fit.get('message')}"))
    if fit.get("status") != "ok":
        return False
    fit_id = fit["fit_id"]
    ok.append(_check(f"fit_id={fit_id}", bool(fit_id)))

    # 2. Selection with held-out validation MSPE.
    sel = server.brier_i_selection(
        fit_id=fit_id,
        criteria="gaussian.mspe",
        X_val_expr="Data_BRIERi$target$validation$X",
        y_val_expr="Data_BRIERi$target$validation$y",
        data_path=rds_path,
    )
    ok.append(_check("brier_i_selection status ok",
                     sel.get("status") == "ok",
                     detail=f"msg={sel.get('message')}"))
    if sel.get("status") != "ok":
        return False
    selection_id = sel.get("selection_id")
    ok.append(_check("selection_id returned",
                     bool(selection_id),
                     detail=f"got {selection_id!r}"))
    ok.append(_check("selection_path exists",
                     sel.get("selection_path") and
                     Path(sel["selection_path"]).exists()))
    sel_eta = sel.get("selected_eta")
    sel_lambda = sel.get("selected_lambda")
    ok.append(_check(f"selected eta/lambda present",
                     sel_eta is not None and sel_lambda is not None,
                     detail=f"eta={sel_eta}, lambda={sel_lambda}"))

    # 3. Predict on the testing split using selection_id.
    pred = server.brier_predict(
        data_path=rds_path,
        newx_expr="Data_BRIERi$target$testing$X",
        selection_id=selection_id,
    )
    ok.append(_check("brier_predict status ok",
                     pred.get("status") == "ok",
                     detail=f"msg={pred.get('message')}"))
    if pred.get("status") != "ok":
        return False
    ok.append(_check("eta_used matches selection",
                     abs(pred["eta_used"] - sel_eta) < 1e-9,
                     detail=f"pred eta_used={pred['eta_used']} vs sel={sel_eta}"))
    ok.append(_check("lambda_used matches selection",
                     abs(pred["lambda_used"] - sel_lambda) < 1e-9))
    ok.append(_check("n_predicted = 150 (testing split size)",
                     pred.get("n_predicted") == 150,
                     detail=f"got n_predicted={pred.get('n_predicted')}"))
    ok.append(_check("summary has all six stats",
                     all(k in (pred.get("summary") or {})
                         for k in ("min", "q25", "median", "mean", "q75", "max"))))
    ok.append(_check("predictions CSV exists and has 150 rows",
                     Path(pred["predictions_path"]).exists()
                     and _csv_row_count(pred["predictions_path"]) == 150,
                     detail=f"path={pred.get('predictions_path')}"))

    # 4. Evaluate on the testing split using selection_id.
    ev = server.brier_evaluate(
        data_path=rds_path,
        newx_expr="Data_BRIERi$target$testing$X",
        newy_expr="Data_BRIERi$target$testing$y",
        criteria="gaussian.mspe",
        selection_id=selection_id,
    )
    ok.append(_check("brier_evaluate status ok",
                     ev.get("status") == "ok",
                     detail=f"msg={ev.get('message')}"))
    if ev.get("status") != "ok":
        return False
    ok.append(_check("metric_value is a finite number",
                     isinstance(ev.get("metric_value"), (int, float))
                     and ev["metric_value"] >= 0,
                     detail=f"got {ev.get('metric_value')!r}"))
    ok.append(_check("criteria echoed back",
                     ev.get("criteria") == "gaussian.mspe"))
    ok.append(_check("eta_used matches selection",
                     abs(ev["eta_used"] - sel_eta) < 1e-9))

    return all(ok)


def _csv_row_count(path: str) -> int:
    """Count data rows in a CSV (excluding header)."""
    with open(path, newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    return len(rows) - 1


def test_predict_with_explicit_eta_lambda(rds_path, fit_id) -> bool:
    """brier_predict with fit_id + which_eta/which_lambda (no selection)."""
    print("\n--- brier_predict with fit_id + grid index (which_eta) ---")
    ok = []
    # v0.10.3 default grid is c(0, exp(seq(log(0.1), log(10), length.out=10))),
    # 11 points total. which_eta=11 picks the last one (=10 exactly).
    pred = server.brier_predict(
        data_path=rds_path,
        newx_expr="Data_BRIERi$target$testing$X",
        fit_id=fit_id,
        which_eta=11,
        which_lambda=1,
    )
    ok.append(_check("status ok", pred.get("status") == "ok",
                     detail=f"msg={pred.get('message')}"))
    if pred.get("status") != "ok":
        return False
    # eta.grid index 11 in the default grid is exactly 10.
    ok.append(_check("eta_used reflects grid[11] (10.0)",
                     abs(pred.get("eta_used") - 10.0) < 1e-3,
                     detail=f"got {pred.get('eta_used')}"))
    ok.append(_check("n_predicted = 150",
                     pred.get("n_predicted") == 150))
    return all(ok)


def test_predict_denylist(rds_path) -> bool:
    """deny-list blocks malicious newx_expr."""
    print("\n--- brier_predict deny-list ---")
    pred = server.brier_predict(
        data_path=rds_path,
        newx_expr='system("rm -rf /tmp/foo")',
        selection_id="dummy",
    )
    return all([
        _check("status error", pred.get("status") == "error"),
        _check("class DenylistViolation",
               pred.get("class") == "DenylistViolation"),
    ])


def test_evaluate_missing_selection(rds_path) -> bool:
    """brier_evaluate against a nonexistent selection_id returns clean error."""
    print("\n--- brier_evaluate with missing selection_id ---")
    ev = server.brier_evaluate(
        data_path=rds_path,
        newx_expr="Data_BRIERi$target$testing$X",
        newy_expr="Data_BRIERi$target$testing$y",
        criteria="gaussian.mspe",
        selection_id="brier_i_sel_bogus_does_not_exist",
    )
    return all([
        _check("status error", ev.get("status") == "error"),
        _check("error mentions selection object",
               "selection object" in (ev.get("message") or "").lower(),
               detail=f"msg={ev.get('message')!r}"),
    ])


def main() -> int:
    print("BRIER MCP v0.3.0 end-to-end smoke test")
    print(f"  Rscript: {server._find_rscript()}")

    workdir, rds_path = _stage_canonical_dataset()
    try:
        # Need a fit_id for the explicit-eta/lambda test, so run a quick fit.
        fit = server.brier_i(
            data_path=rds_path,
            X_expr="Data_BRIERi$target$train$X",
            y_expr="Data_BRIERi$target$train$y",
            beta_external_expr="Data_BRIERi$beta.external",
            family="gaussian",
            multi_method="stacking",
        )
        if fit.get("status") != "ok":
            print(f"  setup fit failed: {fit.get('message')}")
            return 1
        bare_fit_id = fit["fit_id"]

        all_pass = True
        all_pass &= test_full_workflow(rds_path)
        all_pass &= test_predict_with_explicit_eta_lambda(rds_path, bare_fit_id)
        all_pass &= test_predict_denylist(rds_path)
        all_pass &= test_evaluate_missing_selection(rds_path)

        print()
        print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
        return 0 if all_pass else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
