"""Tests for v0.12 brier_auto_tune_eta refinements.

Covers two changes from v0.11:
  (1) Stricter escalation trigger: default near_boundary_top_fraction
      is now 0.0 (strict equality). Escalation fires only when
      eta_min == grid_max exactly.
  (2) Single-shot de-escalation: when the initial fit lands interior
      with eta_min < de_escalation_threshold (default 1.0), the tool
      does ONE refit at eta_ceiling = de_escalation_ceiling (default
      2.0). Mutually exclusive with escalation.

Run:
  cd mcp/
  uv run tests/test_v120.py
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


def _stage_bi(workdir):
    rds = os.path.join(workdir, "bi.rds")
    rscript = server._find_rscript()
    subprocess.run([rscript, "--no-save", "--no-restore", "--no-init-file",
                    "-e",
                    "suppressPackageStartupMessages(library(BRIER)); "
                    f"data(Data_BRIERi); saveRDS(Data_BRIERi, '{rds}')"],
                   capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    return rds


# -- Trigger semantics -----------------------------------------------------

def test_strict_equality_default():
    print("\n--- Test 1: default near_boundary_top_fraction is 0.0 (strict) ---")
    g = server._build_eta_grid()  # [0, 0.1, ..., 5.995, 10]
    ok = []
    ok.append(_check("eta=10 at fraction=0 fires (== grid_max)",
                      server._is_near_boundary(10, g, 0.0)))
    ok.append(_check("eta=5.995 at fraction=0 does NOT fire",
                      not server._is_near_boundary(5.995, g, 0.0)))
    ok.append(_check("eta=5.995 still fires at fraction=0.20 (back-compat)",
                      server._is_near_boundary(5.995, g, 0.20)))
    return all(ok)


def test_escalation_strict_only_at_max():
    print("\n--- Test 2: escalation under strict equality only fires at exact grid_max ---")
    workdir = tempfile.mkdtemp(prefix="v120_t2_")
    try:
        rds = _stage_bi(workdir)
        # BIC picks eta=grid_max -> escalation walks 10 -> 30
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
            },
            selection_kwargs={"criteria": "BIC"},
            escalation_ceilings=[30],
        )
        ok = [_check("status ok", r.get("status") == "ok")]
        if r.get("status") != "ok":
            return False
        ok.append(_check("walked both rungs (boundary at every rung)",
                          len(r["escalation_history"]) == 2))
        ok.append(_check("rung 0 ceiling=10, eta=10 (boundary)",
                          r["escalation_history"][0]["ceiling"] == 10.0 and
                          float(r["escalation_history"][0]["eta_min"]) == 10.0))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_interior_below_max_does_not_escalate():
    print("\n--- Test 3: interior optimum (eta < grid_max) does not escalate ---")
    workdir = tempfile.mkdtemp(prefix="v120_t3_")
    try:
        rds = _stage_bi(workdir)
        # validation MSPE on Data_BRIERi lands at eta=5.995 (NOT 10).
        # With strict equality, this is interior -> no escalation.
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
            },
            selection_kwargs={"criteria": "gaussian.mspe",
                               "data_path": rds,
                               "X_val_expr": "bi$target$testing$X",
                               "y_val_expr": "bi$target$testing$y"},
            escalation_ceilings=[30, 50, 100],
        )
        ok = [_check("status ok", r.get("status") == "ok")]
        if r.get("status") != "ok":
            return False
        ok.append(_check("only one rung walked (no escalation)",
                          len(r["escalation_history"]) == 1))
        ok.append(_check("final eta ~5.995 (not at boundary)",
                          abs(float(r["final_eta_min"]) - 5.995) < 0.01))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -- De-escalation ---------------------------------------------------------

def test_de_escalation_fires():
    print("\n--- Test 4: de-escalation fires when initial eta < threshold ---")
    workdir = tempfile.mkdtemp(prefix="v120_t4_")
    try:
        rds = _stage_bi(workdir)
        # Force the path: set de_escalation_threshold high enough that
        # the typical interior optimum (eta=5.995) triggers de-escalation.
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
            },
            selection_kwargs={"criteria": "gaussian.mspe",
                               "data_path": rds,
                               "X_val_expr": "bi$target$testing$X",
                               "y_val_expr": "bi$target$testing$y"},
            escalation_ceilings=[],
            de_escalation_threshold=8.0,
            de_escalation_ceiling=6.0,
        )
        ok = [_check("status ok", r.get("status") == "ok")]
        if r.get("status") != "ok":
            return False
        ok.append(_check("de_escalated flag is True",
                          r.get("de_escalated") is True))
        ok.append(_check("history has 2 rungs",
                          len(r["escalation_history"]) == 2))
        ok.append(_check("rung 1 tagged as de_escalation",
                          r["escalation_history"][1].get(
                              "de_escalation") is True))
        ok.append(_check("rung 1 ceiling == de_escalation_ceiling",
                          r["escalation_history"][1]["ceiling"] == 6.0))
        ok.append(_check("final_eta_ceiling reflects de-escalation",
                          r["final_eta_ceiling"] == 6.0))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_de_escalation_quiet_when_above_threshold():
    print("\n--- Test 5: de-escalation does NOT fire when eta >= threshold ---")
    workdir = tempfile.mkdtemp(prefix="v120_t5_")
    try:
        rds = _stage_bi(workdir)
        # Default threshold = 1.0; MSPE lands at eta=5.995 -> above threshold
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
            },
            selection_kwargs={"criteria": "gaussian.mspe",
                               "data_path": rds,
                               "X_val_expr": "bi$target$testing$X",
                               "y_val_expr": "bi$target$testing$y"},
            escalation_ceilings=[],
            # defaults: threshold=1.0, ceiling=2.0
        )
        ok = [_check("status ok", r.get("status") == "ok")]
        if r.get("status") != "ok":
            return False
        ok.append(_check("de_escalated is False",
                          r.get("de_escalated") is False))
        ok.append(_check("only one rung walked",
                          len(r["escalation_history"]) == 1))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_mutual_exclusion():
    print("\n--- Test 6: escalation and de-escalation are mutually exclusive ---")
    workdir = tempfile.mkdtemp(prefix="v120_t6_")
    try:
        rds = _stage_bi(workdir)
        # BIC escalates. Set de_escalation_threshold huge so it WOULD fire
        # if mutual exclusion were broken (eta=30 < 100).
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
            },
            selection_kwargs={"criteria": "BIC"},
            escalation_ceilings=[30],
            de_escalation_threshold=100,  # would trip otherwise
            de_escalation_ceiling=5,
        )
        ok = [_check("status ok", r.get("status") == "ok")]
        if r.get("status") != "ok":
            return False
        ok.append(_check("de_escalated is False after escalation",
                          r.get("de_escalated") is False))
        # Walked 2 rungs of escalation, no de-escalation rung appended
        ok.append(_check("history has exactly 2 rungs (no de-escalation)",
                          len(r["escalation_history"]) == 2))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_de_escalation_zero_threshold_disables():
    print("\n--- Test 7: de_escalation_threshold=0 disables de-escalation ---")
    workdir = tempfile.mkdtemp(prefix="v120_t7_")
    try:
        rds = _stage_bi(workdir)
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
            },
            selection_kwargs={"criteria": "gaussian.mspe",
                               "data_path": rds,
                               "X_val_expr": "bi$target$testing$X",
                               "y_val_expr": "bi$target$testing$y"},
            escalation_ceilings=[],
            de_escalation_threshold=0,  # disable
        )
        ok = [_check("status ok", r.get("status") == "ok")]
        if r.get("status") != "ok":
            return False
        ok.append(_check("de_escalated is False",
                          r.get("de_escalated") is False))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    print("BRIER MCP v0.12 auto-tune refinements test suite")
    all_pass = True
    all_pass &= test_strict_equality_default()
    all_pass &= test_escalation_strict_only_at_max()
    all_pass &= test_interior_below_max_does_not_escalate()
    all_pass &= test_de_escalation_fires()
    all_pass &= test_de_escalation_quiet_when_above_threshold()
    all_pass &= test_mutual_exclusion()
    all_pass &= test_de_escalation_zero_threshold_disables()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
