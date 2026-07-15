"""Preprocessing-only mode (the Task-3 cases): prepare, then STOP before the fit.

A T3 case asks the agent to INFER the module and PREPARE the inputs, and forbids
fitting. The normal chain does the opposite: the prep -> fit continuation hook
fires the moment prep_auto succeeds and pushes the model into exactly the step the
case forbids. These tests pin both halves of the fix:

  1. the fitters are not OFFERED (dropped from the tool allowlist), so the step is
     unreachable rather than merely discouraged, and
  2. the post-prep nudge tells the model to STOP (or prepare a second module),
     never to fit.

Driven by the stub LLM against the fake prep server: no network, no BRIER, no data.
Run:  python3 brier_agent/tests/test_preprocessing_only.py
"""
import asyncio
import json
from pathlib import Path

from brier_agent.config import AgentConfig
from brier_agent.llm_client import LLMClient
from brier_agent.loop import BrierAgent
from brier_agent.mcp_client import MCPClient
from brier_agent.stub_llm import StubLLM

_SERVER = str(Path(__file__).parent / "fake_prep_server.py")

_PREP_ONLY_TOOLS = {"inspect_user_data", "inspect_data", "prep_auto", "prep_data"}
_FULL_TOOLS = _PREP_ONLY_TOOLS | {"brier_i"}


def _offered_tools(stub: StubLLM) -> set:
    """The tool names the loop actually offered the model on its last call."""
    tools = stub.calls[-1].get("tools") or []
    return {t["function"]["name"] for t in tools}


def _injected_text(stub: StubLLM) -> str:
    """Every message the loop injected, flattened (this is where nudges land)."""
    msgs = stub.calls[-1].get("messages") or []
    return "\n".join(
        m.get("content") or "" for m in msgs if isinstance(m, dict)
    )


async def _run(preprocessing_only: bool, include_tools: set, script: list):
    cfg = AgentConfig(mcp_server_path=_SERVER, max_turns=6)
    stub = StubLLM(script=script)
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    mcp = MCPClient(server_path=_SERVER)
    agent = BrierAgent(
        config=cfg, llm=llm, mcp=mcp,
        include_tools=include_tools,
        preprocessing_only=preprocessing_only,
    )
    result = await agent.run("Prepare these inputs.")
    return result, stub


async def test_prep_only_does_not_offer_the_fitters():
    """The fit step must be UNREACHABLE, not just discouraged."""
    _, stub = await _run(
        preprocessing_only=True,
        include_tools=_PREP_ONLY_TOOLS,
        script=[
            [{"name": "prep_auto",
              "arguments": {"shape": "brier_i", "roles": {"snp_info": "s.gz"}}}],
            "Prepared for brier_i.",
        ],
    )
    offered = _offered_tools(stub)
    assert "prep_auto" in offered, f"prep_auto must stay available: {offered}"
    assert "brier_i" not in offered, f"a fitter was offered in prep-only mode: {offered}"
    assert not (offered & {"brier_s", "brier_full", "brier_evaluate"}), offered
    print("prep-only: fitters are not offered to the model: OK")


async def test_prep_only_nudge_says_stop_not_fit():
    """After a successful prep, the nudge must NOT push a fit."""
    _, stub = await _run(
        preprocessing_only=True,
        include_tools=_PREP_ONLY_TOOLS,
        script=[
            [{"name": "prep_auto",
              "arguments": {"shape": "brier_s", "roles": {"snp_info": "s.gz"}}}],
            "Prepared for brier_s.",
        ],
    )
    text = _injected_text(stub).lower()
    assert "do not fit a model" in text, \
        "the post-prep nudge must forbid fitting"
    # The normal fit nudge's signature line. (Do not assert on expr_hint NAMES:
    # they legitimately appear in the prep_auto TOOL RESULT, not only in a nudge.)
    assert "only assembled" not in text, \
        "the prep -> fit nudge leaked into preprocessing-only mode"
    assert "next action must be a tool call to brier_" not in text, \
        "a nudge is pushing the model toward a fitter in preprocessing-only mode"
    print("prep-only: post-prep nudge says stop/report, never fit: OK")


async def test_prep_only_allows_a_second_prep_for_two_consumers():
    """T3_intercept-row ships one cohort in TWO representations, so the agent must
    call prep_auto twice (brier_i and brier_s). The nudge has to permit that."""
    result, stub = await _run(
        preprocessing_only=True,
        include_tools=_PREP_ONLY_TOOLS,
        script=[
            [{"name": "prep_auto", "arguments": {"shape": "brier_i", "roles": {}}}],
            [{"name": "prep_auto", "arguments": {"shape": "brier_s", "roles": {}}}],
            "Prepared both.",
        ],
    )
    shapes = [tr["args"].get("shape") for tr in result.tool_results
              if tr["tool"] == "prep_auto"]
    assert shapes == ["brier_i", "brier_s"], shapes
    assert result.error is None, result.error
    text = _injected_text(stub).lower()
    assert "more than one module" in text or "again" in text, \
        "the nudge must invite a second prep when the task names two consumers"
    print("prep-only: two prep_auto calls (two consumers) run clean: OK")


async def test_normal_mode_still_drives_the_fit():
    """The guard must be scoped: with preprocessing_only OFF, prep still -> fit."""
    _, stub = await _run(
        preprocessing_only=False,
        include_tools=_FULL_TOOLS,
        script=[
            [{"name": "prep_auto",
              "arguments": {"shape": "brier_i", "roles": {"snp_info": "s.gz"}}}],
            "done",
        ],
    )
    offered = _offered_tools(stub)
    assert "brier_i" in offered, f"normal mode must still offer the fitter: {offered}"
    text = _injected_text(stub).lower()
    assert "only assembled" in text, \
        "normal mode lost its prep -> fit nudge (regression)"
    assert "next action must be a tool call to brier_i" in text, text[-500:]
    print("normal mode: prep -> fit nudge still fires (no regression): OK")


async def test_failed_prep_still_retried_in_prep_only():
    """The failed-prep retry nudge is what recovers a mis-routed shape, so it must
    survive in preprocessing-only mode."""
    from brier_agent.loop import _format_prep_retry
    msg = ("target_y_train not found, but this data dir has a GWAS summary file: "
           "the target is SUMMARY-level. Re-route to shape='brier_s'")
    out = _format_prep_retry("prep_auto", {"shape": "brier_i"},
                             {"status": "error", "message": msg})
    assert "brier_s" in out and "brier_i" in out
    assert "do not stop" in out.lower()
    print("prep-only: the failed-prep retry nudge is intact: OK")


async def main():
    await test_prep_only_does_not_offer_the_fitters()
    await test_prep_only_nudge_says_stop_not_fit()
    await test_prep_only_allows_a_second_prep_for_two_consumers()
    await test_normal_mode_still_drives_the_fit()
    await test_failed_prep_still_retried_in_prep_only()
    print("\nALL PREPROCESSING-ONLY TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
