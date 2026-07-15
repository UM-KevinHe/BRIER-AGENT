"""Tests for v0.7.1 fixes and additions.

Covers six items:
  1. M-based size recommendation (M >= 3 -> BRIERi over BRIERfull)
  2. Coarser default eta grid in brier_full (7 values, not 21)
  3. eta=0 baseline auto-fix in brier_i (stacking -> ind for zero externals)
  4. Time-expectation field in size_recommendation
  5. BRIERs un-standardize predictions (y_center / y_scale params)
  6. Output-directory configuration tools (set/get)

Run:
  cd mcp/
  uv run tests/test_v071.py
"""
from __future__ import annotations

import json
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
# Test 1: M-based recommendation
# --------------------------------------------------------------------------

def test_m_based_recommendation() -> bool:
    print("\n--- Test 1: M-based recommendation rule ---")
    ok = []

    # M=3 with modest total -> BRIERi (matches Data_BRIERfull live-test case)
    rec = server._recommend_for_sizes(
        n_target=150, n_external_total=900,
        has_individual_external=True, M=3,
    )
    ok.append(_check(
        "M=3 + small total -> primary BRIERi",
        rec and rec["primary"] == "BRIERi",
        detail=f"got primary={rec.get('primary') if rec else None}",
    ))
    ok.append(_check(
        "M=3 reason mentions M (= 3) explicitly",
        rec and "M = 3" in rec.get("reason", ""),
    ))
    ok.append(_check(
        "M=3 includes time_expectation field",
        rec and "time_expectation" in rec,
    ))

    # M=2 -> BRIERfull (below threshold)
    rec2 = server._recommend_for_sizes(
        n_target=150, n_external_total=300,
        has_individual_external=True, M=2,
    )
    ok.append(_check(
        "M=2 + small total -> primary BRIERfull",
        rec2 and rec2["primary"] == "BRIERfull",
        detail=f"got primary={rec2.get('primary') if rec2 else None}",
    ))

    # M=5 -> BRIERi (well over threshold)
    rec3 = server._recommend_for_sizes(
        n_target=200, n_external_total=1000,
        has_individual_external=True, M=5,
    )
    ok.append(_check(
        "M=5 -> primary BRIERi",
        rec3 and rec3["primary"] == "BRIERi",
    ))

    # M=1 with huge total still goes to BRIERi via the n-based rule
    rec4 = server._recommend_for_sizes(
        n_target=2000, n_external_total=20000,
        has_individual_external=True, M=1,
    )
    ok.append(_check(
        "M=1 + huge total -> BRIERi (via size rule, not M rule)",
        rec4 and rec4["primary"] == "BRIERi",
    ))
    ok.append(_check(
        "M=1 + huge total reason explains BRIERfull slowness at scale",
        rec4 and ("slow" in rec4.get("reason", "").lower()
                  or "large" in rec4.get("reason", "").lower()),
    ))

    return all(ok)


# --------------------------------------------------------------------------
# Test 2: Coarser default eta grid for brier_full
# --------------------------------------------------------------------------

def test_brier_full_coarser_eta_grid() -> bool:
    print("\n--- Test 2: brier_full default eta grid is now 7 values ---")
    # Set up a minimal BRIERfull-shaped dataset
    workdir = tempfile.mkdtemp(prefix="brier_v071_test_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        r_code = (
            "set.seed(3);\n"
            "n_t <- 80; n_e <- 80; p <- 20;\n"
            "X_t <- matrix(rnorm(n_t*p), n_t, p); y_t <- rnorm(n_t);\n"
            "X_e <- matrix(rnorm(n_e*p), n_e, p); y_e <- rnorm(n_e);\n"
            "X.full <- rbind(X_t, X_e);\n"
            "y.full <- c(y_t, y_e);\n"
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
        ok = []
        ok.append(_check(
            "brier_full status ok",
            r.get("status") == "ok",
            detail=f"msg={r.get('message')}",
        ))
        if r.get("status") != "ok":
            return False
        eta_used = r.get("eta_list_used")
        # eta_list_used may be a per-external list-of-lists or a flat list
        if isinstance(eta_used, list) and eta_used and isinstance(eta_used[0], list):
            n_eta = len(eta_used[0])
        elif isinstance(eta_used, list):
            n_eta = len(eta_used)
        else:
            n_eta = None
        ok.append(_check(
            "M=1 default eta grid is 11 values "
            "(v0.10.3 principled default: 0 + 10 log-spaced log(0.1)..log(10))",
            n_eta == 11,
            detail=f"got n_eta={n_eta}",
        ))
        # v0.10.3 removed the _notice_default_eta_grid pseudo-notice;
        # the default is now baked into the tool surface itself.
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 3: eta=0 baseline auto-fix
# --------------------------------------------------------------------------

def test_eta_zero_baseline_auto_fix() -> bool:
    print("\n--- Test 3: eta=0 baseline auto-fix (stacking -> ind for zero externals) ---")
    workdir = tempfile.mkdtemp(prefix="brier_v071_test_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        # Build a dataset with an explicit ALL-ZERO beta_external
        # of shape (p+1) x M. Without the auto-fix, this triggers a
        # singular-matrix error in BRIERi's stacking path.
        r_code = (
            "set.seed(4);\n"
            "n <- 60; p <- 15;\n"
            "X <- matrix(rnorm(n*p), n, p);\n"
            "y <- rnorm(n);\n"
            "beta_zero <- matrix(0, nrow = p+1, ncol = 1);\n"
            f"saveRDS(list(X=X, y=y, beta_zero=beta_zero), '{rds_path}')"
        )
        subprocess.run([rscript, "--no-save", "--no-restore",
                        "--no-init-file", "-e", r_code],
                       capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)

        r = server.brier_i(
            data_path=rds_path,
            X_expr="synth$X",
            y_expr="synth$y",
            beta_external_expr="synth$beta_zero",
            family="gaussian",
            eta_list=[0],
            multi_method="stacking",   # auto-fix should switch to ind
        )
        ok = []
        ok.append(_check(
            "brier_i status ok (auto-fix prevents singular-matrix crash)",
            r.get("status") == "ok",
            detail=f"msg={r.get('message')}",
        ))
        if r.get("status") != "ok":
            return False
        ok.append(_check(
            "multi_method_used reports 'ind' (auto-switched from 'stacking')",
            r.get("multi_method_used") == "ind",
        ))
        ok.append(_check(
            "_notice_baseline_auto_ind fires",
            "_notice_baseline_auto_ind" in r,
        ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 4: time_expectation field in size_recommendation
# --------------------------------------------------------------------------

def test_time_expectation_field() -> bool:
    print("\n--- Test 4: time_expectation field present in size_recommendation ---")
    ok = []
    # Each rule branch should populate time_expectation:
    for label, kwargs in [
        ("M >= 3 branch",
         dict(n_target=150, n_external_total=900,
              has_individual_external=True, M=3)),
        ("total > 10000 branch",
         dict(n_target=2000, n_external_total=20000,
              has_individual_external=True, M=1)),
        ("small total branch",
         dict(n_target=100, n_external_total=200,
              has_individual_external=True, M=2)),
        ("unknown-sizes branch",
         dict(n_target=None, n_external_total=None,
              has_individual_external=True, M=None)),
    ]:
        rec = server._recommend_for_sizes(**kwargs)
        ok.append(_check(
            f"{label} -> time_expectation present",
            rec and "time_expectation" in rec,
            detail=f"got {list((rec or {}).keys())}",
        ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 5: BRIERs un-standardize predictions via explicit y_center/y_scale.
#
# v0.8.1 keeps these as predict-time args (explicit, no auto-stash, no
# auto-apply). The user is responsible for sourcing the scalars:
# train y is unbiased, test y is slightly biased, external scalars are
# as good as the source. The MCP does not auto-source them.
# --------------------------------------------------------------------------

def test_brier_predict_unstandardize() -> bool:
    print("\n--- Test 5: brier_predict y_center / y_scale un-standardization (explicit) ---")
    workdir = tempfile.mkdtemp(prefix="brier_v071_test_")
    try:
        rds_path = os.path.join(workdir, "synth.rds")
        rscript = server._find_rscript()
        r_code = (
            "set.seed(5);\n"
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
        if fit.get("status") != "ok":
            return _check("fit ok", False, detail=fit.get("message"))

        sel = server.brier_i_selection(
            fit_id=fit["fit_id"], criteria="BIC",
        )
        if sel.get("status") != "ok":
            return _check("selection ok", False, detail=sel.get("message"))

        ok = []

        # Baseline: no un-standardize
        p1 = server.brier_predict(
            data_path=rds_path, newx_expr="synth$X_test",
            selection_id=sel["selection_id"],
        )
        ok.append(_check(
            "predict without unstandardize: status ok",
            p1.get("status") == "ok",
            detail=f"msg={p1.get('message')}",
        ))
        if p1.get("status") != "ok":
            return False
        baseline_mean = p1["summary"]["mean"]
        ok.append(_check(
            "no unstandardize -> no _notice_unstandardize_applied",
            "_notice_unstandardize_applied" not in p1,
        ))

        # Explicit y_center / y_scale
        p2 = server.brier_predict(
            data_path=rds_path, newx_expr="synth$X_test",
            selection_id=sel["selection_id"],
            y_center=10.0, y_scale=2.0,
        )
        ok.append(_check(
            "predict with explicit unstandardize: status ok",
            p2.get("status") == "ok",
            detail=f"msg={p2.get('message')}",
        ))
        if p2.get("status") != "ok":
            return False
        unstd_mean = p2["summary"]["mean"]
        expected = baseline_mean * 2.0 + 10.0
        ok.append(_check(
            "un-standardized mean = baseline * scale + center",
            abs(unstd_mean - expected) < 1e-3,
            detail=f"baseline={baseline_mean:.4f}, unstd={unstd_mean:.4f}, "
                   f"expected={expected:.4f}",
        ))
        ok.append(_check(
            "_notice_unstandardize_applied fires",
            "_notice_unstandardize_applied" in p2,
        ))
        # v0.8.1 specifically: notice should NOT mention "stashed" since
        # there's no stashing anymore
        notice = p2.get("_notice_unstandardize_applied", "")
        ok.append(_check(
            "notice describes user-supplied (not stashed)",
            "user-supplied" in notice.lower() or "supplied" in notice.lower(),
        ))

        # Invalid y_scale=0 should warn
        p3 = server.brier_predict(
            data_path=rds_path, newx_expr="synth$X_test",
            selection_id=sel["selection_id"],
            y_center=10.0, y_scale=0.0,
        )
        ok.append(_check(
            "y_scale=0 emits _notice_unstandardize_warning",
            p3.get("status") == "ok" and
            "_notice_unstandardize_warning" in p3,
        ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Test 6: set / get output_directory
# --------------------------------------------------------------------------

def test_output_directory_config() -> bool:
    print("\n--- Test 6: set_output_directory / get_output_directory ---")
    workdir = tempfile.mkdtemp(prefix="brier_v071_outputdir_")
    try:
        ok = []

        # Start: no output_directory set (or undo any prior setting)
        # We don't want this test to clobber a real user's config, so
        # use a temp XDG_CONFIG_HOME for the duration.
        with _temp_xdg_config():
            r1 = server.get_output_directory()
            ok.append(_check(
                "get with no config: status ok, output_directory is null",
                r1.get("status") == "ok" and r1.get("output_directory") is None,
            ))
            ok.append(_check(
                "get with no config: _notice_default present",
                "_notice_default" in r1,
            ))

            # Set a real directory
            r2 = server.set_output_directory(path=workdir)
            ok.append(_check(
                "set with valid dir: status ok",
                r2.get("status") == "ok",
                detail=f"msg={r2.get('message')}",
            ))
            ok.append(_check(
                "set returns the path it stored",
                r2.get("output_directory") == workdir,
            ))

            # Retrieve
            r3 = server.get_output_directory()
            ok.append(_check(
                "get after set: output_directory matches",
                r3.get("output_directory") == workdir,
            ))

            # Reject nonexistent
            r4 = server.set_output_directory(path="/this/does/not/exist")
            ok.append(_check(
                "nonexistent path -> status error",
                r4.get("status") == "error",
            ))

            # Reject empty
            r5 = server.set_output_directory(path="")
            ok.append(_check(
                "empty path -> status error",
                r5.get("status") == "error",
            ))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class _temp_xdg_config:
    """Context manager that swaps XDG_CONFIG_HOME to a temp dir for the
    duration of the block, so config-writing tests don't pollute the
    user's real ~/.config/brier-mcp/config.json.
    """
    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="brier_v071_xdg_")
        self.prev = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmpdir
        return self.tmpdir

    def __exit__(self, *args):
        if self.prev is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.prev
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    print("BRIER MCP v0.7.1 end-to-end smoke test")

    all_pass = True
    all_pass &= test_m_based_recommendation()
    all_pass &= test_brier_full_coarser_eta_grid()
    all_pass &= test_eta_zero_baseline_auto_fix()
    all_pass &= test_time_expectation_field()
    all_pass &= test_brier_predict_unstandardize()
    all_pass &= test_output_directory_config()

    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
