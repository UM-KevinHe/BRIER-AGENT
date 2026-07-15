"""End-to-end smoke test for the BRIER MCP v0.2.0 surface.

Exercises every tool with synthetic data: inspect_data,
list_data_directory, brier_i, brier_i_cv, brier_i_selection. Runs
without Claude Desktop.

Prerequisites:
  * R >= 4.0 with: jsonlite, BRIER (>= 1.0.2). BRIER pulls in Rcpp,
    Matrix, ggplot2, pROC, rlang as transitive deps.
  * Python: just the `mcp` package, handled by uv.

Run:
  cd mcp/
  uv run tests/test_v02.py
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


# --------------------------------------------------------------------------
# Test scaffolding
# --------------------------------------------------------------------------

def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def _make_brier_i_data() -> tuple[str, str]:
    """Generate a synthetic BRIERi data file. Returns (dir, rda_path)."""
    workdir = tempfile.mkdtemp(prefix="brier_v02_test_")
    rda_path = os.path.join(workdir, "synth.rda")

    # n_target=80, p=30, M=2 external coefficients.
    # beta.external has p+1=31 rows (intercept slot first), 2 cols.
    r_code = f"""
        set.seed(42)
        n <- 80; p <- 30; M <- 2
        X <- matrix(rnorm(n * p), nrow = n, ncol = p)
        beta_true <- c(rep(0.5, 5), rep(0, p - 5))
        y <- as.numeric(X %*% beta_true + rnorm(n, sd = 0.5))
        beta.external <- rbind(
          intercept = c(0, 0),
          matrix(rnorm(p * M, sd = 0.3), nrow = p, ncol = M)
        )
        # Held-out validation pieces for validation-set selection.
        X.val <- matrix(rnorm(40 * p), nrow = 40, ncol = p)
        y.val <- as.numeric(X.val %*% beta_true + rnorm(40, sd = 0.5))
        save(X, y, beta.external, X.val, y.val,
             file = '{rda_path.replace(chr(92), '/')}')
    """

    rscript = server._find_rscript()
    proc = subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e", r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to make test data:\n{proc.stderr}")
    return workdir, rda_path


# --------------------------------------------------------------------------
# Test 1: inspect_data (regression from v0.1)
# --------------------------------------------------------------------------

def test_inspect_data(rda_path) -> bool:
    print("\n--- Test 1: inspect_data round-trip ---")
    r = server.inspect_data(data_path=rda_path)
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"got {r.get('status')!r}, msg={r.get('message')}"))
    names = set(r.get("top_level_names") or [])
    ok.append(_check("contains X, y, beta.external, X.val, y.val",
                     names == {"X", "y", "beta.external", "X.val", "y.val"},
                     detail=f"got {sorted(names)}"))
    return all(ok)


# --------------------------------------------------------------------------
# Test 2: list_data_directory
# --------------------------------------------------------------------------

def test_list_data_directory(workdir, rda_path) -> bool:
    print("\n--- Test 2: list_data_directory ---")
    r = server.list_data_directory(dir_path=workdir)
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"got {r.get('status')!r}, msg={r.get('message')}"))
    ok.append(_check("found exactly one .rda file",
                     r.get("n_files") == 1,
                     detail=f"got n_files={r.get('n_files')}"))
    files = r.get("files") or []
    if files:
        ok.append(_check(
            "first file matches our test data",
            files[0]["path"] == rda_path,
            detail=f"got {files[0]['path']!r}",
        ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 3: brier_i happy path
# --------------------------------------------------------------------------

def test_brier_i(rda_path) -> dict:
    print("\n--- Test 3: brier_i happy path ---")
    r = server.brier_i(
        data_path=rda_path,
        X_expr="X",
        y_expr="y",
        beta_external_expr="beta.external",
        family="gaussian",
        eta_list=[0, 0.5, 1, 2],
        multi_method="stacking",
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"got {r.get('status')!r}, msg={r.get('message')}"))
    ok.append(_check("returned a fit_id", bool(r.get("fit_id")),
                     detail=f"got fit_id={r.get('fit_id')!r}"))
    ok.append(_check("fit file exists",
                     r.get("fit_path") and Path(r["fit_path"]).exists(),
                     detail=f"path={r.get('fit_path')}"))
    ok.append(_check("n_target=80, p=30, M=2",
                     r.get("n_target") == 80 and r.get("p") == 30
                     and r.get("M_external") == 2,
                     detail=f"got n={r.get('n_target')}, p={r.get('p')}, M={r.get('M_external')}"))
    ok.append(_check("_followup_offer_selection hint present",
                     "_followup_offer_selection" in r))
    # When family is explicitly supplied, family_default notice should NOT fire.
    ok.append(_check("no family_default notice (family was supplied)",
                     "_notice_family_default" not in r))
    return r if all(ok) else {}


# --------------------------------------------------------------------------
# Test 4: brier_i with intercept-row check (should fail loudly)
# --------------------------------------------------------------------------

def test_brier_i_intercept_check(workdir) -> bool:
    print("\n--- Test 4: brier_i shape check (missing intercept row) ---")
    # Build a fresh dataset where beta.external is p x M (NO intercept row).
    rda_bad = os.path.join(workdir, "synth_no_intercept.rda")
    r_code = f"""
        set.seed(43)
        n <- 50; p <- 20; M <- 2
        X <- matrix(rnorm(n * p), nrow = n, ncol = p)
        y <- rnorm(n)
        beta.external.bad <- matrix(rnorm(p * M, sd = 0.3), nrow = p, ncol = M)
        save(X, y, beta.external.bad,
             file = '{rda_bad.replace(chr(92), '/')}')
    """
    rscript = server._find_rscript()
    subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e", r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )

    r = server.brier_i(
        data_path=rda_bad, X_expr="X", y_expr="y",
        beta_external_expr="beta.external.bad", family="gaussian",
    )
    ok = []
    ok.append(_check("status error", r.get("status") == "error"))
    msg = r.get("message") or ""
    ok.append(_check("error mentions intercept row",
                     "intercept" in msg.lower(),
                     detail=f"got: {msg[:200]!r}"))
    return all(ok)


# --------------------------------------------------------------------------
# Test 5: brier_i_selection with BIC
# --------------------------------------------------------------------------

def test_brier_i_selection_bic(fit_id) -> bool:
    print("\n--- Test 5: brier_i_selection with BIC (IC-based, no val set) ---")
    r = server.brier_i_selection(fit_id=fit_id, criteria="BIC")
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"got {r.get('status')!r}, msg={r.get('message')}"))
    ok.append(_check("returned selected_lambda",
                     r.get("selected_lambda") is not None,
                     detail=f"got {r.get('selected_lambda')!r}"))
    return all(ok)


# --------------------------------------------------------------------------
# Test 6: brier_i_selection with validation MSPE
# --------------------------------------------------------------------------

def test_brier_i_selection_val(fit_id, rda_path) -> bool:
    print("\n--- Test 6: brier_i_selection with gaussian.mspe + held-out val set ---")
    r = server.brier_i_selection(
        fit_id=fit_id,
        criteria="gaussian.mspe",
        X_val_expr="X.val",
        y_val_expr="y.val",
        data_path=rda_path,
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"got {r.get('status')!r}, msg={r.get('message')}"))
    ok.append(_check("returned selected_lambda",
                     r.get("selected_lambda") is not None))
    return all(ok)


# --------------------------------------------------------------------------
# Test 7: brier_i_cv emits the leakage warning always
# --------------------------------------------------------------------------

def test_brier_i_cv_emits_leakage_notice(rda_path) -> bool:
    print("\n--- Test 7: brier_i_cv always emits _notice_brier_i_cv_leakage ---")
    r = server.brier_i_cv(
        data_path=rda_path,
        X_expr="X", y_expr="y",
        beta_external_expr="beta.external",
        family="gaussian", nfolds=3,
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"got {r.get('status')!r}, msg={r.get('message')}"))
    ok.append(_check("leakage notice present",
                     "_notice_brier_i_cv_leakage" in r))
    return all(ok)


# --------------------------------------------------------------------------
# Test 8: deny-list catches malicious expression
# --------------------------------------------------------------------------

def test_denylist(rda_path) -> bool:
    print("\n--- Test 8: deny-list blocks system() expression ---")
    r = server.brier_i(
        data_path=rda_path,
        X_expr='system("rm -rf /tmp/foo")',
        y_expr="y",
        beta_external_expr="beta.external",
    )
    ok = []
    ok.append(_check("status error", r.get("status") == "error"))
    ok.append(_check("class DenylistViolation",
                     r.get("class") == "DenylistViolation",
                     detail=f"got class={r.get('class')!r}"))
    return all(ok)


# --------------------------------------------------------------------------
# Test 9: family default notice fires when family is not supplied
#         (still works because brier_i Python signature defaults to gaussian,
#          but the dispatcher detects whether the caller passed it explicitly
#          via inp$family. The Python tool defaults to "gaussian" which IS
#          passed - so this test asserts the EXPECTED current behavior.)
# --------------------------------------------------------------------------

def test_family_default_fires_for_omitted(rda_path) -> bool:
    print("\n--- Test 9: family_default notice fires when omitted at R layer ---")
    # Direct R-layer call bypassing the Python default.
    r = server._run_r("brier_i.R", {
        "data_path": rda_path,
        "X_expr": "X",
        "y_expr": "y",
        "beta_external_expr": "beta.external",
        # family deliberately omitted
        "eta_list": [0, 1],
        "multi_method": "stacking",
    })
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"got {r.get('status')!r}, msg={r.get('message')}"))
    ok.append(_check("_notice_family_default present",
                     "_notice_family_default" in r))
    return all(ok)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    print("BRIER MCP v0.2.0 end-to-end smoke test")
    print(f"  Rscript: {server._find_rscript()}")

    workdir, rda_path = _make_brier_i_data()
    try:
        all_pass = True
        all_pass &= test_inspect_data(rda_path)
        all_pass &= test_list_data_directory(workdir, rda_path)

        fit_result = test_brier_i(rda_path)
        all_pass &= bool(fit_result)

        all_pass &= test_brier_i_intercept_check(workdir)

        if fit_result:
            all_pass &= test_brier_i_selection_bic(fit_result["fit_id"])
            all_pass &= test_brier_i_selection_val(
                fit_result["fit_id"], rda_path)

        all_pass &= test_brier_i_cv_emits_leakage_notice(rda_path)
        all_pass &= test_denylist(rda_path)
        all_pass &= test_family_default_fires_for_omitted(rda_path)

        print()
        print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
        return 0 if all_pass else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
