"""The eta-ceiling escalation hook, driven through the real loop.

The selection tools have ALWAYS emitted `_notice_eta_boundary` when the chosen eta
lands on the top rung of the grid, and the harness has always dropped it: four of
nine scored runs selected eta AT the boundary and reported the truncated model's
test metrics as if they were the model's. Every other step of the chain (prep ->
fit -> select -> evaluate -> second metric -> comparator) has a continuation hook;
this one did not, so nothing pushed a refit.

A boundary is not an optimum. The best eta lies OUTSIDE the grid, so the fit is cut
off and its numbers are not the model's numbers. These tests pin the three things
that make the hook real, through the actual loop (stub LLM + fake MCP: no network,
no BRIER, no data):

  1. a pinned selection nudges a REFIT with a raised ceiling, and never nudges the
     evaluation (or the run scores the truncated model);
  2. that nudge OUTRANKS a same-turn fit nudge, which is the only place the priority
     number is actually load-bearing (fault injection found that out: the suite
     passed with the priority wrong, because escalate-vs-evaluate is an if/else);
  3. an interior optimum does NOT escalate (no spurious refits);
  4. escalation is CAPPED, so data that pins at every ceiling cannot spin the run.

Run:  python3 brier_agent/tests/test_eta_escalation.py
"""
import asyncio
from pathlib import Path

from brier_agent.config import AgentConfig
from brier_agent.llm_client import LLMClient
from brier_agent.loop import BrierAgent
from brier_agent.mcp_client import MCPClient
from brier_agent.stub_llm import StubLLM

_SERVER = str(Path(__file__).parent / "fake_prep_server.py")
_TOOLS = {"inspect_user_data", "prep_auto", "brier_i", "brier_i_selection",
          "brier_evaluate"}

_PREP = [{"name": "prep_auto",
          "arguments": {"shape": "brier_i", "roles": {"snp_info": "s.gz"}}}]


def _fit(ceiling=None):
    args = {"data_path": "/tmp/prep_auto_brier_i.rds",
            "X_expr": "prep_auto_brier_i$X",
            "y_expr": "prep_auto_brier_i$y",
            "beta_external_expr": "prep_auto_brier_i$beta_external"}
    if ceiling is not None:
        args["eta_ceiling"] = ceiling
    return [{"name": "brier_i", "arguments": args}]


def _select(fit_id):
    return [{"name": "brier_i_selection",
             "arguments": {"fit_id": fit_id, "criteria": "gaussian.mspe"}}]


def _nudges(stub) -> str:
    """Every message the loop injected, flattened. This is where nudges land."""
    msgs = stub.calls[-1].get("messages") or []
    return "\n".join(m.get("content") or "" for m in msgs if isinstance(m, dict))


async def _run(script, max_turns=10, pin_upto=None):
    """Drive the real loop. `pin_upto` sets how far the fake data's optimum lies:
    every fit whose ceiling is at or below it pins at the grid's top rung."""
    cfg = AgentConfig(mcp_server_path=_SERVER, max_turns=max_turns)
    stub = StubLLM(script=script)
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    env = {"FAKE_PIN_UPTO": str(pin_upto)} if pin_upto is not None else None
    mcp = MCPClient(server_path=_SERVER, env=env)
    agent = BrierAgent(config=cfg, llm=llm, mcp=mcp, include_tools=_TOOLS)
    return await agent.run("Build the best model and report test performance."), stub


async def test_a_pinned_selection_nudges_a_wider_refit():
    _, stub = await _run([
        _PREP,
        _fit(),                 # default ceiling 10 -> fit_10
        _select("fit_10"),      # pins at the top of the grid
        "done",
    ])
    text = _nudges(stub)
    assert "eta_ceiling=50" in text, (
        "a boundary-pinned selection must nudge a refit at a 5x ceiling:\n" + text[-800:])
    assert "brier_i" in text
    low = text.lower()
    assert "boundary" in low and "do not evaluate" in low, (
        "the nudge must say WHY: the model is truncated, so it must not be scored")
    print("escalation: a pinned selection nudges a wider refit: OK")


async def test_a_pinned_selection_never_nudges_the_evaluation():
    """If the evaluate nudge fires anyway, the run scores the TRUNCATED fit and
    reports its metrics -- which is precisely the bug."""
    _, stub = await _run([_PREP, _fit(), _select("fit_10"), "done"])
    text = _nudges(stub)
    assert "eta_ceiling=50" in text
    assert "brier_evaluate" not in text, (
        "the evaluate nudge fired on a boundary-pinned model:\n" + text[-800:])
    print("escalation: a pinned selection never nudges the evaluation: OK")


async def test_the_escalation_nudge_outranks_a_same_turn_fit_nudge():
    """Priority is load-bearing when a turn carries MORE THAN ONE tool call.

    A model that emits the fit and its selection together produces two candidate
    nudges in one turn: the fit's "now select" (priority 3) and the boundary
    escalation. Only one is injected. If the fit nudge wins, the model re-selects the
    SAME truncated fit and the boundary is lost -- so escalation has to outrank it.
    (Fault injection wrote this test: with the escalation branch dropped to priority
    3 the whole suite still passed, because the escalate-vs-evaluate choice is an
    if/else and never actually compares priorities.)
    """
    _, stub = await _run([
        _PREP,
        _fit() + _select("fit_10"),   # both tool calls in ONE turn
        "done",
    ])
    text = _nudges(stub)
    assert "eta_ceiling=50" in text, (
        "the fit nudge outranked the boundary escalation, so the truncated fit "
        "would just be re-selected:\n" + text[-800:])
    print("escalation: outranks a same-turn fit nudge: OK")


async def test_an_interior_optimum_does_not_escalate():
    """No notice -> no refit. A spurious escalation is a full refit of a 10k-predictor
    model, so this must not fire on a healthy fit."""
    _, stub = await _run([
        _PREP,
        _fit(ceiling=50),       # -> fit_50, whose selection finds an interior optimum
        _select("fit_50"),
        "done",
    ])
    text = _nudges(stub)
    assert "WIDEN the search" not in text, "escalated a fit that never pinned"
    assert "brier_evaluate" in text, "the normal evaluate nudge should follow instead"
    print("escalation: an interior optimum is left alone: OK")


async def test_the_full_widen_and_refit_trajectory_runs():
    """prep -> fit(10) -> select(pinned) -> refit(50) -> select(interior) -> evaluate."""
    result, _ = await _run([
        _PREP,
        _fit(),
        _select("fit_10"),
        _fit(ceiling=50),
        _select("fit_50"),
        [{"name": "brier_evaluate",
          "arguments": {"selection_id": "sel_fit_50", "criteria": "gaussian.rsq",
                        "newx_expr": "prep_auto_brier_i$X_test"}}],
        "Test R^2 = 0.0142.",
    ])
    assert result.error is None, result.error
    grids = [tr["result"]["eta_list_used"] for tr in result.tool_results
             if tr["tool"] == "brier_i"]
    assert len(grids) == 2, f"expected an initial fit and a widened refit: {grids}"
    assert max(grids[1]) > max(grids[0]), (
        f"the refit did not search a wider eta range: {grids}")
    print("escalation: the widen-and-refit trajectory completes: OK")


async def test_escalation_is_capped():
    """Data whose optimum lies past ANY sane ceiling (T1_brieri's external only starts
    to help far beyond the grid, and never catches the direct EUR PRS) must not spin
    the run: every rung is a full refit + selection.

    So: pin at EVERY ceiling. The agent should widen twice (10 -> 50 -> 250), then
    give up and move on to the evaluation, reporting the widest fit it has.
    """
    _, stub = await _run(
        [_PREP,
         _fit(), _select("fit_10"),
         _fit(ceiling=50), _select("fit_50"),
         _fit(ceiling=250), _select("fit_250"),
         "done"],
        max_turns=12, pin_upto=1e9,
    )
    text = _nudges(stub)
    n = text.count("WIDEN the search")
    assert n == 2, f"expected exactly 2 rungs (the cap), got {n}"
    assert "brier_evaluate" in text, (
        "after the cap the chain must move on to the evaluation, not stall")
    print("escalation: capped at 2 rungs, then the chain moves on: OK")


async def main():
    await test_a_pinned_selection_nudges_a_wider_refit()
    await test_a_pinned_selection_never_nudges_the_evaluation()
    await test_the_escalation_nudge_outranks_a_same_turn_fit_nudge()
    await test_an_interior_optimum_does_not_escalate()
    await test_the_full_widen_and_refit_trajectory_runs()
    await test_escalation_is_capped()
    print("\nALL ETA-ESCALATION TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
