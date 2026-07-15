"""End-to-end smoke test for the BRIER MCP v0.6.0 surface.

Tests start_analysis, the only new tool in v0.6. The wizard is
pure-Python and stateless: it returns a structured dict that the
calling AI threads through conversation.

Validates:
  * Wizard returns the expected top-level structure
  * Welcome message present and concise
  * Three familiarity primers (genetic risk, transfer learning, BRIER)
    with the expected anchor phrases and links
  * Five problem-description questions, all with the right ids
  * Three routing questions
  * Four model paths (BRIERi, BRIERi-baseline, BRIERfull, BRIERs)
    each with summary, tool_sequence, canonical_call, pitfalls,
    selection_options
  * Pitfall coverage: intercept row (BRIERi), cohort encoding
    (BRIERfull), standardize_X (BRIERs)
  * preprocessI / preprocessS mentioned with v0.7 wrapper note
  * Baseline-first offer present
  * Family caveats for binomial, poisson, and time-to-event (out of scope)
  * Missingness note present
  * Size-based recommendation: with sizes -> BRIERi when total > 10000,
    BRIERfull when smaller; without sizes -> generic
  * Style: no em-dashes anywhere in the output
  * Statelessness: two calls with the same args return identical dicts

Run:
  cd mcp/
  uv run tests/test_v06.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


# --------------------------------------------------------------------------
# Test 1: top-level structure
# --------------------------------------------------------------------------

def test_top_level_structure() -> bool:
    print("\n--- Test 1: top-level structure ---")
    r = server.start_analysis()
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok"))
    expected_keys = {
        "welcome", "familiarity_check", "problem_description_questions",
        "routing_questions", "paths", "preprocessing_hints",
        "baseline_offer", "family_caveats", "missingness_note",
        "size_recommendation", "inspect_first_reminder", "ai_instructions",
    }
    actual_keys = set(r.keys()) - {"status"}
    missing = expected_keys - actual_keys
    ok.append(_check(
        "all expected top-level sections present",
        not missing,
        detail=f"missing: {missing}" if missing else "",
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 2: welcome message
# --------------------------------------------------------------------------

def test_welcome_message() -> bool:
    print("\n--- Test 2: welcome message ---")
    welcome = server.start_analysis()["welcome"]
    ok = []
    ok.append(_check(
        "welcome is a string",
        isinstance(welcome, str),
    ))
    ok.append(_check(
        "welcome mentions transfer-learning",
        "transfer-learning" in welcome.lower()
        or "transfer learning" in welcome.lower(),
    ))
    ok.append(_check(
        "welcome under 500 chars (concise)",
        len(welcome) < 500,
        detail=f"len={len(welcome)}",
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 3: familiarity primers
# --------------------------------------------------------------------------

def test_familiarity_primers() -> bool:
    print("\n--- Test 3: familiarity primers ---")
    primers = server.start_analysis()["familiarity_check"]["primers"]
    ok = []

    # Genetic risk prediction primer
    grp = primers.get("genetic_risk_prediction", "")
    ok.append(_check(
        "genetic_risk_prediction primer present",
        bool(grp) and isinstance(grp, str),
    ))
    ok.append(_check(
        "GRP primer mentions underrepresented populations",
        "underrepresented" in grp.lower(),
    ))
    ok.append(_check(
        "GRP primer mentions both SNP and gene expression",
        ("SNP" in grp or "polygenic" in grp.lower())
        and "gene expression" in grp.lower(),
    ))
    ok.append(_check(
        "GRP primer includes a link",
        "http" in grp,
    ))

    # Transfer learning primer
    tl = primers.get("transfer_learning", "")
    ok.append(_check(
        "transfer_learning primer mentions negative transfer",
        "negative transfer" in tl.lower(),
    ))
    ok.append(_check(
        "transfer_learning primer mentions eta",
        "eta" in tl.lower(),
    ))

    # BRIER primer
    br = primers.get("BRIER", "")
    ok.append(_check(
        "BRIER primer mentions all three variants",
        "BRIERi" in br and "BRIERfull" in br and "BRIERs" in br,
    ))
    ok.append(_check(
        "BRIER primer includes pkgdown link",
        "um-kevinhe.github.io/BRIER" in br,
    ))

    return all(ok)


# --------------------------------------------------------------------------
# Test 4: problem-description questions
# --------------------------------------------------------------------------

def test_problem_description() -> bool:
    print("\n--- Test 4: problem-description questions ---")
    qs = server.start_analysis()["problem_description_questions"]
    ok = []
    ok.append(_check("5 questions present", len(qs) == 5,
                     detail=f"got {len(qs)}"))
    expected_ids = {
        "outcome_type", "predictor_type", "include_demographics",
        "sample_sizes", "ancestry_context",
    }
    actual_ids = {q["id"] for q in qs}
    ok.append(_check(
        "all 5 expected question ids present",
        actual_ids == expected_ids,
        detail=f"got {actual_ids}",
    ))

    # outcome_type should flag time-to-event explicitly
    outcome_q = next(q for q in qs if q["id"] == "outcome_type")
    ok.append(_check(
        "outcome_type lists time-to-event as an option",
        "time-to-event" in outcome_q["options"],
    ))
    ok.append(_check(
        "outcome_type downstream_effect flags time-to-event as unsupported",
        "not supported" in outcome_q["downstream_effect"].lower()
        or "out of scope" in outcome_q["downstream_effect"].lower(),
    ))

    # sample_sizes should drive the size-based recommendation
    sizes_q = next(q for q in qs if q["id"] == "sample_sizes")
    ok.append(_check(
        "sample_sizes mentions n_target and n_external",
        "n_target" in sizes_q["ask"].lower()
        or "target" in sizes_q["ask"].lower(),
    ))
    ok.append(_check(
        "sample_sizes downstream_effect mentions the 10000 threshold",
        "10000" in sizes_q["downstream_effect"],
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 5: routing questions
# --------------------------------------------------------------------------

def test_routing_questions() -> bool:
    print("\n--- Test 5: routing questions ---")
    rqs = server.start_analysis()["routing_questions"]
    ok = []
    ok.append(_check("3 routing questions", len(rqs) == 3))
    ids = {q["id"] for q in rqs}
    ok.append(_check(
        "expected routing ids",
        ids == {"target_data_shape", "external_data_shape",
                "validation_set_available"},
        detail=f"got {ids}",
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 6: model paths
# --------------------------------------------------------------------------

def test_paths_structure() -> bool:
    print("\n--- Test 6: model paths (BRIERi, BRIERi-baseline, BRIERfull, BRIERs) ---")
    paths = server.start_analysis()["paths"]
    ok = []
    expected_paths = {"BRIERi", "BRIERi-baseline", "BRIERfull", "BRIERs"}
    ok.append(_check(
        "all 4 paths present",
        set(paths.keys()) == expected_paths,
        detail=f"got {set(paths.keys())}",
    ))

    for path_name in expected_paths:
        path = paths.get(path_name, {})
        ok.append(_check(
            f"{path_name}: summary present",
            isinstance(path.get("summary"), str) and len(path["summary"]) > 50,
        ))
        ok.append(_check(
            f"{path_name}: tool_sequence is a non-empty list",
            isinstance(path.get("tool_sequence"), list)
            and len(path["tool_sequence"]) >= 2,
        ))
        ok.append(_check(
            f"{path_name}: canonical_call present",
            isinstance(path.get("canonical_call"), str)
            and "brier" in path["canonical_call"].lower(),
        ))
        ok.append(_check(
            f"{path_name}: pitfalls list non-empty",
            isinstance(path.get("pitfalls"), list)
            and len(path["pitfalls"]) >= 1,
        ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 7: pitfall coverage
# --------------------------------------------------------------------------

def test_pitfall_coverage() -> bool:
    print("\n--- Test 7: pitfall coverage across the four paths ---")
    paths = server.start_analysis()["paths"]
    ok = []

    brieri_pitfalls = " ".join(paths["BRIERi"]["pitfalls"]).lower()
    ok.append(_check(
        "BRIERi flags intercept row",
        "intercept" in brieri_pitfalls,
    ))

    brierfull_pitfalls = " ".join(paths["BRIERfull"]["pitfalls"]).lower()
    ok.append(_check(
        "BRIERfull flags cohort = 0 for target",
        "cohort" in brierfull_pitfalls and "0" in brierfull_pitfalls,
    ))

    briers_pitfalls = " ".join(paths["BRIERs"]["pitfalls"]).lower()
    ok.append(_check(
        "BRIERs flags standardize_X",
        "standardiz" in briers_pitfalls,
    ))
    ok.append(_check(
        "BRIERs flags no-intercept-row",
        "no intercept" in briers_pitfalls or "p x m" in briers_pitfalls,
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 8: preprocessing hints
# --------------------------------------------------------------------------

def test_preprocessing_hints() -> bool:
    print("\n--- Test 8: preprocessing hints ---")
    hints = server.start_analysis()["preprocessing_hints"]
    ok = []
    ok.append(_check(
        "preprocessI present",
        "preprocessI" in hints and "preprocessI" in hints["preprocessI"],
    ))
    ok.append(_check(
        "preprocessS present",
        "preprocessS" in hints and "preprocessS" in hints["preprocessS"],
    ))
    ok.append(_check(
        "preprocessing hints mention v0.7 wrappers",
        "v0.7" in hints["preprocessI"] or "v0.7" in hints["preprocessS"],
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 9: baseline offer and missingness note
# --------------------------------------------------------------------------

def test_baseline_and_missingness() -> bool:
    print("\n--- Test 9: baseline offer and missingness note ---")
    r = server.start_analysis()
    ok = []
    ok.append(_check(
        "baseline_offer mentions target-only",
        "target-only" in r["baseline_offer"].lower(),
    ))
    ok.append(_check(
        "baseline_offer mentions eta_list=[0] mechanism",
        "eta_list" in r["baseline_offer"],
    ))
    ok.append(_check(
        "missingness_note mentions BRIER does not impute",
        "not impute" in r["missingness_note"].lower(),
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 10: family caveats
# --------------------------------------------------------------------------

def test_family_caveats() -> bool:
    print("\n--- Test 10: family caveats including time-to-event ---")
    fc = server.start_analysis()["family_caveats"]
    ok = []
    ok.append(_check(
        "all three family caveats present",
        set(fc.keys()) == {"binomial", "poisson", "time-to-event"},
    ))
    ok.append(_check(
        "time-to-event flagged as not supported",
        "not support" in fc["time-to-event"].lower()
        or "does not support" in fc["time-to-event"].lower(),
    ))
    ok.append(_check(
        "binomial caveat mentions imbalance",
        "imbalance" in fc["binomial"].lower(),
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 11: size-based recommendation
# --------------------------------------------------------------------------

def test_size_recommendation() -> bool:
    print("\n--- Test 11: size-based recommendation logic ---")
    ok = []

    # No sizes -> generic
    r = server.start_analysis()
    ok.append(_check(
        "no sizes: size_recommendation is None",
        r["size_recommendation"] is None,
    ))

    # Individual external + total > 10000 -> BRIERi
    r = server.start_analysis(
        n_target=2000, n_external_total=40000,
        has_individual_external=True,
    )
    rec = r["size_recommendation"]
    ok.append(_check(
        "large individual: primary = BRIERi",
        rec is not None and rec["primary"] == "BRIERi",
        detail=f"got {rec}",
    ))
    ok.append(_check(
        "large individual: BRIERfull listed as alternative",
        "BRIERfull" in rec["alternatives"],
    ))

    # Individual external + total <= 10000 -> BRIERfull
    r = server.start_analysis(
        n_target=500, n_external_total=2000,
        has_individual_external=True,
    )
    rec = r["size_recommendation"]
    ok.append(_check(
        "small individual: primary = BRIERfull",
        rec is not None and rec["primary"] == "BRIERfull",
        detail=f"got {rec}",
    ))

    # has_individual_external=False -> no size logic applies
    r = server.start_analysis(
        n_target=2000, n_external_total=40000,
        has_individual_external=False,
    )
    ok.append(_check(
        "no individual external: size_recommendation is None",
        r["size_recommendation"] is None,
    ))

    return all(ok)


# --------------------------------------------------------------------------
# Test 12: style (no em-dashes)
# --------------------------------------------------------------------------

def test_no_em_dashes() -> bool:
    print("\n--- Test 12: style check: no em-dashes anywhere ---")
    r = server.start_analysis(
        n_target=2000, n_external_total=40000,
        has_individual_external=True,
    )
    # Serialize the whole payload as one string and scan.
    blob = json.dumps(r, ensure_ascii=False)
    has_em_dash = "\u2014" in blob or "\u2013" in blob
    return _check(
        "no em-dash or en-dash characters in wizard payload",
        not has_em_dash,
        detail="found unicode dash" if has_em_dash else "",
    )


# --------------------------------------------------------------------------
# Test 13: statelessness
# --------------------------------------------------------------------------

def test_statelessness() -> bool:
    print("\n--- Test 13: statelessness (same args -> same output) ---")
    r1 = server.start_analysis()
    r2 = server.start_analysis()
    r3 = server.start_analysis(n_target=2000, n_external_total=40000,
                                has_individual_external=True)
    r4 = server.start_analysis(n_target=2000, n_external_total=40000,
                                has_individual_external=True)
    ok = []
    ok.append(_check(
        "no-args calls produce identical output",
        json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True),
    ))
    ok.append(_check(
        "with-args calls produce identical output",
        json.dumps(r3, sort_keys=True) == json.dumps(r4, sort_keys=True),
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 14: tool name coverage
# --------------------------------------------------------------------------

def test_tool_name_coverage() -> bool:
    print("\n--- Test 14: wizard references real tool names ---")
    blob = json.dumps(server.start_analysis())
    tools = ["brier_i", "brier_full", "brier_s",
             "brier_i_selection", "brier_full_selection",
             "brier_s_selection",
             "brier_predict", "brier_evaluate",
             "cal_ld", "get_ldb",
             "inspect_user_data"]   # v0.7 uses inspect_user_data, not inspect_data
    ok = []
    for t in tools:
        ok.append(_check(f"references {t}", t in blob))
    return all(ok)


# --------------------------------------------------------------------------
# Test 15: four external_data_shape options (v0.6 refinement)
# --------------------------------------------------------------------------

def test_external_data_shape_four_options() -> bool:
    print("\n--- Test 15: external_data_shape has 4 options (refined) ---")
    rqs = server.start_analysis()["routing_questions"]
    ext_q = next(q for q in rqs if q["id"] == "external_data_shape")
    branches = ext_q["branches"]
    ok = []
    ok.append(_check(
        "4 branches present",
        len(branches) == 4,
        detail=f"got {len(branches)} branches: {list(branches.keys())}",
    ))
    ok.append(_check(
        "pretrained coefficients branch present",
        "pretrained coefficients" in branches,
    ))
    ok.append(_check(
        "raw individual-level branch present",
        any("individual" in k for k in branches),
    ))
    ok.append(_check(
        "external sumstats branch present",
        any("sumstats" in k.lower() or "summary statistics" in k.lower()
            for k in branches),
    ))
    ok.append(_check(
        "none / baseline branch present",
        "none" in branches or any("baseline" in v.lower()
                                   for v in branches.values()),
    ))
    # Sumstats branch should mention upstream PRS methods.
    sumstats_branch_val = next(
        (v for k, v in branches.items()
         if "sumstats" in k.lower() or "summary statistics" in k.lower()),
        "",
    )
    ok.append(_check(
        "sumstats branch mentions PRS methods (C+T / lassosum / PRS-CS / glmnet)",
        any(m in sumstats_branch_val
            for m in ["lassosum", "PRS-CS", "thresholding", "glmnet"]),
    ))
    ok.append(_check(
        "sumstats branch flags BRIERfull as unavailable",
        "BRIERfull" in sumstats_branch_val
        and ("not" in sumstats_branch_val.lower()
             or "cannot" in sumstats_branch_val.lower()),
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 16: prep recipes embedded in BRIERi and BRIERs paths
# --------------------------------------------------------------------------

def test_prep_recipes() -> bool:
    print("\n--- Test 16: prep recipes for individual and sumstats external ---")
    paths = server.start_analysis()["paths"]
    ok = []

    # BRIERi: prep_external_individual and prep_external_sumstats both present
    brieri = paths["BRIERi"]
    ok.append(_check(
        "BRIERi has prep_external_individual recipe",
        "prep_external_individual" in brieri,
    ))
    ok.append(_check(
        "BRIERi has prep_external_sumstats recipe",
        "prep_external_sumstats" in brieri,
    ))
    # Level 2: recipe with cv.glmnet, no standardization required.
    brieri_indiv = brieri.get("prep_external_individual", "")
    ok.append(_check(
        "BRIERi individual recipe mentions cv.glmnet",
        "cv.glmnet" in brieri_indiv,
    ))
    ok.append(_check(
        "BRIERi individual recipe mentions intercept row stacking",
        "intercept" in brieri_indiv.lower(),
    ))

    # BRIERs: same plus standardization (Level 3)
    briers = paths["BRIERs"]
    ok.append(_check(
        "BRIERs has prep_external_individual recipe",
        "prep_external_individual" in briers,
    ))
    ok.append(_check(
        "BRIERs has prep_external_sumstats recipe",
        "prep_external_sumstats" in briers,
    ))
    briers_indiv = briers.get("prep_external_individual", "")
    ok.append(_check(
        "BRIERs individual recipe REQUIRES standardize_X before fitting",
        "standardize_X" in briers_indiv,
    ))
    ok.append(_check(
        "BRIERs individual recipe notes no-intercept stacking ([-1])",
        "[-1]" in briers_indiv or "no intercept" in briers_indiv.lower(),
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 17: non-SNP predictors note in BRIERs path
# --------------------------------------------------------------------------

def test_non_snp_predictors() -> bool:
    print("\n--- Test 17: BRIERs path mentions non-SNP predictor handling ---")
    briers = server.start_analysis()["paths"]["BRIERs"]
    ok = []
    ok.append(_check(
        "BRIERs has non_snp_predictors entry",
        "non_snp_predictors" in briers,
    ))
    note = briers.get("non_snp_predictors", "")
    ok.append(_check(
        "note clarifies cal_ld still works for non-SNP",
        "cal_ld" in note,
    ))
    ok.append(_check(
        "note advises skipping get_ldb for non-SNP",
        "get_ldb" in note
        and ("skip" in note.lower() or "not" in note.lower()),
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 18: phase_gates and tightened ai_instructions
# --------------------------------------------------------------------------

def test_phase_gates_and_flow_control() -> bool:
    print("\n--- Test 18: phase_gates and flow-control directives ---")
    r = server.start_analysis()
    ok = []
    ok.append(_check(
        "phase_gates field present",
        "phase_gates" in r,
    ))
    gates = r.get("phase_gates", {})
    ok.append(_check(
        "before_recommendation lists at least 5 required answers",
        len(gates.get("before_recommendation", [])) >= 5,
    ))
    ok.append(_check(
        "ask_one_at_a_time = True (in phase_gates)",
        gates.get("ask_one_at_a_time") is True,
    ))
    ok.append(_check(
        "do_not_batch_questions = True (in phase_gates)",
        gates.get("do_not_batch_questions") is True,
    ))
    ok.append(_check(
        "do_not_skip_ahead = True (in phase_gates)",
        gates.get("do_not_skip_ahead") is True,
    ))
    # v0.7 ai_instructions now points at the data-first flow; the
    # per-question machinery lives in phase_gates above instead of text.
    ai_inst = r["ai_instructions"]
    ok.append(_check(
        "ai_instructions mentions data-first flow OR inspect_user_data",
        "data-first" in ai_inst.lower()
        or "inspect_user_data" in ai_inst,
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    print("BRIER MCP v0.6.0 end-to-end smoke test")

    all_pass = True
    all_pass &= test_top_level_structure()
    all_pass &= test_welcome_message()
    all_pass &= test_familiarity_primers()
    all_pass &= test_problem_description()
    all_pass &= test_routing_questions()
    all_pass &= test_paths_structure()
    all_pass &= test_pitfall_coverage()
    all_pass &= test_preprocessing_hints()
    all_pass &= test_baseline_and_missingness()
    all_pass &= test_family_caveats()
    all_pass &= test_size_recommendation()
    all_pass &= test_no_em_dashes()
    all_pass &= test_statelessness()
    all_pass &= test_tool_name_coverage()
    all_pass &= test_external_data_shape_four_options()
    all_pass &= test_prep_recipes()
    all_pass &= test_non_snp_predictors()
    all_pass &= test_phase_gates_and_flow_control()

    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
