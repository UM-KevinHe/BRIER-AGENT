"""Tests for v0.13.3 - pin the bootstrap_n defaults that were dropped
from 100 to 20.

The change in v0.13.3 is one line per tool, but it's the kind of default
that drifts back to "round number" (100) on autopilot during a future
refactor. This suite pins the three relevant signatures.

Run:
  cd mcp/
  uv run tests/test_v133.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def _default(fn, param):
    return inspect.signature(fn).parameters[param].default


def test_summarize_fit_bootstrap_n_default():
    print("\n--- Test 1: summarize_fit bootstrap_n default is 20 ---")
    return _check("summarize_fit.bootstrap_n == 20",
                   _default(server.summarize_fit, "bootstrap_n") == 20,
                   detail=f"got {_default(server.summarize_fit, 'bootstrap_n')}")


def test_plot_box_bootstrap_n_default():
    print("\n--- Test 2: brier_plot_box bootstrap_n default is 20 ---")
    return _check("brier_plot_box.bootstrap_n == 20",
                   _default(server.brier_plot_box, "bootstrap_n") == 20,
                   detail=f"got {_default(server.brier_plot_box, 'bootstrap_n')}")


def test_plot_importance_replications_default():
    print("\n--- Test 3: brier_plot_importance replications default is 20 ---")
    return _check(
        "brier_plot_importance.replications == 20",
        _default(server.brier_plot_importance, "replications") == 20,
        detail=f"got {_default(server.brier_plot_importance, 'replications')}")


def test_plot_eta_bootstrap_n_unchanged():
    print("\n--- Test 4: brier_plot_eta bootstrap_n stays at 100 ---")
    # plot_eta only uses bootstrap_n when bootstrap=True (deliberate
    # opt-in). v0.13.3 deliberately left it at 100 since it isn't on
    # the hot path; the user didn't ask to change it.
    return _check(
        "brier_plot_eta.bootstrap_n == 100",
        _default(server.brier_plot_eta, "bootstrap_n") == 100,
        detail=f"got {_default(server.brier_plot_eta, 'bootstrap_n')}")


def test_docstrings_mention_v133_change():
    print("\n--- Test 5: docstrings explain the v0.13.3 default change ---")
    ok = []
    for fn in (server.summarize_fit, server.brier_plot_box,
                server.brier_plot_importance):
        doc = fn.__doc__ or ""
        ok.append(_check(
            f"{fn.__name__} doc mentions 'v0.13.3' or 'Default 20'",
            "v0.13.3" in doc or "Default 20" in doc,
            detail="docstring should explain why the default changed"))
    return all(ok)


def main():
    print("BRIER MCP v0.13.3 bootstrap_n default test suite")
    all_pass = True
    all_pass &= test_summarize_fit_bootstrap_n_default()
    all_pass &= test_plot_box_bootstrap_n_default()
    all_pass &= test_plot_importance_replications_default()
    all_pass &= test_plot_eta_bootstrap_n_unchanged()
    all_pass &= test_docstrings_mention_v133_change()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
