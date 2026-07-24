"""Unit tests for loop.py's pure helpers.

_external_count drives the per-source diagnostic sub-chain: it decides M, and the
loop only nudges "fit each external one-by-one" when M > 1. It has to survive every
spelling a small model actually produces -- which, on real runs, has included a
packed list under one key, MIXED unnumbered + numbered roles naming two DISTINCT
models, and (for Bucket B) raw externals that prep_auto fits itself.

Imported by file path: brier_agent/__init__ pulls in the `mcp` SDK, which is not
needed (and may not be importable) for a pure-logic test.
"""
import importlib.util
from pathlib import Path

_LOOP = Path(__file__).resolve().parents[1] / "loop.py"
_src = _LOOP.read_text()
_start = _src.index("def _external_count(")
_end = _src.index("\ndef ", _start + 1)
_ns: dict = {}
exec(_src[_start:_end], _ns)
_external_count = _ns["_external_count"]


def ec(roles):
    return _external_count({"roles": roles})


def test_no_external():
    assert ec({"target_X_train": "x.gz"}) == 0
    assert ec({}) == 0
    print("external-count: none: OK")


def test_pretrained_single_and_numbered():
    assert ec({"external_coef": "m1.gz"}) == 1
    assert ec({"external_coef_1": "m1.gz", "external_coef_2": "m2.gz"}) == 2
    print("external-count: pretrained single + numbered: OK")


def test_pretrained_packed_list():
    """The 7B once packed BOTH externals into ONE role as a list."""
    assert ec({"external_coef": ["m1.gz", "m2.gz"]}) == 2
    print("external-count: packed list: OK")


def test_pretrained_mixed_spellings():
    """Observed on a real run: external_coef=model1 AND external_coef_1=model2.

    These are two DISTINCT models; counting must merge the spellings, not pick one.
    """
    assert ec({"external_coef": "m1.gz", "external_coef_1": "m2.gz"}) == 2
    print("external-count: mixed spellings, distinct files: OK")


def test_same_file_under_two_spellings_counts_once():
    assert ec({"external_coef": "m1.gz", "external_coef_1": "m1.gz"}) == 1
    print("external-count: duplicate file deduped: OK")


def test_raw_externals_count_too():
    """Bucket B: prep_auto FITS these, but they still become M coefficient columns,
    so the per-source diagnostic must fire for them exactly as for pretrained files."""
    assert ec({"external_sumstats": "g.gz"}) == 1
    assert ec({"external_sumstats_1": "g1.gz", "external_sumstats_2": "g2.gz"}) == 2
    assert ec({"external_X_1": "x1.gz", "external_y_1": "y1.gz",
               "external_X_2": "x2.gz", "external_y_2": "y2.gz"}) == 2
    assert ec({"external_sumstats": ["g1.gz", "g2.gz"]}) == 2
    print("external-count: raw (Bucket B) externals: OK")


def test_brier_full_cohorts_are_not_externals():
    """brier_full pools RAW cohorts (external_X_k), which are NOT coefficient columns:
    the per-source diagnostic must NOT fire for them."""
    assert ec({"external_X_k": "c1.gz"}) == 0
    print("external-count: brier_full cohort roles excluded: OK")


# --------------------------------------------------------------- prep retry
_start2 = _src.index("def _format_prep_retry(")
_end2 = _src.index("\ndef ", _start2 + 1)
exec(_src[_start2:_end2], _ns)
_format_prep_retry = _ns["_format_prep_retry"]


def test_prep_retry_demands_a_reissued_call():
    """On a real run the 7B read prep_auto's re-route steer, restated it correctly in
    PROSE, and then STOPPED -- one turn after being told exactly what to do. Nothing
    downstream can run without a prepared object, so that dead-end kills the run."""
    msg = ("target_y_train not found, but this data dir has a GWAS summary file: the "
           "target is SUMMARY-level. Re-route to shape='brier_s'")
    out = _format_prep_retry("prep_auto", {"shape": "brier_i"},
                             {"status": "error", "message": msg})
    assert "brier_s" in out, "the actionable error must be surfaced to the model"
    assert "brier_i" in out, "the failed shape must be named back"
    low = out.lower()
    assert "do not stop" in low and "prose" in low, "must forbid narrating the fix"
    assert "again" in low, "must demand the call be reissued"
    print("prep-retry: failed prep is pushed to reissue the corrected call: OK")


def test_prep_retry_without_a_shape():
    out = _format_prep_retry("prep_data", {}, {"status": "error", "message": "boom"})
    assert "boom" in out
    print("prep-retry: tolerates a call with no shape: OK")


# ---------------------------------------------------------------------------
# ETA-CEILING ESCALATION.
#
# The selection tools have always emitted `_notice_eta_boundary` when the chosen
# eta lands on the TOP rung of the grid, and the harness has always dropped it on
# the floor: four of nine scored runs selected eta AT the boundary and reported the
# truncated model's test metrics as the model's. Nothing pushed a refit, because
# every other step of the chain has a continuation hook and this one did not.
#
# Worse, the harness was the SOURCE of the problem: the prep -> fit nudges used to
# hand the model an ad-hoc `eta_list`, and an explicit eta_list overrides
# eta_ceiling, so widening the ceiling could not have worked even if it had been
# asked for.
_start3 = _src.index("def _grid_max(")
_end3 = _src.index("\ndef _core_fit_hints(")
import typing

_ns3: dict = {
    "_FIT_TOOLS": frozenset({"brier_i", "brier_s", "brier_full"}),
    "Optional": typing.Optional, "List": typing.List,
    "Dict": typing.Dict, "Any": typing.Any,
    "_ETA_CEILING_FACTOR": 5.0,
    "_OMIT_ETA_LIST": "OMIT eta_list entirely: the default eta grid is "
                      "log-spaced and ALREADY includes eta=0.",
    # Family-aware criteria helpers (mirror the module; gaussian default preserves
    # the pre-binomial behavior the gaussian-case tests assert).
    "_prep_family": (lambda lp: (lp[1] or {}).get("outcome_family")
                     if lp and (lp[1] or {}).get("outcome_family")
                     in ("gaussian", "binomial", "poisson") else "gaussian"),
    "_sel_criteria": (lambda f: {"binomial": "binomial.dev",
                                 "poisson": "poisson.dev"}.get(f, "gaussian.mspe")),
    "_report_criteria": (lambda f: {"binomial": "binomial.auc",
                                    "poisson": "poisson.dev"}.get(f, "gaussian.rsq")),
}
exec(_src[_start3:_end3], _ns3)
_grid_max = _ns3["_grid_max"]
_fit_behind_selection = _ns3["_fit_behind_selection"]
_format_eta_escalation = _ns3["_format_eta_escalation"]

_SEL_PINNED = {
    "status": "ok", "selection_id": "sel1", "selected_eta": 10.0,
    "eta_grid_values": [0, 0.1, 1, 10],
    "_notice_eta_boundary": "Selected eta (10) is at the top of the selection grid.",
}


def _fit_call(fit_id="fit1", grid=(0, 0.1, 1, 10), args=None):
    return {
        "tool": "brier_i",
        "args": args or {"data_path": "/p/prepared.rds", "X_expr": "prepared$X",
                         "y_expr": "prepared$y",
                         "beta_external_expr": "prepared$beta_external"},
        "result": {"status": "ok", "fit_id": fit_id, "eta_list_used": list(grid)},
    }


def test_grid_max_handles_a_nested_multi_source_grid():
    assert _grid_max([0, 0.1, 1, 10]) == 10
    # M>1: eta is a vector per rung.
    assert _grid_max([[0, 0], [1, 10], [10, 100]]) == 100
    assert _grid_max("nonsense") is None
    print("eta-escalation: grid max, flat and nested: OK")


def test_escalation_restates_the_fit_with_a_higher_ceiling():
    results = [_fit_call()]
    out = _format_eta_escalation(
        "brier_i_selection", {"fit_id": "fit1"}, _SEL_PINNED, results)
    assert "eta_ceiling=50" in out, "must widen 5x past the grid top (10 -> 50)"
    assert "brier_i" in out, "must name the fitter to reissue"
    assert 'X_expr="prepared$X"' in out, "must restate the fit's own arguments"
    low = out.lower()
    assert "do not pass eta_list" in low, (
        "an explicit eta_list overrides eta_ceiling, so the widening would be a no-op")
    assert "do not evaluate" in low, (
        "a boundary-pinned model must not be scored: its numbers are not the model's")
    print("eta-escalation: refit nudge widens the ceiling and forbids eta_list: OK")


def test_escalation_reads_the_grid_that_RAN_not_the_one_asked_for():
    """The agent is now told to OMIT eta_list, so the args carry no grid at all.

    Only the fit's RESOLVED grid (`eta_list_used`) knows where the top is. Keying off
    the argument would silently disable escalation on exactly the runs that follow
    the new instruction.
    """
    results = [_fit_call(grid=(0, 0.1, 1, 10))]  # args deliberately have no eta_list
    out = _format_eta_escalation(
        "brier_i_selection", {"fit_id": "fit1"}, _SEL_PINNED, results)
    assert "eta_ceiling=50" in out
    print("eta-escalation: reads eta_list_used, not the (absent) eta_list arg: OK")


def test_escalation_drops_a_stale_ceiling_and_grid_from_the_restated_call():
    results = [_fit_call(args={"data_path": "/p.rds", "X_expr": "p$X",
                               "eta_list": [0, 1, 10], "eta_ceiling": 10,
                               "eta_n": 10})]
    out = _format_eta_escalation(
        "brier_i_selection", {"fit_id": "fit1"}, _SEL_PINNED, results)
    assert "eta_list=[0, 1, 10]" not in out, "the stale grid must not be restated"
    assert "eta_ceiling=10" not in out, "the ceiling it already pinned at must not be restated"
    assert "eta_n=10" not in out, "the old grid knobs go with the old ceiling"
    assert "eta_ceiling=50" in out
    print("eta-escalation: stale eta_list / eta_ceiling stripped from the refit: OK")


def test_escalation_matches_the_fit_by_fit_id():
    results = [_fit_call("old", grid=(0, 1, 5)), _fit_call("fit1", grid=(0, 1, 10))]
    name, args, res = _fit_behind_selection({"fit_id": "fit1"}, results)
    assert res["fit_id"] == "fit1", "must escalate the fit the SELECTION actually ran on"
    print("eta-escalation: the fit behind the selection is found by fit_id: OK")


def test_escalation_declines_when_there_is_no_fit_to_restate():
    """No fit in the trace -> no nudge, rather than a nudge invented from nothing."""
    assert _format_eta_escalation(
        "brier_i_selection", {"fit_id": "gone"}, _SEL_PINNED, []) is None
    print("eta-escalation: declines rather than guessing a fit: OK")


def test_the_fit_nudges_no_longer_teach_the_model_to_invent_a_grid():
    """The nudges USED to spell out `eta_list=[0, 0.1, 1, 10, 100, 1000, 10000]`.

    That is where the invented grids came from, and an explicit eta_list disables the
    ceiling knobs entirely -- so the harness was defeating its own escalation tool.
    """
    import re
    # A multi-point literal grid: eta_list=[0, 0.1, ...]. Pinning a single eta
    # (eta_list=[0]) is legitimate -- it is how a baseline / external-only comparator
    # is forced to no-transfer -- and the prompt explicitly allows exactly that.
    grids = re.findall(r"eta_list=\[[^\]]*,[^\]]*\]", _src)
    assert not grids, f"a nudge is handing the model a hand-written eta grid again: {grids}"
    assert "_OMIT_ETA_LIST" in _src
    print("eta-escalation: no nudge dictates an eta_list: OK")


# ---------------------------------------------------------------------------
# UNUSED INPUT = a second representation of the cohort (preprocessing-only).
#
# T3_intercept-row ships ONE cohort in TWO representations (individual X + y, and a
# GWAS of the same samples), so it needs TWO preps. The post-prep nudge left that
# judgment to the model and offered an "otherwise you are done" exit, and the model
# took it: 3 of 3 runs prepared brier_i, ignored the GWAS they had JUST INSPECTED, and
# scored 0 (gated).
#
# The harness can see what the model cannot be trusted to notice: a file was handed
# over and never used. It surfaces that FACT and leaves the INFERENCE (which module the
# file calls for) to the model, because inferring the module is what the case tests.
_start4 = _src.index("def _basename(")
_end4 = _src.index("\ndef _core_fit_hints(")
# The slice runs from _basename to _core_fit_hints, which sweeps up the eta helpers
# that sit between them, so it needs their globals too.
_ns4: dict = {
    "Any": typing.Any, "List": typing.List, "Optional": typing.Optional,
    "Dict": typing.Dict,
    "_FIT_TOOLS": frozenset({"brier_i", "brier_s", "brier_full"}),
    "_ETA_CEILING_FACTOR": 5.0,
}
exec(_src[_start4:_end4], _ns4)
_unused_representation = _ns4["_unused_representation"]
_role_basenames = _ns4["_role_basenames"]
_inspected_basenames = _ns4["_inspected_basenames"]


def test_basenames_survive_absolute_paths():
    """The model mixes bare names and absolute paths for the SAME file. Comparing raw
    strings would report a used file as unused and nudge on every case."""
    used = _role_basenames({"roles": {
        "target_X_train": "/abs/dir/height_AFR_X_training.txt.gz",
        "external_coef": "height_EUR_model1.txt.gz",
    }})
    assert used == {"height_AFR_X_training.txt.gz", "height_EUR_model1.txt.gz"}
    seen = _inspected_basenames({"data_paths": ["/abs/dir/height_AFR_X_training.txt.gz"]})
    assert seen == {"height_AFR_X_training.txt.gz"}
    assert not (seen - used), "the same file under two spellings must not look unused"
    print("unused-input: basenames survive absolute paths: OK")


def test_an_unused_GWAS_after_a_brier_i_prep_is_surfaced():
    """The real T3_intercept-row trace: it INSPECTED the GWAS, then never used it."""
    inspected = {"height_AFR_GWAS_training.txt.gz", "height_AFR_SNP_info.txt.gz",
                 "height_AFR_X_training.txt.gz", "height_AFR_pheno_training.txt.gz",
                 "height_EUR_model1.txt.gz"}
    used = {"height_AFR_SNP_info.txt.gz", "height_AFR_X_training.txt.gz",
            "height_AFR_pheno_training.txt.gz", "height_EUR_model1.txt.gz"}
    out = _unused_representation("brier_i", inspected, used)
    assert out == ["height_AFR_GWAS_training.txt.gz"], out
    print("unused-input: an unused GWAS after a brier_i prep is surfaced: OK")


def test_the_nudge_states_the_fact_and_does_NOT_name_the_module():
    """Row #1 of the case is 'Infers BOTH modules'. If the harness says 'call brier_s',
    it has answered the very thing being scored. It must name the FILE, not the shape."""
    _format_unused_input = _ns4["_format_unused_input"]
    msg = _format_unused_input(["height_AFR_GWAS_training.txt.gz"], "brier_i")
    assert "height_AFR_GWAS_training.txt.gz" in msg
    assert "brier_s" not in msg, "the nudge must not do the inference for the model"
    low = msg.lower()
    assert "second representation" in low and "never passed" in low
    print("unused-input: states the fact, does not name the module: OK")


def test_it_does_NOT_fire_when_every_input_was_used():
    """The single-consumer cases (allele-flip, overlap_brieri, overlap_briers, ...) must
    be untouched, or a spurious second prep would break cases that already pass."""
    files = {"height_AFR_X_training.txt.gz", "height_AFR_pheno_training.txt.gz",
             "height_EUR_model1.txt.gz"}
    assert _unused_representation("brier_i", files, files) == []
    print("unused-input: silent when every input was consumed: OK")


def test_an_unused_file_that_is_not_another_representation_is_ignored():
    """A leftover README or an unused SNP map is not a second consumer. Only a file that
    represents the cohort ANOTHER way (a GWAS opposite X+y, a phenotype opposite a GWAS)
    counts, or the nudge would fire on almost every case."""
    inspected = {"height_AFR_X_training.txt.gz", "notes.txt", "height_AFR_SNP_info.txt.gz"}
    used = {"height_AFR_X_training.txt.gz"}
    assert _unused_representation("brier_i", inspected, used) == []
    # ... but an unused PHENOTYPE after a SUMMARY prep is the mirror image, and counts.
    assert _unused_representation(
        "brier_s",
        {"height_AFR_GWAS_training.txt.gz", "height_AFR_pheno_training.txt.gz"},
        {"height_AFR_GWAS_training.txt.gz"},
    ) == ["height_AFR_pheno_training.txt.gz"]
    print("unused-input: only a genuine second representation counts: OK")


def test_brier_full_never_triggers_it():
    """A pooled case consumes several phenotypes by design; it has ONE consumer."""
    assert _unused_representation(
        "brier_full",
        {"height_AFR_pheno_training.txt.gz", "height_EUR_pheno_training.txt.gz"},
        {"height_AFR_pheno_training.txt.gz"},
    ) == []
    print("unused-input: brier_full is never nudged: OK")


# ---------------------------------------------------------------------------
# FIT-RETRY must carry the ERROR, not a guess about it.
#
# T1_brieri_noval went 70/80 -> 0/80 GATED on this. The 7B fills in every optional
# schema field and set `alpha = 0`, which BRIER rejects. The retry nudge then asserted,
# unconditionally, that the call had failed "because required arguments were missing or
# empty" -- a diagnosis that was simply WRONG -- and re-fed the same data_path and
# expr_hints, leaving the poisoned argument in place. The model reissued the identical
# call until the repeat guard aborted the run. A retry that misdiagnoses the failure
# GUARANTEES the identical call.
_start5 = _src.index("def _core_fit_hints(")
_end5 = _src.index("\ndef _external_count(")
exec(_src[_start5:_end5], _ns3)          # _core_fit_hints, which _format_fit_retry uses
_start6 = _src.index("def _format_fit_retry(")
_end6 = _src.index("\ndef ", _start6 + 1)
exec(_src[_start6:_end6], _ns3)
_format_fit_retry = _ns3["_format_fit_retry"]

_PREP = ({"shape": "brier_i"},
         {"prepared_path": "/p/prepared.rds",
          "expr_hints": {"X_expr": "p$X", "y_expr": "p$y",
                         "beta_external_expr": "p$beta_external"}})


def test_fit_retry_surfaces_the_actual_error():
    err = {"status": "error",
           "message": "OMIT `alpha` entirely to use the BRIER default (LASSO, alpha=1)."}
    out = _format_fit_retry("brier_i", *_PREP, err)
    assert "alpha" in out, "the nudge must carry the error that actually happened"
    assert "missing or empty" not in out.lower(), (
        "the old nudge asserted a WRONG cause, which is what made the model repeat")
    print("fit-retry: surfaces the real error: OK")


def test_fit_retry_tells_it_to_DROP_an_optional_knob():
    """The way out of an invalid optional argument is to remove it, not to guess another
    value. The model cannot know that unless it is told."""
    err = {"status": "error", "message": "alpha must be in (0, 1]"}
    low = _format_fit_retry("brier_i", *_PREP, err).lower()
    assert "drop it" in low or "omit" in low
    assert "alpha" in low and "penalty" in low, "name the optional knobs to drop"
    assert "same call unchanged" in low, "must forbid reissuing it verbatim"
    print("fit-retry: says to DROP the optional knob, not re-guess it: OK")


def test_fit_retry_still_restates_the_prep_contract():
    """The original job of this nudge (re-feed data_path + expr_hints for a model that
    called the fitter with empty args) must survive."""
    out = _format_fit_retry("brier_i", *_PREP, None)
    assert 'data_path="/p/prepared.rds"' in out
    assert 'X_expr="p$X"' in out
    print("fit-retry: still restates the prepared inputs: OK")


# ---------------------------------------------------------------------------
# The brier_full COMPARISON sub-chain.
#
# With brier_full the externals are RAW cohorts: no coefficient vector exists, so
# neither the target-only baseline nor the external-only comparator can be SCORED --
# each must be FIT as a single-cohort brier_i(eta=0). Nothing drove that, and the
# selection nudge actively said "there is no external-only comparator to run", so the
# real T1_brierfull run fit the pooled model, evaluated it, and stopped. It still
# scored 70/80, because the scorer credited the baseline row off the POOLED fit's eta
# grid and the comparator row off the POOLED model's test evals. Both ends were wrong.
_start7 = _src.index("def _expr_base(")          # _expr_base / _prep_hints / _split_exprs
_end7 = _src.index("\ndef _format_inspect_block(")   # ... through the followup builders
exec(_src[_start7:_end7], _ns3)
_brierfull_comparators = _ns3["_brierfull_comparators"]
_next_brierfull_comparator = _ns3["_next_brierfull_comparator"]
_format_brierfull_comparator = _ns3["_format_brierfull_comparator"]
_is_external_cohort_fit = _ns3["_is_external_cohort_fit"]

_BF_HINTS = {
    "X_expr": "p$X", "y_expr": "p$y", "cohort_expr": "p$cohort",
    "X_val_expr": "p$X_val", "y_val_expr": "p$y_val",
    "X_test_expr": "p$X_test", "y_test_expr": "p$y_test",
    "beta_zero_expr": "p$beta_zero",
    "X_target_expr": "p$X[p$cohort == 0, ]", "y_target_expr": "p$y[p$cohort == 0]",
    "X_ext_1_expr": "p$X[p$cohort == 1, ]", "y_ext_1_expr": "p$y[p$cohort == 1]",
}
_BF_PREP = ({"shape": "brier_full"},
            {"prepared_path": "/p/bf.rds", "expr_hints": _BF_HINTS})


def test_brierfull_needs_a_baseline_AND_a_comparator_fit():
    cs = _brierfull_comparators(_BF_HINTS)
    assert [c["label"] for c in cs] == [
        "the TARGET-ONLY (eta=0) baseline",
        "the EXTERNAL-ONLY comparator for cohort 1",
    ], cs
    print("brierfull: baseline + one comparator per external cohort: OK")


def test_a_shape_with_a_coefficient_vector_has_no_such_subchain():
    """brier_i / brier_s score their external directly (score_external_models). Only a RAW
    cohort has to be fit, so the sub-chain must not fire for them."""
    assert _brierfull_comparators({"X_expr": "p$X",
                                   "beta_external_expr": "p$beta"}) == []
    print("brierfull: sub-chain does not fire for a pretrained-coef shape: OK")


def test_the_next_comparator_skips_the_ones_already_fit():
    done = [{"tool": "brier_i",
             "args": {"X_expr": "p$X[p$cohort == 0, ]"},
             "result": {"status": "ok", "fit_id": "f1"}}]
    nxt = _next_brierfull_comparator(_BF_PREP, done)
    assert nxt["label"] == "the EXTERNAL-ONLY comparator for cohort 1", nxt
    done.append({"tool": "brier_i",
                 "args": {"X_expr": "p$X[p$cohort == 1, ]"},
                 "result": {"status": "ok", "fit_id": "f2"}})
    assert _next_brierfull_comparator(_BF_PREP, done) is None, "all done -> stop"
    print("brierfull: drives each comparator once, then stops: OK")


def test_the_comparator_nudge_pins_eta_to_zero_and_uses_the_zero_beta():
    c = _brierfull_comparators(_BF_HINTS)[1]
    out = _format_brierfull_comparator(c, _BF_PREP)
    assert "brier_i" in out and "eta_list=[0]" in out
    assert 'beta_external_expr="p$beta_zero"' in out, (
        "a comparator has no external to transfer from: it needs the ZERO beta")
    assert 'X_expr="p$X[p$cohort == 1, ]"' in out
    assert "BRIERfull cannot" in out, "say WHY it is a brier_i and not a brier_full"
    print("brierfull: comparator nudge is a single-cohort eta=0 brier_i: OK")


def test_an_external_comparator_is_NEVER_selected_on_the_TARGET_val():
    """The leak that matters. The target's val exists (X_val_expr is in the hints), and
    the ordinary fit->select nudge would happily use it -- putting target data into a
    comparator whose entire purpose is to be purely external."""
    c = _brierfull_comparators(_BF_HINTS)[1]        # cohort 1, which shipped no own val
    out = _format_brierfull_comparator(c, _BF_PREP)
    assert 'X_val_expr="p$X_val"' not in out, "the TARGET's val must never appear here"
    assert "BIC" in out
    assert "leak" in out.lower()
    # ... and the fit->select nudge must agree.
    sel = _ns3["_format_fit_followup"](
        "brier_i", {"X_expr": "p$X[p$cohort == 1, ]", "data_path": "/p/bf.rds"},
        {"fit_id": "f9"}, _BF_PREP)
    assert 'X_val_expr="p$X_val"' not in sel, "fit->select leaked the target val"
    assert "BIC" in sel
    print("brierfull: an external comparator is never selected on the target val: OK")


def test_a_cohort_with_its_OWN_val_is_selected_on_it():
    hints = dict(_BF_HINTS, X_ext_1_val_expr="p$X_e1_val", y_ext_1_val_expr="p$y_e1_val")
    prep = ({"shape": "brier_full"}, {"prepared_path": "/p/bf.rds", "expr_hints": hints})
    sel = _ns3["_format_fit_followup"](
        "brier_i", {"X_expr": "p$X[p$cohort == 1, ]"}, {"fit_id": "f9"}, prep)
    assert 'X_val_expr="p$X_e1_val"' in sel, "use the cohort's OWN held-out data"
    assert 'X_val_expr="p$X_val"' not in sel
    print("brierfull: a cohort with its own val is selected on that: OK")


def test_the_target_baseline_MAY_use_the_target_val():
    """The target-only baseline is a target model, so the target's val is the right
    selection set for it -- the leak rule applies to EXTERNAL comparators only."""
    # The TARGET cohort's subset is not an external cohort ... and the EXTERNAL one is.
    # Both are matched against the prep hints' VALUES, never a substring of the name.
    assert not _is_external_cohort_fit({"X_expr": _BF_HINTS["X_target_expr"]}, _BF_HINTS)
    assert _is_external_cohort_fit({"X_expr": _BF_HINTS["X_ext_1_expr"]}, _BF_HINTS)
    c = _brierfull_comparators(_BF_HINTS)[0]
    out = _format_brierfull_comparator(c, _BF_PREP)
    assert 'X_val_expr="p$X_val"' in out, "the baseline SHOULD use the target's val"
    print("brierfull: the target baseline may use the target val: OK")
