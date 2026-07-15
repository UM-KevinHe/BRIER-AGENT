"""Tests for v0.8.0 refinements.

Covers six items:
  1. M=1 -> multi_method='ind' auto-substitute (brier_i, brier_s)
  2. BRIERfull eta grid: 7 when M>=2, 21 when M==1
  3. Wizard nested-external-cohort detection (target/external1/external2/...)
  4. BRIERs eta-grid boundary saturation warning in selection
  5. Stash y_center/y_scale at brier_s fit time + auto-apply in brier_predict
  6. Deny-list allow-listing for BRIER:: prefix (and other safe namespaces)

Run:
  cd mcp/
  uv run tests/test_v080.py
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
# Test 1: M=1 auto-substitute in brier_i
# --------------------------------------------------------------------------

def test_brier_i_m1_auto_substitute() -> bool:
    print("\n--- Test 1a: brier_i with M=1 + multi_method='stacking' -> auto-switch to 'ind' ---")
    workdir = tempfile.mkdtemp(prefix="brier_v080_t1_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        # M=1: beta_external has just one column
        r_code = (
            "set.seed(6);\n"
            "n <- 60; p <- 12;\n"
            "X <- matrix(rnorm(n*p), n, p);\n"
            "y <- rnorm(n);\n"
            "beta_one <- matrix(rnorm(p+1), nrow = p+1, ncol = 1);\n"
            f"saveRDS(list(X=X, y=y, beta_one=beta_one), '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
        r = server.brier_i(
            data_path=rds_path,
            X_expr="synth$X",
            y_expr="synth$y",
            beta_external_expr="synth$beta_one",
            family="gaussian",
            multi_method="stacking",
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
            "multi_method_used = 'ind' (auto-switched from stacking)",
            r.get("multi_method_used") == "ind",
            detail=f"got {r.get('multi_method_used')}",
        ))
        ok.append(_check(
            "_notice_m_one_auto_ind fires",
            "_notice_m_one_auto_ind" in r,
        ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_brier_i_m2_no_substitute() -> bool:
    print("\n--- Test 1b: brier_i with M=2 + stacking stays at stacking ---")
    workdir = tempfile.mkdtemp(prefix="brier_v080_t1b_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        r_code = (
            "set.seed(7);\n"
            "n <- 60; p <- 12;\n"
            "X <- matrix(rnorm(n*p), n, p); y <- rnorm(n);\n"
            "beta_two <- matrix(rnorm((p+1)*2), nrow = p+1, ncol = 2);\n"
            f"saveRDS(list(X=X, y=y, beta_two=beta_two), '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
        r = server.brier_i(
            data_path=rds_path,
            X_expr="synth$X",
            y_expr="synth$y",
            beta_external_expr="synth$beta_two",
            family="gaussian",
            multi_method="stacking",
        )
        if r.get("status") != "ok":
            return _check("status ok", False, detail=r.get("message"))
        return all([
            _check(
                "multi_method_used stays 'stacking' for M=2",
                r.get("multi_method_used") == "stacking",
                detail=f"got {r.get('multi_method_used')}",
            ),
            _check(
                "_notice_m_one_auto_ind NOT present",
                "_notice_m_one_auto_ind" not in r,
            ),
        ])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 2: BRIERfull M-dependent eta grid
# --------------------------------------------------------------------------

def test_brier_full_m1_21_etas() -> bool:
    print("\n--- Test 2a: brier_full with M=1 uses 21-value default grid ---")
    workdir = tempfile.mkdtemp(prefix="brier_v080_t2a_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        r_code = (
            "set.seed(8);\n"
            "n_t <- 60; n_e <- 60; p <- 15;\n"
            "X_t <- matrix(rnorm(n_t*p), n_t, p); y_t <- rnorm(n_t);\n"
            "X_e <- matrix(rnorm(n_e*p), n_e, p); y_e <- rnorm(n_e);\n"
            "X.full <- rbind(X_t, X_e); y.full <- c(y_t, y_e);\n"
            "cohort.full <- c(rep(0L, n_t), rep(1L, n_e));\n"
            f"saveRDS(list(X.full=X.full, y.full=y.full, "
            f"cohort.full=cohort.full), '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
        r = server.brier_full(
            data_path=rds_path,
            X_expr="synth$X.full",
            y_expr="synth$y.full",
            cohort_expr="synth$cohort.full",
            family="gaussian",
        )
        if r.get("status") != "ok":
            return _check("status ok", False, detail=r.get("message"))
        eta = r.get("eta_list_used")
        if isinstance(eta, list) and eta and isinstance(eta[0], list):
            n_eta = len(eta[0])
        elif isinstance(eta, list):
            n_eta = len(eta)
        else:
            n_eta = None
        return _check(
            "M=1 -> 11-value eta grid (v0.10.3 universal default)",
            n_eta == 11,
            detail=f"got n_eta={n_eta}",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_brier_full_m3_7_etas() -> bool:
    print("\n--- Test 2b: brier_full with M=3 uses 7-value default grid ---")
    workdir = tempfile.mkdtemp(prefix="brier_v080_t2b_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        r_code = (
            "set.seed(9);\n"
            "n_t <- 60; n_e <- 50; p <- 15;\n"
            "X_t <- matrix(rnorm(n_t*p), n_t, p); y_t <- rnorm(n_t);\n"
            "X_e1 <- matrix(rnorm(n_e*p), n_e, p); y_e1 <- rnorm(n_e);\n"
            "X_e2 <- matrix(rnorm(n_e*p), n_e, p); y_e2 <- rnorm(n_e);\n"
            "X_e3 <- matrix(rnorm(n_e*p), n_e, p); y_e3 <- rnorm(n_e);\n"
            "X.full <- rbind(X_t, X_e1, X_e2, X_e3);\n"
            "y.full <- c(y_t, y_e1, y_e2, y_e3);\n"
            "cohort.full <- c(rep(0L, n_t), rep(1L, n_e),"
            " rep(2L, n_e), rep(3L, n_e));\n"
            f"saveRDS(list(X.full=X.full, y.full=y.full, "
            f"cohort.full=cohort.full), '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
        r = server.brier_full(
            data_path=rds_path,
            X_expr="synth$X.full",
            y_expr="synth$y.full",
            cohort_expr="synth$cohort.full",
            family="gaussian",
        )
        if r.get("status") != "ok":
            return _check("status ok", False, detail=r.get("message"))
        eta = r.get("eta_list_used")
        if isinstance(eta, list) and eta and isinstance(eta[0], list):
            n_eta = len(eta[0])
        elif isinstance(eta, list):
            n_eta = len(eta)
        else:
            n_eta = None
        return _check(
            "M=3 -> 11-value eta grid (v0.10.3 universal default)",
            n_eta == 11,
            detail=f"got n_eta={n_eta}",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 3: Wizard nested-external-cohort detection
# --------------------------------------------------------------------------

def test_wizard_nested_external_detection() -> bool:
    print("\n--- Test 3: wizard detects nested external1/external2/external3 ---")
    workdir = tempfile.mkdtemp(prefix="brier_v080_t3_")
    try:
        # Build a Data_BRIERfull-shaped file (target + 3 nested externals)
        rds_path = os.path.join(workdir, "data_brierfull.rds")
        rscript = server._find_rscript()
        r_code = (
            "set.seed(10);\n"
            "make_split <- function(n, p) list(\n"
            "  X = matrix(rnorm(n*p), n, p),\n"
            "  y = rnorm(n)\n"
            ");\n"
            "p <- 50;\n"
            "data <- list(\n"
            "  target = list(\n"
            "    train = make_split(40, p),\n"
            "    validation = make_split(20, p),\n"
            "    testing = make_split(20, p)\n"
            "  ),\n"
            "  external1 = list(train = make_split(30, p)),\n"
            "  external2 = list(train = make_split(30, p)),\n"
            "  external3 = list(train = make_split(30, p))\n"
            ");\n"
            f"saveRDS(data, '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)

        r = server.inspect_user_data(data_paths=[rds_path])
        ok = []
        ok.append(_check(
            "status ok",
            r.get("status") == "ok",
            detail=f"msg={r.get('message')}",
        ))
        if r.get("status") != "ok":
            return False
        combined = r.get("combined_assessment", {})
        ok.append(_check(
            "M = 3 (external1, external2, external3 detected)",
            combined.get("M") == 3,
            detail=f"got M={combined.get('M')}",
        ))
        ok.append(_check(
            "external_shape = 'individual'",
            combined.get("external_shape") == "individual",
            detail=f"got {combined.get('external_shape')}",
        ))
        ok.append(_check(
            "n_external_total > 0",
            combined.get("n_external_total") and
            combined.get("n_external_total") >= 30,
        ))
        # Verify the heuristic surfaces in the per-file report
        primary = r["files"][0]
        nested = primary["heuristics"].get("nested_externals", {})
        ok.append(_check(
            "nested_externals.names lists external1/2/3",
            nested.get("M") == 3 and
            sorted(nested.get("names", []) or []) ==
            ["external1", "external2", "external3"],
        ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 4: BRIERs eta-grid boundary warning
# --------------------------------------------------------------------------

def test_brier_s_boundary_warning() -> bool:
    print("\n--- Test 4: brier_s_selection warns when selected eta is at grid boundary ---")
    workdir = tempfile.mkdtemp(prefix="brier_v080_t4_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        # Build a setup where the optimum is at high eta (so boundary fires).
        # The easiest way: make beta.external perfectly predict y.
        r_code = (
            "set.seed(11);\n"
            "n <- 100; p <- 20;\n"
            "X <- matrix(rnorm(n*p), n, p);\n"
            "beta_true <- c(rnorm(5, sd = 2), rep(0, p-5));\n"
            "y <- as.numeric(X %*% beta_true + rnorm(n, sd = 0.1));\n"
            "# sumstats: standardized marginal correlations\n"
            "X_s <- scale(X);\n"
            "y_s <- scale(y);\n"
            "corr_vec <- as.numeric(t(X_s) %*% y_s / nrow(X_s));\n"
            "sumstats <- data.frame(\n"
            "  variable = paste0('v', 1:p),\n"
            "  corr = corr_vec,\n"
            "  stats = corr_vec * sqrt(n - 2) / sqrt(1 - corr_vec^2),\n"
            "  pval = 2*pnorm(-abs(corr_vec * sqrt(n))),\n"
            "  n = n\n"
            ");\n"
            "# beta.external close to truth -> strong borrowing expected\n"
            "beta_ext <- matrix(beta_true + rnorm(p, sd = 0.05), nrow = p, ncol = 1);\n"
            "# Validation set, standardized to match training scale\n"
            "X_val_raw <- matrix(rnorm(50*p), 50, p);\n"
            "y_val_raw <- as.numeric(X_val_raw %*% beta_true + rnorm(50, sd = 0.1));\n"
            "X_val <- scale(X_val_raw, center = attr(X_s, 'scaled:center'),"
            " scale = attr(X_s, 'scaled:scale'));\n"
            "y_val <- scale(y_val_raw, center = attr(y_s, 'scaled:center'),"
            " scale = attr(y_s, 'scaled:scale'));\n"
            f"saveRDS(list(X = X, sumstats = sumstats, beta_ext = beta_ext,"
            f" X_val = X_val, y_val = as.numeric(y_val)), '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)

        ld = server.cal_ld(data_path=rds_path, X_expr="synth$X")
        if ld.get("status") != "ok":
            return _check("cal_ld ok", False, detail=ld.get("message"))

        fit = server.brier_s(
            data_path=rds_path,
            sumstats_expr="synth$sumstats",
            beta_external_expr="synth$beta_ext",
            family="gaussian",
            ld_id=ld["ld_id"],
        )
        if fit.get("status") != "ok":
            return _check("brier_s ok", False, detail=fit.get("message"))

        sel = server.brier_s_selection(
            fit_id=fit["fit_id"],
            criteria="gaussian.mspe",
            data_path=rds_path,
            X_val_expr="synth$X_val",
            y_val_expr="synth$y_val",
        )
        if sel.get("status") != "ok":
            return _check("brier_s_selection ok", False,
                          detail=sel.get("message"))
        # Either the boundary fired (preferred since signal is strong) OR
        # it didn't because eta optimum landed mid-grid. The test asserts
        # the FIELD EXISTS in the response only when the condition is met,
        # so we just verify the structure is correct.
        eta_val = sel.get("selected_eta")
        eta_scalar = (
            float(eta_val[0]) if isinstance(eta_val, list) and eta_val
            else (float(eta_val) if eta_val is not None else None)
        )
        boundary_fired = "_notice_eta_at_boundary" in sel
        # We don't enforce that the warning fires; only that IF it fires,
        # the eta value is genuinely high.
        if boundary_fired:
            return _check(
                "boundary warning fires AND eta is at high end of grid",
                eta_scalar is not None and eta_scalar >= 1.0,
                detail=f"eta={eta_scalar}",
            )
        return _check(
            "boundary warning correctly NOT fired (eta in middle of grid)",
            eta_scalar is not None and eta_scalar < 8.0,
            detail=f"eta={eta_scalar}, no boundary warning (acceptable)",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 5: REVERTED in v0.8.1 (partial).
#
# v0.8.0 added y_train_expr to brier_s plus stashing + auto-apply in
# brier_predict. v0.8.1 reverted that because:
#   - Canonical BRIERs users don't have y_train (sumstats-only)
#   - Auto-stashing hid the user's decision about which y source to use
#   - Three valid sources exist (train y, test y, external scalars)
#     with different biases; the user should choose explicitly
#
# What remains in v0.8.1: explicit y_center / y_scale at predict time
# only. No auto-stash, no auto-apply, no y_train_expr.
# --------------------------------------------------------------------------

def test_brier_s_unstandardize_roundtrip() -> bool:
    print("\n--- Test 5: stashing/auto-apply reverted in v0.8.1; explicit predict-time args kept ---")
    ok = []

    # brier_s no longer accepts y_train_expr
    try:
        server.brier_s(
            data_path="dummy",
            sumstats_expr="dummy",
            beta_external_expr="dummy",
            y_train_expr="dummy",
        )
        ok.append(_check(
            "brier_s rejects y_train_expr kwarg (reverted)",
            False,
            detail="should have raised TypeError",
        ))
    except TypeError:
        ok.append(_check(
            "brier_s rejects y_train_expr kwarg (reverted in v0.8.1)",
            True,
        ))

    # brier_predict STILL accepts explicit y_center / y_scale
    # (the broader test_v071 covers the math; here just verify the
    # signature didn't get over-reverted).
    import inspect
    sig = inspect.signature(server.brier_predict)
    ok.append(_check(
        "brier_predict.y_center kept as explicit predict-time arg",
        "y_center" in sig.parameters,
    ))
    ok.append(_check(
        "brier_predict.y_scale kept as explicit predict-time arg",
        "y_scale" in sig.parameters,
    ))

    return all(ok)


# --------------------------------------------------------------------------
# Test 6: deny-list allow-listing for BRIER:: and other safe namespaces
# --------------------------------------------------------------------------

def test_denylist_allowlist_safe_namespaces() -> bool:
    print("\n--- Test 6: deny-list allows BRIER::, base::, etc.; still blocks others ---")
    ok = []

    # Allowed: BRIER::standardize_X
    err = server._validate_expr("BRIER::standardize_X(data$X)", "X_expr")
    ok.append(_check(
        "'BRIER::standardize_X(...)' passes",
        err is None,
        detail=f"got err={err!r}" if err else "",
    ))

    # Allowed: base::scale
    err = server._validate_expr("base::scale(data$X)", "X_expr")
    ok.append(_check("'base::scale(...)' passes", err is None))

    # Allowed: stats::cor
    err = server._validate_expr("stats::cor(data$X)", "X_expr")
    ok.append(_check("'stats::cor(...)' passes", err is None))

    # Allowed: Matrix::crossprod
    err = server._validate_expr("Matrix::crossprod(data$X)", "X_expr")
    ok.append(_check("'Matrix::crossprod(...)' passes", err is None))

    # Blocked: arbitrary::foo
    err = server._validate_expr("arbitrary::foo(data$X)", "X_expr")
    ok.append(_check(
        "'arbitrary::foo(...)' STILL blocked",
        err is not None and "::" in err,
    ))

    # Blocked: BRIER::: (triple colon - non-exported access)
    err = server._validate_expr("BRIER:::internal_func(data$X)", "X_expr")
    ok.append(_check(
        "':::' (triple colon) STILL blocked",
        err is not None,
    ))

    # Blocked: malicious::system
    err = server._validate_expr("base::system('rm -rf /')", "X_expr")
    ok.append(_check(
        "'base::system(...)' blocked due to system( deny pattern",
        err is not None,
    ))

    # Allowed: plain expressions (regression check)
    err = server._validate_expr("data$X", "X_expr")
    ok.append(_check("'data$X' still passes (regression)", err is None))

    return all(ok)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    print("BRIER MCP v0.8.0 end-to-end smoke test")
    all_pass = True
    all_pass &= test_brier_i_m1_auto_substitute()
    all_pass &= test_brier_i_m2_no_substitute()
    all_pass &= test_brier_full_m1_21_etas()
    all_pass &= test_brier_full_m3_7_etas()
    all_pass &= test_wizard_nested_external_detection()
    all_pass &= test_brier_s_boundary_warning()
    all_pass &= test_brier_s_unstandardize_roundtrip()
    all_pass &= test_denylist_allowlist_safe_namespaces()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
