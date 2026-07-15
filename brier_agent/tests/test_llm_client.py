"""Test the endpoint-probe helper.

No external network: it targets a refused localhost port, which fails instantly, so the
suite's no-network promise holds (nothing reaches the internet).
"""
from brier_agent.llm_client import probe_endpoint


def test_probe_endpoint_unreachable_returns_message_not_exception():
    # Port 1 refuses immediately. The probe must REPORT the failure as a string (so a UI
    # handler can show it), never raise.
    msg = probe_endpoint("http://127.0.0.1:1/v1", "some-model", "EMPTY", timeout=3.0)
    assert "Could not connect" in msg
    assert "127.0.0.1:1" in msg
