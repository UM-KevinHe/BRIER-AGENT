"""Test mcp_client against a real (fake-BRIER) MCP server: launch, list, call."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from brier_agent.mcp_client import MCPClient

async def main():
    client = MCPClient(server_path=str(Path(__file__).parent / "fake_server.py"))
    async with client.session() as session:
        # 1. list tools
        tools = await client.list_tools(session)
        names = sorted(t["name"] for t in tools)
        print("tools listed:", names)
        assert "inspect_data" in names, "inspect_data missing"
        assert "brier_s" in names, "brier_s missing"
        # check schema shape
        insp = next(t for t in tools if t["name"] == "inspect_data")
        assert "inputSchema" in insp
        assert insp["description"], "description should be non-empty"
        print("inspect_data schema:", insp["inputSchema"].get("properties", {}).keys())

        # 2. call a tool, get a parsed dict back
        r = await client.call_tool(session, "inspect_data",
                                   {"data_path": "/tmp/X_seed1.pgen"})
        print("inspect_data result:", r)
        assert r["status"] == "ok"
        assert r["n_variants"] == 96674
        assert r["has_phenotype"] is False

        # 3. call the other tool with a list arg
        r2 = await client.call_tool(session, "brier_s",
                                    {"target_sumstats": "/x.txt", "eta_list": [0]})
        print("brier_s result:", r2)
        assert r2["status"] == "ok"
        assert r2["eta_list"] == [0]

        # 4. call a nonexistent tool -> structured error, no crash
        r3 = await client.call_tool(session, "does_not_exist", {})
        print("bad tool result:", r3)
        assert r3["status"] == "error"

    print("\nALL MCP CLIENT TESTS PASSED")

if __name__ == "__main__":
    asyncio.run(main())
