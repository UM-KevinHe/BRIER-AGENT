"""Tier 1 guardrails: cheap, universal checks that help any model.

These are the always-on safety rails the agent loop applies regardless of
which model drives it. They are deliberately model-agnostic (they help a
strong model too); the heavier, Qwen-specific, BRIER-tailored scaffolding
(scaffolded-context injection, routing hints, tool subsetting, ASCII gate)
lives behind extension hooks in ``loop.py`` and gets filled in after the
Phase 4 Qwen evaluation reveals BRIER's actual failure modes.

Each guardrail here is a small pure function so it can be unit-tested in
isolation and reasoned about independently of the loop.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# -- Hard-guard: reject hallucinated / non-exposed tool names ----------------


def check_tool_available(
    name: str, available_names: List[str]
) -> Optional[Dict[str, Any]]:
    """Return a structured error dict if ``name`` is not exposed, else None.

    Even when the exposed tool set is correct, a model can emit a tool
    name that was never offered (hallucination, or a name from its
    training data). Dispatching that would either fail confusingly or, in
    a direct-call design, run something unintended. We reject it here and
    tell the model exactly which tools it may use, forcing a retry within
    the allowed set. Returning None means the call may proceed.
    """
    if name in available_names:
        return None
    return {
        "status": "error",
        "class": "ToolNotAvailable",
        "where": "guardrails.check_tool_available",
        "message": (
            f"Tool '{name}' is not available. Choose one of the available "
            f"tools instead: {sorted(available_names)}."
        ),
    }


# -- Malformed-arguments retry -----------------------------------------------


def check_arguments_parsed(
    name: str, parsed_args: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Return a structured error if argument parsing failed, else None.

    ``tools.parse_tool_call_arguments`` returns ``{"_parse_error": raw}``
    when the model emitted unparseable JSON for a tool call. Rather than
    dispatch garbage, we surface a clear error telling the model its JSON
    was malformed, so its next turn can re-emit valid arguments. Returning
    None means the arguments are clean and dispatch may proceed.
    """
    if "_parse_error" in parsed_args:
        return {
            "status": "error",
            "class": "MalformedArguments",
            "where": "guardrails.check_arguments_parsed",
            "message": (
                f"The arguments for tool '{name}' were not valid JSON: "
                f"{parsed_args['_parse_error']!r}. Re-issue the tool call "
                f"with a valid JSON object of arguments."
            ),
        }
    return None


# -- Result compression (shrink the LLM-facing copy of a tool result) --------

# Keys whose values can be very large (full coefficient vectors, matrices,
# per-variant arrays). The model does not need the full payload to decide
# its next step; the UI and the trace keep the complete result separately.
_BULKY_KEYS = frozenset(
    {
        "beta",
        "betas",
        "coefficients",
        "coef",
        "eta",
        "etas",
        "values",
        "predictions",
        "pred",
        "y_pred",
        "ld",
        "ld_matrix",
        "snp_list",
        "variants",
        "importance",
    }
)

# A scalar list longer than this is summarized rather than shown in full.
_MAX_LIST_LEN = 8

# A dict with more entries than this is truncated to a head sample. Inspecting
# a wide genotype matrix returns a per-variant dict with thousands of keys; fed
# back uncapped it blows the context window (a real 422 seen on a 10000-variant
# file: one inspect result was 266k chars). The model only needs a sample of
# field names, not all of them.
_MAX_DICT_KEYS = 40

# Hard backstop on the serialized LLM-facing result. If the per-field shrink
# still leaves something huge, truncate the whole thing so one bulky tool
# result cannot overflow the next turn's request.
_MAX_RESULT_CHARS = 16000

# ---- conversation compaction ------------------------------------------------
# compress_tool_result_for_llm caps ONE result, but nothing capped the CONVERSATION:
# every tool result stayed in the history verbatim, so a long chain grows without
# bound. On a 32k-context model that eventually 422s ("inputs tokens + max_new_tokens
# must be <= 32769") -- which is exactly how the hardest case (16 files, a long
# self-correcting tool chain) died at turns=0.
#
# The fix is a sliding window: the MOST RECENT tool results stay verbatim (the model
# is actively reasoning about those), while OLDER ones shrink to a stub. Their
# messages are kept in place, with tool_call_id intact, because the API requires each
# tool result to answer its tool_call -- we shorten the content, never drop the turn.
_KEEP_FULL_TOOL_RESULTS = 4
_STALE_TOOL_CHARS = 500


# A HARD ceiling on the conversation, in characters.
#
# Keeping the last 4 tool results verbatim is not a budget: each can be 16k chars, so a
# long self-correcting chain still overflows. T2_afr-summary_eur-summary died on a 422 --
# "31050 `inputs` tokens and 2048 `max_new_tokens`" against Qwen-7B's 32769 -- which
# surfaces as an opaque ExceptionGroup with turns=0 and looks like a logic failure.
#
# Sized from the REAL 422s, not from a guess about tokenization. Three attempts:
#
#   1st: assumed ~4 chars/token and ~9k tokens of schemas -> 60k. The 422 came back.
#   2nd: assumed 3 chars/token, still guessed -> 45k. The 422 came back (31533 tokens).
#   3rd: SOLVE for the ratio from that failure instead of assuming it.
#
#        (52378 schema chars + 45000 message chars) / 31533 observed tokens
#          = 3.09 chars/token, for THIS payload mix
#
# The mix matters: schema docstrings are English (~4 c/t) but tool results are JSON full
# of paths and R expressions (far worse), so a single assumed ratio was never going to
# hold. Use the measured one, with 6% pessimism.
#
#     window                                    32769 tok
#   - reply allowance (max_new_tokens)           2048
#   = for schemas + messages                    30721 tok
#
#   schemas (10 tools, after the dead ones were dropped)   39923 chars
#   + this budget (INCLUDING the system prompt)            38000 chars
#   = 77923 chars / 2.9 c/t                              ~26.9k tok  -> fits, ~3.9k spare
#
# Cutting the four never-used tools from the benchmark allowlist bought ~3k tokens of
# PERMANENT headroom -- worth more than any history trimming, because the schemas are
# re-sent on every single turn while history is only trimmed once it is already large.
_MAX_HISTORY_CHARS = 38000
_MIN_STALE_CHARS = 80


def _history_chars(messages: List[Dict[str, Any]]) -> int:
    """The size of what is actually SENT, not just the text.

    Summing `content` alone under-counts badly: an assistant message that makes a tool
    call carries its arguments in `tool_calls`, and a prep_auto call's `roles` map is
    hundreds of characters. A budget that cannot see those is not measuring the thing it
    is budgeting -- which is why the 422 came back even with a "budget" in place.
    """
    total = 0
    for m in messages:
        total += len(str(m.get("content") or ""))
        tcs = m.get("tool_calls")
        if tcs:
            try:
                total += len(json.dumps(tcs, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                total += len(str(tcs))
    return total


def compact_history(messages: List[Dict[str, Any]],
                    keep_full: int = _KEEP_FULL_TOOL_RESULTS,
                    stale_chars: int = _STALE_TOOL_CHARS,
                    max_chars: int = _MAX_HISTORY_CHARS) -> List[Dict[str, Any]]:
    """Shrink tool-result contents so a long chain fits a small context window.

    Keeps the last `keep_full` tool results verbatim and truncates every earlier one to
    `stale_chars`. If the result STILL exceeds `max_chars`, it keeps shrinking -- fewer
    verbatim results, then shorter stubs, then truncating even the recent ones -- because
    a sliding window with no ceiling is not a budget, and the 422 it produces is opaque.

    Only tool-result CONTENT is touched. Message structure (roles, tool_call_id pairing)
    is preserved, so the request stays valid, and the system prompt and the user's own
    messages are never cut. Returns a new list; the caller's messages are untouched.
    """
    def shrink(keep: int, stale: int, cap_recent: int = 0) -> List[Dict[str, Any]]:
        tool_positions = [i for i, m in enumerate(messages)
                          if m.get("role") == "tool"]
        stale_idx = (set(tool_positions[:-keep]) if keep > 0
                     else set(tool_positions))
        out: List[Dict[str, Any]] = []
        for i, m in enumerate(messages):
            if m.get("role") != "tool":
                out.append(m)
                continue
            content = str(m.get("content") or "")
            limit = stale if i in stale_idx else cap_recent
            if limit and len(content) > limit:
                m = dict(m)
                m["content"] = (
                    content[:limit]
                    + f"\n...[tool result truncated, {len(content)} chars total]"
                )
            out.append(m)
        return out

    # Nothing to do: few enough results that none is stale, and no budget pressure.
    # Return the caller's own list (identity), so an untouched history stays untouched.
    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_positions) <= keep_full and _history_chars(messages) <= max_chars:
        return messages

    out = shrink(keep_full, stale_chars)
    if _history_chars(out) <= max_chars:
        return out

    # Still over budget. Give up the verbatim window before mangling anything else: an
    # older result is likelier to be spent than the one the model is reasoning about now.
    for keep in (2, 1, 0):
        out = shrink(keep, stale_chars)
        if _history_chars(out) <= max_chars:
            return out

    # Then shorten the stubs.
    for stale in (200, _MIN_STALE_CHARS):
        out = shrink(0, stale)
        if _history_chars(out) <= max_chars:
            return out

    # Last resort: cap EVERY tool result, recent ones included. Better a truncated
    # result the model can still read than a 422 that kills the run outright.
    return shrink(0, _MIN_STALE_CHARS, cap_recent=_MIN_STALE_CHARS)


def _summarize_value(key: str, value: Any) -> Any:
    """Shrink one value for the LLM-facing copy.

    Long numeric lists become a short summary (length + first few
    elements); wide dicts are truncated to a head sample of keys; nested
    dicts are recursed; everything else passes through.
    """
    if isinstance(value, list):
        n = len(value)
        if n > _MAX_LIST_LEN:
            head = value[:3]
            return {
                "_summary": f"list of {n} items (truncated for the model)",
                "head": head,
            }
        return value
    if isinstance(value, dict):
        n = len(value)
        items = list(value.items())
        if n > _MAX_DICT_KEYS:
            kept = {k: _summarize_value(k, v) for k, v in items[:_MAX_DICT_KEYS]}
            kept["_summary"] = (
                f"dict of {n} entries (showing first {_MAX_DICT_KEYS}; "
                f"truncated for the model)"
            )
            return kept
        return {k: _summarize_value(k, v) for k, v in items}
    return value


def compress_tool_result_for_llm(result: Any) -> Any:
    """Return a token-shrunk copy of a tool result for the LLM loop.

    The FULL result is kept by the loop separately (for the UI and the
    audit trace); only the copy fed back into the model's context on the
    next turn is shrunk. This keeps multi-turn loops from blowing the
    context window when a fit/cv result carries large arrays.

    Non-dict results are returned unchanged (already small). For dicts,
    bulky known-large fields are summarized and any long list is
    truncated; status/notice/message fields are preserved verbatim so the
    model still sees what happened and any guidance.
    """
    if not isinstance(result, dict):
        return result

    compressed: Dict[str, Any] = {}
    for key, value in result.items():
        if key in _BULKY_KEYS:
            if isinstance(value, list):
                n = len(value)
                compressed[key] = {
                    "_summary": f"{n} values (omitted for the model; "
                    f"available in the full result)",
                    "head": value[:3] if n else [],
                }
            elif isinstance(value, dict):
                compressed[key] = _summarize_value(key, value)
            else:
                compressed[key] = value
        else:
            compressed[key] = _summarize_value(key, value)

    # Hard backstop: if the per-field shrink still leaves an oversized result,
    # truncate so a single bulky tool result cannot overflow the next request.
    # Preserve the small status/signal fields verbatim; replace the rest with a
    # truncation note.
    try:
        serialized = json.dumps(compressed, ensure_ascii=False)
    except (TypeError, ValueError):
        return compressed
    if len(serialized) > _MAX_RESULT_CHARS:
        keep = {
            k: compressed[k]
            for k in ("status", "message", "class", "where", "shape",
                      "prepared_path", "expr_hints", "fit_id", "selection_id",
                      "criteria", "metric_value")
            if k in compressed and len(json.dumps(compressed[k],
                                                  ensure_ascii=False,
                                                  default=str)) < 2000
        }
        keep["_truncated"] = (
            f"result was {len(serialized)} chars; truncated for the model. "
            f"The full result is preserved in the trace. Re-inspect a specific "
            f"field or object if you need more detail."
        )
        return keep
    return compressed


# -- Iteration-cap helper (the cap itself lives in the loop) -----------------


def max_turns_error(max_turns: int) -> Dict[str, Any]:
    """Standard error payload when the loop hits its iteration cap."""
    return {
        "status": "error",
        "class": "MaxTurnsReached",
        "where": "loop",
        "message": (
            f"Reached the maximum of {max_turns} reasoning turns without a "
            f"final answer. The task may be too complex, or the model may be "
            f"stuck; try rephrasing or breaking it into smaller steps."
        ),
    }


# ---------------------------------------------------------------------------
# NULL-FILLED OPTIONAL ARGUMENTS.
#
# A small model does not leave an optional parameter out: it fills every field the
# schema declares, with the TYPE-ZERO of that field. On a real run the 7B sent
#
#     alpha = 0,  penalty = "",  gamma = 0
#
# to brier_s -- six times, varying only `beta_external_expr` -- and every call was
# rejected, because BRIER requires alpha in (0, 1]. It was not asking for alpha=0; there
# is no such model. It was saying "I have nothing to put here". The run looped until the
# guard aborted it. The same slip has produced `penalty_factor_expr = "false"`, which the
# tool then evaluated as an R expression and died on `object 'false' not found`.
#
# The server ALREADY treats `penalty = ""` as unset. It just never applied the same rule
# to alpha and gamma, and that inconsistency is the whole bug.
#
# So strip them HERE, in the harness, rather than loosening the tool. The MCP tool must
# stay strict for a human: someone who types alpha=0 meaning "no penalty" deserves the
# error, not a silent LASSO. But an agent null-filling a schema should not be able to
# hard-error a run with an argument it never meant to send.
#
# ONLY the no-op sentinel is dropped, and only for optional knobs where the zero carries
# no meaning: alpha=0 and gamma=0 are not models (BRIER rejects both), an empty penalty
# is not a penalty, an empty eta grid is not a grid. A MEANINGFUL value is never touched
# -- `alpha=0.5` is elastic net and survives, and `eta_list=[0]` is the no-transfer
# baseline and survives, which is why the rule is per-argument and not "drop all zeros".
_NULL_FILL_SENTINELS: Dict[str, tuple] = {
    "alpha": (0, 0.0),                      # BRIER: alpha in (0, 1]; 0 is not a model
    "gamma": (0, 0.0),                      # SCAD concavity > 2, MCP > 1; 0 is not one
    "penalty": ("",),                       # already treated as unset server-side
    "penalty_factor_expr": ("", "false", "true", "FALSE", "TRUE", "NULL", "None"),
    "eta_list": ([],),                      # NOT [0]: that is the baseline, and it means it
    "eta_floor": (0, 0.0),
    "eta_ceiling": (0, 0.0),
    "eta_n": (0,),
    "multi_method": ("",),
    "family": ("",),
    "outcome_family": ("",),
    "standardize_method": ("",),
    "align_method": ("",),
    "criteria": ("",),
}


def _is_null_like(v: Any) -> bool:
    """A placeholder a model writes for an optional it means to omit: None, or a string
    like "NULL" / "None" / "NA" / "" (case-insensitive). Distinct from a MEANINGFUL zero
    (0, [], [0]) -- those are handled by the per-key sentinels, not here."""
    return v is None or (
        isinstance(v, str) and v.strip().lower() in ("", "null", "none", "na", "nan"))


def _unwrap_value(v: Any) -> Any:
    """Unwrap a scalar that a model emitted as a typed envelope, e.g.
    ``{"type": "string", "value": "X"}`` or ``{"value": 3}`` -> ``"X"`` / ``3``. Only the
    exact ``{value}`` / ``{type, value}`` shape is unwrapped, so a real dict argument (a
    ``roles`` map, whose keys are role names) is untouched."""
    if isinstance(v, dict) and "value" in v and set(v.keys()) <= {"type", "value"}:
        return v["value"]
    return v


def strip_null_filled_optionals(args: Any) -> tuple:
    """Sanitise tool arguments before dispatch: unwrap typed-envelope values, then drop
    optional arguments the model set to a meaningless no-op sentinel or a null-like
    placeholder ("NULL"/None). Both are malformed-call patterns a small model produces
    (observed on real runs: gamma="NULL", eta_list=["NULL"], X_expr={"type":"string",
    "value":"X"}), which would otherwise hard-error a run on schema validation.

    Returns (cleaned_args, dropped_names). Non-dict input is passed through.
    """
    if not isinstance(args, dict):
        return args, []
    cleaned = {}
    dropped = []
    for k, v in args.items():
        v = _unwrap_value(v)
        sentinels = _NULL_FILL_SENTINELS.get(k)
        if sentinels is not None and (
            any(v == s and isinstance(v, type(s)) for s in sentinels)
            or _is_null_like(v)
            or (isinstance(v, (list, tuple)) and len(v) > 0
                and all(_is_null_like(x) for x in v))
        ):
            dropped.append(k)
            continue
        cleaned[k] = v
    return cleaned, dropped
