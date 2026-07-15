"""Tests for v0.10.1 wizard UX cleanup.

Covers:
  1. familiarity_check has structured options with render hint
  2. Each option has id, label, primer_key
  3. primer_key values match actual _FAMILIARITY_PRIMERS keys
  4. path_question has structured response with text_input hint
  5. path_question accepts directory_path
  6. ai_instructions documents the directory-path flow

Run:
  cd mcp/
  uv run tests/test_v101.py
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


def test_familiarity_check_structure():
    print("\n--- Test 1: familiarity_check has structured options ---")
    r = server.start_analysis()
    fc = r.get("familiarity_check", {})
    ok = []
    ok.append(_check("_render_hint present",
                      fc.get("_render_hint") == "multi_select_buttons"))
    ok.append(_check("multi_select == True",
                      fc.get("multi_select") is True))
    ok.append(_check("options is a list",
                      isinstance(fc.get("options"), list)))
    ok.append(_check("4 options (3 topics + skip)",
                      len(fc.get("options", [])) == 4))
    # Each option has id, label, primer_key
    for i, opt in enumerate(fc.get("options", [])):
        ok.append(_check(
            f"option {i+1} has id/label/primer_key keys",
            all(k in opt for k in ("id", "label", "primer_key")),
        ))
    return all(ok)


def test_primer_keys_match():
    print("\n--- Test 2: primer_keys match _FAMILIARITY_PRIMERS keys ---")
    r = server.start_analysis()
    fc = r.get("familiarity_check", {})
    actual_keys = set(server._FAMILIARITY_PRIMERS.keys())
    referenced = {opt["primer_key"] for opt in fc["options"]
                   if opt["primer_key"] is not None}
    ok = []
    ok.append(_check(
        f"referenced primer_keys all exist in _FAMILIARITY_PRIMERS",
        referenced.issubset(actual_keys),
        detail=f"referenced={referenced}, missing={referenced - actual_keys}",
    ))
    # Skip option has primer_key=None
    skip_opt = next(o for o in fc["options"] if o["id"] == 4)
    ok.append(_check(
        "Skip option has primer_key=None",
        skip_opt["primer_key"] is None,
    ))
    return all(ok)


def test_familiarity_fallback_text():
    print("\n--- Test 3: familiarity_check fallback text is well-formed ---")
    r = server.start_analysis()
    fc = r.get("familiarity_check", {})
    fallback = fc.get("prompt_fallback", "")
    ok = []
    ok.append(_check(
        "fallback present",
        bool(fallback),
    ))
    # All 4 options appear in fallback as numbered items
    for i in range(1, 5):
        ok.append(_check(
            f"fallback has numbered option {i}.",
            f"  {i}." in fallback,
        ))
    ok.append(_check(
        "fallback mentions multi-select ('1, 2' example)",
        "'1, 2'" in fallback or "1, 2" in fallback,
    ))
    return all(ok)


def test_path_question_structure():
    print("\n--- Test 4: path_question has structured response ---")
    r = server.start_analysis()
    pq = r.get("path_question", {})
    ok = []
    ok.append(_check(
        "path_question present",
        bool(pq),
    ))
    ok.append(_check(
        "_render_hint == text_input",
        pq.get("_render_hint") == "text_input",
    ))
    ok.append(_check(
        "question field present",
        bool(pq.get("question")),
    ))
    ok.append(_check(
        "prompt_fallback present",
        bool(pq.get("prompt_fallback")),
    ))
    accepts = pq.get("accepts", [])
    for shape in ("file_path", "comma_separated_file_paths",
                   "directory_path"):
        ok.append(_check(
            f"accepts {shape}",
            shape in accepts,
        ))
    return all(ok)


def test_directory_path_in_fallback():
    print("\n--- Test 5: path question fallback documents directory case ---")
    r = server.start_analysis()
    fb = r.get("path_question", {}).get("prompt_fallback", "")
    ok = []
    ok.append(_check(
        "fallback mentions directory",
        "directory" in fb.lower() or "/" in fb,
    ))
    ok.append(_check(
        "fallback shows directory example with trailing slash",
        "study_data/" in fb or "data/" in fb,
        detail="should show a directory example",
    ))
    return all(ok)


def test_ai_instructions_describe_directory_flow():
    print("\n--- Test 6: ai_instructions describe directory handling ---")
    r = server.start_analysis()
    ai = r.get("ai_instructions", "")
    ok = []
    ok.append(_check(
        "instructions mention list_data_directory for dirs",
        "list_data_directory" in ai,
    ))
    ok.append(_check(
        "instructions mention numbered list for dir contents",
        "numbered" in ai.lower() and "list" in ai.lower(),
    ))
    ok.append(_check(
        "instructions mention multi-select / checkboxes for familiarity",
        ("multi-select" in ai.lower() or "checkboxes" in ai.lower() or
         "chips" in ai.lower()),
    ))
    return all(ok)


def main():
    print("BRIER MCP v0.10.1 wizard UX test suite")
    all_pass = True
    all_pass &= test_familiarity_check_structure()
    all_pass &= test_primer_keys_match()
    all_pass &= test_familiarity_fallback_text()
    all_pass &= test_path_question_structure()
    all_pass &= test_directory_path_in_fallback()
    all_pass &= test_ai_instructions_describe_directory_flow()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
