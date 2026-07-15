#!/usr/bin/env bash
# =============================================================================
# Public test suite: the unit tests for the agent and the bundled BRIER-MCP server.
#
# Pure logic, no LLM and no network. The R tests exercise the preprocessing helpers,
# the aligner, and the prepared-object contract (some skip cleanly when BRIER or a
# reference dataset is absent). The Python tests exercise the guardrails, the loop
# hooks, the environment check, and the eta-boundary diagnostic.
#
#   ./test.sh
#
# There is no pytest requirement: the Python side is a tiny discover-and-call runner
# over plain `def test_*` + assert functions.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"

PY="mcp/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
RSCRIPT="$(command -v Rscript || true)"

fails=0

run_r() {  # $1 = title, $2 = test file, $3 = extra grep filter (optional)
  echo
  echo "=============================================================="
  echo "R: $1"
  echo "=============================================================="
  if [ -z "$RSCRIPT" ]; then
    echo "  SKIP: Rscript not found"
    return
  fi
  local filter="^Warning|bit64|integer64|^Loading"
  [ -n "${3:-}" ] && filter="$filter|$3"
  "$RSCRIPT" "mcp/tests/$2" 2>&1 | grep -viE "$filter" || fails=$((fails + 1))
}

run_r "prep_auto helpers"                         test_prep_auto_helpers.R
run_r "PLINK counted allele (skips without plink2)" test_plink_counted_allele.R "^Extracting"
run_r "GENERIC (non-genotype) predictors"         test_generic_predictors.R "eta.list is not a list"
run_r "strand ambiguity"                          test_ambiguity.R
run_r "the aligner vs preprocessI/S (skips without data)" test_aligner_differential.R
run_r "the eta grid, per external source"         test_eta_grid.R
run_r "the prepared-object contract"              test_contract.R
run_r "multi.method, resolved from M"             test_multi_method.R

echo
echo "=============================================================="
echo "Python: the eta-boundary diagnostic (ANY component, per axis)"
echo "=============================================================="
# server.py cannot be imported (its module name collides with the MCP SDK's `mcp`
# package), so this one runs as a file rather than through the module runner below.
"$PY" mcp/tests/test_eta_boundary.py || fails=$((fails + 1))

echo
echo "=============================================================="
echo "Python: guardrails / loop helpers / env check / preprocessing-only"
echo "=============================================================="
# EXTRA_PY_MODULES lets a private wrapper (run_tests.sh) append its own modules
# without duplicating this runner. It is empty for the public suite.
PYTHONPATH="$(pwd)" EXTRA_PY_MODULES="${EXTRA_PY_MODULES:-}" "$PY" - <<'EOF'
import asyncio, importlib, inspect, os, sys, traceback

MODULES = [
    "brier_agent.tests.test_guardrails",
    "brier_agent.tests.test_loop_helpers",
    # The environment preflight (python -m brier_agent.check_env): pure-logic checks.
    "brier_agent.tests.test_check_env",
    # The endpoint-probe helper (targets a refused localhost port; no internet).
    "brier_agent.tests.test_llm_client",
    # Drives the stub LLM against a fake MCP server (no network, no BRIER): its
    # tests are async, hence the coroutine handling below.
    "brier_agent.tests.test_preprocessing_only",
    # The eta-ceiling escalation hook, through the real loop.
    "brier_agent.tests.test_eta_escalation",
]
MODULES += [m for m in os.environ.get("EXTRA_PY_MODULES", "").split() if m]

failed = passed = 0
for name in MODULES:
    try:
        mod = importlib.import_module(name)
    except Exception as e:
        print(f"  IMPORT FAIL {name}: {e}")
        failed += 1
        continue
    print(f"\n-- {name}")
    for fn in sorted(f for f in dir(mod) if f.startswith("test_")):
        try:
            out = getattr(mod, fn)()
            if inspect.iscoroutine(out):
                asyncio.run(out)
            passed += 1
        except Exception:
            failed += 1
            print(f"  FAIL {fn}")
            traceback.print_exc(limit=2)

print("\n" + "-" * 62)
if failed:
    print(f"python: {failed} FAILED, {passed} passed")
    sys.exit(1)
print(f"python: ALL {passed} TESTS PASS")
EOF
py_status=$?

echo
echo "=============================================================="
if [ "$fails" -eq 0 ] && [ "$py_status" -eq 0 ]; then
  echo "ALL SUITES PASS"
  exit 0
fi
echo "SUITE FAILURES"
exit 1
