"""Tests for v0.11.

Covers brier_auto_tune_eta:
- near-boundary helper math
- ladder-above helper
- empty ladder (no escalation, one rung)
- ladder skips rungs below initial ceiling
- escalation stops on interior optimum (early termination)
- ladder exhaustion (boundary persists -> notice fires)
- eta_list rejection
- bad-family rejection

Run:
  cd mcp/
  uv run tests/test_v110.py
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


# -- Helper tests ----------------------------------------------------------

def test_is_near_boundary():
    print("\n--- Test 1: _is_near_boundary helper ---")
    g = server._build_eta_grid()  # [0, 0.1, ..., 10]
    ok = []
    ok.append(_check("eta=10 is near-boundary", server._is_near_boundary(10, g)))
    ok.append(_check("eta=5.995 (top 20%) is near-boundary",
                      server._is_near_boundary(5.995, g)))
    ok.append(_check("eta=3.594 is NOT near-boundary (just below 3.98)",
                      not server._is_near_boundary(3.594, g)))
    ok.append(_check("eta=0.1 is NOT near-boundary",
                      not server._is_near_boundary(0.1, g)))
    ok.append(_check("M=2 with one component near-boundary fires",
                      server._is_near_boundary([10, 0.5], g)))
    ok.append(_check("M=2 with both interior is quiet",
                      not server._is_near_boundary([1, 0.5], g)))
    # Tighter threshold (top 5%): only eta >= ~7.9 fires
    ok.append(_check("eta=5.995 NOT near at top_fraction=0.05",
                      not server._is_near_boundary(5.995, g, 0.05)))
    ok.append(_check("eta=10 still near at top_fraction=0.05",
                      server._is_near_boundary(10, g, 0.05)))
    return all(ok)


def test_ladder_above():
    print("\n--- Test 2: _ladder_above helper ---")
    ok = []
    ok.append(_check("[30,50,100] above 10 -> [30,50,100]",
                      server._ladder_above(10, [30, 50, 100]) == [30.0, 50.0, 100.0]))
    ok.append(_check("[30,50,100] above 40 -> [50,100]",
                      server._ladder_above(40, [30, 50, 100]) == [50.0, 100.0]))
    ok.append(_check("[30,50,100] above 100 -> []",
                      server._ladder_above(100, [30, 50, 100]) == []))
    ok.append(_check("empty ladder -> []",
                      server._ladder_above(10, []) == []))
    ok.append(_check("ladder gets sorted",
                      server._ladder_above(10, [100, 30, 50]) == [30.0, 50.0, 100.0]))
    return all(ok)


# -- Behavior tests --------------------------------------------------------

def test_empty_ladder_runs_one_rung():
    print("\n--- Test 3: empty escalation_ceilings -> one rung only ---")
    workdir = tempfile.mkdtemp(prefix="v110_t3_")
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
            selection_kwargs={"criteria": "BIC"},
            escalation_ceilings=[],
        )
        ok = [_check("status ok", r.get("status") == "ok",
                      detail=str(r.get("message", "")))]
        if r.get("status") != "ok":
            return False
        ok.append(_check("exactly 1 rung walked",
                          len(r["escalation_history"]) == 1))
        ok.append(_check("ceiling was the default 10",
                          r["final_eta_ceiling"] == 10.0))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_escalation_stops_on_interior():
    print("\n--- Test 4: escalation stops the first time optimum is interior ---")
    workdir = tempfile.mkdtemp(prefix="v110_t4_")
    try:
        rds = _stage_bi(workdir)
        # Validation MSPE on the canonical data lands near-boundary at
        # ceiling=10 (eta~5.995) but interior at ceiling=30 (eta~4.48).
        # Confirmed empirically in v0.11 build session.
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
            },
            selection_kwargs={
                "criteria": "gaussian.mspe",
                "data_path": rds,
                "X_val_expr": "bi$target$testing$X",
                "y_val_expr": "bi$target$testing$y",
            },
            escalation_ceilings=[30, 50, 100],
            # v0.11 behavior: top-20% trigger (v0.12 default tightened to strict).
            # Disable de-escalation (v0.12 feature) for a clean v0.11 path.
            near_boundary_top_fraction=0.20,
            de_escalation_threshold=0,
        )
        ok = [_check("status ok", r.get("status") == "ok",
                      detail=str(r.get("message", "")))]
        if r.get("status") != "ok":
            return False
        rungs = r["escalation_history"]
        # First rung should be near-boundary, second interior.
        ok.append(_check("rung 0 (ceiling=10) was near-boundary",
                          rungs[0]["ceiling"] == 10.0 and
                          rungs[0]["hit_near_boundary"]))
        ok.append(_check("rung 1 (ceiling=30) was interior - loop stops",
                          len(rungs) == 2 and rungs[1]["ceiling"] == 30.0 and
                          not rungs[1]["hit_near_boundary"]))
        ok.append(_check("final_hit_near_boundary is False",
                          r["final_hit_near_boundary"] is False))
        ok.append(_check("no exhausted-ladder notice",
                          "_notice" not in r))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_ladder_exhausted_notice():
    print("\n--- Test 5: ladder exhausted -> notice fires ---")
    workdir = tempfile.mkdtemp(prefix="v110_t5_")
    try:
        rds = _stage_bi(workdir)
        # BIC on Data_BRIERi keeps picking boundary at every ceiling.
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
            },
            selection_kwargs={"criteria": "BIC"},
            escalation_ceilings=[30, 50, 100],
        )
        ok = [_check("status ok", r.get("status") == "ok")]
        if r.get("status") != "ok":
            return False
        ok.append(_check("walked all 4 rungs (10, 30, 50, 100)",
                          len(r["escalation_history"]) == 4))
        ok.append(_check("final_hit_near_boundary is True",
                          r["final_hit_near_boundary"] is True))
        ok.append(_check("_notice present (ladder exhausted)",
                          "_notice" in r))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_ladder_skips_rungs_below_initial():
    print("\n--- Test 6: ladder skips rungs <= initial eta_ceiling ---")
    workdir = tempfile.mkdtemp(prefix="v110_t6_")
    try:
        rds = _stage_bi(workdir)
        # initial ceiling 40 with ladder [30, 50, 100] -> only 50 and 100
        # are above, so max 3 rungs: [40, 50, 100]
        r = server.brier_auto_tune_eta(
            family="brier_i",
            fit_kwargs={
                "data_path": rds, "X_expr": "bi$target$train$X",
                "y_expr": "bi$target$train$y",
                "beta_external_expr": "bi$beta.external[, 1, drop=FALSE]",
                "family": "gaussian",
                "eta_ceiling": 40,
            },
            selection_kwargs={"criteria": "BIC"},
            escalation_ceilings=[30, 50, 100],
        )
        ok = [_check("status ok", r.get("status") == "ok")]
        if r.get("status") != "ok":
            return False
        ceilings_seen = [row["ceiling"] for row in r["escalation_history"]]
        ok.append(_check("first ceiling is 40 (initial)",
                          ceilings_seen[0] == 40.0))
        ok.append(_check("never visits ceiling=30 (below initial)",
                          30.0 not in ceilings_seen))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_eta_list_rejected():
    print("\n--- Test 7: eta_list in fit_kwargs is rejected ---")
    r = server.brier_auto_tune_eta(
        family="brier_i",
        fit_kwargs={"eta_list": [0, 1, 5]},
        selection_kwargs={"criteria": "BIC"},
    )
    ok = [
        _check("status error", r.get("status") == "error"),
        _check("message mentions eta_list",
                "eta_list" in r.get("message", "")),
    ]
    return all(ok)


def test_bad_family_rejected():
    print("\n--- Test 8: unknown family is rejected ---")
    r = server.brier_auto_tune_eta(
        family="brier_x",
        fit_kwargs={},
        selection_kwargs={},
    )
    ok = [
        _check("status error", r.get("status") == "error"),
        _check("message mentions allowed families",
                "brier_i" in r.get("message", "") and
                "brier_s" in r.get("message", "")),
    ]
    return all(ok)


def main():
    print("BRIER MCP v0.11 brier_auto_tune_eta test suite")
    all_pass = True
    all_pass &= test_is_near_boundary()
    all_pass &= test_ladder_above()
    all_pass &= test_empty_ladder_runs_one_rung()
    all_pass &= test_escalation_stops_on_interior()
    all_pass &= test_ladder_exhausted_notice()
    all_pass &= test_ladder_skips_rungs_below_initial()
    all_pass &= test_eta_list_rejected()
    all_pass &= test_bad_family_rejected()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
