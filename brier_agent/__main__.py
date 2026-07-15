"""Command-line entry point for the BRIER agent.

Run one query against the agent from the terminal::

    python -m brier_agent "Fit a baseline PRS on my summary stats" \\
        --data /path/to/sumstats.rds

Configuration comes from the environment (see ``config.py``): point
``BRIER_MODEL_ENDPOINT`` / ``BRIER_MODEL_NAME`` / ``BRIER_API_KEY`` at a
stub, a cheap cloud model, or a local vLLM Qwen. The MCP server path
defaults to the bundled ``mcp/server.py``.

This is a thin wrapper: it builds the config, runs the async loop once,
and prints the result (final text, the tools that were called, and any
error). The UI (Phase 3) wraps the same :class:`BrierAgent.run`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional

from .config import AgentConfig
from .loop import BrierAgent
from .prompts import deployment_prompt


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brier_agent",
        description="Run one query against the BRIER agent.",
    )
    p.add_argument("query", help="The natural-language request.")
    p.add_argument(
        "--data",
        dest="data_path",
        default=None,
        help="Path to a data file the query refers to (optional).",
    )
    p.add_argument(
        "--endpoint",
        default=None,
        help="Override the model endpoint (else BRIER_MODEL_ENDPOINT / default).",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the model name (else BRIER_MODEL_NAME / default).",
    )
    p.add_argument(
        "--show-tools",
        action="store_true",
        help="Print the tool calls that were made.",
    )
    return p


async def _run(args: argparse.Namespace) -> int:
    config = AgentConfig.from_env()
    if args.endpoint:
        config.model_endpoint = args.endpoint
    if args.model:
        config.model_name = args.model

    agent = BrierAgent(config=config, system_prompt=deployment_prompt())

    try:
        result = await agent.run(args.query, data_path=args.data_path)
    except Exception as e:  # surface a clean message, not a raw traceback
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.show_tools and result.tool_results:
        print("Tools called:", file=sys.stderr)
        for tr in result.tool_results:
            status = (
                tr["result"].get("status")
                if isinstance(tr["result"], dict)
                else "?"
            )
            print(f"  - {tr['tool']} -> {status}", file=sys.stderr)
        print("", file=sys.stderr)

    print(result.text or "(no answer)")

    if result.error:
        print(f"\n[note: {result.error}]", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
