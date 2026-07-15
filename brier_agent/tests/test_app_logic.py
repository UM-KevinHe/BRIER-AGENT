"""Test app.py's handler logic without constructing Gradio components."""
import asyncio, sys, types
from pathlib import Path

# Stub gradio + gradio_client so importing app.py doesn't build real components.
# We only want to exercise the pure-python handlers.
fake_gc = types.ModuleType("gradio_client")
fake_gc_utils = types.ModuleType("gradio_client.utils")
fake_gc_utils.get_type = lambda s: "Any"
fake_gc_utils._json_schema_to_python_type = lambda s, d=None: "Any"
fake_gc.utils = fake_gc_utils
sys.modules["gradio_client"] = fake_gc
sys.modules["gradio_client.utils"] = fake_gc_utils

# Minimal fake gradio: Blocks as a no-op context manager, components as stubs.
fake_gr = types.ModuleType("gradio")
class _Stub:
    def __init__(self,*a,**k): pass
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def click(self,*a,**k): pass
    def submit(self,*a,**k): pass
for name in ["Blocks","Row","Column","Accordion","Chatbot","Textbox","Button",
             "File","Dataframe","Markdown","HTML"]:
    setattr(fake_gr, name, _Stub)
fake_gr.themes = types.SimpleNamespace(Soft=lambda *a,**k: None)
sys.modules["gradio"] = fake_gr

# Now import app.py
import importlib.util
spec = importlib.util.spec_from_file_location("app", "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
print("app.py logic imported with stubbed gradio: OK")

# --- test the mode banner ---
assert "LOCAL" in app._mode_banner_html() or "DEMO" in app._mode_banner_html()
print("mode banner: OK")

# --- test chat_submit with a stubbed agent ---
from brier_agent.config import AgentConfig
from brier_agent.llm_client import LLMClient
from brier_agent.mcp_client import MCPClient
from brier_agent.stub_llm import StubLLM
from brier_agent.loop import BrierAgent

# monkeypatch _make_agent to return a stub-driven agent against the fake server
def fake_make_agent(endpoint, model, api_key):
    stub = StubLLM(script=[
        [{"name":"inspect_data","arguments":{"data_path":"/x.rds"}}],
        "I inspected your data: 96674 variants.",
    ])
    cfg = AgentConfig(mcp_server_path=str(Path(__file__).parent / "fake_server.py"))
    llm = LLMClient(endpoint="stub://", model_name="fake", client=stub)
    mcp = MCPClient(server_path=cfg.mcp_server_path)
    return BrierAgent(config=cfg, llm=llm, mcp=mcp, system_prompt="test")
app._make_agent = fake_make_agent

history, cleared, tools = app.chat_submit(
    "inspect my data", [], None, "", "", "")
print("\nchat_submit returned:")
print("  history last turn:", history[-1][1][:60], "...")
print("  cleared msg box:", repr(cleared))
print("  tools table:", tools)
assert len(history) == 1
assert "inspected" in history[-1][1].lower()
assert cleared == ""
assert tools == [["inspect_data","ok"]]
print("\nAPP LOGIC TEST PASSED")
