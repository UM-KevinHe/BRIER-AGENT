"""Test the MCP<->OpenAI schema translation and arg parsing."""
from brier_agent.tools import (
    mcp_tool_to_openai, mcp_tools_to_openai,
    parse_tool_call_arguments, tool_names,
)

def test_basic_translation():
    mcp = {
        "name": "inspect_data",
        "description": "Describe the structure of a local R data file.",
        "inputSchema": {
            "properties": {"data_path": {"title": "Data Path", "type": "string"}},
            "required": ["data_path"],
            "title": "inspect_dataArguments",
            "type": "object",
        },
    }
    out = mcp_tool_to_openai(mcp)
    assert out["type"] == "function"
    assert out["function"]["name"] == "inspect_data"
    assert out["function"]["description"].startswith("Describe")
    p = out["function"]["parameters"]
    assert p["type"] == "object"
    assert "data_path" in p["properties"]
    assert p["required"] == ["data_path"]
    assert p["additionalProperties"] is False
    print("basic translation: OK")

def test_no_arg_tool():
    mcp = {"name": "get_output_directory", "description": "Return the dir.",
           "inputSchema": {"type": "object"}}
    out = mcp_tool_to_openai(mcp)
    p = out["function"]["parameters"]
    assert p["type"] == "object"
    assert p["properties"] == {}   # filled in
    assert p["additionalProperties"] is False
    print("no-arg tool: OK")

def test_missing_inputschema():
    mcp = {"name": "weird", "description": ""}
    out = mcp_tool_to_openai(mcp)
    assert out["function"]["parameters"]["type"] == "object"
    print("missing inputSchema handled: OK")

def test_list_translation_and_names():
    mcp_list = [
        {"name": "a", "description": "", "inputSchema": {"type": "object"}},
        {"name": "b", "description": "", "inputSchema": {"type": "object"}},
    ]
    out = mcp_tools_to_openai(mcp_list)
    assert len(out) == 2
    assert tool_names(out) == ["a", "b"]
    print("list translation + names: OK")

def test_arg_parsing():
    # normal JSON string
    assert parse_tool_call_arguments('{"x": 1}') == {"x": 1}
    # empty string
    assert parse_tool_call_arguments("") == {}
    # None
    assert parse_tool_call_arguments(None) == {}
    # already a dict
    assert parse_tool_call_arguments({"y": 2}) == {"y": 2}
    # malformed JSON -> parse error sentinel
    bad = parse_tool_call_arguments('{"x": ')
    assert "_parse_error" in bad
    # JSON but not an object (a bare list) -> flagged
    notobj = parse_tool_call_arguments('[1,2,3]')
    assert "_parse_error" in notobj
    print("arg parsing (incl. malformed): OK")

if __name__ == "__main__":
    test_basic_translation()
    test_no_arg_tool()
    test_missing_inputschema()
    test_list_translation_and_names()
    test_arg_parsing()
    print("\nALL TOOLS TESTS PASSED")
