"""Phase 4a routing suite: harder single-turn BRIER routing cases.

Extends the quick probe with the cases that stress a 7B model's routing:
brier_full vs brier_i, eta-tuning paths, and the vague -> start_analysis
path. Single-turn direct calls (no MCP subprocess), so it is fast and does
not touch the memory-heavy loop. Times each call.
"""
import os, time
from openai import OpenAI

ENDPOINT = os.environ["BRIER_MODEL_ENDPOINT"]
MODEL = os.environ["BRIER_MODEL_NAME"]
KEY = os.environ["BRIER_API_KEY"]

# A fuller BRIER tool surface for routing (real names + descriptions).
TOOLS = [
    {"type":"function","function":{"name":"inspect_data","description":
      "Describe the structure of a local R data file before fitting.",
      "parameters":{"type":"object","properties":{"data_path":{"type":"string"}},
        "required":["data_path"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"brier_s","description":
      "Fit BRIERs with a SUMMARY-STATISTICS target (GWAS summary stats: "
      "per-variant effect sizes/z-scores/p-values) plus external coefficients.",
      "parameters":{"type":"object","properties":{
        "sumstats_expr":{"type":"string"},"beta_external_expr":{"type":"string"}},
        "required":["sumstats_expr","beta_external_expr"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"brier_i","description":
      "Fit BRIERi with an INDIVIDUAL-LEVEL target (genotype matrix X + phenotype "
      "y) plus PRETRAINED external coefficients from another study.",
      "parameters":{"type":"object","properties":{
        "X_expr":{"type":"string"},"y_expr":{"type":"string"},
        "beta_external_expr":{"type":"string"}},
        "required":["X_expr","y_expr","beta_external_expr"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"brier_full","description":
      "Fit BRIERfull by POOLING MULTIPLE raw cohorts together (a target cohort "
      "plus one or more external cohorts of individual-level records), identified "
      "by a cohort label. Use when you have RAW individual-level data from several "
      "cohorts, not pretrained coefficients.",
      "parameters":{"type":"object","properties":{
        "X_expr":{"type":"string"},"y_expr":{"type":"string"},
        "cohort_expr":{"type":"string"}},
        "required":["X_expr","y_expr","cohort_expr"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"brier_i_cv","description":
      "Cross-validate the transfer strength eta for an individual-level BRIERi fit.",
      "parameters":{"type":"object","properties":{
        "X_expr":{"type":"string"},"y_expr":{"type":"string"},
        "beta_external_expr":{"type":"string"}},
        "required":["X_expr","y_expr","beta_external_expr"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"brier_auto_tune_eta","description":
      "Automatically widen the eta search grid until the optimum is interior "
      "(auto escalate/de-escalate). Use when the user wants the tool to tune eta "
      "automatically rather than specifying a grid.",
      "parameters":{"type":"object","properties":{"family":{"type":"string"}},
        "required":["family"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"start_analysis","description":
      "Launch the guided wizard when the request is too vague to determine the "
      "data type or whether external information is present.",
      "parameters":{"type":"object","properties":{},"additionalProperties":False}}},
]

SYSTEM = (
  "You are BRIER-Agent, for transfer-learning polygenic risk scores. Route the "
  "user's request to the right tool. brier_s = summary-statistics target; "
  "brier_i = individual-level target (X,y) + pretrained external coefficients; "
  "brier_full = pooling multiple RAW cohorts (no pretrained coefficients); "
  "brier_i_cv = cross-validate eta; brier_auto_tune_eta = auto-tune eta grid; "
  "start_analysis = vague request. Call a tool; do not just describe.")

CASES = [
  ("brier_full (pooled raw cohorts)",
   "I have raw individual-level genotypes and phenotypes from three cohorts "
   "(my target plus two external cohorts), with a cohort label column. Pool them "
   "in a transfer-learning fit.", ["brier_full"]),
  ("brier_i vs brier_full (has pretrained coefs -> brier_i)",
   "I have my own genotype matrix and phenotype, and a pretrained beta vector "
   "from a published EUR GWAS. Combine them.", ["brier_i"]),
  ("brier_i_cv (cross-validate eta)",
   "For my individual-level data with external coefficients, cross-validate the "
   "transfer strength eta to pick the best value.", ["brier_i_cv","brier_i"]),
  ("auto-tune eta",
   "Fit my summary-stats PRS but automatically widen the eta grid until the best "
   "value is not at the boundary. I don't want to pick the grid myself.",
   ["brier_auto_tune_eta","brier_s"]),
  ("vague -> start_analysis",
   "Hi, I have some genetic data and I want to build a risk score. Can you help?",
   ["start_analysis","inspect_data"]),
  ("inspect-first instinct",
   "Here is my data file at /data/mystery.rds. Analyze it with BRIER.",
   ["inspect_data","start_analysis"]),
]

def probe(label, query, acceptable):
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
    called = [tc.function.name for tc in tcs]
    ok = any(c in acceptable for c in called) if called else False
    mark = "PASS" if ok else ("no-call" if not called else "MISS")
    print(f"\n[{mark}] {label}  ({dt:.1f}s)")
    if called:
        for tc in tcs:
            print(f"    -> {tc.function.name}({tc.function.arguments[:80]})")
    else:
        print(f"    -> NO TOOL CALL: {(msg.content or '')[:120]}")
    print(f"    acceptable: {acceptable}")
    return ok

if __name__ == "__main__":
    print("PHASE 4a ROUTING SUITE (single-turn, no MCP)")
    print(f"model: {MODEL}\n" + "="*60)
    results = []
    for label, query, acceptable in CASES:
        results.append((label, probe(label, query, acceptable)))
    print("\n" + "="*60)
    print("SUMMARY")
    passed = sum(1 for _, ok in results if ok)
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'MISS'}] {label}")
    print(f"\n{passed}/{len(results)} routing cases passed")
