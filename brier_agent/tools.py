"""Translate between MCP tool schemas and the OpenAI tool-call format.

This is the seam between the two halves of the agent. The MCP client
(``mcp_client.py``) lists tools in MCP's shape::

    {"name": ..., "description": ..., "inputSchema": {JSON Schema}}

The LLM client (``llm_client.py``) needs them in OpenAI's function-tool
shape::

    {"type": "function",
     "function": {"name": ..., "description": ..., "parameters": {JSON Schema}}}

And when the model emits a tool call, it gives a name + a JSON-string of
arguments, which the loop hands back to the MCP client as (name, dict).
This module does only that translation; it holds no transport or model
logic, which keeps the format-mapping (the most likely place for a subtle
bug) isolated and unit-testable.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


def mcp_tool_to_openai(mcp_tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one MCP tool dict to one OpenAI function-tool schema.

    The MCP ``inputSchema`` is already JSON Schema, which is what OpenAI's
    ``parameters`` field wants, so the body is a re-wrap. We make two
    small normalizations for robustness with strict/small models:

      * ensure ``parameters`` is a well-formed object schema even when a
        tool takes no arguments (some servers omit ``properties``);
      * set ``additionalProperties: false`` so the model is steered to
        emit only declared arguments (a hallucinated extra key is then a
        schema violation the model is less likely to produce).

    The MCP ``title`` keys (e.g. ``"title": "Data Path"``) are harmless
    metadata; we leave them in place rather than risk stripping something
    a downstream consumer expects.
    """
    name = mcp_tool["name"]
    description = mcp_tool.get("description", "") or ""
    schema = mcp_tool.get("inputSchema") or {}

    parameters: Dict[str, Any] = dict(schema) if isinstance(schema, dict) else {}
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})
    # Steer the model to declared arguments only. Do not override an
    # explicit setting if the server already provided one.
    parameters.setdefault("additionalProperties", False)

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def mcp_tools_to_openai(
    mcp_tools: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Convert a list of MCP tool dicts to OpenAI function-tool schemas."""
    return [mcp_tool_to_openai(t) for t in mcp_tools]


def parse_tool_call_arguments(arguments: Any) -> Dict[str, Any]:
    """Parse the ``arguments`` field of a model tool call into a dict.

    The OpenAI SDK provides ``tool_call.function.arguments`` as a JSON
    *string*. A small or imperfect model can emit invalid JSON, an empty
    string, or (rarely) a dict already. We always return a dict so the
    loop can dispatch; on unparseable input we return a sentinel the loop
    can detect and turn into a retry-prompt rather than crashing.

    Returns a normal dict of arguments on success. On failure returns
    ``{"_parse_error": <original string>}`` so the caller can decide how
    to recover (e.g. tell the model its JSON was malformed).
    """
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        s = arguments.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return {"_parse_error": arguments}
        if isinstance(parsed, dict):
            return parsed
        # A JSON value that is not an object (e.g. a bare string or list);
        # tools expect named arguments, so flag it for the loop.
        return {"_parse_error": arguments}
    # Any other type is unexpected; flag it.
    return {"_parse_error": str(arguments)}


def tool_names(openai_tools: List[Dict[str, Any]]) -> List[str]:
    """Return the function names from a list of OpenAI tool schemas.

    Convenience for the loop's hard-guard (rejecting a model tool call
    whose name is not in the exposed set).
    """
    names: List[str] = []
    for t in openai_tools:
        fn = t.get("function") or {}
        name = fn.get("name")
        if name:
            names.append(name)
    return names
