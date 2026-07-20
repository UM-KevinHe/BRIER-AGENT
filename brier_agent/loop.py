"""The BRIER agent loop: an async ReAct conductor.

This ties the pieces together. One ``run()`` call takes a user message and:

    user message
      -> (Tier 2 hook: scaffolded-context injection)
      -> (Tier 2 hook: tool subsetting)
      -> LLM completion with the exposed tools
      -> if tool_calls: guardrail-check each, dispatch via the MCP client,
         feed (compressed) results back, loop
      -> if no tool_calls: that is the final answer, stop
    capped at config.max_turns.

Tier 1 guardrails (iteration cap, malformed-argument retry, hard-guard on
hallucinated tool names, result compression, temperature/token caps) are
active. Tier 2 scaffolding (scaffolded inspect, routing hint, tool
subsetting, ASCII gate) is left as pass-through hooks to be filled in
after the Phase 4 Qwen evaluation reveals BRIER's real failure modes;
their insertion points are marked so the heavy logic slots in exactly
where it belongs without reshaping the loop.

The loop is async because the MCP client is async. It runs entirely inside
one MCP ``session`` so the server subprocess stays warm across tool calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .config import AgentConfig
from .llm_client import LLMClient
from .mcp_client import MCPClient
from . import tools as tools_mod
from . import guardrails


@dataclass
class AgentResult:
    """What one ``run()`` returns.

    ``text`` is the model's final natural-language answer. ``tool_results``
    is the list of full (uncompressed) tool results, for a UI to render.
    ``transcript`` is the message list as sent/received (audit). ``error``
    is set when the loop ended abnormally (max turns, context overflow).
    """

    text: str
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    turns: int = 0


class BrierAgent:
    """Async ReAct loop over the BRIER MCP tool surface.

    Parameters
    ----------
    config:
        Resolved :class:`AgentConfig`.
    llm:
        Optional pre-built :class:`LLMClient`. When omitted, one is built
        from the config. Tests inject a stub-backed client here.
    mcp:
        Optional pre-built :class:`MCPClient`. When omitted, one is built
        from the config's ``mcp_server_path``.
    system_prompt:
        The system prompt. When omitted, a minimal default is used; the
        BRIER-specific routing prompt lives in ``prompts.py`` and is wired
        in by the CLI/UI.
    """

    def __init__(
        self,
        config: AgentConfig,
        llm: Optional[LLMClient] = None,
        mcp: Optional[MCPClient] = None,
        system_prompt: Optional[str] = None,
        exclude_tools: Optional[Iterable[str]] = None,
        include_tools: Optional[Iterable[str]] = None,
        preprocessing_only: bool = False,
    ) -> None:
        self.config = config
        # PREPROCESSING-ONLY (the Task-3 cases): infer the module, prepare the
        # inputs, and STOP before the fit. The continuation hooks exist to drive a
        # small model through inspect -> prep -> fit -> select -> evaluate, so left
        # on they would push it into exactly the step a T3 case forbids. This
        # suppresses every post-prep nudge; the caller drops the fitters from
        # include_tools as well, so the step is unreachable, not merely discouraged.
        self.preprocessing_only = bool(preprocessing_only)
        # Tools removed from the active set for this agent. Used to drop
        # interactive-only tools (e.g. the start_analysis wizard) in
        # autonomous contexts like the benchmark, where there is no human to
        # answer the wizard's questions and it can only dead-end. The
        # interactive UI leaves this empty and keeps the wizard.
        self.exclude_tools = set(exclude_tools or ())
        # Allowlist: when set, ONLY these tools are exposed to the model
        # (exclude_tools still applies on top). This is the token-budget
        # subsetting hook: the full 31-tool surface exceeds the 32k context
        # of a small model once schemas + prompt are counted, so an
        # autonomous caller narrows to the tools its task actually needs.
        # Empty/None means expose all (the interactive UI default).
        self.include_tools = set(include_tools or ())
        self.llm = llm or LLMClient(
            endpoint=config.model_endpoint,
            model_name=config.model_name,
            api_key=config.api_key,
        )
        self.mcp = mcp or MCPClient(
            server_path=config.mcp_server_path,
            env=config.server_env(),
        )
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT

    # -- Tier 2 extension hooks (currently pass-through) -------------------
    #
    # These are the insertion points for the heavier, BRIER-specific,
    # Qwen-tailored scaffolding. They are deliberately no-ops for v1.0.0
    # and get filled in after Phase 4. Keeping them as named methods means
    # the loop body below does not change when they gain real logic.

    async def _inject_scaffolded_context(
        self, session: Any, user_text: str, data_path: Optional[str]
    ) -> str:
        """Pre-inspect the data and embed its structure in the message.

        Tier 2 scaffolding (Option A). When a ``data_path`` is provided, the
        harness itself calls ``inspect_data`` BEFORE the model's first turn
        and prepends the resulting structure (object and field names) to the
        user message, with an instruction to use those names verbatim in any
        ``*_expr`` argument. This makes correct field names an architectural
        guarantee rather than relying on the model to inspect and then reuse
        them: small models route correctly (verified) but invent placeholder
        argument values when left to guess. ``inspect_data`` reads only
        metadata (never the data values), so it is cheap and safe to pre-call
        even on very large genotype/LD files.

        When no ``data_path`` is available (e.g. a vague request), this is a
        pass-through and the model's own inspect-first behaviour handles it.

        Failures are non-fatal: if the pre-inspect errors, the original
        message is returned unchanged so the run still proceeds (the model
        can inspect itself).
        """
        if not data_path:
            return user_text

        try:
            inspect_result = await self.mcp.call_tool(
                session, "inspect_data", {"data_path": data_path}
            )
        except Exception:
            return user_text

        if not isinstance(inspect_result, dict) or (
            inspect_result.get("status") == "error"
        ):
            return user_text

        block = _format_inspect_block(data_path, inspect_result)
        if not block:
            return user_text

        # Prepend the authoritative structure, then the user's request.
        return f"{block}\n\n{user_text}"

    def _select_tool_subset(
        self,
        all_tools: List[Dict[str, Any]],
        inspect_result: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Hook: narrow exposed tools to the admissible BRIER family.

        Phase-4 TODO: classify the data (individual vs summary-stats,
        pretrained-coefs present, pooled cohorts) and drop inadmissible
        tools to shrink the routing space. For now: apply the ``include_tools``
        allowlist (if set) and then drop the explicit ``exclude_tools``. The
        allowlist is the token-budget control (the full surface overflows a
        small model's context).
        """
        if not self.include_tools and not self.exclude_tools:
            return all_tools
        out = []
        for t in all_tools:
            name = (t.get("function") or {}).get("name")
            if self.include_tools and name not in self.include_tools:
                continue
            if name in self.exclude_tools:
                continue
            out.append(t)
        return out

    def _routing_hint(self, inspect_result: Optional[Dict[str, Any]]) -> str:
        """Hook: a one-line family hint appended to the user message.

        Phase-4 TODO: return a BRIER routing hint once we know which
        signals confuse Qwen. For now, no hint.
        """
        return ""

    # -- The main loop ----------------------------------------------------

    async def run(
        self,
        user_text: str,
        data_path: Optional[str] = None,
        history: Optional[List] = None,
    ) -> AgentResult:
        """Run one user turn end-to-end inside a single MCP session."""
        async with self.mcp.session() as session:
            # List tools once per run and translate to OpenAI format.
            mcp_tools = await self.mcp.list_tools(session)
            all_openai_tools = tools_mod.mcp_tools_to_openai(mcp_tools)

            # Tier 2 hook: scaffolded-context injection (pass-through now).
            user_content = await self._inject_scaffolded_context(
                session, user_text, data_path
            )

            # Tier 2 hook: tool subsetting (pass-through now -> all tools).
            active_tools = self._select_tool_subset(all_openai_tools, None)
            active_names = tools_mod.tool_names(active_tools)

            # Build the message list: system, prior turns (text only), user.
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": self.system_prompt},
            ]
            if history:
                for entry in history:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    u, a = entry[0], entry[1]
                    if u:
                        messages.append({"role": "user", "content": str(u)})
                    if a:
                        messages.append({"role": "assistant", "content": str(a)})
            messages.append({"role": "user", "content": user_content})

            tool_results: List[Dict[str, Any]] = []
            called_tools: set = set()  # tools called so far (across turns)
            # Last successful prep contract (parsed_args, result), kept so a
            # failed fit call (e.g. a small model that emits empty args) can be
            # re-nudged with the exact data_path + expr_hints to retry.
            last_prep: Optional[tuple] = None
            # Repeated-call guard state (across turns): count identical
            # (name+args) calls and per-tool totals so a stuck model is nudged
            # onward and, if it keeps repeating, the run aborts instead of
            # spinning to the turn cap.
            call_sig_counts: Dict[str, int] = {}
            tool_call_counts: Dict[str, int] = {}
            stuck_repeat: Optional[str] = None
            # Per-source diagnostic state (brier_s multi-source): after prep_auto
            # assembles M>1 externals, drive one single-external fit per source
            # BEFORE the pooled multi-source fit, so each source is checked against
            # the eta=0 baseline individually.
            per_source_total = 0     # M (number of externals) once known
            per_source_done = 0      # single-external fits completed
            per_source_started = False
            per_source_fitter = "brier_s"  # the beta-external fitter for this case
            # Eta-ceiling escalations spent this run (each is a full refit +
            # selection, so it is capped).
            eta_escalations = 0
            # Preprocessing-only: which inputs were inspected, and which were actually
            # consumed by a prep. An inspected-but-unused file is usually a SECOND
            # representation of the cohort, hence a second consumer needing its own prep.
            inspected_files: set = set()
            used_files: set = set()
            unused_nudged = False
            final_text = ""
            error: Optional[str] = None
            turns = 0

            for turn in range(self.config.max_turns):
                turns = turn + 1
                # Compact the history before every call. A long self-correcting chain
                # accumulates tool results without bound and eventually overflows a
                # small model's context window (Qwen-7B is 32k), which surfaces as an
                # opaque 422 at turns=0. Older results shrink to a stub; the most
                # recent stay verbatim.
                completion = self.llm.complete(
                    messages=guardrails.compact_history(messages),
                    tools=active_tools,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                choice = completion.choices[0]
                msg = choice.message
                tool_calls = getattr(msg, "tool_calls", None) or []

                # Record the assistant turn (with tool_calls if present).
                assistant_entry: Dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or "",
                }
                if tool_calls:
                    assistant_entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ]
                messages.append(assistant_entry)

                # No tool calls -> this is the final answer.
                if not tool_calls:
                    final_text = msg.content or ""
                    break

                # Dispatch each tool call, with Tier 1 guardrails.
                # Tier 2 continuation hooks: a small model tends to stop after a
                # single successful tool instead of driving the multi-step
                # analysis chain (inspect -> prep -> fit -> select -> evaluate ->
                # compare). After each step we queue a nudge for the NEXT step;
                # the highest-priority (most advanced) nudge wins the turn.
                followup: Optional[str] = None
                followup_pri = 0
                for tc in tool_calls:
                    name = tc.function.name
                    parsed = tools_mod.parse_tool_call_arguments(
                        tc.function.arguments
                    )

                    # A small model fills EVERY optional field the schema declares, with
                    # the type-zero of that field: alpha=0, penalty="", gamma=0. It is
                    # not asking for alpha=0 -- there is no such model, BRIER rejects it
                    # -- it is saying "I have nothing to put here", and the run then
                    # loops on a rejected argument it never meant to send. Drop the
                    # no-op sentinels before dispatch. A MEANINGFUL value is untouched
                    # (alpha=0.5 is elastic net; eta_list=[0] is the baseline).
                    parsed, dropped_args = guardrails.strip_null_filled_optionals(
                        parsed
                    )

                    # Repeated-call guard: fingerprint this call (name + args).
                    sig = name + "|" + json.dumps(
                        parsed, sort_keys=True, ensure_ascii=False, default=str
                    )
                    call_sig_counts[sig] = call_sig_counts.get(sig, 0) + 1
                    tool_call_counts[name] = tool_call_counts.get(name, 0) + 1

                    # Guardrail: malformed JSON arguments -> retry message.
                    arg_err = guardrails.check_arguments_parsed(name, parsed)
                    if arg_err is not None:
                        result = arg_err
                    else:
                        # Guardrail: hard-guard on hallucinated tool names.
                        guard_err = guardrails.check_tool_available(
                            name, active_names
                        )
                        if guard_err is not None:
                            result = guard_err
                        else:
                            # Real dispatch via the MCP client.
                            result = await self.mcp.call_tool(
                                session, name, parsed
                            )

                    # Keep the full result for the UI / audit. `args` is what was
                    # actually DISPATCHED (post-sanitizing), so the trace shows the call
                    # that ran; `dropped_args` records what the harness removed, so the
                    # sanitizing is auditable rather than invisible.
                    entry: Dict[str, Any] = {
                        "tool": name, "args": parsed, "result": result
                    }
                    if dropped_args:
                        entry["dropped_args"] = dropped_args
                    tool_results.append(entry)
                    called_tools.add(name)

                    # Tier 2 continuation hooks: queue the NEXT-step nudge based
                    # on which step just succeeded. Priority orders the chain so
                    # the most advanced nudge wins if several tools ran this turn.
                    # Re-nudge a failed fit call with the exact args from the
                    # last prep contract (small models sometimes call the fitter
                    # with empty/missing args, then stop instead of retrying).
                    if (
                        name in _FIT_TOOLS
                        and isinstance(result, dict)
                        and result.get("status") == "error"
                        and last_prep is not None
                        and 5 > followup_pri
                    ):
                        followup_pri = 5
                        followup = _format_fit_retry(name, *last_prep, result)

                    # Re-nudge a failed SELECTION call to RETRY with a corrected
                    # criterion instead of narrating the fix and stopping (a small
                    # model tends to explain "we should use GIC" in prose rather
                    # than issuing the corrected call).
                    if (
                        name.endswith("_selection")
                        and isinstance(result, dict)
                        and result.get("status") == "error"
                        and 5 > followup_pri
                    ):
                        followup_pri = 5
                        followup = _format_selection_retry(name, result)

                    # Re-nudge a failed PREP call the same way. prep_auto's errors are
                    # deliberately actionable ("re-route to shape='brier_s'", "pass
                    # external_X + external_y") -- but the model reads the fix, writes
                    # it out in PROSE, and stops, so the run dies one turn after being
                    # told exactly what to do. Nothing downstream can run without a
                    # prepared object, so this dead-end is terminal: push it to reissue
                    # the CORRECTED call now.
                    if (
                        name in _PREP_TOOLS
                        and isinstance(result, dict)
                        and result.get("status") == "error"
                        and 5 > followup_pri
                    ):
                        followup_pri = 5
                        followup = _format_prep_retry(name, parsed, result)

                    if isinstance(result, dict) and result.get("status") == "ok":
                        cand_pri, cand_text = 0, None
                        if self.preprocessing_only:
                            # T3: the chain ENDS at prep. Only two nudges apply:
                            # inspect -> prep, and prep -> (maybe a second prep, for
                            # a task naming two consumers) -> report. Everything
                            # downstream is suppressed. The failed-prep retry nudge
                            # above still fires: it is what recovers a mis-route.
                            if result.get("prepared_path"):
                                last_prep = (parsed, result)
                                used_files |= _role_basenames(parsed)
                                shape = ((parsed or {}).get("shape")
                                         or result.get("shape") or "")
                                cand_pri = 2
                                unused = _unused_representation(
                                    shape, inspected_files, used_files
                                )
                                if unused and not unused_nudged:
                                    # An input was handed over and never used. That is
                                    # usually a second representation of the cohort,
                                    # and a second consumer. Say so ONCE.
                                    unused_nudged = True
                                    cand_text = _format_unused_input(unused, shape)
                                else:
                                    cand_text = _PREP_ONLY_FOLLOWUP
                            elif name.startswith("inspect_"):
                                inspected_files |= _inspected_basenames(parsed)
                                cand_pri = 1
                                cand_text = _INSPECT_FOLLOWUP_PREP_ONLY
                        elif result.get("prepared_path") and result.get("expr_hints"):
                            # prep (prep_auto / prep_data persist) -> fit next.
                            last_prep = (parsed, result)
                            cand_pri = 2
                            shape = (parsed or {}).get("shape") or result.get("shape") or ""
                            m_ext = _external_count(parsed)
                            # brier_s AND brier_i take a beta_external matrix, so a
                            # multi-source case of either gets the per-source
                            # diagnostic (fit each external alone before the pooled
                            # fit). brier_full pools RAW cohorts (external_X_k, not
                            # external_coef): _external_count returns 0, so it is
                            # naturally excluded.
                            if (shape in ("brier_s", "brier_i") and m_ext > 1
                                    and not per_source_started):
                                per_source_total = m_ext
                                per_source_done = 0
                                per_source_started = True
                                per_source_fitter = _PREP_AUTO_FITTER.get(shape, "brier_s")
                                cand_text = _format_per_source_nudge(
                                    1, m_ext, last_prep, per_source_fitter
                                )
                            else:
                                cand_text = _format_prep_auto_followup(parsed, result)
                        elif name in _FIT_TOOLS and result.get("fit_id"):
                            m_fit = result.get("M_external")
                            if (per_source_started and per_source_total
                                    and m_fit == 1
                                    and per_source_done < per_source_total):
                                # A single-external diagnostic fit completed: drive
                                # the next source, or the pooled fit once all done.
                                per_source_done += 1
                                cand_pri = 3
                                if per_source_done < per_source_total:
                                    cand_text = _format_per_source_nudge(
                                        per_source_done + 1, per_source_total,
                                        last_prep, per_source_fitter
                                    )
                                else:
                                    per_source_started = False
                                    cand_text = _format_pooled_fit_nudge(
                                        last_prep, per_source_fitter
                                    )
                            else:
                                # pooled / single-source fit -> selection next.
                                per_source_started = False
                                cand_pri = 3
                                cand_text = _format_fit_followup(
                                    name, parsed, result, last_prep
                                )
                        elif name.endswith("_selection") and result.get(
                            "selection_id"
                        ):
                            esc = None
                            if (result.get("_notice_eta_boundary")
                                    and eta_escalations < _MAX_ETA_ESCALATIONS):
                                # The selected eta pinned at the top of the grid:
                                # the optimum is outside it, so this model is
                                # truncated. Widen the ceiling and refit BEFORE
                                # evaluating: a boundary-pinned model's test
                                # metrics are not the model's metrics. Beats the
                                # evaluate nudge, or the run would score the
                                # truncated fit.
                                esc = _format_eta_escalation(
                                    name, parsed, result, tool_results
                                )
                            if esc is not None:
                                eta_escalations += 1
                                cand_pri = 6
                                cand_text = esc
                            else:
                                # selection -> test evaluation + comparator next.
                                cand_pri = 4
                                cand_text = _format_selection_followup(
                                    parsed, result, last_prep
                                )
                        elif name == "brier_evaluate" and _second_metric(
                            parsed, tool_results
                        ):
                            # A test eval reported ONE family metric; the policy
                            # is to report BOTH (R^2 + MSPE / AUC + deviance).
                            # Nudge the missing sibling metric before moving on.
                            cand_pri = 4
                            cand_text = _format_second_metric(
                                parsed, _second_metric(parsed, tool_results)
                            )
                        elif (
                            name == "brier_evaluate"
                            and "score_external_prs" not in called_tools
                            and _prep_hints(last_prep).get("beta_external_expr")
                        ):
                            # test eval done, comparator still missing (only
                            # when the shape has an external coefficient vector).
                            cand_pri = 3
                            cand_text = _format_evaluate_followup(
                                parsed, last_prep
                            )
                        elif name == "brier_evaluate" and _next_brierfull_comparator(
                            last_prep, tool_results
                        ):
                            # brier_full: the baseline and the external-only comparator
                            # have no coefficient vector to score, so each must be FIT
                            # (a single-cohort brier_i). Nothing drove that, and the
                            # selection nudge used to say there was no comparator to
                            # run, so a real run fit the pooled model, evaluated it, and
                            # stopped. Drive them one at a time.
                            cand_pri = 3
                            cand_text = _format_brierfull_comparator(
                                _next_brierfull_comparator(last_prep, tool_results),
                                last_prep,
                            )
                        elif name.startswith("inspect_"):
                            # inspect -> route to prep/fit next.
                            cand_pri = 1
                            cand_text = _INSPECT_FOLLOWUP
                        if cand_text is not None and cand_pri > followup_pri:
                            followup_pri, followup = cand_pri, cand_text

                    # Repeated-call guard (top priority): nudge, then abort.
                    n_ident = call_sig_counts[sig]
                    n_tool = tool_call_counts[name]
                    if n_ident > _MAX_IDENTICAL_CALLS:
                        stuck_repeat = (
                            f"Aborted after a repeated-call loop: `{name}` was "
                            f"called with identical arguments {n_ident} times "
                            f"without progress."
                        )
                    elif _is_spammy_tool(name) and n_tool >= _SAME_TOOL_HARD:
                        stuck_repeat = (
                            f"Aborted after a repeated-call loop: `{name}` was "
                            f"called {n_tool} times without progressing to the fit."
                        )
                    elif n_ident == _MAX_IDENTICAL_CALLS and followup_pri < 9:
                        followup_pri, followup = 9, _format_repeat_guard(name, result)
                    elif (
                        _is_spammy_tool(name)
                        and n_tool >= _SAME_TOOL_SOFT
                        and followup_pri < 8
                    ):
                        followup_pri, followup = 8, _format_stall_guard(
                            name, n_tool, last_prep is not None
                        )

                    # Feed a compressed copy back to the model.
                    llm_facing = guardrails.compress_tool_result_for_llm(result)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(llm_facing, ensure_ascii=False),
                        }
                    )

                # Inject the winning continuation nudge so the next turn issues
                # the follow-on call instead of stopping with a summary.
                if followup:
                    messages.append({"role": "user", "content": followup})

                # Repeated-call guard hit its hard limit: abort instead of
                # spinning to the turn cap.
                if stuck_repeat is not None:
                    error = stuck_repeat
                    final_text = final_text or ""
                    break
            else:
                # Loop exhausted without a final text answer.
                err = guardrails.max_turns_error(self.config.max_turns)
                error = err["message"]
                final_text = final_text or ""

            return AgentResult(
                text=final_text,
                tool_results=tool_results,
                transcript=messages,
                error=error,
                turns=turns,
            )


# A minimal placeholder system prompt. The real BRIER routing prompt lives
# in prompts.py and is passed in by the CLI/UI; this keeps the loop usable
# (and testable) on its own.
_DEFAULT_SYSTEM_PROMPT = (
    "You are BRIER-Agent, an assistant for transfer-learning genetic risk "
    "prediction (polygenic risk scores) using the BRIER R package. Use the "
    "provided tools to inspect data, fit models, and report results. Call a "
    "tool to act; only write a final prose answer once you have the result."
)

# Continuation nudge appended after a successful inspect, so a small model
# proceeds to prep/fit instead of stopping with a summary of the inspection.
_INSPECT_FOLLOWUP = (
    "You have inspected the data and now know its object and field names. Do "
    "NOT stop or write a summary here: the task requires building and "
    "evaluating a model, which you have not done yet. Your NEXT action MUST be "
    "a tool call to prep_auto (choose the shape from the target data: brier_i "
    "for an individual-level target plus a pretrained external model, brier_s "
    "for a summary-statistics target, brier_full for pooled raw cohorts).\n"
    "Build the `roles` mapping to include EVERY relevant file, not just "
    "training: for brier_i map target_X_train, target_y_train, snp_info, "
    "external_coef, AND (when the task provides them) the validation and test "
    "splits target_X_val, target_y_val, target_X_test, target_y_test. Later "
    "validation-set selection and test-set scoring FAIL if the validation and "
    "test splits are not assembled now.\n"
    "Set standardize=TRUE when the external model / coefficients are on a "
    "standardized scale (the task usually says so); a raw-scale target fit "
    "against a standardized external is a silent scale mismatch. Issue the "
    "prep_auto call now."
)

# ---------------------------------------------------------------------------
# PREPROCESSING-ONLY mode (the Task-3 cases).
#
# A T3 case asks the agent to INFER the module and PREPARE the inputs, and
# explicitly forbids fitting. The normal chain does the opposite: the prep -> fit
# hook fires the moment prep_auto succeeds and pushes the model straight into the
# step the case forbids. So in this mode the fitters are dropped from the tool
# allowlist AND the fit/selection/evaluate nudges are suppressed, leaving only
# inspect -> prep (-> prep again, if the task names more than one consumer).

_INSPECT_FOLLOWUP_PREP_ONLY = (
    "You have inspected the data and now know its object and field names. Do "
    "NOT stop or write a summary here. This is a PREPROCESSING task: your NEXT "
    "action MUST be a tool call to prep_auto.\n"
    "INFER the shape from the data you were given:\n"
    "  * brier_i    - an individual-level target (a genotype matrix AND a "
    "phenotype) plus a pretrained external coefficient model.\n"
    "  * brier_s    - a SUMMARY target (a GWAS file, no phenotype). Any genotype "
    "matrix present is an LD REFERENCE PANEL: pass it as target_ld_panel with "
    "ld_ancestry + ld_build, NOT as target_X_train.\n"
    "  * brier_full - two or more RAW individual-level cohorts (genotypes + "
    "phenotypes) and NO pretrained coefficients, so the cohorts are POOLED.\n"
    "Do NOT fit a model: no brier_i / brier_s / brier_full fit, no selection, no "
    "evaluation. Preparing the inputs IS the whole task. Issue the prep_auto call "
    "now."
)

_PREP_ONLY_FOLLOWUP = (
    "prep_auto succeeded and the inputs are prepared. Do NOT fit a model: this "
    "task is preprocessing only.\n"
    "If the task asks you to prepare the data for MORE THAN ONE module (for "
    "example the same cohort supplied both as individual-level data and as "
    "summary statistics, which are two different consumers), call prep_auto "
    "AGAIN now with the other shape.\n"
    "Otherwise you are done: report what you produced (the shape you routed to, "
    "how many variants survived, and the dimensions of beta.external) and stop."
)


# A file the model INSPECTED and then never passed to any prep. Two representations
# of one cohort (individual-level X + y AND a GWAS of the same samples) are two
# different consumers and need two preps, but the nudge above leaves that judgment to
# the model, which takes the "otherwise you are done" exit and stops: 3 of 3 runs
# prepared brier_i, ignored the GWAS they had just inspected, and lost the case.
#
# So surface the FACT -- you were handed an input and never used it -- and leave the
# INFERENCE (which module that input calls for) to the model, because inferring the
# module is the thing the case exists to test. Naming the shape here would be
# answering the question for it.
def _basename(p: Any) -> str:
    """Filename only. The model mixes bare names and absolute paths for the SAME file,
    so comparing raw strings would report a used file as unused."""
    return str(p).replace("\\", "/").rsplit("/", 1)[-1]


def _inspected_basenames(args: dict) -> set:
    a = args or {}
    paths = a.get("data_paths") or a.get("data_path") or []
    if isinstance(paths, str):
        paths = [paths]
    return {_basename(p) for p in paths if p}


def _role_basenames(args: dict) -> set:
    """Every file a prep call consumed, across scalar and packed-list roles."""
    out: set = set()
    for v in ((args or {}).get("roles") or {}).values():
        if isinstance(v, (list, tuple)):
            out |= {_basename(x) for x in v if x}
        elif v:
            out.add(_basename(v))
    return out


_REPRESENTATION_SIGNAL = {
    # a prep of THIS shape leaves THESE kinds of file conspicuously unused
    "brier_i": ("gwas", "sumstat"),      # ... a summary view of the same cohort
    "brier_s": ("pheno",),               # ... individual-level outcomes
}


def _unused_representation(shape: str, inspected: set, used: set) -> List[str]:
    """Inspected files, never used by any prep, that look like ANOTHER representation
    of the target. Empty when every input was consumed (the normal single-consumer
    case), so the nudge cannot fire spuriously."""
    markers = _REPRESENTATION_SIGNAL.get(shape or "", ())
    if not markers:
        return []
    return sorted(
        f for f in (inspected - used)
        if any(m in f.lower() for m in markers)
    )


def _format_unused_input(files: List[str], shape: str) -> str:
    listed = ", ".join(f"`{f}`" for f in files)
    return (
        f"prep_auto succeeded for shape='{shape}'. Do NOT fit a model: this task is "
        "preprocessing only.\n"
        f"But you INSPECTED {listed} and then never passed it to prep_auto. Every "
        "file in the case is supplied for a reason, and an input you never used is "
        "usually a SECOND REPRESENTATION of the same cohort: the same samples "
        "described a different way. A different representation is consumed by a "
        "DIFFERENT BRIER module, and the task asks you to prepare the inputs for "
        "EVERY consumer it names.\n"
        "Work out which module that unused file calls for, and if the task names it "
        "as a second consumer, call prep_auto AGAIN now with that shape and the roles "
        "it needs. If you conclude it is not a second consumer, say why and stop."
    )


# ---------------------------------------------------------------------------
# The eta grid, and the ceiling the harness used to ignore.
#
# The fit tools build a principled log-spaced eta grid (0 plus eta_floor..
# eta_ceiling) and the selection tools emit `_notice_eta_boundary` when the chosen
# eta lands exactly on the grid's top rung. That is a BOUNDARY, not an optimum: the
# best eta lies outside the grid, so the fit is truncated and its test metrics
# belong to a model that was cut off. Four of nine scored runs pinned this way and
# every one of them reported the truncated number, because nothing in the harness
# acted on the notice.
#
# Two halves of the fix live here. FIRST: these nudges used to hand the model an
# ad-hoc eta_list (`[0, 0.1, 1, 10, 100, 1000, 10000]`), which is how it learned to
# invent grids in the first place -- and an explicit eta_list overrides the
# principled knobs, so eta_ceiling escalation cannot work at all. They now tell it
# to OMIT the argument. SECOND: _format_eta_escalation below turns the notice into
# a continuation nudge (refit with a higher ceiling, then re-select), the same
# pattern every other step of the chain already uses.
_OMIT_ETA_LIST = (
    "OMIT eta_list entirely: the default eta grid is log-spaced and ALREADY "
    "includes eta=0 (the no-transfer baseline)."
)

# How many times one run may widen the eta ceiling. Each rung is a full refit +
# selection, so this is a compute cap as much as a loop guard: two rungs take the
# default ceiling of 10 out to 250, which is far enough to tell a real interior
# optimum from a grid artifact.
_MAX_ETA_ESCALATIONS = 2
_ETA_CEILING_FACTOR = 5.0


def _grid_max(grid) -> Optional[float]:
    """The largest eta in a (possibly nested, M>1) eta grid, or None."""
    if not isinstance(grid, (list, tuple)):
        return None
    flat: List[float] = []
    for x in grid:
        try:
            if isinstance(x, (list, tuple)):
                flat.extend(float(v) for v in x)
            else:
                flat.append(float(x))
        except (TypeError, ValueError):
            return None
    return max(flat) if flat else None


def _fit_behind_selection(sel_args: dict, tool_results: List[Dict[str, Any]]):
    """The (name, args, result) of the fit a selection call selected over.

    A selection carries the `fit_id` of the fit it ran on, so the escalation nudge
    can restate that exact fit with a raised ceiling instead of guessing.
    """
    fit_id = (sel_args or {}).get("fit_id")
    for tr in reversed(tool_results):
        if tr.get("tool") not in _FIT_TOOLS:
            continue
        res = tr.get("result")
        if not isinstance(res, dict) or res.get("status") != "ok":
            continue
        if fit_id is None or res.get("fit_id") == fit_id:
            return tr["tool"], (tr.get("args") or {}), res
    return None, None, None


def _format_eta_escalation(sel_name: str, sel_args: dict, sel_result: dict,
                           tool_results: List[Dict[str, Any]]) -> Optional[str]:
    """Nudge a REFIT with a raised eta_ceiling after a boundary-pinned selection.

    Returns None when the fit behind the selection cannot be identified (no fit to
    restate), in which case the chain proceeds as before rather than nudging blind.
    """
    fit_name, fit_args, fit_result = _fit_behind_selection(sel_args, tool_results)
    if fit_name is None:
        return None
    # The grid that actually ran, not the one that was asked for: the model may have
    # omitted eta_list, in which case only the resolved grid knows where the top is.
    top = _grid_max(fit_result.get("eta_list_used"))
    if top is None:
        top = _grid_max(sel_result.get("eta_grid_values"))
    if top is None or top <= 0:
        return None
    new_ceiling = top * _ETA_CEILING_FACTOR

    keep = {k: v for k, v in (fit_args or {}).items()
            if k not in ("eta_list", "eta_ceiling", "eta_floor", "eta_n")}
    arg_pairs = ", ".join(
        f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}"
        for k, v in keep.items()
    )
    return "\n".join([
        f"STOP: `{sel_name}` selected eta at the TOP of the grid (the grid ends at "
        f"{top:g}). That is a BOUNDARY, not an optimum: the best eta lies OUTSIDE "
        "the grid, so this model is truncated and its test numbers are not the "
        "model's numbers. Do NOT evaluate it and do NOT report it.",
        f"WIDEN the search and refit. Issue a `{fit_name}` call NOW with the same "
        f"inputs and a higher ceiling:",
        f"  {arg_pairs}" if arg_pairs else "",
        f"  eta_ceiling={new_ceiling:g}",
        "Do NOT pass eta_list (an explicit grid overrides eta_ceiling and defeats "
        "the widening). Then re-run the selection on the new fit, exactly as "
        "before. Issue the refit now.",
    ])


def _core_fit_hints(hints: dict) -> dict:
    """Keep only the expr_hints the FITTER consumes (X/y/beta or sumstats/XtX/
    beta or X/y/cohort). The validation/test hints are for the later selection
    and evaluation steps, and the per-cohort external-only comparator hints
    (``*_ext_k_expr`` / ``beta_zero_expr`` from brier_full) plus the
    target-cohort baseline hints (``*_target_expr``) are for the decision
    workflow: none are valid arguments to the main fit tools, so they must not be
    listed in the fit nudge."""
    return {
        k: v for k, v in hints.items()
        if not (k.endswith("_val_expr") or k.endswith("_test_expr")
                or "_ext_" in k or "_target_" in k or k == "beta_zero_expr")
    }


def _external_count(prep_args: dict) -> int:
    """How many external coefficient models a prep_auto call carried (M).

    Mirrors prep_auto's .load_externals: COMBINE every external role -- the
    unnumbered alias (a scalar or a packed list) PLUS the numbered
    external_coef_1/_2/... -- deduping by filename. A small model mixes spellings
    (e.g. external_coef=model1 AND external_coef_1=model2 for two distinct
    models), so counting must sum them, not pick one spelling.
    """
    roles = (prep_args or {}).get("roles") or {}
    files: list = []
    for k in ("external_coef", "beta_external", "external", "external_beta"):
        v = roles.get(k)
        if isinstance(v, (list, tuple)):
            files.extend(v)
            break
        if v is not None:
            files.append(v)
            break
    for i in range(1, 21):
        for b in ("external_coef", "beta_external", "external"):
            v = roles.get(f"{b}_{i}")
            if v is not None:
                files.append(v)
                break
    # RAW externals fit internally (Bucket B): a summary GWAS (external_sumstats)
    # or an individual cohort (external_X keyed by its X file), unnumbered (scalar
    # or packed list) plus numbered _1/_2.... prep_auto fits each and merges them
    # into the same p x M beta.external, so they count toward M for the per-source
    # diagnostic exactly like pretrained coef files.
    for primary in ("external_sumstats", "external_X"):
        v = roles.get(primary)
        if isinstance(v, (list, tuple)):
            files.extend(v)
        elif v is not None:
            files.append(v)
        for i in range(1, 21):
            vk = roles.get(f"{primary}_{i}")
            if vk is not None:
                files.append(vk)
    # Dedup by value (same file named under two spellings counts once).
    seen: set = set()
    uniq = [f for f in files if not (f in seen or seen.add(f))]
    return len(uniq)


def _format_per_source_nudge(k: int, total: int, last_prep, fitter: str = "brier_s") -> str:
    """Nudge the k-th single-external diagnostic fit (before the pooled fit).

    The per-source diagnostic fits EACH external alone (beta_external one column)
    so its transfer benefit can be read against the eta=0 no-transfer baseline,
    per rubric row #3 ("fits each external one-by-one vs the eta=0 baseline").
    Applies to the fitters that take a beta_external matrix (brier_s, brier_i).
    """
    _, prep_result = last_prep
    prepared_path = prep_result.get("prepared_path", "")
    hints = _core_fit_hints(prep_result.get("expr_hints") or {})
    beta = hints.get("beta_external_expr", "")
    beta_k = f"{beta}[, {k}]" if beta else ""
    other = ", ".join(f'{kk}="{vv}"' for kk, vv in hints.items()
                      if kk != "beta_external_expr")
    lines = [
        f"PER-SOURCE DIAGNOSTIC (external {k} of {total}): BEFORE the pooled "
        "multi-source fit, fit EACH external ALONE to see whether it helps the "
        f"target versus the eta=0 (no-transfer) baseline. Issue a {fitter} call "
        "NOW with a SINGLE external column:",
    ]
    if prepared_path:
        lines.append(f'  data_path="{prepared_path}"')
    beta_line = f'  beta_external_expr="{beta_k}"'
    if other:
        beta_line += f", {other}"
    lines.append(beta_line)
    lines.append(_OMIT_ETA_LIST)
    lines.append(
        f"This is source {k}. After all {total} single-external fits are done, "
        "fit the POOLED model with every external together."
    )
    return "\n".join(lines)


def _format_pooled_fit_nudge(last_prep, fitter: str = "brier_s") -> str:
    """After the per-source diagnostics, nudge the POOLED multi-source fit."""
    _, prep_result = last_prep
    prepared_path = prep_result.get("prepared_path", "")
    hints = _core_fit_hints(prep_result.get("expr_hints") or {})
    hint_pairs = ", ".join(f'{k}="{v}"' for k, v in hints.items())
    lines = [
        "Per-source diagnostics done. NOW fit the POOLED multi-source model with "
        f"ALL externals together. Issue a {fitter} call with:",
    ]
    if prepared_path:
        lines.append(f'  data_path="{prepared_path}"')
    if hint_pairs:
        lines.append(f"  {hint_pairs}")
    lines.append(
        "Pass beta_external_expr VERBATIM (the FULL matrix, every external). "
        + _OMIT_ETA_LIST + " Issue the pooled fit now."
    )
    return "\n".join(lines)


# Map a prep_auto shape to the fitting tool it feeds.
_PREP_AUTO_FITTER = {
    "brier_i": "brier_i",
    "brier_s": "brier_s",
    "brier_full": "brier_full",
}


def _format_prep_auto_followup(args: dict, result: dict) -> str:
    """Build the continuation reminder injected after a successful prep_auto.

    prep_auto only ASSEMBLES fit-ready inputs; it is not a fit and not the
    final step of a fitting task. A small model tends to summarise here and
    narrate the fit in future tense ("I will now fit...") instead of issuing
    the follow-on tool call. This reminder restates the fitter to call, the
    object to load, and the exact expr_hints, at the decision point (the same
    "inject the facts" pattern as the scaffolded-context field-name hook).
    """
    shape = (args or {}).get("shape") or result.get("shape") or ""
    fitter = _PREP_AUTO_FITTER.get(shape, "the matching brier_* fitting tool")
    prepared_path = result.get("prepared_path", "")
    hints = _core_fit_hints(result.get("expr_hints") or {})
    hint_pairs = ", ".join(f"{k}=\"{v}\"" for k, v in hints.items())

    lines = [
        "REMINDER: prep_auto has only ASSEMBLED the fit-ready inputs; it is "
        "NOT a fit and NOT the final step of a fitting task. Do NOT reply with "
        "a summary that says you \"will\" fit next, and do NOT hand-write R "
        "code.",
        f"Your NEXT action MUST be a tool call to {fitter} with these "
        f"arguments:",
    ]
    if prepared_path:
        # The fitter loads the prepared object itself when given the path; the
        # expr_hints reference the variable name that load binds. Pass the
        # path as an argument, not as hand-written readRDS.
        lines.append(f'  data_path="{prepared_path}"')
    if hint_pairs:
        lines.append(f"  {hint_pairs}")
    lines.append(
        "Use those expr_hints VERBATIM (they name the loaded object). "
        + _OMIT_ETA_LIST + " Issue the fit tool call now."
    )
    return "\n".join(lines)


def _format_fit_retry(name: str, prep_args: dict, prep_result: dict,
                      result: Any = None) -> str:
    """Re-nudge a failed fit call with the exact args from the prep contract.

    A small model sometimes issues the fitter with empty/missing ``*_expr``
    arguments and then stops when the tool rejects them. This restates the
    precise call to make, using the prepared object's path and expr_hints.

    It must also carry the ERROR. This nudge used to assert, unconditionally, that
    the call failed "because required arguments were missing or empty" -- and then
    re-fed the same data_path and expr_hints. When the real cause was a REJECTED
    argument (the 7B fills in every optional schema field, and set `alpha = 0`, which
    BRIER rejects), the diagnosis was simply wrong: nothing was missing, and the
    restated call still carried the poisoned argument. The model reissued it verbatim
    until the repeat guard aborted the run. A retry nudge that misdiagnoses the failure
    guarantees the identical call.
    """
    prepared_path = prep_result.get("prepared_path", "")
    hints = _core_fit_hints(prep_result.get("expr_hints") or {})
    hint_pairs = ", ".join(f'{k}="{v}"' for k, v in hints.items())
    msg = result.get("message", "") if isinstance(result, dict) else ""
    lines = [
        f"Your last {name} call FAILED: {msg}" if msg
        else f"Your last {name} call FAILED.",
        "",
        "Read that error and FIX THE ARGUMENT IT NAMES. If the argument is an "
        "OPTIONAL one you were not asked for, DROP IT ENTIRELY rather than guessing "
        "a value: omit `alpha`, `penalty`, `gamma`, `penalty_factor_expr` and "
        "`eta_list` unless the user explicitly asked for them. Do NOT reissue the "
        "same call unchanged.",
        f"Retry {name} NOW with these arguments (the prepared inputs are already "
        "assembled):",
    ]
    if prepared_path:
        lines.append(f'  data_path="{prepared_path}"')
    if hint_pairs:
        lines.append(f"  {hint_pairs}")
    lines.append(
        "Pass the expr_hints VERBATIM and pass NOTHING ELSE. " + _OMIT_ETA_LIST
        + f" Issue the corrected {name} call now."
    )
    return "\n".join(lines)


def _format_prep_retry(name: str, args: dict, result: dict) -> str:
    """Re-nudge a FAILED prep call to REISSUE the corrected call.

    prep_auto's errors are deliberately actionable: they name the shape to re-route
    to, the roles to use, the ancestry to pass. On a real run the 7B read a re-route
    steer, restated it correctly in PROSE ("therefore we need to route to brier_s"),
    and then STOPPED -- one turn after being told exactly what to do. Nothing
    downstream can run without a prepared object, so a dead-end here kills the run.
    """
    msg = result.get("message", "") if isinstance(result, dict) else ""
    shape = (args or {}).get("shape")
    lines = [
        f"Your `{name}` call FAILED: {msg}",
        "",
        "Do NOT stop, and do NOT restate the fix in prose. The error above tells you "
        f"exactly what to change. Apply it and issue the `{name}` call AGAIN now, in "
        "this turn, as a tool call.",
    ]
    if shape:
        lines.append(
            f"Your last attempt used shape='{shape}'. If the error says to re-route "
            "to a different shape, use that shape and keep the roles it tells you to "
            "keep. Nothing else can run until this call succeeds."
        )
    return "\n".join(lines)


def _format_selection_retry(name: str, result: dict) -> str:
    """Re-nudge a FAILED selection to RETRY with a corrected criterion.

    A small model often reads a selection error (invalid criterion, missing TN)
    and then STOPS, narrating the correction in prose instead of issuing the
    corrected call. This restates: fix the named argument and call again now.
    """
    msg = result.get("message", "") if isinstance(result, dict) else ""
    return (
        f"Your `{name}` call FAILED: {msg} Do NOT stop and do NOT narrate the fix "
        f"in prose. Correct the argument the error names (use a valid `criteria`; "
        f"for an information criterion such as GIC/Cp also pass `TN`, the training "
        f"sample size) and issue the `{name}` call AGAIN now."
    )


# Family metric pairs: reporting policy is BOTH per family. poisson has only dev.
_METRIC_SIBLING = {
    "gaussian.rsq": "gaussian.mspe",
    "gaussian.mspe": "gaussian.rsq",
    "binomial.auc": "binomial.dev",
    "binomial.dev": "binomial.auc",
}


def _is_test_eval_args(args: Any) -> bool:
    return "test" in str(args).lower()


def _second_metric(parsed: Any, tool_results: List[Dict[str, Any]]) -> Optional[str]:
    """If a TEST brier_evaluate reported one family metric whose SIBLING metric
    has not yet been successfully evaluated on test, return the sibling metric to
    report next; else None (no sibling, not a test eval, or both already done)."""
    if not isinstance(parsed, dict):
        return None
    crit = str(parsed.get("criteria", "")).strip().lower()
    sib = _METRIC_SIBLING.get(crit)
    if sib is None or not _is_test_eval_args(parsed):
        return None
    for tr in tool_results:
        if tr.get("tool") in ("brier_evaluate", "score_external_prs"):
            a = tr.get("args") or {}
            r = tr.get("result")
            if (isinstance(a, dict)
                    and str(a.get("criteria", "")).strip().lower() == sib
                    and _is_test_eval_args(a)
                    and isinstance(r, dict) and r.get("status") == "ok"):
                return None
    return sib


def _format_second_metric(parsed: dict, sib: str) -> str:
    """Nudge the missing family metric on the test set (report BOTH policy)."""
    return (
        "You reported ONE metric on the test set. The policy is to report BOTH "
        f"family metrics. Call `brier_evaluate` AGAIN on the SAME test set with "
        f'`criteria="{sib}"` (same `selection_id` and the same test `*_expr` '
        "arguments) to report the second metric now."
    )


# The base fit tools whose result should trigger the fit -> selection nudge.
_FIT_TOOLS = frozenset({"brier_i", "brier_s", "brier_full"})
_PREP_TOOLS = frozenset({"prep_auto", "prep_data"})

# Repeated-call guard thresholds. A small model can get stuck re-issuing the same
# call and never progress (observed: inspect_user_data x22). Any tool called with
# IDENTICAL arguments is nudged on the 2nd such call and the run aborts on the 3rd.
# The spam-prone, should-run-once-ish tools (inspect_* / prep_*) also get a
# per-tool soft nudge and a hard abort regardless of args; the fit/select/evaluate
# tools are exempt from the per-tool caps because the comparison workflow calls
# them several times with DIFFERENT args (per model, per metric).
_MAX_IDENTICAL_CALLS = 2
_SAME_TOOL_SOFT = 4
_SAME_TOOL_HARD = 8


def _is_spammy_tool(name: str) -> bool:
    return name.startswith("inspect_") or name.startswith("prep_")


def _format_repeat_guard(name: str, result: Any) -> str:
    status = result.get("status") if isinstance(result, dict) else "unknown"
    return (
        f"STOP repeating: you have already called `{name}` with these EXACT "
        f"arguments (it returned status='{status}'). Calling it again with the "
        f"same arguments will not help. If it errored, FIX the arguments; "
        f"otherwise proceed to the NEXT step of the workflow. Do not repeat this "
        f"identical call."
    )


def _format_stall_guard(name: str, n: int, has_prep: bool) -> str:
    if has_prep:
        return (
            f"You have called `{name}` {n} times, and the data is ALREADY "
            f"prepared. Do NOT inspect or prepare again. Call the FIT tool now "
            f"with the prepared object's data_path and the returned expr_hints, "
            f"then continue the comparison workflow."
        )
    return (
        f"You have called `{name}` {n} times and now have enough information "
        f"about the data. STOP inspecting and call `prep_auto` now with the "
        f"correct shape and roles to assemble the fit-ready inputs."
    )

# The post-fit chain's metrics must match the outcome family, or brier_*_selection
# rejects the criteria (it validates the metric against the fit's family) and a
# binomial run gets a linear-probability analysis. prep_auto now echoes the detected
# family in its result, so the continuation hooks read it from last_prep.
def _prep_family(last_prep) -> str:
    """The outcome family prep_auto detected/declared (for choosing metrics).
    Defaults to gaussian when unknown (e.g. an older prep result without the echo)."""
    if not last_prep:
        return "gaussian"
    fam = (last_prep[1] or {}).get("outcome_family")
    return fam if fam in ("gaussian", "binomial", "poisson") else "gaussian"


def _sel_criteria(family: str) -> str:
    """Default VALIDATION-selection metric for a family."""
    return {"binomial": "binomial.dev", "poisson": "poisson.dev"}.get(family, "gaussian.mspe")


def _report_criteria(family: str) -> str:
    """Primary held-out reporting/comparison metric for a family (comparable across
    the transfer fit and the external-only score)."""
    return {"binomial": "binomial.auc", "poisson": "poisson.dev"}.get(family, "gaussian.rsq")


def _expr_base(args: dict) -> str:
    """Recover the prepared object's variable name from an ``*_expr`` argument.

    prep_auto binds the assembled object under a file-basename variable and its
    expr_hints look like ``prep_auto_brier_i$X``. The fit / selection / evaluate
    calls carry that same base in their ``*_expr`` args, so a follow-up nudge
    can build the val / test / beta expressions without threading extra state.
    """
    for k, v in (args or {}).items():
        if isinstance(v, str) and k.endswith("_expr") and "$" in v:
            return v.split("$", 1)[0]
    return "prepared"


def _prep_hints(last_prep) -> dict:
    """Return the stored prep contract's expr_hints (basename-var form) or {}."""
    if last_prep and isinstance(last_prep[1], dict):
        return last_prep[1].get("expr_hints") or {}
    return {}


def _split_exprs(args: dict, last_prep) -> dict:
    """Resolve val/test/beta expressions and which splits were assembled.

    Prefer the prep contract's expr_hints (they name the loaded object and list
    ONLY the splits prep_auto actually assembled, so their presence tells us
    whether a validation split exists); fall back to <base>$field.
    """
    hints = _prep_hints(last_prep)
    base = _expr_base(args)

    def pick(key, field):
        return hints.get(key, f"{base}${field}")

    return {
        "has_val": "X_val_expr" in hints,
        # No beta hint means the shape has no external coefficient vector (e.g.
        # brier_full pools raw cohorts), so the external-only comparator does
        # not apply.
        "has_beta": "beta_external_expr" in hints,
        "X_val": pick("X_val_expr", "X_val"),
        "y_val": pick("y_val_expr", "y_val"),
        "X_test": pick("X_test_expr", "X_test"),
        "y_test": pick("y_test_expr", "y_test"),
        "beta": hints.get("beta_external_expr", f"{base}$beta_external"),
    }


def _format_fit_followup(name: str, args: dict, result: dict, last_prep) -> str:
    """After a base fit succeeds, nudge selection: validation-MSPE if a val split
    was assembled, else IC-based (BIC). Never select on the test split."""
    sel_tool = f"{name}_selection"
    fit_id = result.get("fit_id", "")
    dp = (args or {}).get("data_path", "")
    dp_arg = f', data_path="{dp}"' if dp else ""
    sp = _split_exprs(args, last_prep)
    val_crit = _sel_criteria(_prep_family(last_prep))  # binomial.dev / gaussian.mspe / ...

    # An EXTERNAL-COHORT fit (a brier_full external-only comparator) must NOT be
    # selected on the TARGET's validation set: that leaks target data into a comparator
    # whose entire point is to be purely external. Select it on its OWN held-out split
    # when it shipped one, else on an information criterion.
    hints = _prep_hints(last_prep)
    k_ext = _external_cohort_of(args, hints)
    if k_ext is not None:
        own_x = hints.get(f"X_ext_{k_ext}_val_expr", "")
        own_y = hints.get(f"y_ext_{k_ext}_val_expr", "")
        if own_x and own_y:
            select_line = (f'  fit_id="{fit_id}", criteria="{val_crit}", '
                           f'X_val_expr="{own_x}", y_val_expr="{own_y}"{dp_arg}')
            how = ("on THAT COHORT'S OWN validation split (never the target's: this "
                   "comparator must stay purely external)")
        else:
            select_line = f'  fit_id="{fit_id}", criteria="BIC"{dp_arg}'
            how = ("with BIC (this cohort shipped no validation split; do NOT select it "
                   "on the target's validation set, which would leak target data into a "
                   "comparator that must be purely external)")
        return "\n".join([
            f'REMINDER: the comparator fit is done (fit_id="{fit_id}"). Do NOT stop.',
            f"Your NEXT action MUST be a tool call to {sel_tool} to select it {how}:",
            select_line,
            "Issue that call now.",
        ])

    if sp["has_val"]:
        how = "on the VALIDATION set (a val split was assembled)"
        select_line = (
            f'  fit_id="{fit_id}", criteria="{val_crit}", '
            f'X_val_expr="{sp["X_val"]}", y_val_expr="{sp["y_val"]}"{dp_arg}'
        )
    else:
        how = ("with BIC (NO validation split was assembled; do NOT invent a "
               "held-out set and do NOT select on the test split)")
        select_line = f'  fit_id="{fit_id}", criteria="BIC"{dp_arg}'
    return "\n".join([
        f'REMINDER: the fit is done (fit_id="{fit_id}"), but this is NOT the '
        "final step. Do NOT stop or summarise yet.",
        f"Your NEXT action MUST be a tool call to {sel_tool} to select "
        f"hyperparameters {how}:",
        select_line,
        "Issue that call now.",
    ])


def _format_selection_followup(args: dict, result: dict, last_prep) -> str:
    """After selection, nudge test-set scoring + the external-only comparator."""
    sid = result.get("selection_id", "")
    dp = (args or {}).get("data_path", "")
    dp_arg = f', data_path="{dp}"' if dp else ""
    sp = _split_exprs(args, last_prep)
    fam = _prep_family(last_prep)
    rep_crit = _report_criteria(fam)
    fam_arg = f', family="{fam}"' if fam != "gaussian" else ""
    lines = [
        f'REMINDER: selection is done (selection_id="{sid}"). Do NOT stop: you '
        "must now score the fitted model on the held-out TEST set.",
        f'Call brier_evaluate with selection_id="{sid}", '
        f'newx_expr="{sp["X_test"]}", newy_expr="{sp["y_test"]}", '
        f'criteria="{rep_crit}"{dp_arg}.',
    ]
    if sp["has_beta"]:
        lines.append(
            'Then ALSO score the external-only comparator: score_external_prs '
            f'with newx_expr="{sp["X_test"]}", newy_expr="{sp["y_test"]}", '
            f'beta_expr="{sp["beta"]}", criteria="{rep_crit}"{fam_arg}{dp_arg}. '
            "If the transfer model does NOT beat the external-only score, say so "
            "and recommend running brier_s."
        )
    elif _prep_hints(last_prep).get("beta_zero_expr"):
        # brier_full. The externals are RAW cohorts, so no coefficient vector exists
        # and the comparator cannot be SCORED -- it must be FIT. This branch used to
        # say "there is no external-only comparator to run", which is simply false, and
        # it talked the model out of the step the rubric scores: a real run fit the
        # pooled model, evaluated it, and stopped, having never fit the baseline or the
        # comparator. Say what actually has to happen; the sub-chain below drives it.
        lines.append(
            "Then the comparison work: this shape's externals are RAW cohorts, so the "
            "baseline and the external-only comparator must each be FIT with brier_i "
            "(BRIERfull needs >= 2 cohorts, so it cannot fit a single-cohort model). "
            "You will be prompted for each one."
        )
    lines.append("Issue the brier_evaluate call now.")
    return "\n".join(lines)


def _external_cohort_of(args: dict, hints: dict) -> Optional[int]:
    """Which external cohort k this fit runs on, or None if it is not one.

    Match the fit's X_expr against the VALUE of prep_auto's `X_ext_k_expr` hint. The
    obvious shortcut -- looking for "_ext_" in the expression -- is wrong: that string
    is in the hint's NAME, never in its value. The values are cohort subsets, e.g.
    `prepared$X[prepared$cohort == 1L, , drop = FALSE]`. A check written that way can
    never fire on a real run, and would have failed a CORRECT comparator instead of
    catching a missing one.
    """
    x = str((args or {}).get("X_expr", "")).strip()
    if not x:
        return None
    for k in range(1, 21):
        h = hints.get(f"X_ext_{k}_expr")
        if h and str(h).strip() == x:
            return k
    return None


def _is_external_cohort_fit(args: dict, hints: dict) -> bool:
    return _external_cohort_of(args, hints) is not None


# ---------------------------------------------------------------------------
# The brier_full COMPARISON sub-chain.
#
# With brier_full the externals are RAW individual-level cohorts: no coefficient vector
# exists anywhere, so neither the target-only baseline nor the external-only comparator
# can be SCORED. Each has to be FIT, as a single-cohort brier_i(eta=0) -- BRIERfull
# itself cannot do it, because it requires at least two pooled cohorts and would just
# pool the external rows back in.
#
# Nothing drove that. The selection nudge actively said "there is no external-only
# comparator to run", and the real run did exactly as told: pooled fit, evaluate, stop.
# It scored 70/80 anyway, because the scorer credited the baseline row off the pooled
# fit's eta grid and the comparator row off the pooled model's test evals. Both are now
# fixed; this drives the work those rows are actually asking for.
#
# prep_auto exposes the hints that make each fit a clean call: X_target_expr /
# y_target_expr (cohort 0), X_ext_k_expr / y_ext_k_expr per external cohort, and
# beta_zero_expr (a stored zero (p+1)x1 beta, so brier_i can run with no transfer).

def _brierfull_comparators(hints: dict) -> List[dict]:
    """The single-cohort fits a brier_full comparison needs, in order."""
    if not hints.get("beta_zero_expr"):
        return []
    out: List[dict] = []
    if hints.get("X_target_expr"):
        out.append({
            "label": "the TARGET-ONLY (eta=0) baseline",
            "X": hints["X_target_expr"], "y": hints.get("y_target_expr", ""),
            # The target's own val is the right selection set for the target baseline.
            "X_val": hints.get("X_val_expr", ""), "y_val": hints.get("y_val_expr", ""),
        })
    for k in range(1, 21):
        xk = hints.get(f"X_ext_{k}_expr")
        if not xk:
            continue
        out.append({
            "label": f"the EXTERNAL-ONLY comparator for cohort {k}",
            "X": xk, "y": hints.get(f"y_ext_{k}_expr", ""),
            # An external-only comparator must be selected on ITS OWN held-out data, if
            # it shipped any. NEVER on the target's val: that would leak target data
            # into a comparator whose whole point is to be purely external.
            "X_val": hints.get(f"X_ext_{k}_val_expr", ""),
            "y_val": hints.get(f"y_ext_{k}_val_expr", ""),
        })
    return out


def _next_brierfull_comparator(last_prep, tool_results) -> Optional[dict]:
    """The first comparator fit that has not been done yet, or None if all are."""
    todo = _brierfull_comparators(_prep_hints(last_prep))
    if not todo:
        return None
    done = {
        str((tr.get("args") or {}).get("X_expr"))
        for tr in tool_results
        if tr.get("tool") in _FIT_TOOLS
        and isinstance(tr.get("result"), dict)
        and tr["result"].get("status") == "ok"
    }
    for c in todo:
        if c["X"] not in done:
            return c
    return None


def _format_brierfull_comparator(c: dict, last_prep) -> str:
    _, prep_result = last_prep
    hints = _prep_hints(last_prep)
    dp = prep_result.get("prepared_path", "")
    sp_test_x = hints.get("X_test_expr", "")
    sp_test_y = hints.get("y_test_expr", "")
    fam = _prep_family(last_prep)
    if c["X_val"] and c["y_val"]:
        how = (f'criteria="{_sel_criteria(fam)}", X_val_expr="{c["X_val"]}", '
               f'y_val_expr="{c["y_val"]}"')
        why = "select it on its OWN validation split"
    else:
        how = 'criteria="BIC"'
        why = ("select it with BIC (it has no validation split of its own; do NOT "
               "select it on the target's validation set, which would leak target "
               "data into a comparator that must be purely external)")
    lines = [
        f"NOT DONE YET: the comparison needs {c['label']}. This shape's externals are "
        "RAW cohorts, so there is no coefficient vector to score: the comparator must "
        "be FIT. BRIERfull cannot do it (it requires at least two pooled cohorts and "
        "would pool the external rows back in), so use brier_i on that cohort alone.",
        "Issue this brier_i call NOW:",
    ]
    if dp:
        lines.append(f'  data_path="{dp}"')
    lines.append(f'  X_expr="{c["X"]}", y_expr="{c["y"]}", '
                 f'beta_external_expr="{hints.get("beta_zero_expr", "")}", eta_list=[0]')
    lines.append(f"Then {why}: call brier_i_selection with {how}.")
    if sp_test_x:
        lines.append(
            f'Then brier_evaluate it on the TARGET test set (newx_expr="{sp_test_x}", '
            f'newy_expr="{sp_test_y}", criteria="{_report_criteria(fam)}") so it can be '
            "compared like for like."
        )
    return "\n".join(lines)


def _format_evaluate_followup(args: dict, last_prep) -> str:
    """After a test evaluation, nudge the external-only comparator if not done."""
    dp = (args or {}).get("data_path", "")
    dp_arg = f', data_path="{dp}"' if dp else ""
    sp = _split_exprs(args, last_prep)
    fam = _prep_family(last_prep)
    fam_arg = f', family="{fam}"' if fam != "gaussian" else ""
    return (
        "REMINDER: you have the transfer model's test metric but NOT yet the "
        "external-only comparator. Do NOT stop. Your NEXT action MUST be a tool "
        "call to score_external_prs to score the external model as-is on the "
        "same test set:\n"
        f'  newx_expr="{sp["X_test"]}", newy_expr="{sp["y_test"]}", '
        f'beta_expr="{sp["beta"]}", criteria="{_report_criteria(fam)}"{fam_arg}{dp_arg}\n'
        "Then compare the two metrics and, if the transfer model does not beat "
        "the external-only score, recommend running brier_s. Issue that call now."
    )


def _format_inspect_block(data_path: str, inspect_result: dict) -> str:
    """Turn an inspect_data result into a verbatim structure block.

    The block tells the model exactly what objects and fields the file
    contains and instructs it to reference them by their exact names. The
    aim is that the model COPIES these names into *_expr arguments rather
    than inventing placeholder values.

    inspect_data return shapes vary, so this reads the common keys
    defensively (objects / fields / columns / dims) and includes whatever
    structure is present. Returns an empty string if nothing useful is
    found (the caller then falls back to no injection).
    """
    lines = []

    # Object names (top-level R objects in the file).
    objects = inspect_result.get("objects")
    if isinstance(objects, dict):
        # e.g. {"X": {"class": "matrix", "dim": [1000, 5000]}, "y": {...}}
        for name, meta in objects.items():
            desc = ""
            if isinstance(meta, dict):
                cls = meta.get("class")
                dim = meta.get("dim") or meta.get("dimension")
                length = meta.get("length")
                bits = []
                if cls:
                    bits.append(str(cls))
                if dim:
                    bits.append(f"dim {dim}")
                elif length is not None:
                    bits.append(f"length {length}")
                desc = " (" + ", ".join(bits) + ")" if bits else ""
            lines.append(f"  {name}{desc}")
    elif isinstance(objects, list):
        for name in objects:
            lines.append(f"  {name}")

    # Column names (for tabular / sumstats files).
    columns = inspect_result.get("columns")
    if isinstance(columns, list) and columns:
        lines.append("  columns: " + ", ".join(str(c) for c in columns))

    # Any explicit fields mapping.
    fields = inspect_result.get("fields")
    if isinstance(fields, list) and fields:
        lines.append("  fields: " + ", ".join(str(f) for f in fields))

    if not lines:
        return ""

    header = (
        "DATA STRUCTURE (from inspecting the file; use these object and field "
        "names EXACTLY as written in any *_expr argument, do not invent or "
        "rename them):\n"
        f"file: {data_path}\n"
    )
    return header + "\n".join(lines)
