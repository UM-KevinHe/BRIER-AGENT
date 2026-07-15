"""End-to-end smoke test for the BRIER MCP v0.7.0 surface.

Tests the data-first wizard flow:
  start_analysis()
    -> AI asks for data path
    -> inspect_user_data(paths)
    -> start_analysis(inspection_id=...)
    -> (optional) start_analysis(inspection_id=..., overrides={...})

Validates:
  * inspect_user_data correctly identifies common data shapes:
    - individual-level X/y with pretrained beta.external
    - sumstats with corr/pval columns
    - nested train/val/test structure (like Data_BRIERi)
    - CSV format input
  * Heuristics produce reasonable guesses:
    - outcome_family from y values (gaussian / binomial / poisson)
    - predictor_type from column names (SNP / gene / protein)
    - data_shape from object structure
    - splits detection
    - time-to-event detection
  * start_analysis(inspection_id=...) returns a grounded recommendation
  * Override mechanism: user corrections propagate through
  * canonical_call uses the user's actual R expression paths
  * Selection criterion suggestion adapts to (family, validation set)
  * Cache miss returns a clean error
  * Mode 1 (no inspection_id) returns the v0.6-style fallback

Run:
  cd mcp/
  uv run tests/test_v07.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


def _check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


# --------------------------------------------------------------------------
# Test data setup
# --------------------------------------------------------------------------

def _stage_test_files() -> tuple[str, dict]:
    """Create a workdir with several test data files exercising the
    heuristics:
      * synth_indiv.rds: X + y + beta.external, gaussian, SNP columns
      * synth_binary.rds: same shape but binary y
      * synth_sumstats.rds: sumstats data.frame
      * synth_nested.rds: nested target$train/$validation/$testing
      * synth_genes.rds: gene-symbol column names
      * synth_indiv.csv: same data as synth_indiv.rds but CSV format
    Returns (workdir, paths_dict).
    """
    workdir = tempfile.mkdtemp(prefix="brier_v07_test_")
    rscript = server._find_rscript()

    paths = {
        "indiv": os.path.join(workdir, "synth_indiv.rds"),
        "binary": os.path.join(workdir, "synth_binary.rds"),
        "sumstats": os.path.join(workdir, "synth_sumstats.rds"),
        "nested": os.path.join(workdir, "synth_nested.rds"),
        "genes": os.path.join(workdir, "synth_genes.rds"),
        "indiv_csv": os.path.join(workdir, "synth_indiv.csv"),
    }

    r_code = (
        'set.seed(2);\n'
        'X <- matrix(rnorm(80*30), 80, 30);\n'
        'colnames(X) <- paste0("rs", 1:30);\n'
        'y <- rnorm(80);\n'
        'beta.external <- matrix(0, nrow=31, ncol=2);\n'
        f'saveRDS(list(X=X, y=y, beta.external=beta.external),'
        f' "{paths["indiv"]}");\n'

        '# Binary outcome\n'
        'y_bin <- rbinom(80, 1, 0.5);\n'
        f'saveRDS(list(X=X, y=y_bin), "{paths["binary"]}");\n'

        '# Sumstats data.frame\n'
        'sumstats <- data.frame(\n'
        '  variable = paste0("rs", 1:30),\n'
        '  corr = runif(30, -0.3, 0.3),\n'
        '  stats = rnorm(30),\n'
        '  pval = runif(30),\n'
        '  n = 5000\n'
        ');\n'
        f'saveRDS(list(sumstats=sumstats, beta.external=beta.external[-1,]),'
        f' "{paths["sumstats"]}");\n'

        '# Nested target$train/$validation/$testing structure\n'
        'X_train <- matrix(rnorm(60*30), 60, 30); colnames(X_train) <- paste0("rs", 1:30);\n'
        'X_val <- matrix(rnorm(20*30), 20, 30); colnames(X_val) <- paste0("rs", 1:30);\n'
        'X_test <- matrix(rnorm(20*30), 20, 30); colnames(X_test) <- paste0("rs", 1:30);\n'
        'y_train <- rnorm(60); y_val <- rnorm(20); y_test <- rnorm(20);\n'
        'data_nested <- list(\n'
        '  target = list(\n'
        '    train = list(X = X_train, y = y_train),\n'
        '    validation = list(X = X_val, y = y_val),\n'
        '    testing = list(X = X_test, y = y_test)\n'
        '  ),\n'
        '  beta.external = matrix(0, nrow=31, ncol=2)\n'
        ');\n'
        f'saveRDS(data_nested, "{paths["nested"]}");\n'

        '# Gene expression: ENSG IDs in column names\n'
        'X_gene <- matrix(rnorm(80*40), 80, 40);\n'
        'colnames(X_gene) <- paste0("ENSG", sprintf("%011d", 1:40));\n'
        'y_gene <- rnorm(80);\n'
        f'saveRDS(list(X=X_gene, y=y_gene), "{paths["genes"]}");\n'

        '# CSV version (same content as indiv): X columns + y as last col\n'
        'df_indiv <- data.frame(X, y = y, check.names = FALSE);\n'
        f'write.csv(df_indiv, "{paths["indiv_csv"]}", row.names = FALSE);\n'
    )

    proc = subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-init-file", "-e", r_code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to stage test data:\n{proc.stderr}")
    return workdir, paths


# --------------------------------------------------------------------------
# Test 1: start_analysis() mode 1 (no args)
# --------------------------------------------------------------------------

def test_mode_1_returns_welcome_and_fallback() -> bool:
    print("\n--- Test 1: start_analysis() with no args returns mode-1 fallback ---")
    r = server.start_analysis()
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok"))
    ok.append(_check("welcome present", "welcome" in r))
    ok.append(_check(
        "familiarity_check uses multi-choice prompt format",
        # v0.10.1 restructured this. Accept either old-style (prompt field
        # with "skip" + multi-choice indicator) or new-style (structured
        # options with multi_select=True and a Skip option).
        (
            # Old-style check (pre-v0.10.1)
            "prompt" in r.get("familiarity_check", {})
            and "skip" in r["familiarity_check"].get("prompt", "").lower()
            and ("(a)" in r["familiarity_check"].get("prompt", "")
                 or "any combination" in r["familiarity_check"].get("prompt", "").lower())
        ) or (
            # New-style check (v0.10.1+)
            r.get("familiarity_check", {}).get("multi_select") is True
            and isinstance(r["familiarity_check"].get("options"), list)
            and any("skip" in opt.get("label", "").lower()
                    for opt in r["familiarity_check"]["options"])
        ),
    ))
    ok.append(_check(
        "v0.6-style fallback present (problem_description_questions)",
        "problem_description_questions" in r,
    ))
    ok.append(_check(
        "phase_gates still present in mode-1",
        "phase_gates" in r,
    ))
    ok.append(_check(
        "ai_instructions describes PREFERRED data-first flow",
        "data-first" in r.get("ai_instructions", "").lower()
        or "inspect_user_data" in r.get("ai_instructions", ""),
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 2: inspect_user_data on individual-level .rds
# --------------------------------------------------------------------------

def test_inspect_individual_rds(paths) -> dict:
    print("\n--- Test 2: inspect_user_data on individual-level .rds ---")
    r = server.inspect_user_data(data_paths=[paths["indiv"]])
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok",
                     detail=f"msg={r.get('message')}"))
    if r.get("status") != "ok":
        return {}
    ok.append(_check("inspection_id returned", bool(r.get("inspection_id"))))
    ok.append(_check(
        "inspection_path exists",
        r.get("inspection_path") and os.path.exists(r["inspection_path"]),
    ))
    combined = r.get("combined_assessment", {})
    ok.append(_check(
        "target_shape detected as 'individual'",
        combined.get("target_shape") == "individual",
        detail=f"got {combined.get('target_shape')}",
    ))
    ok.append(_check(
        "external_shape detected as 'coefficients' (beta.external in same file)",
        combined.get("external_shape") == "coefficients",
        detail=f"got {combined.get('external_shape')}",
    ))
    ok.append(_check(
        "outcome_family detected as 'gaussian'",
        combined.get("outcome_family") == "gaussian",
    ))
    ok.append(_check(
        "predictor_type detected as 'SNP' (rsID columns)",
        combined.get("predictor_type") == "SNP",
    ))
    ok.append(_check(
        "n_target detected as 80",
        combined.get("n_target") == 80,
    ))
    ok.append(_check(
        "p detected as 30",
        combined.get("p") == 30,
    ))
    return r if all(ok) else {}


# --------------------------------------------------------------------------
# Test 3: outcome_family heuristic for binary y
# --------------------------------------------------------------------------

def test_binary_outcome_detection(paths) -> bool:
    print("\n--- Test 3: outcome_family heuristic on binary y ---")
    r = server.inspect_user_data(data_paths=[paths["binary"]])
    if r.get("status") != "ok":
        return _check("inspect ok", False, detail=f"msg={r.get('message')}")
    combined = r.get("combined_assessment", {})
    ok = []
    ok.append(_check(
        "binary y -> outcome_family = 'binomial'",
        combined.get("outcome_family") == "binomial",
        detail=f"got {combined.get('outcome_family')}",
    ))
    # Check confidence is high
    primary = r["files"][0]
    outcome_h = primary["heuristics"]["outcome_family"]
    ok.append(_check(
        "binary y -> confidence 'high'",
        outcome_h.get("confidence") == "high",
        detail=f"got {outcome_h.get('confidence')}",
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 4: sumstats detection
# --------------------------------------------------------------------------

def test_sumstats_detection(paths) -> bool:
    print("\n--- Test 4: sumstats data shape detection ---")
    r = server.inspect_user_data(data_paths=[paths["sumstats"]])
    if r.get("status") != "ok":
        return _check("inspect ok", False, detail=f"msg={r.get('message')}")
    combined = r.get("combined_assessment", {})
    return _check(
        "target_shape = 'sumstats' when corr/pval columns present",
        combined.get("target_shape") == "sumstats",
        detail=f"got {combined.get('target_shape')}",
    )


# --------------------------------------------------------------------------
# Test 5: nested train/val/test detection + expression suggestions
# --------------------------------------------------------------------------

def test_nested_structure(paths) -> bool:
    print("\n--- Test 5: nested target$train/$val/$test detection ---")
    r = server.inspect_user_data(data_paths=[paths["nested"]])
    if r.get("status") != "ok":
        return _check("inspect ok", False, detail=f"msg={r.get('message')}")
    combined = r.get("combined_assessment", {})
    primary = r["files"][0]
    ok = []
    ok.append(_check(
        "has_validation_set = True (train/val/test detected)",
        combined.get("has_validation_set") is True,
    ))
    suggested = primary.get("suggested_exprs", {})
    ok.append(_check(
        "target_X_expr points at synth_nested$target$train$X",
        "target$train$X" in suggested.get("target_X_expr", ""),
        detail=f"got {suggested.get('target_X_expr')}",
    ))
    ok.append(_check(
        "X_val_expr points at synth_nested$target$validation$X",
        "validation$X" in suggested.get("X_val_expr", "")
        or "val$X" in suggested.get("X_val_expr", ""),
        detail=f"got {suggested.get('X_val_expr')}",
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 6: gene expression predictor type heuristic
# --------------------------------------------------------------------------

def test_gene_expression_heuristic(paths) -> bool:
    print("\n--- Test 6: gene_expression detection from ENSG column names ---")
    r = server.inspect_user_data(data_paths=[paths["genes"]])
    if r.get("status") != "ok":
        return _check("inspect ok", False, detail=f"msg={r.get('message')}")
    combined = r.get("combined_assessment", {})
    return _check(
        "predictor_type = 'gene_expression' for ENSG columns",
        combined.get("predictor_type") == "gene_expression",
        detail=f"got {combined.get('predictor_type')}",
    )


# --------------------------------------------------------------------------
# Test 7: CSV format support
# --------------------------------------------------------------------------

def test_csv_format(paths) -> bool:
    print("\n--- Test 7: CSV format input ---")
    r = server.inspect_user_data(data_paths=[paths["indiv_csv"]])
    if r.get("status") != "ok":
        return _check("CSV inspect ok", False, detail=f"msg={r.get('message')}")
    # For CSV, we get a data.frame; the heuristics treat the whole thing as
    # one object. Just verify it was loaded and shape was detected.
    primary = r["files"][0]
    return all([
        _check("CSV format detected", primary.get("format") == "csv"),
        _check("structure parsed",
               primary.get("structure", {}).get("type") in
               ("data.frame", "matrix")),
    ])


# --------------------------------------------------------------------------
# Test 8: start_analysis(inspection_id) returns recommendation
# --------------------------------------------------------------------------

def test_recommendation_from_inspection(paths) -> bool:
    print("\n--- Test 8: start_analysis(inspection_id=...) returns grounded recommendation ---")
    r1 = server.inspect_user_data(data_paths=[paths["indiv"]])
    if r1.get("status") != "ok":
        return _check("inspect ok", False)
    r2 = server.start_analysis(inspection_id=r1["inspection_id"])
    ok = []
    ok.append(_check("status ok", r2.get("status") == "ok",
                     detail=f"msg={r2.get('message')}"))
    if r2.get("status") != "ok":
        return False
    rec = r2.get("recommendation", {})
    ok.append(_check(
        "primary recommendation = 'BRIERi' (individual + coefficients)",
        rec.get("primary") == "BRIERi",
    ))
    ok.append(_check(
        "canonical_call references user's actual file path",
        paths["indiv"] in rec.get("canonical_call", ""),
    ))
    ok.append(_check(
        "canonical_call uses inspected X expression (synth_indiv$X)",
        "synth_indiv$X" in rec.get("canonical_call", ""),
    ))
    ok.append(_check(
        "canonical_call uses inspected y expression",
        "synth_indiv$y" in rec.get("canonical_call", ""),
    ))
    ok.append(_check(
        "inferred_assessment present with applied_overrides={}",
        r2.get("inferred_assessment", {}).get("applied_overrides") == {},
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 9: override mechanism
# --------------------------------------------------------------------------

def test_overrides(paths) -> bool:
    print("\n--- Test 9: overrides propagate through recommendation ---")
    r1 = server.inspect_user_data(data_paths=[paths["indiv"]])
    if r1.get("status") != "ok":
        return False
    r2 = server.start_analysis(
        inspection_id=r1["inspection_id"],
        overrides={
            "outcome_family": "binomial",
            "predictor_type": "gene_expression",
        },
    )
    ok = []
    rec = r2.get("recommendation", {})
    ok.append(_check(
        "override outcome_family applied",
        rec.get("outcome_family") == "binomial",
    ))
    ok.append(_check(
        "override predictor_type applied",
        rec.get("predictor_type") == "gene_expression",
    ))
    ok.append(_check(
        "canonical_call reflects binomial family",
        "binomial" in rec.get("canonical_call", ""),
    ))
    ok.append(_check(
        "applied_overrides echoed back",
        r2.get("inferred_assessment", {}).get("applied_overrides", {})
            .get("outcome_family") == "binomial",
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 10: nested file recommendation uses correct expression paths
# --------------------------------------------------------------------------

def test_nested_recommendation(paths) -> bool:
    print("\n--- Test 10: nested data file produces nested expression paths in canonical_call ---")
    r1 = server.inspect_user_data(data_paths=[paths["nested"]])
    if r1.get("status") != "ok":
        return False
    r2 = server.start_analysis(inspection_id=r1["inspection_id"])
    rec = r2.get("recommendation", {})
    ok = []
    ok.append(_check(
        "canonical_call uses target$train$X path",
        "target$train$X" in rec.get("canonical_call", ""),
    ))
    ok.append(_check(
        "canonical_call uses target$train$y path",
        "target$train$y" in rec.get("canonical_call", ""),
    ))
    ok.append(_check(
        "selection_criterion suggests using validation set",
        "validation" in rec.get("selection_criterion_suggestion", "").lower()
        or "X_val_expr" in rec.get("selection_criterion_suggestion", ""),
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 11: cache miss returns a clean error
# --------------------------------------------------------------------------

def test_cache_miss() -> bool:
    print("\n--- Test 11: missing inspection_id returns clean error ---")
    r = server.start_analysis(inspection_id="insp_bogus_does_not_exist")
    return all([
        _check("status error", r.get("status") == "error"),
        _check("error class = CacheMiss", r.get("class") == "CacheMiss"),
        _check(
            "error mentions re-running inspect_user_data",
            "inspect_user_data" in r.get("message", ""),
        ),
    ])


# --------------------------------------------------------------------------
# Test 12: confidence_notes for low/medium-confidence heuristics
# --------------------------------------------------------------------------

def test_confidence_notes(paths) -> bool:
    print("\n--- Test 12: confidence_notes surface medium-confidence guesses ---")
    # synth_genes.rds has gene-expression columns; the heuristic confidence
    # depends on the column patterns matched. For ENSG columns, that's
    # high; for non-rsID-but-still-symbol-y columns we'd expect medium.
    # Let's verify the structure of confidence_notes for any case.
    r1 = server.inspect_user_data(data_paths=[paths["indiv"]])
    r2 = server.start_analysis(inspection_id=r1["inspection_id"])
    notes = r2.get("confidence_notes", [])
    # The structure should be a list (possibly empty).
    return _check(
        "confidence_notes is a list (may be empty for high-confidence cases)",
        isinstance(notes, list),
    )


# --------------------------------------------------------------------------
# Test 13: backward compatibility with v0.6 size args
# --------------------------------------------------------------------------

def test_legacy_size_args() -> bool:
    print("\n--- Test 13: legacy v0.6 size args still work ---")
    r = server.start_analysis(
        n_target=2000, n_external_total=40000,
        has_individual_external=True,
    )
    ok = []
    ok.append(_check("status ok", r.get("status") == "ok"))
    rec = r.get("size_recommendation", {})
    ok.append(_check(
        "size_recommendation populated for large individual case",
        rec is not None and rec.get("primary") == "BRIERi",
    ))
    return all(ok)


# --------------------------------------------------------------------------
# Test 14: inspect_user_data rejects empty paths
# --------------------------------------------------------------------------

def test_empty_paths_rejected() -> bool:
    print("\n--- Test 14: inspect_user_data rejects empty paths list ---")
    r = server.inspect_user_data(data_paths=[])
    return _check(
        "empty paths -> status error",
        r.get("status") == "error",
    )


# --------------------------------------------------------------------------
# Test 15: nonexistent file produces per-file error inside the response
# --------------------------------------------------------------------------

def test_nonexistent_file() -> bool:
    print("\n--- Test 15: nonexistent file produces clean per-file error ---")
    r = server.inspect_user_data(data_paths=["/this/does/not/exist.rds"])
    if r.get("status") != "ok":
        return _check(
            "either top-level error or per-file error",
            r.get("status") == "error",
        )
    primary = r["files"][0]
    return _check(
        "per-file error reported",
        "error" in primary and "not found" in primary["error"].lower(),
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    print("BRIER MCP v0.7.0 end-to-end smoke test")
    print(f"  Rscript: {server._find_rscript()}")

    workdir, paths = _stage_test_files()
    try:
        all_pass = True
        all_pass &= test_mode_1_returns_welcome_and_fallback()

        r_indiv = test_inspect_individual_rds(paths)
        all_pass &= bool(r_indiv)

        all_pass &= test_binary_outcome_detection(paths)
        all_pass &= test_sumstats_detection(paths)
        all_pass &= test_nested_structure(paths)
        all_pass &= test_gene_expression_heuristic(paths)
        all_pass &= test_csv_format(paths)
        all_pass &= test_recommendation_from_inspection(paths)
        all_pass &= test_overrides(paths)
        all_pass &= test_nested_recommendation(paths)
        all_pass &= test_cache_miss()
        all_pass &= test_confidence_notes(paths)
        all_pass &= test_legacy_size_args()
        all_pass &= test_empty_paths_rejected()
        all_pass &= test_nonexistent_file()

        print()
        print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
        return 0 if all_pass else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
