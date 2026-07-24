"""Test the Tier 1 guardrails."""
from brier_agent.guardrails import (
    check_tool_available, check_arguments_parsed,
    compress_tool_result_for_llm, max_turns_error,
)

def test_hard_guard():
    assert check_tool_available("brier_s", ["brier_s","brier_i"]) is None
    err = check_tool_available("hallucinated", ["brier_s","brier_i"])
    assert err and err["class"] == "ToolNotAvailable"
    assert "brier_s" in err["message"]
    print("hard-guard: OK")

def test_malformed_args():
    assert check_arguments_parsed("brier_s", {"x":1}) is None
    err = check_arguments_parsed("brier_s", {"_parse_error": '{"x": '})
    assert err and err["class"] == "MalformedArguments"
    print("malformed-args: OK")

def test_compression():
    # a fit-like result with a big beta vector
    big = {"status":"ok","eta":[0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0],
           "beta":[0.1]*96674, "message":"fit complete"}
    c = compress_tool_result_for_llm(big)
    assert c["status"] == "ok"               # preserved
    assert c["message"] == "fit complete"    # preserved
    # beta summarized, not full
    assert isinstance(c["beta"], dict) and "_summary" in c["beta"]
    assert "96674" in c["beta"]["_summary"]
    # eta is a bulky key and long -> summarized
    assert isinstance(c["eta"], dict) and "_summary" in c["eta"]
    print("compression: OK")

def test_compression_smallresult_untouched():
    small = {"status":"ok","n_variants":96674,"has_phenotype":False}
    c = compress_tool_result_for_llm(small)
    assert c == small  # nothing bulky, unchanged
    print("compression leaves small results alone: OK")

def test_compression_nondict():
    assert compress_tool_result_for_llm("just text") == "just text"
    assert compress_tool_result_for_llm([1,2,3]) == [1,2,3]
    print("compression non-dict passthrough: OK")

def test_max_turns():
    e = max_turns_error(12)
    assert e["class"] == "MaxTurnsReached"
    assert "12" in e["message"]
    print("max-turns error: OK")

def _history(n_tool_results, size=6000):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(n_tool_results):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "prep_auto", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "X" * size})
    return msgs


def test_compact_history_shrinks_long_chains():
    """A long self-correcting chain once overflowed Qwen-7B's 32k context (422 at
    turns=0): compress_tool_result_for_llm caps ONE result, but nothing capped the
    CONVERSATION."""
    from brier_agent.guardrails import compact_history
    msgs = _history(8)
    before = sum(len(str(m.get("content") or "")) for m in msgs)
    out = compact_history(msgs)
    after = sum(len(str(m.get("content") or "")) for m in out)
    assert after < before / 1.5, "history must shrink materially"
    print("compact-history: long chain shrinks: OK")


def test_compact_history_preserves_message_validity():
    """The API requires every tool result to answer its tool_call: contents may
    shrink, but turns and tool_call_id pairing must survive."""
    from brier_agent.guardrails import compact_history
    msgs = _history(8)
    out = compact_history(msgs)
    assert [m["role"] for m in out] == [m["role"] for m in msgs]
    ids_in = [m.get("tool_call_id") for m in msgs if m["role"] == "tool"]
    ids_out = [m.get("tool_call_id") for m in out if m["role"] == "tool"]
    assert ids_in == ids_out
    print("compact-history: roles + tool_call_id pairing preserved: OK")


def test_compact_history_keeps_recent_results_verbatim():
    from brier_agent.guardrails import compact_history
    out = compact_history(_history(8))
    tools = [m for m in out if m["role"] == "tool"]
    assert all(len(t["content"]) == 6000 for t in tools[-4:]), "recent stay verbatim"
    assert all(len(t["content"]) < 700 for t in tools[:-4]), "older are stubbed"
    print("compact-history: recent verbatim, older stubbed: OK")


def test_compact_history_is_a_noop_below_the_window():
    from brier_agent.guardrails import compact_history
    short = _history(3)
    assert compact_history(short) is short
    print("compact-history: no-op below the window: OK")


def test_compact_history_does_not_mutate_the_caller():
    from brier_agent.guardrails import compact_history
    msgs = _history(8)
    compact_history(msgs)
    assert all(len(m["content"]) == 6000 for m in msgs if m["role"] == "tool"), \
        "the caller's messages must not be mutated"
    print("compact-history: caller not mutated: OK")


if __name__ == "__main__":
    test_hard_guard()
    test_malformed_args()
    test_compression()
    test_compression_smallresult_untouched()
    test_compression_nondict()
    test_max_turns()
    test_compact_history_shrinks_long_chains()
    test_compact_history_preserves_message_validity()
    test_compact_history_keeps_recent_results_verbatim()
    test_compact_history_is_a_noop_below_the_window()
    test_compact_history_does_not_mutate_the_caller()
    print("\nALL GUARDRAILS TESTS PASSED")


# ---------------------------------------------------------------------------
# NULL-FILLED OPTIONAL ARGUMENTS.
#
# The 7B does not omit an optional parameter -- it fills every field the schema declares
# with that field's type-zero. On a real run it sent `alpha=0, penalty="", gamma=0` to
# brier_s six times in a row, varying only beta_external_expr, and every call was
# rejected (BRIER requires alpha in (0,1]) until the repeat guard aborted the run. It was
# never asking for alpha=0: there is no such model. It was saying "nothing to put here".
from brier_agent.guardrails import strip_null_filled_optionals


def test_the_exact_call_that_looped_a_real_run():
    args, dropped = strip_null_filled_optionals({
        "data_path": "/p/prep.rds", "sumstats_expr": "p$sumstats",
        "beta_external_expr": "p$beta_external", "family": "gaussian",
        "alpha": 0, "penalty": "", "gamma": 0,
    })
    assert sorted(dropped) == ["alpha", "gamma", "penalty"]
    assert args == {"data_path": "/p/prep.rds", "sumstats_expr": "p$sumstats",
                    "beta_external_expr": "p$beta_external", "family": "gaussian"}
    print("null-fill: the alpha=0/penalty=''/gamma=0 call is cleaned: OK")


def test_a_MEANINGFUL_value_is_never_dropped():
    """This is the whole line. alpha=0.5 IS elastic net; eta_list=[0] IS the no-transfer
    baseline and the comparison workflow depends on it. Dropping either would silently
    change the analysis -- which is worse than the bug being fixed."""
    args, dropped = strip_null_filled_optionals({
        "alpha": 0.5, "gamma": 3, "penalty": "MCP",
        "eta_list": [0], "eta_ceiling": 50,
    })
    assert dropped == [], dropped
    assert args["alpha"] == 0.5 and args["eta_list"] == [0] and args["eta_ceiling"] == 50
    print("null-fill: a meaningful knob survives untouched: OK")


def test_the_other_sentinels_that_broke_real_runs():
    # penalty_factor_expr="false" was evaluated as an R expression: object 'false' not found
    args, dropped = strip_null_filled_optionals({"penalty_factor_expr": "false"})
    assert dropped == ["penalty_factor_expr"] and args == {}
    # an EMPTY eta grid is not a grid (but [0] is a baseline -- see the test above)
    _, d2 = strip_null_filled_optionals({"eta_list": []})
    assert d2 == ["eta_list"]
    print("null-fill: penalty_factor_expr='false' and an empty eta grid are dropped: OK")


def test_non_dict_args_pass_through():
    assert strip_null_filled_optionals(None) == (None, [])
    assert strip_null_filled_optionals("boom") == ("boom", [])
    print("null-fill: malformed args pass through untouched: OK")


def test_null_string_placeholders_are_dropped():
    """A model that writes the literal string "NULL" (or None) for an optional it means to
    omit -- observed on a real run: brier_s with gamma="NULL", penalty="NULL",
    eta_list=["NULL"], alpha=None, which pydantic could not parse and hard-errored."""
    args, dropped = strip_null_filled_optionals({
        "beta_external_expr": "p$beta", "gamma": "NULL", "penalty": "NULL",
        "eta_list": ["NULL"], "alpha": None, "multi_method": "None",
    })
    assert sorted(dropped) == ["alpha", "eta_list", "gamma", "multi_method", "penalty"]
    assert args == {"beta_external_expr": "p$beta"}
    print("null-fill: 'NULL'/None placeholders for optionals are dropped: OK")


def test_typed_envelope_values_are_unwrapped():
    """Some models wrap a scalar arg as {'type':'string','value':X} instead of X, which
    fails a string/number schema. Unwrap it; a real dict arg (roles) is untouched."""
    args, dropped = strip_null_filled_optionals({
        "X_expr": {"type": "string", "value": "p$X"},
        "y_expr": {"value": "p$y"},
        "roles": {"target_X_train": "x.txt.gz", "target_y_train": "y.txt.gz"},
    })
    assert dropped == []
    assert args["X_expr"] == "p$X" and args["y_expr"] == "p$y"
    assert args["roles"] == {"target_X_train": "x.txt.gz", "target_y_train": "y.txt.gz"}
    print("null-fill: typed-envelope scalars unwrapped; a roles dict is untouched: OK")


# ---------------------------------------------------------------------------
# A SLIDING WINDOW IS NOT A BUDGET.
#
# compact_history kept the last 4 tool results verbatim -- but each can be 16k chars, so
# a long self-correcting chain still overflowed. T2_afr-summary_eur-summary died on a 422:
# "31050 `inputs` tokens and 2048 `max_new_tokens`" against Qwen-7B's 32769 window. That
# surfaces as an opaque ExceptionGroup with turns=0, and reads like a logic failure when
# it is arithmetic.
from brier_agent.guardrails import compact_history, _history_chars, _MAX_HISTORY_CHARS


def _long_chain(n=12, size=16000):
    msgs = [{"role": "system", "content": "S" * 5000},
            {"role": "user", "content": "analyse this"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": str(i)}]})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": "X" * size})
    return msgs


def test_a_long_chain_is_forced_under_the_budget():
    msgs = _long_chain()
    assert _history_chars(msgs) > 190000, "the fixture must actually be too big"
    out = compact_history(msgs)
    assert _history_chars(out) <= _MAX_HISTORY_CHARS, (
        "the window must be a BUDGET, not just a slide: this is the 422")
    print("compact: an over-long chain is forced under the budget: OK")


def test_the_structure_survives_so_the_request_stays_valid():
    """Only tool-result CONTENT may be cut. Drop a message or break a tool_call_id
    pairing and the API rejects the request outright -- a worse failure than the one
    being fixed."""
    msgs = _long_chain()
    out = compact_history(msgs)
    assert [m["role"] for m in out] == [m["role"] for m in msgs]
    assert [m.get("tool_call_id") for m in out] == [m.get("tool_call_id") for m in msgs]
    print("compact: roles and tool_call_id pairing are preserved: OK")


def test_the_system_prompt_and_the_user_are_never_cut():
    """The instructions and the task are the two things the model cannot do without."""
    msgs = _long_chain()
    out = compact_history(msgs)
    assert out[0]["content"] == msgs[0]["content"], "the system prompt was cut"
    assert out[1]["content"] == msgs[1]["content"], "the user's request was cut"
    print("compact: the system prompt and the user's request are untouched: OK")


def test_a_short_chain_is_left_alone():
    """No shrinking when there is no pressure: the model should see its recent results
    in full, and truncating them would cost accuracy for nothing."""
    msgs = [{"role": "system", "content": "S"},
            {"role": "tool", "tool_call_id": "1", "content": "small result"}]
    assert compact_history(msgs) == msgs
    print("compact: a chain under budget is untouched: OK")


def test_even_one_enormous_recent_result_cannot_blow_the_budget():
    """The last resort. A single tool result bigger than the whole budget used to sail
    through, because it was inside the verbatim window."""
    msgs = [{"role": "system", "content": "S" * 1000},
            {"role": "assistant", "content": ""},
            {"role": "tool", "tool_call_id": "1", "content": "X" * 200000}]
    out = compact_history(msgs)
    assert _history_chars(out) <= _MAX_HISTORY_CHARS
    assert "truncated" in out[-1]["content"]
    print("compact: even one enormous recent result is capped: OK")


def test_the_budget_counts_TOOL_CALLS_not_just_content():
    """The first attempt at this budget came back with the SAME 422, because it summed
    `content` and ignored `tool_calls` -- where every prep_auto call carries its whole
    roles map. A budget that cannot see what fills the window is not measuring the thing
    it is budgeting."""
    call_only = [{"role": "assistant", "content": "",
                  "tool_calls": [{"id": "1", "type": "function",
                                  "function": {"name": "prep_auto",
                                               "arguments": "{\"roles\": {" + "\"k\":\"v\"," * 400 + "}}"}}]}]
    assert _history_chars(call_only) > 2000, (
        "an assistant message whose text is empty can still be enormous")
    print("compact: the budget counts tool_calls, not just content: OK")


def test_the_budget_leaves_room_for_the_tool_schemas():
    """The schemas are ~13k tokens -- 43% of Qwen-7B's window -- and they are NOT in the
    message list, so they are invisible to any messages-only measurement. The budget must
    be sized with them subtracted, or it will authorise a request that cannot be sent."""
    schemas_tok = 13094
    reply_tok = 2048
    window_tok = 32769
    worst_chars_per_tok = 3
    assert _MAX_HISTORY_CHARS / worst_chars_per_tok + schemas_tok + reply_tok < window_tok, (
        "the budget must fit the window even under the WORSE tokenization estimate")
    print("compact: the budget fits the window with the schemas accounted for: OK")


def test_the_budget_is_sized_from_the_MEASURED_ratio_not_a_guess():
    """Two earlier budgets failed because the chars/token ratio was ASSUMED (4, then 3).
    Solved from a real 422 -- (52378 schema chars + 45000 message chars) / 31533 observed
    tokens = 3.09 -- because the payload is a mix: English docstrings tokenize near 4,
    JSON full of paths and R expressions far worse. A single guessed ratio was never
    going to hold.
    """
    schema_chars = 39923      # the 10 tools the benchmark actually offers
    reply_tok = 2048
    window_tok = 32769
    measured_cpt = 3.09
    pessimistic = measured_cpt * 0.94
    worst = (schema_chars + _MAX_HISTORY_CHARS) / pessimistic + reply_tok
    assert worst < window_tok, (
        f"the budget must fit the window at the MEASURED ratio: {worst:.0f} >= {window_tok}")
    assert window_tok - worst > 2000, "and it must keep real margin, not scrape by"
    print("compact: the budget fits the window at the measured ratio, with margin: OK")
