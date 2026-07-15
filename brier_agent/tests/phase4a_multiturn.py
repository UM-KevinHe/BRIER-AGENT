"""Phase 4a multi-turn test: does REAL Qwen carry a full inspect -> fit ->
report sequence through the agent loop? Uses the real agent + real hosted
Qwen + a fake MCP server (no R). Hard timeout so it cannot hang/eat memory.
"""
import asyncio, os, sys
from pathlib import Path
from brier_agent.config import AgentConfig
from brier_agent.loop import BrierAgent
from brier_agent.mcp_client import MCPClient
from brier_agent.prompts import SYSTEM_PROMPT

TIMEOUT_SECONDS = 90  # hard cap: if it runs longer, abort (no runaway)

async def _run():
    server = str(Path(__file__).parent / "phase4a_multiturn_server.py")
    repo_root = Path(__file__).resolve().parents[2]
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(repo_root) + os.pathsep + child_env.get("PYTHONPATH","")

    cfg = AgentConfig.from_env()
    cfg.mcp_server_path = server
    cfg.max_turns = 8
    mcp = MCPClient(server_path=server, env=child_env)
    agent = BrierAgent(config=cfg, mcp=mcp, system_prompt=SYSTEM_PROMPT)

    # NOTE: no data_path passed -> we are testing whether Qwen ITSELF drives
    # inspect-then-fit across turns (the harder multi-turn case). To test WITH
    # scaffolding instead, pass data_path="/data/study.rds".
    query = ("I have individual-level genotypes, a phenotype, and pretrained "
             "external coefficients in /data/study.rds. Inspect it, then fit an "
             "integrated BRIER model, and tell me the result.")
    print("Running multi-turn agent (real Qwen)...\n")
    res = await agent.run(query, data_path="/data/study.rds")

    print("="*60)
    print("TURNS TAKEN:", res.turns)
    print("TOOLS CALLED (in order):")
    for i, tr in enumerate(res.tool_results, 1):
        status = tr["result"].get("status") if isinstance(tr["result"],dict) else "?"
        print(f"  {i}. {tr['tool']}  -> {status}")
        if tr["tool"].startswith("brier"):
            used = tr["result"].get("used") if isinstance(tr["result"],dict) else None
            if used:
                print(f"       args used: {used}")
    print("\nFINAL ANSWER:")
    print(" ", (res.text or "(none)")[:400])
    if res.error:
        print("\nERROR:", res.error)

    # verdict
    tools = [tr["tool"] for tr in res.tool_results]
    print("\n" + "="*60)
    print("VERDICT")
    inspected = "inspect_data" in tools
    fit = any(t.startswith("brier_") for t in tools)
    order_ok = (inspected and fit and
                tools.index("inspect_data") < min(
                    (i for i,t in enumerate(tools) if t.startswith("brier_")),
                    default=99))
    print(f"  inspected the data:        {inspected}")
    print(f"  ran a fit:                 {fit}")
    print(f"  inspect BEFORE fit:        {order_ok}")
    print(f"  produced a final answer:   {bool(res.text)}")
    print(f"  turns (<= cap):            {res.turns} (cap {cfg.max_turns})")

async def main():
    try:
        await asyncio.wait_for(_run(), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        print(f"\nABORTED: exceeded {TIMEOUT_SECONDS}s hard timeout.")
        print("(Not a hang risk: process will exit. If this happens, the "
              "multi-turn loop is too slow or stuck; report it.)")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
