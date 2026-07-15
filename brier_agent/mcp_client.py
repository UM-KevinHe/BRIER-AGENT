"""MCP client: launch the bundled BRIER server and call its tools.

This is the transport half of the agent. It spawns ``mcp/server.py`` as a
stdio subprocess, performs the MCP handshake, lists the server's tools,
and calls them by name. It depends on the official ``mcp`` Python SDK and
is async (the SDK is asyncio-based).

Design note: BRIER-Agent reuses the bundled MCP server as the single
source of truth for the tool surface (the "Option A" decision), rather
than re-implementing the tool dispatch in Python. The cost is this stdio
client plumbing; the benefit is that tools are defined once, in
``mcp/server.py``.

The server is launched the same way a client like Claude or Codex would
launch it locally: ``uv run --directory <mcp_dir> server.py`` is the usual
form, but to keep the agent self-contained we launch the server's Python
entry point directly with the interpreter running the agent, which already
has the ``mcp`` dependency. The Rscript path is forwarded via env.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Default per-tool-call transport cap (seconds). Generous on purpose: the slow
# tools here are real statistical fits, not chatty RPCs. The binding case is an
# external model fit from individual-level data -- a penalized fit on a 20k x 10k
# genotype matrix runs ~7 min, and a two-external case fits two of them plus an
# LD build, so ~15 min.
#
# This sits ABOVE the server's own Rscript cap (server._PREP_AUTO_TIMEOUT_S,
# 1800s) so an over-long R step fails on the SERVER with a clean "Rscript timed
# out" message, rather than the transport tearing down first and surfacing a
# masked TaskGroup/ExceptionGroup error. A genuinely hung tool still fails here
# rather than blocking forever.
# Nesting: Rscript (1800) < MCP transport (2100) < benchmark per-case (2700).
# Override with BRIER_MCP_CALL_TIMEOUT.
_DEFAULT_CALL_TIMEOUT_S = 2100.0


class MCPClient:
    """Async client over a spawned BRIER MCP stdio server.

    Usage::

        client = MCPClient(server_path="/path/to/mcp/server.py", env=...)
        async with client.session() as session:
            tools = await client.list_tools(session)
            result = await client.call_tool(session, "inspect_data",
                                             {"data_path": "/x.rds"})

    The ``session`` context manager owns the subprocess lifecycle: the
    server starts on enter and is shut down on exit. Keep the agent loop
    inside one ``session`` so the server stays warm across tool calls.
    """

    def __init__(
        self,
        server_path: str,
        env: Optional[Dict[str, str]] = None,
        python_executable: Optional[str] = None,
        read_timeout_seconds: Optional[float] = None,
    ) -> None:
        self.server_path = str(Path(server_path).resolve())
        self.server_dir = str(Path(self.server_path).parent)
        self.env = env
        # Per-tool-call transport cap. Some tools legitimately run for many
        # minutes (a penalized fit on a 20k x 10k genotype matrix takes ~7 min;
        # a two-external case fits two of them), so the cap must clear the
        # slowest real tool, not a nominal few minutes. Env-overridable; None
        # leaves it to the MCP SDK default.
        if read_timeout_seconds is None:
            import os

            env_v = os.environ.get("BRIER_MCP_CALL_TIMEOUT", "").strip()
            read_timeout_seconds = float(env_v) if env_v else _DEFAULT_CALL_TIMEOUT_S
        self.read_timeout_seconds = read_timeout_seconds
        # Launch the server with the same interpreter running the agent,
        # unless an explicit one is given. This interpreter already has the
        # mcp dependency (the agent imports it), and running >=3.10 (the
        # server's requirement) is guaranteed because the agent itself does.
        self.python_executable = python_executable or sys.executable

    def _server_params(self) -> StdioServerParameters:
        # Merge any caller-provided env ONTO the inherited environment rather
        # than replacing it. Passing a bare dict to the subprocess would drop
        # PATH, PYTHONPATH, and everything else the server needs to start
        # (e.g. to import its own package, or to find uv / R). We start from
        # the parent env and overlay the extras.
        import os

        merged_env = dict(os.environ)
        if self.env:
            merged_env.update(self.env)
        return StdioServerParameters(
            command=self.python_executable,
            args=[self.server_path],
            env=merged_env,
            cwd=self.server_dir,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        """Spawn the server, handshake, and yield an initialized session.

        On exit the session and the subprocess are torn down. The whole
        agent loop should run inside one ``async with client.session()``
        so the server process is reused across tool calls.
        """
        params = self._server_params()
        rt = (timedelta(seconds=self.read_timeout_seconds)
              if self.read_timeout_seconds else None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write,
                                     read_timeout_seconds=rt) as session:
                await session.initialize()
                yield session

    async def list_tools(self, session: ClientSession) -> List[Dict[str, Any]]:
        """Return the server's tools as raw MCP tool dicts.

        Each entry has ``name``, ``description``, and ``inputSchema`` (a
        JSON Schema). Translation to the OpenAI tool format happens in
        ``tools.py``, not here, to keep this module transport-only.
        """
        result = await session.list_tools()
        tools: List[Dict[str, Any]] = []
        for t in result.tools:
            tools.append(
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema or {"type": "object",
                                                     "properties": {}},
                }
            )
        return tools

    async def call_tool(
        self,
        session: ClientSession,
        name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Call one tool by name; return a normalized result dict.

        The MCP call returns content blocks (usually one text block holding
        the tool's JSON). We extract the text and parse it as JSON when
        possible, returning a structured dict either way so the loop always
        gets a dict. Errors are returned as ``{"status": "error", ...}``
        rather than raised, matching the BRIER tools' own contract.
        """
        import json

        rt = (timedelta(seconds=self.read_timeout_seconds)
              if self.read_timeout_seconds else None)
        try:
            result = await session.call_tool(name, arguments=arguments,
                                             read_timeout_seconds=rt)
        except Exception as e:  # transport / protocol error
            return {
                "status": "error",
                "class": type(e).__name__,
                "where": "mcp_client.call_tool",
                "message": f"MCP call failed for {name!r}: {e}",
            }

        # MCP returns a list of content blocks; BRIER tools emit one text
        # block containing the JSON result. Concatenate any text blocks.
        text_parts: List[str] = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text is not None:
                text_parts.append(text)
        raw = "".join(text_parts).strip()

        # The MCP protocol signals tool-level failures via isError. When set,
        # surface the text as a structured error so the loop can react (and
        # the model can retry), rather than treating it as a success.
        if getattr(result, "isError", False):
            return {
                "status": "error",
                "class": "ToolError",
                "where": f"mcp_server.{name}",
                "message": raw or f"Tool {name!r} reported an error.",
            }

        if not raw:
            # Some tools may return structured content directly.
            structured = getattr(result, "structuredContent", None)
            if isinstance(structured, dict):
                return structured
            return {
                "status": "error",
                "class": "EmptyResult",
                "where": "mcp_client.call_tool",
                "message": f"Tool {name!r} returned no content.",
            }

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            return {"status": "ok", "result": parsed}
        except json.JSONDecodeError:
            # Not JSON: wrap the text so the caller still gets a dict.
            return {"status": "ok", "text": raw}
