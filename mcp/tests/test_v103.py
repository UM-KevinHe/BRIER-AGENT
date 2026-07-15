"""Tests for v0.10.3.

Covers four features:
  Feature 1 - output_dir override on summarize_fit, plot tools, predict
  Feature 2 - brier_plot_selection: criterion-vs-eta plot from cached
              selection alone (no test data); always-on in summarize_fit
  Feature 3 - principled eta.list default (log-spaced grid)
  Feature 4 - boundary-optimum diagnostic notice

Run:
  cd mcp/
  uv run tests/test_v103.py
"""
from __future__ import annotations

import math
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


# -- Feature 3: principled eta default --------------------------------------

def test_eta_grid_builder():
    print("\n--- Test 1: _build_eta_grid math ---")
    g = server._build_eta_grid()
    ok = []
    ok.append(_check("default grid length == 11", len(g) == 11))
    ok.append(_check("first point is 0.0", g[0] == 0.0))
    ok.append(_check("second point is exactly eta_floor 0.1",
                      abs(g[1] - 0.1) < 1e-12))
    ok.append(_check("last point is exactly eta_ceiling 10",
                      abs(g[-1] - 10.0) < 1e-12))
    # Log-spacing check: consecutive ratios (after the 0) should be equal
    ratios = [g[i+1] / g[i] for i in range(1, len(g) - 1)]
    ok.append(_check("log-spaced (consecutive ratios equal)",
                      all(abs(r - ratios[0]) < 1e-9 for r in ratios),
                      detail=f"ratios sample: {ratios[:3]}"))
    # Custom knobs
    custom = server._build_eta_grid(eta_floor=0.05, eta_ceiling=50, eta_n=8)
    ok.append(_check("custom floor honored",
                      abs(custom[1] - 0.05) < 1e-12))
    ok.append(_check("custom ceiling honored",
                      abs(custom[-1] - 50.0) < 1e-12))
    ok.append(_check("custom length", len(custom) == 9))
    # Invalid inputs
    try:
        server._build_eta_grid(eta_floor=0)
        ok.append(_check("rejects eta_floor=0", False))
    except ValueError:
        ok.append(_check("rejects eta_floor=0", True))
    try:
        server._build_eta_grid(eta_floor=1, eta_ceiling=0.5)
        ok.append(_check("rejects ceiling<floor", False))
    except ValueError:
        ok.append(_check("rejects ceiling<floor", True))
    return all(ok)


def test_default_grid_used_when_eta_list_omitted():
    print("\n--- Test 2: default grid is used when eta_list is omitted ---")
    workdir = tempfile.mkdtemp(prefix="v103_t2_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian",
        )
        ok = [_check("fit ok", fit.get("status") == "ok")]
        if fit.get("status") != "ok":
            return False
        # Read fit meta and confirm eta_list is the principled default
        fid = fit["fit_id"]
        cache = os.path.expanduser(f"~/.cache/brier-mcp/fits/{fid}.rds")
        r = subprocess.run(
            [server._find_rscript(), "--no-save", "--no-restore",
             "--no-init-file", "-e",
             f'm <- readRDS("{cache}")$meta; '
             'if (!is.null(m$eta_list)) cat(paste(m$eta_list, collapse=","))'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        stored = r.stdout.strip()
        ok.append(_check("meta stores eta_list as a vector",
                          bool(stored)))
        if stored:
            vals = [float(x) for x in stored.split(",")]
            ok.append(_check("length 11", len(vals) == 11))
            ok.append(_check("first is 0.0", vals[0] == 0.0))
            ok.append(_check("last is exactly 10.0",
                              abs(vals[-1] - 10.0) < 1e-9))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_custom_knobs_widen_grid():
    print("\n--- Test 3: eta_ceiling/eta_n knobs override the default ---")
    workdir = tempfile.mkdtemp(prefix="v103_t3_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian",
            eta_ceiling=50, eta_n=6,
        )
        fid = fit["fit_id"]
        cache = os.path.expanduser(f"~/.cache/brier-mcp/fits/{fid}.rds")
        r = subprocess.run(
            [server._find_rscript(), "--no-save", "--no-restore",
             "--no-init-file", "-e",
             f'm <- readRDS("{cache}")$meta; '
             'cat(paste(m$eta_list, collapse=","))'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        vals = [float(x) for x in r.stdout.strip().split(",")]
        ok = []
        ok.append(_check("custom grid length == 7", len(vals) == 7))
        ok.append(_check("custom grid tops at 50",
                          abs(vals[-1] - 50.0) < 1e-9))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -- Feature 4: boundary-optimum diagnostic ---------------------------------

def test_boundary_notice_helper():
    print("\n--- Test 4: _boundary_optimum_notice helper ---")
    ok = []
    grid = server._build_eta_grid()  # max=10
    # At boundary -> notice
    n1 = server._boundary_optimum_notice(10.0, grid)
    ok.append(_check("fires when eta_min == grid max", n1 is not None))
    ok.append(_check("notice mentions 'top of'",
                      "top of" in (n1 or "").lower()))
    # Interior -> no notice
    n2 = server._boundary_optimum_notice(1.292, grid)
    ok.append(_check("quiet when interior", n2 is None))
    # M=2 with one at boundary -> fires
    n3 = server._boundary_optimum_notice([10.0, 2.0],
                                          [[0, 1, 10], [0, 1, 10]])
    ok.append(_check("fires when any M-component at boundary",
                      n3 is not None))
    return all(ok)


def test_selection_emits_boundary_notice():
    print("\n--- Test 5: selection tools emit boundary notice when applicable ---")
    workdir = tempfile.mkdtemp(prefix="v103_t5_")
    try:
        rds = _stage_bi(workdir)
        # Default grid tops at 10; high external borrowing on these data
        # tends to pick eta=10 (boundary). If not, force the boundary.
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian",
        )
        sel = server.brier_i_selection(fit_id=fit["fit_id"],
                                        criteria="BIC")
        ok = [_check("selection ok", sel.get("status") == "ok")]
        ok.append(_check("eta_grid_values returned",
                          sel.get("eta_grid_values") is not None))
        # If the selection happens to pick the boundary, the notice fires;
        # if not, no notice should fire. Either way, the field structure
        # should be correct.
        if abs(float(sel["selected_eta"]) - 10.0) < 1e-9:
            ok.append(_check("boundary notice present (selected eta==10)",
                              "_notice_eta_boundary" in sel))
        else:
            ok.append(_check("no boundary notice (interior optimum)",
                              "_notice_eta_boundary" not in sel,
                              detail=f"selected_eta={sel['selected_eta']}"))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -- Feature 2: brier_plot_selection ---------------------------------------

def test_plot_selection_m1():
    print("\n--- Test 6: brier_plot_selection M=1 (line) ---")
    workdir = tempfile.mkdtemp(prefix="v103_t6_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian",
        )
        sel = server.brier_i_selection(fit_id=fit["fit_id"],
                                        criteria="BIC")
        r = server.brier_plot_selection(selection_id=sel["selection_id"])
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok",
                          detail=str(r.get("message", ""))))
        if r.get("status") != "ok":
            return False
        ok.append(_check("M == 1", r.get("M") == 1))
        ok.append(_check("rendered_kind == M1_selection_curve",
                          r.get("rendered_kind") == "M1_selection_curve"))
        ok.append(_check("PNG exists",
                          os.path.exists(r.get("plot_png_path", ""))))
        ok.append(_check("CSV exists",
                          os.path.exists(r.get("plot_csv_path", ""))))
        ok.append(_check("n_eta_points matches default grid (11)",
                          r.get("n_eta_points") == 11))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_plot_selection_m2():
    print("\n--- Test 7: brier_plot_selection M=2 (heatmap) ---")
    workdir = tempfile.mkdtemp(prefix="v103_t7_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1:2]",
            multi_method="ind", family="gaussian",
            eta_list=[[0, 1, 2], [0, 1, 2]],
        )
        sel = server.brier_i_selection(fit_id=fit["fit_id"],
                                        criteria="BIC")
        r = server.brier_plot_selection(selection_id=sel["selection_id"])
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("M == 2", r.get("M") == 2))
        ok.append(_check("rendered_kind == M2_selection_heatmap",
                          r.get("rendered_kind") == "M2_selection_heatmap"))
        ok.append(_check("PNG exists",
                          os.path.exists(r.get("plot_png_path", ""))))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_summarize_fit_includes_selection_plot_no_test_set():
    print("\n--- Test 8: summarize_fit includes selection plot WITHOUT test set ---")
    workdir = tempfile.mkdtemp(prefix="v103_t8_")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian",
        )
        sel = server.brier_i_selection(fit_id=fit["fit_id"],
                                        criteria="BIC")
        rep = server.summarize_fit(selection_id=sel["selection_id"])
        ok = []
        ok.append(_check("summarize_fit ok",
                          rep.get("status") == "ok"))
        if rep.get("status") != "ok":
            return False
        # No test set -> only the selection plot should be included
        ok.append(_check("plots_included == 1 (the selection plot)",
                          rep["summary"]["plots_included"] == 1))
        with open(rep["report_html_path"]) as f:
            html = f.read()
        ok.append(_check("HTML mentions 'Selection criterion vs eta'",
                          "Selection criterion vs eta" in html))
        ok.append(_check("HTML has exactly 1 base64 PNG",
                          html.count("data:image/png;base64") == 1))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -- Feature 1: output_dir override ----------------------------------------

def test_output_dir_override_plot_tools():
    print("\n--- Test 9: output_dir override works on all plot tools ---")
    workdir = tempfile.mkdtemp(prefix="v103_t9_")
    custom = os.path.join(workdir, "custom_outputs")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian",
        )
        sel = server.brier_i_selection(fit_id=fit["fit_id"],
                                        criteria="BIC")
        ok = []
        # plot_selection
        r = server.brier_plot_selection(selection_id=sel["selection_id"],
                                          output_dir=custom)
        ok.append(_check("plot_selection lands under custom dir",
                          custom in r.get("plot_png_path", "")))
        # plot_eta (needs test set)
        r2 = server.brier_plot_eta(
            selection_id=sel["selection_id"], data_path=rds,
            newx_expr="bi$target$testing$X",
            newy_expr="bi$target$testing$y",
            criteria="gaussian.mspe", output_dir=custom,
        )
        ok.append(_check("plot_eta lands under custom dir",
                          custom in r2.get("plot_png_path", "")))
        ok.append(_check("custom dir was created",
                          os.path.isdir(custom)))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_output_dir_override_summarize_fit():
    print("\n--- Test 10: output_dir override on summarize_fit ---")
    workdir = tempfile.mkdtemp(prefix="v103_t10_")
    custom = os.path.join(workdir, "report_dir")
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian",
        )
        sel = server.brier_i_selection(fit_id=fit["fit_id"],
                                        criteria="BIC")
        rep = server.summarize_fit(
            selection_id=sel["selection_id"],
            output_dir=custom,
        )
        ok = []
        ok.append(_check("summarize_fit ok",
                          rep.get("status") == "ok"))
        ok.append(_check("report HTML under custom dir",
                          custom in rep.get("report_html_path", "")))
        ok.append(_check("reproduce.R under custom dir",
                          custom in rep.get("reproduce_r_path", "")))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_welcome_message():
    print("\n--- Test 11: welcome message + background ---")
    r = server.start_analysis()
    w = r.get("welcome", "")
    ok = []
    ok.append(_check("welcome starts with 'Welcome to BRIER'",
                      w.startswith("Welcome to BRIER")))
    # v0.13.1 put URLs in a `references` list; v0.13.2 moved them into
    # the `background` block as inline prose links (rendered only when
    # the user is new). Either way they're still discoverable from the
    # start_analysis payload, just structurally relocated.
    bg = r.get("background", "")
    ok.append(_check("background includes GitHub repo URL",
                      "github.com/UM-KevinHe/BRIER" in bg))
    ok.append(_check("background includes Choi 2020 PRS tutorial URL",
                      "nature.com" in bg and "s41596" in bg))
    ai = r.get("ai_instructions", "")
    ok.append(_check("ai_instructions has WHERE OUTPUTS GO section",
                      "WHERE OUTPUTS GO" in ai))
    return all(ok)


def test_default_output_backstop_notice():
    print("\n--- Test 12: backstop notice when output_dir omitted + no config ---")
    workdir = tempfile.mkdtemp(prefix="v103_t12_")
    cfg_path = server._config_file_path()
    saved_cfg = None
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            saved_cfg = f.read()
        os.remove(cfg_path)
    try:
        rds = _stage_bi(workdir)
        fit = server.brier_i(
            data_path=rds, X_expr="bi$target$train$X",
            y_expr="bi$target$train$y",
            beta_external_expr="bi$beta.external[, 1, drop=FALSE]",
            family="gaussian",
        )
        sel = server.brier_i_selection(fit_id=fit["fit_id"],
                                        criteria="BIC")
        # No output_dir, no config -> notice should fire
        rep = server.summarize_fit(selection_id=sel["selection_id"])
        ok = []
        ok.append(_check(
            "_notice_default_output fires when output_dir omitted",
            "_notice_default_output" in rep,
        ))
        # Explicit output_dir -> notice should NOT fire
        custom = os.path.join(workdir, "explicit_outputs")
        rep2 = server.summarize_fit(
            selection_id=sel["selection_id"], output_dir=custom)
        ok.append(_check(
            "notice quiet when output_dir given",
            "_notice_default_output" not in rep2,
        ))
        return all(ok)
    finally:
        # Restore config if it was present
        if saved_cfg is not None:
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, "w") as f:
                f.write(saved_cfg)
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    print("BRIER MCP v0.10.3 test suite")
    all_pass = True
    all_pass &= test_eta_grid_builder()
    all_pass &= test_default_grid_used_when_eta_list_omitted()
    all_pass &= test_custom_knobs_widen_grid()
    all_pass &= test_boundary_notice_helper()
    all_pass &= test_selection_emits_boundary_notice()
    all_pass &= test_plot_selection_m1()
    all_pass &= test_plot_selection_m2()
    all_pass &= test_summarize_fit_includes_selection_plot_no_test_set()
    all_pass &= test_output_dir_override_plot_tools()
    all_pass &= test_output_dir_override_summarize_fit()
    all_pass &= test_welcome_message()
    all_pass &= test_default_output_backstop_notice()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
