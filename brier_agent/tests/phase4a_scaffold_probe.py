"""Phase 4a scaffold probe: does injecting the data structure make Qwen use
REAL field names instead of placeholders? Runs the real hosted model twice
on the same request: once WITHOUT the structure block, once WITH it. Direct
single-turn calls (no MCP subprocess). Compares the *_expr arguments.
"""
import os, json
from openai import OpenAI

ENDPOINT = os.environ["BRIER_MODEL_ENDPOINT"]
MODEL = os.environ["BRIER_MODEL_NAME"]
KEY = os.environ["BRIER_API_KEY"]

TOOLS = [
    {"type":"function","function":{"name":"brier_i","description":
      "Fit BRIERi with an INDIVIDUAL-LEVEL target (genotype matrix X + phenotype "
      "y) plus PRETRAINED external coefficients.",
      "parameters":{"type":"object","properties":{
        "X_expr":{"type":"string","description":"R expression for the genotype matrix"},
        "y_expr":{"type":"string","description":"R expression for the phenotype"},
        "beta_external_expr":{"type":"string","description":"R expression for external coefficients"}},
        "required":["X_expr","y_expr","beta_external_expr"],"additionalProperties":False}}},
]
SYSTEM = ("You are BRIER-Agent. Route to the right tool and fill its arguments. "
          "*_expr arguments must be valid R expressions referencing the actual "
          "objects in the data. Call the tool.")

STRUCTURE_BLOCK = (
  "DATA STRUCTURE (from inspecting the file; use these object names EXACTLY in "
  "any *_expr argument, do not invent or rename them):\n"
  "file: /data/study.rds\n"
  "  geno (matrix, dim [1000, 50000])\n"
  "  pheno (numeric, length 1000)\n"
  "  ext_beta (numeric, length 50000)\n")

REQUEST = ("I have individual genotypes, a phenotype, and pretrained external "
           "coefficients in /data/study.rds. Fit an integrated BRIER model.")

def run(label, user_content):
    c = OpenAI(base_url=ENDPOINT, api_key=KEY)
    r = c.chat.completions.create(
        model=MODEL,
        messages=[{"role":"system","content":SYSTEM},
                  {"role":"user","content":user_content}],
        tools=TOOLS, tool_choice="auto", temperature=0.0, max_tokens=400)
    msg = r.choices[0].message
    tcs = getattr(msg,"tool_calls",None) or []
    print(f"\n[{label}]")
    if tcs:
        args = json.loads(tcs[0].function.arguments)
        print(f"  tool: {tcs[0].function.name}")
        for k,v in args.items():
            print(f"    {k} = {v!r}")
        return args
    print(f"  NO TOOL CALL: {(msg.content or '')[:100]}")
    return {}

if __name__ == "__main__":
    print("SCAFFOLD PROBE: does injecting structure fix placeholder args?")
    print(f"model: {MODEL}")
    print("="*60)
    without = run("WITHOUT structure block", REQUEST)
    with_ = run("WITH structure block", STRUCTURE_BLOCK + "\n" + REQUEST)
    print("\n" + "="*60)
    print("COMPARISON")
    real_names = {"geno","pheno","ext_beta"}
    def uses_real(args):
        vals = {str(v) for v in args.values()}
        return len(vals & real_names)
    print(f"  WITHOUT: {uses_real(without)}/3 args use real names ({set(str(v) for v in without.values())})")
    print(f"  WITH:    {uses_real(with_)}/3 args use real names ({set(str(v) for v in with_.values())})")
    if uses_real(with_) > uses_real(without):
        print("\n  -> scaffolding IMPROVED argument grounding (gap closing).")
    elif uses_real(with_) == 3:
        print("\n  -> WITH-scaffold used all real names (gap closed).")
    else:
        print("\n  -> no clear improvement; may need stronger injection wording.")
