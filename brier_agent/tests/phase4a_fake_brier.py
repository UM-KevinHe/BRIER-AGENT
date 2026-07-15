"""Phase 4a smoke test: can a hosted Qwen 2.5-7B actually drive the agent?

This is the project's central-risk test. It runs in three stages, each
printing clearly, so you can see EXACTLY where a 7B model succeeds or fails.

Usage:
    export BRIER_MODEL_ENDPOINT=https://api.together.xyz/v1   # or deepinfra, etc.
    export BRIER_MODEL_NAME="Qwen/Qwen2.5-7B-Instruct-Turbo"  # provider's id
    export BRIER_API_KEY=sk-...                                # your key
    PYTHONPATH=. mcp/.venv/bin/python phase4a_smoketest.py

It uses EXAMPLE/dummy data only (no real data), so there is no privacy
concern with the external endpoint.
"""
import asyncio
import json
import os
import sys

# ---------------------------------------------------------------------------
# Stage 0: config check
# ---------------------------------------------------------------------------
ENDPOINT = os.environ.get("BRIER_MODEL_ENDPOINT", "")
MODEL = os.environ.get("BRIER_MODEL_NAME", "")
KEY = os.environ.get("BRIER_API_KEY", "")

print("=" * 70)
print("PHASE 4a SMOKE TEST: can hosted Qwen-7B drive the BRIER agent?")
print("=" * 70)
print(f"endpoint: {ENDPOINT or '(unset!)'}")
print(f"model:    {MODEL or '(unset!)'}")
print(f"key set:  {'yes' if KEY else 'NO (unset!)'}")
print()
if not (ENDPOINT and MODEL and KEY):
    print("ERROR: set BRIER_MODEL_ENDPOINT, BRIER_MODEL_NAME, BRIER_API_KEY first.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Stage 1: does this endpoint do NATIVE TOOL-CALLING at all?
# A direct OpenAI-SDK call with one trivial tool. If the model returns a
# tool_call, native tool-calling works. If it only returns text, your agent's
# native-tool-calling path will not work on this endpoint/model and we learn
# that NOW (before blaming the agent).
# ---------------------------------------------------------------------------
def stage1_native_toolcall():
    from openai import OpenAI
    client = OpenAI(base_url=ENDPOINT, api_key=KEY)
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }]
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "What's the weather in Paris? Use the tool."}],
        tools=tools,
        tool_choice="auto",
        temperature=0.0,
        max_tokens=256,
    )
    msg = resp.choices[0].message
    tcs = getattr(msg, "tool_calls", None) or []
    if tcs:
        print("STAGE 1 PASS: endpoint returns native tool_calls.")
        print(f"  -> called: {tcs[0].function.name}({tcs[0].function.arguments})")
        return True
    print("STAGE 1 FAIL: no tool_calls returned. The model replied with text:")
    print(f"  -> {(msg.content or '')[:200]}")
    print("  This endpoint/model may not support native tool-calling.")
    print("  (We would then need the prompted/ReAct fallback path.)")
    return False


# ---------------------------------------------------------------------------
# Stage 2: run the REAL agent against a fake MCP server (so we test the
# model's ROUTING without needing real R/data). The fake server has the same
# tool contract shape. We watch whether Qwen routes a summary-stats request
# to brier_s and an individual-level request to brier_i.
# ---------------------------------------------------------------------------
async def stage2_agent_routing():
    from pathlib import Path
    from brier_agent.config import AgentConfig
    from brier_agent.loop import BrierAgent
    from brier_agent.prompts import SYSTEM_PROMPT

    fake_server = Path(__file__).parent / "phase4a_fake_brier.py"
    cfg = AgentConfig.from_env()
    cfg.mcp_server_path = str(fake_server)
    agent = BrierAgent(config=cfg, system_prompt=SYSTEM_PROMPT)

    tests = [
        ("summary-stats -> expect brier_s",
         "I have GWAS summary statistics in /data/ss.txt and an external beta "
         "vector. Fit a BRIER PRS.", "brier_s"),
        ("individual-level -> expect brier_i",
         "I have individual genotypes X and phenotype y in /data/ind.rds, plus "
         "pretrained external coefficients from a EUR study. Integrate them.",
         "brier_i"),
    ]
    results = []
    for label, query, expected in tests:
        print(f"\n--- {label} ---")
        print(f"query: {query[:80]}...")
        try:
            res = await agent.run(query)
        except Exception as e:
            print(f"  AGENT CRASHED: {type(e).__name__}: {e}")
            results.append((label, "crash", None))
            continue
        called = [tr["tool"] for tr in res.tool_results]
        print(f"  tools Qwen called: {called or '(none)'}")
        print(f"  final text: {(res.text or '')[:150]}")
        if res.error:
            print(f"  error: {res.error}")
        hit = expected in called
        print(f"  routed to {expected}? {'YES' if hit else 'NO'}")
        results.append((label, "ok", hit))
    return results


def main():
    print("-" * 70)
    print("STAGE 1: native tool-calling probe")
    print("-" * 70)
    try:
        s1 = stage1_native_toolcall()
    except Exception as e:
        print(f"STAGE 1 ERROR: {type(e).__name__}: {e}")
        print("(check endpoint URL, model id, and key)")
        return

    print()
    print("-" * 70)
    print("STAGE 2: real agent routing on example data (fake MCP server)")
    print("-" * 70)
    if not s1:
        print("Skipping stage 2: native tool-calling failed in stage 1.")
        print("First decision: try a provider/model that supports tool-calling,")
        print("or plan the prompted/ReAct fallback.")
        return
    results = asyncio.run(stage2_agent_routing())

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Stage 1 (native tool-calling): {'PASS' if s1 else 'FAIL'}")
    for label, status, hit in results:
        verdict = "crash" if status == "crash" else ("PASS" if hit else "miss")
        print(f"Stage 2 [{verdict}]: {label}")
    print()
    print("Paste this whole output back to analyze the failure modes.")


if __name__ == "__main__":
    main()
