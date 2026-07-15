"""Translate REAL MCP-server tools to OpenAI format end-to-end."""
import asyncio, json
from pathlib import Path
from brier_agent.mcp_client import MCPClient
from brier_agent.tools import mcp_tools_to_openai, tool_names

async def main():
    c = MCPClient(server_path=str(Path(__file__).parent / "fake_server.py"))
    async with c.session() as s:
        mcp_tools = await c.list_tools(s)
        openai_tools = mcp_tools_to_openai(mcp_tools)
        print(f"translated {len(openai_tools)} tools: {tool_names(openai_tools)}")
        # validate each has the required OpenAI shape
        for t in openai_tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "parameters" in t["function"]
            assert t["function"]["parameters"]["type"] == "object"
        # show one fully translated, pretty
        print("\nexample translated tool (brier_s):")
        bs = next(t for t in openai_tools if t["function"]["name"] == "brier_s")
        print(json.dumps(bs, indent=2))
    print("\nLIVE TRANSLATION TEST PASSED")

asyncio.run(main())
