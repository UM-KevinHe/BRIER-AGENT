"""Phase 4a QUICK probe: one direct Qwen call with the real BRIER tools,
no MCP subprocess, no multi-turn loop. Fastest way to see if Qwen routes
a summary-stats request to brier_s. Times the call so we see latency.
"""
import os, time, json
from openai import OpenAI

ENDPOINT = os.environ["BRIER_MODEL_ENDPOINT"]
MODEL = os.environ["BRIER_MODEL_NAME"]
KEY = os.environ["BRIER_API_KEY"]

# The three core BRIER routing tools, as OpenAI tool schemas.
TOOLS = [
    {"type":"function","function":{"name":"brier_s","description":
      "Fit BRIERs with a SUMMARY-STATISTICS target (GWAS summary stats: per-variant "
      "effect sizes/z-scores/p-values) plus external coefficients.",
      "parameters":{"type":"object","properties":{
        "sumstats_expr":{"type":"string"},"beta_external_expr":{"type":"string"}},
        "required":["sumstats_expr","beta_external_expr"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"brier_i","description":
      "Fit BRIERi with an INDIVIDUAL-LEVEL target (genotype matrix X + phenotype y) "
      "plus PRETRAINED external coefficients.",
      "parameters":{"type":"object","properties":{
        "X_expr":{"type":"string"},"y_expr":{"type":"string"},
        "beta_external_expr":{"type":"string"}},
        "required":["X_expr","y_expr","beta_external_expr"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"inspect_data","description":
      "Describe the structure of a local R data file before fitting.",
      "parameters":{"type":"object","properties":{"data_path":{"type":"string"}},
        "required":["data_path"],"additionalProperties":False}}},
]

SYSTEM = ("You are BRIER-Agent. Route the user's request to the right tool. "
          "Use brier_s for summary-statistics targets, brier_i for individual-level "
          "targets with pretrained coefficients. Call a tool; do not just describe.")

def probe(label, query):
    c = OpenAI(base_url=ENDPOINT, api_key=KEY)
    t0 = time.time()
    r = c.chat.completions.create(
        model=MODEL,
        messages=[{"role":"system","content":SYSTEM},
                  {"role":"user","content":query}],
        tools=TOOLS, tool_choice="auto", temperature=0.0, max_tokens=512)
    dt = time.time() - t0
    msg = r.choices[0].message
    tcs = getattr(msg, "tool_calls", None) or []
    print(f"\n[{label}]  ({dt:.1f}s)")
    if tcs:
        for tc in tcs:
            print(f"  -> {tc.function.name}({tc.function.arguments})")
    else:
        print(f"  -> NO TOOL CALL. text: {(msg.content or '')[:150]}")
    return [tc.function.name for tc in tcs]

if __name__ == "__main__":
    print("QUICK BRIER routing probe (single-turn, no MCP)")
    print(f"model: {MODEL}")
    a = probe("summary-stats -> expect brier_s",
              "I have GWAS summary statistics in /data/ss.txt and an external "
              "beta vector. Fit a BRIER PRS.")
    b = probe("individual-level -> expect brier_i",
              "I have individual genotypes X and phenotype y in /data/ind.rds, "
              "plus pretrained external coefficients from a EUR study. Integrate them.")
    print("\n--- verdict ---")
    print("summary-stats routed to brier_s:", "brier_s" in a)
    print("individual routed to brier_i:", "brier_i" in b)
