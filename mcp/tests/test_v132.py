"""Tests for v0.13.2 wizard-text refinements.

Two fixes from real-world Claude Desktop runs:

1. Welcome no longer appends URLs to the greeting. _display_instructions
   spells out the URL-append anti-pattern. Background remains gated to
   familiarity_check.

2. ai_instructions includes an EXPRESSION VALIDATOR section telling the
   assistant the whitelist is permissive (BRIER::, base::, etc.) and that
   claiming '::' is blocked is FALSE.

Run:
  cd mcp/
  uv run tests/test_v132.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def test_welcome_has_no_appended_urls_anti_pattern():
    print("\n--- Test 1: _display_instructions forbids URL-append on "
          "welcome ---")
    r = server.start_analysis()
    di = r.get("_display_instructions", "")
    ok = []
    ok.append(_check("welcome itself has no URLs",
                      "http://" not in r["welcome"]
                      and "https://" not in r["welcome"]))
    ok.append(_check("display instructions warn against appending URLs",
                      "Do NOT append URLs" in di))
    ok.append(_check(
        "instructions mention 'References' / 'See also' anti-pattern",
        "'See also'" in di or "GitHub links" in di))
    ok.append(_check("instructions specify URLs belong in background",
                      "belong in `background`" in di))
    ok.append(_check(
        "instructions specify primers as the other home for URLs",
        "per-concept primers" in di))
    return all(ok)


def test_background_still_gated():
    print("\n--- Test 2: background is gated on familiarity / explicit "
          "ask ---")
    r = server.start_analysis()
    di = r.get("_display_instructions", "")
    bg = r.get("background", "")
    ok = []
    ok.append(_check("background field present", bool(bg)))
    ok.append(_check("background contains PRS tutorial URL",
                      "s41596-020-0353-1" in bg))
    ok.append(_check("background contains BRIER docs URL",
                      "um-kevinhe.github.io" in bg))
    ok.append(_check("background mentions all 3 flavors",
                      all(x in bg for x in
                          ("BRIERi", "BRIERfull", "BRIERs"))))
    ok.append(_check("instructions gate background on familiarity_check",
                      "ONLY when" in di
                      and "familiarity_check" in di))
    return all(ok)


def test_validator_misdescribe_warning():
    print("\n--- Test 3: ai_instructions has EXPRESSION VALIDATOR "
          "section ---")
    r = server.start_analysis()
    ai = r.get("ai_instructions", "")
    ok = []
    ok.append(_check("section header present",
                      "EXPRESSION VALIDATOR" in ai))
    ok.append(_check(
        "lists the whitelist explicitly",
        "BRIER::, base::, stats::, utils::, Matrix::" in ai))
    ok.append(_check(
        "warns 'don't tell the user :: is blocked' anti-pattern",
        "is FALSE" in ai))
    ok.append(_check(
        "names the specific bad workaround "
        "(swap BRIER::standardize_X for scale)",
        "standardize_X" in ai and "scale()" in ai))
    return all(ok)


def test_python_validator_actually_allows_safe_namespaces():
    print("\n--- Test 4: Python validator allows the documented "
          "namespaces ---")
    cases = [
        ("BRIER::standardize_X(X)", True),
        ("base::scale(X)", True),
        ("stats::cor(x, y)", True),
        ("utils::head(data)", True),
        ("Matrix::Diagonal(5)", True),
        ("unknown::function(x)", False),
        ("BRIER:::internal_thing(x)", False),  # ::: denied
    ]
    ok = []
    for expr, should_pass in cases:
        result = server._validate_exprs(test=expr)
        passed = (result is None)
        label = ("ALLOW expected" if should_pass else "DENY expected")
        ok.append(_check(f"{label}: {expr}", passed == should_pass,
                          detail=("got DENY" if should_pass and not passed
                                   else "got ALLOW"
                                        if not should_pass and passed
                                        else "")))
    return all(ok)


def main():
    print("BRIER MCP v0.13.2 wizard-text fixes test suite")
    all_pass = True
    all_pass &= test_welcome_has_no_appended_urls_anti_pattern()
    all_pass &= test_background_still_gated()
    all_pass &= test_validator_misdescribe_warning()
    all_pass &= test_python_validator_actually_allows_safe_namespaces()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
