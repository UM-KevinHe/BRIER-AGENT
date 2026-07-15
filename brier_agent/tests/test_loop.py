"""Test the full agent loop: stub LLM drives the real (fake) MCP server."""
import asyncio
from pathlib import Path
from brier_agent.config import AgentConfig
from brier_agent.llm_client import LLMClient
from brier_agent.mcp_client import MCPClient
from brier_agent.stub_llm import StubLLM
from brier_agent.loop import BrierAgent

async def main():
    # Script the stub: call inspect_data, then call brier_s, then answer.
    stub = StubLLM(script=[
        [{"name":"inspect_data","arguments":{"data_path":"/tmp/X.pgen"}}],
        [{"name":"brier_s","arguments":{"target_sumstats":"/tmp/ss.txt","eta_list":[0]}}],
        "I inspected your data (96674 variants, no phenotype) and fit the "
        "summary-statistics BRIER baseline at eta=0.",
    ])
    cfg = AgentConfig(
        mcp_server_path=str(Path(__file__).parent / "fake_server.py"),
        max_turns=6,
    )
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    mcp = MCPClient(server_path=cfg.mcp_server_path)
    agent = BrierAgent(config=cfg, llm=llm, mcp=mcp)

    result = await agent.run("Fit a baseline PRS on my summary stats")

    print("=== FINAL TEXT ===")
    print(result.text)
    print("\n=== TOOL CALLS MADE ===")
    for tr in result.tool_results:
        print(f"  {tr['tool']}({tr['args']}) -> status={tr['result'].get('status')}")
    print(f"\nturns: {result.turns}, error: {result.error}")

    # assertions
    assert result.error is None
    assert len(result.tool_results) == 2
    assert result.tool_results[0]["tool"] == "inspect_data"
    assert result.tool_results[0]["result"]["n_variants"] == 96674
    assert result.tool_results[1]["tool"] == "brier_s"
    assert "eta=0" in result.text
    assert result.turns == 3   # 2 tool turns + 1 final
    print("\nFULL LOOP TEST PASSED")

async def test_hard_guard_in_loop():
    """A hallucinated tool name should be rejected, not dispatched."""
    stub = StubLLM(script=[
        [{"name":"nonexistent_tool","arguments":{}}],
        "ok done",
    ])
    cfg = AgentConfig(mcp_server_path=str(Path(__file__).parent / "fake_server.py"), max_turns=4)
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    mcp = MCPClient(server_path=cfg.mcp_server_path)
    agent = BrierAgent(config=cfg, llm=llm, mcp=mcp)
    result = await agent.run("do something")
    # the bad tool should have produced a ToolNotAvailable error result
    assert result.tool_results[0]["result"]["class"] == "ToolNotAvailable"
    print("hard-guard rejects hallucinated tool in-loop: OK")

async def test_max_turns():
    """A model that always calls a tool should hit the cap cleanly."""
    # always asks for inspect_data, never gives final text
    stub = StubLLM(script=[[{"name":"inspect_data","arguments":{"data_path":"/x"}}]] * 10)
    cfg = AgentConfig(mcp_server_path=str(Path(__file__).parent / "fake_server.py"), max_turns=3)
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    mcp = MCPClient(server_path=cfg.mcp_server_path)
    agent = BrierAgent(config=cfg, llm=llm, mcp=mcp)
    result = await agent.run("loop forever")
    assert result.error is not None and "maximum" in result.error.lower()
    assert result.turns == 3
    print("max-turns cap fires cleanly: OK")

if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_hard_guard_in_loop())
    asyncio.run(test_max_turns())
    print("\nALL LOOP TESTS PASSED")
