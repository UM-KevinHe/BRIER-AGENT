"""Tests for v0.13.1 fixes.

Three fixes from real-world Claude Desktop runs:

1. start_analysis welcome no longer ships URLs in prose; instead exposes
   a structured `references` list and a `_display_instructions` string
   telling the assistant to render references verbatim.

2. ai_instructions includes the ETA GRID - DO NOT HAND-WRITE block,
   directing the assistant to omit eta_list (use the default) or call
   brier_auto_tune_eta rather than constructing custom grids from
   memory.

3. summarize_fit's bootstrap_plot_max_p hard cap is replaced with soft
   graceful degradation:
     - Default no longer skips on high p; emits a heads-up notice for
       p > 2000 instead.
     - Per-plot timeout (plot_timeout_seconds, default 300s).
     - On timeout / failure, the report still completes; the missing
       plot is replaced by a fallback notice + runnable R snippet.
     - Legacy bootstrap_plot_max_p kwarg still works for back-compat.

Run:
  cd mcp/
  uv run tests/test_v131.py
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


def _rscript():
    return server._find_rscript()


def _stage_bi(workdir):
    rds = os.path.join(workdir, "bi.rds")
    subprocess.run([_rscript(), "--no-save", "--no-restore",
                    "--no-init-file", "-e",
                    "suppressPackageStartupMessages(library(BRIER)); "
                    f"data(Data_BRIERi); saveRDS(Data_BRIERi, '{rds}')"],
                   capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    return rds


# -- Fix 1: welcome stays clean; background carries depth + URLs ----------

def test_welcome_clean_background_carries_depth():
    print("\n--- Test 1: welcome + background block ---")
    r = server.start_analysis()
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok"))
    w = r.get("welcome", "")
    ok.append(_check("welcome present",
                      "Welcome to BRIER" in w))
    ok.append(_check("welcome has NO inline URLs",
                      "https://" not in w))
    bg = r.get("background", "")
    ok.append(_check("background field present",
                      bool(bg)))
    ok.append(_check("background includes PRS tutorial URL inline",
                      "nature.com" in bg and "s41596-020-0353-1" in bg))
    ok.append(_check("background mentions docs link",
                      "um-kevinhe.github.io/BRIER" in bg))
    ok.append(_check("background mentions GitHub source",
                      "github.com/UM-KevinHe/BRIER" in bg))
    ok.append(_check("background lists three flavors",
                      "BRIERi" in bg and "BRIERfull" in bg
                      and "BRIERs" in bg))
    # Old field shouldn't be there
    ok.append(_check("'references' list is no longer top-level",
                      "references" not in r))
    di = r.get("_display_instructions", "")
    ok.append(_check("_display_instructions tells assistant to show "
                      "background only when user is new",
                      "background" in di.lower()
                      and ("new" in di.lower() or "familiarity" in di.lower())))
    ok.append(_check("display instructions mention 'do not paraphrase'",
                      "paraphrase" in di.lower()))
    return all(ok)


# -- Fix 2: eta grid guidance ----------------------------------------------

def test_eta_grid_guidance_in_ai_instructions():
    print("\n--- Test 2: ai_instructions warns against hand-writing "
          "eta_list ---")
    r = server.start_analysis()
    ai = r.get("ai_instructions", "")
    ok = []
    ok.append(_check("section header present",
                      "ETA GRID - DO NOT HAND-WRITE" in ai))
    ok.append(_check("mentions principled log-spaced default",
                      "log-spaced default" in ai))
    ok.append(_check("warns about [0, 1, 5, 10, 25, 50, 100] anti-pattern",
                      "[0, 1, 5, 10, 25, 50, 100]" in ai))
    ok.append(_check("directs to brier_auto_tune_eta on boundary",
                      "brier_auto_tune_eta" in ai))
    return all(ok)


# -- Fix 3a: high-p heads-up (no cap, no timeout) --------------------------

def test_high_p_heads_up_does_not_skip():
    print("\n--- Test 3a: legacy bootstrap_plot_max_p still works ---")
    workdir = tempfile.mkdtemp(prefix="v131_t3a_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian", eta_list=[0, 1])
        sel = server.brier_i_selection(
            fit_id=fit["fit_id"], criteria="gaussian.mspe",
            data_path=rds, X_val_expr="bi$target$testing$X",
            y_val_expr="bi$target$testing$y")
        # Force the legacy skip path: max_p smaller than the fit's p
        rep = server.summarize_fit(
            selection_id=sel["selection_id"],
            data_path=rds, newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y", criteria="gaussian.mspe",
            bootstrap_n=10, bootstrap_plot_max_p=10,
            output_dir=os.path.join(workdir, "rep"))
        ok = []
        ok.append(_check("status ok", rep.get("status") == "ok"))
        ok.append(_check("legacy-skip notice present",
                          "legacy v0.13"
                          in (rep.get("_notice", "") or "")))
        ok.append(_check("notice flags bootstrap_plot_max_p as deprecated",
                          "deprecated"
                          in (rep.get("_notice", "") or "").lower()))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -- Fix 3b: timeout -> fallback snippet (monkeypatched, no wall-clock racing) -

def test_plot_timeout_triggers_fallback():
    """Monkeypatch brier_plot_box and brier_plot_importance to return a
    synthetic TimeoutExpired-class error. This exercises the fallback
    path directly without depending on whether real R execution is slow
    enough to trip a timeout (which is fragile across machines).
    """
    print("\n--- Test 3b: synthetic TimeoutExpired -> fallback snippet ---")
    workdir = tempfile.mkdtemp(prefix="v131_t3b_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian", eta_list=[0, 1])
        sel = server.brier_i_selection(
            fit_id=fit["fit_id"], criteria="gaussian.mspe",
            data_path=rds, X_val_expr="bi$target$testing$X",
            y_val_expr="bi$target$testing$y")

        # Monkeypatch the plot tools to simulate a timeout deterministically.
        original_box = server.brier_plot_box
        original_imp = server.brier_plot_importance
        def fake_timeout(**kwargs):
            return {
                "status": "error",
                "message": "Rscript timed out after 1s",
                "class": "TimeoutExpired",
                "where": "test fake",
            }
        server.brier_plot_box = fake_timeout
        server.brier_plot_importance = fake_timeout
        try:
            rep = server.summarize_fit(
                selection_id=sel["selection_id"],
                data_path=rds, newx_expr="bi$target$testing$X",
                newy_expr="bi$target$testing$y",
                criteria="gaussian.mspe",
                bootstrap_n=10,
                plot_timeout_seconds=1,  # value doesn't matter (we faked)
                output_dir=os.path.join(workdir, "rep"))
        finally:
            server.brier_plot_box = original_box
            server.brier_plot_importance = original_imp

        ok = []
        ok.append(_check("status ok", rep.get("status") == "ok"))
        ok.append(_check("report path exists",
                          os.path.exists(rep["report_html_path"])))
        with open(rep["report_html_path"]) as f:
            html = f.read()
        ok.append(_check("HTML shows 'Skipped (timeout)'",
                          "Skipped (timeout)" in html))
        ok.append(_check("HTML includes 'Standalone R snippet'",
                          "Standalone R snippet" in html))
        ok.append(_check("snippet references brier_plot_box",
                          "brier_plot_box" in html))
        ok.append(_check("snippet references brier_plot_importance",
                          "brier_plot_importance" in html))
        notices = rep.get("_notice", "") or ""
        ok.append(_check("top-level notice mentions timeout",
                          "timeout" in notices.lower()))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_structural_failure_no_snippet():
    """A structural error (bad selection_id, criterion mismatch, etc.)
    should produce the error message but NOT a fallback snippet -
    running the snippet would just produce the same error.
    """
    print("\n--- Test 3b2: structural failure -> error only, no snippet ---")
    workdir = tempfile.mkdtemp(prefix="v131_t3b2_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian", eta_list=[0, 1])
        sel = server.brier_i_selection(
            fit_id=fit["fit_id"], criteria="gaussian.mspe",
            data_path=rds, X_val_expr="bi$target$testing$X",
            y_val_expr="bi$target$testing$y")

        original_box = server.brier_plot_box
        original_imp = server.brier_plot_importance
        def fake_structural(**kwargs):
            return {
                "status": "error",
                "message": "criterion 'foo' not valid for family 'gaussian'",
                "class": "InvalidCriteria",
                "where": "test fake",
            }
        server.brier_plot_box = fake_structural
        server.brier_plot_importance = fake_structural
        try:
            rep = server.summarize_fit(
                selection_id=sel["selection_id"],
                data_path=rds, newx_expr="bi$target$testing$X",
                newy_expr="bi$target$testing$y",
                criteria="gaussian.mspe",
                bootstrap_n=10,
                output_dir=os.path.join(workdir, "rep"))
        finally:
            server.brier_plot_box = original_box
            server.brier_plot_importance = original_imp

        ok = []
        ok.append(_check("status ok", rep.get("status") == "ok"))
        with open(rep["report_html_path"]) as f:
            html = f.read()
        ok.append(_check("HTML mentions the failure message",
                          "InvalidCriteria" not in html
                          and "not valid for family" in html))
        ok.append(_check(
            "no 'Standalone R snippet' for structural failure",
            "Standalone R snippet" not in html))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -- Fix 3c: no hard cap by default ----------------------------------------

def test_default_does_not_skip_on_high_p():
    print("\n--- Test 3c: default summarize_fit does NOT silently skip "
          "based on p ---")
    workdir = tempfile.mkdtemp(prefix="v131_t3c_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian", eta_list=[0, 1])
        sel = server.brier_i_selection(
            fit_id=fit["fit_id"], criteria="gaussian.mspe",
            data_path=rds, X_val_expr="bi$target$testing$X",
            y_val_expr="bi$target$testing$y")
        # Default call - no bootstrap_plot_max_p, plot_timeout_seconds
        # should be large enough that no timeout fires for this small fit.
        rep = server.summarize_fit(
            selection_id=sel["selection_id"],
            data_path=rds, newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y", criteria="gaussian.mspe",
            bootstrap_n=10,
            output_dir=os.path.join(workdir, "rep"))
        ok = []
        ok.append(_check("status ok", rep.get("status") == "ok"))
        # All four plots should be present (eta, box, importance,
        # selection) since p=~100 is small and the timeout isn't tripped.
        ok.append(_check(
            "all four plots included",
            rep["summary"]["plots_included"] == 4,
            detail=f"got {rep['summary']['plots_included']}"))
        # No legacy notice (we didn't pass bootstrap_plot_max_p)
        notice = rep.get("_notice", "") or ""
        ok.append(_check("no legacy-v0.13 notice in default mode",
                          "legacy v0.13" not in notice.lower()))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_r_namespace_whitelist():
    """v0.13.2: R-side safe_eval now mirrors Python's namespace whitelist
    so BRIER::, base::, stats::, utils::, Matrix:: all evaluate while
    unknown:: and ::: stay blocked.
    """
    print("\n--- Test R whitelist: BRIER::/base::/stats:: allowed, "
          "others blocked ---")
    rscript = server._find_rscript()
    common_path = os.path.join(os.path.dirname(__file__), "..", "r_scripts",
                                 "_common.R")
    common_path = os.path.abspath(common_path)
    r_code = (
        f"source('{common_path}')\n"
        "cases <- list(\n"
        "  list(expr='BRIER::standardize_X(X)', want='allow'),\n"
        "  list(expr='base::scale(X)', want='allow'),\n"
        "  list(expr='stats::lm(y ~ x)', want='allow'),\n"
        "  list(expr='Matrix::sparse.model.matrix(~ x)', want='allow'),\n"
        "  list(expr='X + y', want='allow'),\n"
        "  list(expr='dplyr::filter(d, x > 0)', want='block'),\n"
        "  list(expr='BRIER:::internal()', want='block'),\n"
        "  list(expr='unknown::foo()', want='block'),\n"
        "  list(expr='system(\"rm -rf /\")', want='block')\n"
        ")\n"
        "for (tc in cases) {\n"
        "  res <- tryCatch({ safe_eval(tc$expr, environment()); 'allow' },\n"
        "    error = function(e) if (grepl('Refusing', conditionMessage(e), "
        "fixed=TRUE)) 'block' else 'allow')\n"
        "  cat(sprintf('%s|%s|%s\\n', tc$expr, res, tc$want))\n"
        "}\n"
    )
    proc = subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e",
         r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
        timeout=60,
    )
    ok = []
    for line in proc.stdout.strip().splitlines():
        if "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        expr, got, want = parts
        ok.append(_check(f"{expr}: got={got}, want={want}", got == want))
    return all(ok)


def main():
    print("BRIER MCP v0.13.1/v0.13.2 fixes test suite")
    all_pass = True
    all_pass &= test_welcome_clean_background_carries_depth()
    all_pass &= test_eta_grid_guidance_in_ai_instructions()
    all_pass &= test_high_p_heads_up_does_not_skip()
    all_pass &= test_plot_timeout_triggers_fallback()
    all_pass &= test_structural_failure_no_snippet()
    all_pass &= test_default_does_not_skip_on_high_p()
    all_pass &= test_r_namespace_whitelist()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
