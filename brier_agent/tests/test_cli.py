"""Test prompts load and the CLI runs end-to-end against a stub."""
import asyncio
from pathlib import Path
from brier_agent.prompts import SYSTEM_PROMPT, prompt_sha256
from brier_agent.config import AgentConfig
from brier_agent.llm_client import LLMClient
from brier_agent.mcp_client import MCPClient
from brier_agent.stub_llm import StubLLM
from brier_agent.loop import BrierAgent

def test_prompt_loads():
    assert "brier_s" in SYSTEM_PROMPT
    assert "brier_i" in SYSTEM_PROMPT
    assert "brier_full" in SYSTEM_PROMPT
    assert "summary-statistic" in SYSTEM_PROMPT.lower()
    assert "individual-level" in SYSTEM_PROMPT.lower()
    h = prompt_sha256(SYSTEM_PROMPT)
    assert len(h) == 16
    print("prompt loads + routing tree present: OK")

async def test_agent_with_real_prompt():
    """Run the loop with the REAL system prompt + stub model + fake server."""
    stub = StubLLM(script=[
        [{"name":"inspect_data","arguments":{"data_path":"/x.rds"}}],
        "Done: inspected the data.",
    ])
    cfg = AgentConfig(mcp_server_path=str(Path(__file__).parent / "fake_server.py"))
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    mcp = MCPClient(server_path=cfg.mcp_server_path)
    agent = BrierAgent(config=cfg, llm=llm, mcp=mcp, system_prompt=SYSTEM_PROMPT)
    result = await agent.run("inspect my data")
    # the system prompt should be the first message
    assert result.transcript[0]["role"] == "system"
    assert "BRIER-Agent" in result.transcript[0]["content"]
    assert result.error is None
    assert result.tool_results[0]["tool"] == "inspect_data"
    print("agent runs with real prompt: OK")

if __name__ == "__main__":
    test_prompt_loads()
    asyncio.run(test_agent_with_real_prompt())
    print("\nALL CLI/PROMPT TESTS PASSED")
