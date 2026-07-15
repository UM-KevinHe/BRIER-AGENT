"""Test config + llm_client + stub together, no network/GPU needed."""
from brier_agent.config import AgentConfig
from brier_agent.llm_client import LLMClient
from brier_agent.stub_llm import StubLLM
import json, os

def test_config_defaults():
    c = AgentConfig()
    assert c.model_endpoint == "https://api.openai.com/v1"
    assert c.model_name == "gpt-4o-mini"
    assert c.max_turns == 12
    assert c.temperature == 0.0
    assert c.mcp_server_path.endswith("mcp/server.py")
    print("config defaults: OK")

def test_config_from_env():
    os.environ["BRIER_MODEL_ENDPOINT"] = "http://localhost:8000/v1"
    os.environ["BRIER_MODEL_NAME"] = "qwen2.5-7b-awq"
    os.environ["BRIER_MAX_TURNS"] = "5"
    c = AgentConfig.from_env()
    assert c.model_endpoint == "http://localhost:8000/v1"
    assert c.model_name == "qwen2.5-7b-awq"
    assert c.max_turns == 5
    # cleanup
    for k in ("BRIER_MODEL_ENDPOINT","BRIER_MODEL_NAME","BRIER_MAX_TURNS"):
        del os.environ[k]
    print("config from_env: OK")

def test_llm_client_with_stub_toolcall():
    stub = StubLLM(script=[
        [{"name":"inspect_data","arguments":{"data_path":"/tmp/x.rds"}}],
        "I inspected your data.",
    ])
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    # First call: should return a tool_call
    r1 = llm.complete(messages=[{"role":"user","content":"hi"}],
                      tools=[{"type":"function","function":{"name":"inspect_data"}}])
    msg1 = r1.choices[0].message
    assert msg1.tool_calls, "expected a tool call"
    assert msg1.tool_calls[0].function.name == "inspect_data"
    args = json.loads(msg1.tool_calls[0].function.arguments)
    assert args["data_path"] == "/tmp/x.rds"
    # Second call: should return final text
    r2 = llm.complete(messages=[{"role":"user","content":"hi"}])
    msg2 = r2.choices[0].message
    assert not msg2.tool_calls
    assert msg2.content == "I inspected your data."
    # The stub recorded both calls
    assert len(stub.calls) == 2
    # The model name was passed through
    assert stub.calls[0]["model"] == "fake"
    print("llm_client + stub (toolcall then final): OK")

def test_stub_exhaustion_ends_safely():
    stub = StubLLM(script=["only one turn"])
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    r1 = llm.complete(messages=[{"role":"user","content":"a"}])
    assert r1.choices[0].message.content == "only one turn"
    # Past the script: empty final answer, no crash
    r2 = llm.complete(messages=[{"role":"user","content":"b"}])
    assert r2.choices[0].message.content == ""
    assert not r2.choices[0].message.tool_calls
    print("stub exhaustion ends safely: OK")

if __name__ == "__main__":
    test_config_defaults()
    test_config_from_env()
    test_llm_client_with_stub_toolcall()
    test_stub_exhaustion_ends_safely()
    print("\nALL FOUNDATION TESTS PASSED")
