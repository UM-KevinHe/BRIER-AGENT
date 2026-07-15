"""End-to-end smoke test for the BRIER MCP skeleton.

Verifies the full round-trip without needing Claude Desktop:
  Python tool call -> JSON payload -> Rscript subprocess -> JSON result
  -> back to Python dict.

What this catches:
  * Rscript discovery (BRIER_RSCRIPT env var, PATH fallback, well-known paths)
  * subprocess.run flag invariants (stdin=DEVNULL, --no-save etc.)
  * JSON handshake on both sides (Python toJSON / R toJSON round-trip)
  * source("_common.R") resolution from inside Rscript
  * R-side error path (when given a bogus file)

Prerequisites:
  * R >= 4.0 installed on the system, OR BRIER_RSCRIPT env var pointing
    at Rscript explicitly.
  * jsonlite installed in R: install.packages("jsonlite")
  * BRIER R package is NOT required for this test - inspect_data only
    uses base R + jsonlite.

Run:
  cd mcp/
  python tests/test_inspect_data.py

Expected output: 6 PASS lines.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make `import server` work from the tests/ subdirectory.
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


def _check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def _write_minimal_rda() -> str:
    """Drop a tiny .rda with three objects to a temp path, return the path.

    Uses Rscript to write the file, so we depend on the same Rscript the
    server will later read it with. This is intentional - if Rscript is
    broken or missing, this helper fails first with a clearer error than
    the inspect_data round-trip would.
    """
    import os
    import subprocess

    tmp = tempfile.NamedTemporaryFile(suffix=".rda", delete=False)
    tmp_path = tmp.name
    tmp.close()

    r_code = (
        "X <- matrix(rnorm(20), nrow = 5, ncol = 4)\n"
        "y <- rnorm(5)\n"
        "cohort <- c(0L, 0L, 1L, 1L, 1L)\n"
        f"save(X, y, cohort, file = '{tmp_path.replace(chr(92), '/')}')\n"
    )

    rscript = server._find_rscript()
    proc = subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e", r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to create test .rda. stderr:\n{proc.stderr}"
        )
    return tmp_path


def test_inspect_data_happy_path() -> bool:
    print("\n--- Test 1: inspect_data on a minimal .rda ---")
    results = []

    rda_path = _write_minimal_rda()
    try:
        r = server.inspect_data(data_path=rda_path)

        results.append(_check(
            "status == 'ok'",
            r.get("status") == "ok",
            detail=f"got status={r.get('status')!r}, message={r.get('message')}",
        ))

        names = r.get("top_level_names") or []
        results.append(_check(
            "top_level_names contains X, y, cohort",
            set(names) == {"X", "y", "cohort"},
            detail=f"got {sorted(names)}",
        ))

        struct = r.get("structure") or {}
        results.append(_check(
            "X is described as a matrix",
            isinstance(struct.get("X"), str) and "matrix" in struct["X"],
            detail=f"got X={struct.get('X')!r}",
        ))

        results.append(_check(
            "cohort is described as integer or numeric",
            isinstance(struct.get("cohort"), str)
            and ("integer" in struct["cohort"] or "numeric" in struct["cohort"]),
            detail=f"got cohort={struct.get('cohort')!r}",
        ))
    finally:
        Path(rda_path).unlink(missing_ok=True)

    return all(results)


def test_inspect_data_missing_file() -> bool:
    print("\n--- Test 2: inspect_data on a nonexistent file ---")
    results = []

    bogus = "/this/path/definitely/does/not/exist.rda"
    r = server.inspect_data(data_path=bogus)

    results.append(_check(
        "status == 'error'",
        r.get("status") == "error",
        detail=f"got status={r.get('status')!r}",
    ))
    results.append(_check(
        "error mentions the missing path",
        bogus in (r.get("message") or ""),
        detail=f"got message={r.get('message')!r}",
    ))

    return all(results)


def main() -> int:
    print("BRIER MCP skeleton: end-to-end smoke test")
    print(f"  Rscript: {server._find_rscript()}")

    all_pass = True
    all_pass &= test_inspect_data_happy_path()
    all_pass &= test_inspect_data_missing_file()

    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
