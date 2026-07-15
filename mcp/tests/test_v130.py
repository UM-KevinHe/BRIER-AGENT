"""Tests for v0.13 prep_data.

Covers all 9 operations, the audit log, session continuation, and the
end-to-end integration where prep_data feeds a fit and summarize_fit
renders the prep history.

Run:
  cd mcp/
  uv run tests/test_v130.py
"""
from __future__ import annotations

import json
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


def _rscript():
    return server._find_rscript()


def _stage_synthetic_pair(workdir, n_snps=100, n_drop_eur=10, n_swap=20,
                            n_mismatch=5):
    """Build an AFR-like sumstats and an EUR-like external table with a
    mix of same/swap/strand-ambiguous/mismatched alleles. Returns paths.
    """
    afr_path = os.path.join(workdir, "AFR.rds")
    eur_path = os.path.join(workdir, "EUR.rds")
    r_code = f"""
set.seed(11)
n_snps <- {n_snps}
all_ids <- paste0("rs", 1:n_snps)
chr <- sample(1:22, n_snps, replace=TRUE)
bp <- sample(1e6:5e6, n_snps, replace=FALSE)
pairs <- list(c("A","C"), c("A","G"), c("C","T"), c("G","T"),
              c("A","T"), c("C","G"))
picks <- sample(pairs, n_snps, replace=TRUE)
alleles <- do.call(rbind, picks)
A1 <- alleles[,1]; A2 <- alleles[,2]
afr <- data.frame(SNP=all_ids, CHR=chr, BP=bp, REF=A2, ALT=A1,
                   pval=runif(n_snps, 1e-8, 0.9), n=rep(5000, n_snps),
                   beta=rnorm(n_snps, 0, 0.1), stringsAsFactors=FALSE)
saveRDS(afr, "{afr_path}")
keep <- sort(sample(n_snps, n_snps - {n_drop_eur}))
eur_A1 <- A1[keep]; eur_A2 <- A2[keep]
swap_idx <- sample(seq_along(keep), {n_swap})
tmp <- eur_A1[swap_idx]; eur_A1[swap_idx] <- eur_A2[swap_idx]; eur_A2[swap_idx] <- tmp
mm_idx <- setdiff(sample(seq_along(keep), {n_mismatch}), swap_idx)
eur_A1[mm_idx] <- "X"
eur <- data.frame(rsid=all_ids[keep], CHR=chr[keep], BP=bp[keep],
                   A1=eur_A1, A2=eur_A2,
                   coef=rnorm(length(keep), 0, 0.05),
                   stringsAsFactors=FALSE)
saveRDS(eur, "{eur_path}")
"""
    subprocess.run([_rscript(), "--no-save", "--no-restore",
                    "--no-init-file", "-e", r_code],
                   capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    return afr_path, eur_path


def test_alias_root_rds():
    print("\n--- Test 1: alias_root for .rds files ---")
    workdir = tempfile.mkdtemp(prefix="v130_t1_")
    try:
        afr, _ = _stage_synthetic_pair(workdir)
        r = server.prep_data(operation="alias_root", data_path=afr)
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("session_id assigned",
                          bool(r.get("session_id"))))
        ok.append(_check("alias name == basename", "AFR" in r["aliases"]))
        ok.append(_check("alias is data.frame",
                          r["aliases"]["AFR"]["kind"] == "data.frame"))
        ok.append(_check("AFR has 100 rows",
                          r["aliases"]["AFR"]["nrow"] == 100))
        ok.append(_check("new-session notice present",
                          "_notice_new_session" in r))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_unknown_operation_rejected():
    print("\n--- Test 2: unknown operation rejected ---")
    r = server.prep_data(operation="evil_op")
    ok = [_check("status error", r.get("status") == "error"),
          _check("message lists valid ops",
                  "alias_root" in r.get("message", "") and
                  "harmonize_alleles" in r.get("message", ""))]
    return all(ok)


def test_rename_columns():
    print("\n--- Test 3: rename_columns ---")
    workdir = tempfile.mkdtemp(prefix="v130_t3_")
    try:
        afr, _ = _stage_synthetic_pair(workdir)
        r = server.prep_data(operation="alias_root", data_path=afr)
        sid = r["session_id"]
        r = server.prep_data(operation="rename_columns", session_id=sid,
                             alias="AFR",
                             mapping={"SNP": "rsid", "ALT": "A1",
                                       "REF": "A2", "fake": "irrelevant"})
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        renamed = r["summary"]["renamed"]
        ok.append(_check("3 columns renamed",
                          len(renamed) == 3))
        ok.append(_check("'fake' reported as not_found",
                          "fake" in r["summary"]["not_found"]))
        ok.append(_check("'rsid' is now in columns_after",
                          "rsid" in r["summary"]["columns_after"]))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_derive_corr_from_pvalue():
    print("\n--- Test 4: derive_corr_from_pvalue ---")
    workdir = tempfile.mkdtemp(prefix="v130_t4_")
    try:
        afr, _ = _stage_synthetic_pair(workdir, n_snps=80)
        r = server.prep_data(operation="alias_root", data_path=afr)
        sid = r["session_id"]
        r = server.prep_data(operation="derive_corr_from_pvalue",
                             session_id=sid, alias="AFR")
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("n_finite > 0",
                          r["summary"]["n_finite"] > 0))
        ok.append(_check("r values in [-1, 1]",
                          -1 <= r["summary"]["r_summary"]["min"]
                          and r["summary"]["r_summary"]["max"] <= 1))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_subset_to_common_snps():
    print("\n--- Test 5: subset_to_common_snps ---")
    workdir = tempfile.mkdtemp(prefix="v130_t5_")
    try:
        afr, eur = _stage_synthetic_pair(workdir, n_snps=100, n_drop_eur=15)
        r = server.prep_data(operation="alias_root", data_path=afr)
        sid = r["session_id"]
        server.prep_data(operation="alias_root", data_path=eur,
                          session_id=sid)
        server.prep_data(operation="rename_columns", session_id=sid,
                          alias="AFR",
                          mapping={"SNP": "rsid"})
        r = server.prep_data(operation="subset_to_common_snps",
                              session_id=sid,
                              aliases=["AFR", "EUR"])
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("n_common == 85 (EUR had 100-15)",
                          r["summary"]["n_common_out"] == 85))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_harmonize_alleles():
    print("\n--- Test 6: harmonize_alleles with synthetic AFR/EUR ---")
    workdir = tempfile.mkdtemp(prefix="v130_t6_")
    try:
        afr, eur = _stage_synthetic_pair(workdir, n_snps=100,
                                          n_drop_eur=10, n_swap=20,
                                          n_mismatch=5)
        r = server.prep_data(operation="alias_root", data_path=afr)
        sid = r["session_id"]
        server.prep_data(operation="alias_root", data_path=eur,
                          session_id=sid)
        server.prep_data(operation="rename_columns", session_id=sid,
                          alias="AFR",
                          mapping={"SNP": "rsid", "ALT": "A1",
                                    "REF": "A2"})
        server.prep_data(operation="subset_to_common_snps",
                          session_id=sid,
                          aliases=["AFR", "EUR"])
        r = server.prep_data(operation="harmonize_alleles",
                              session_id=sid,
                              target_alias="AFR",
                              external_alias="EUR")
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        s = r["summary"]
        ok.append(_check("n_swap_flipped > 0",
                          s["n_swap_flipped"] > 0,
                          detail=f"got {s['n_swap_flipped']}"))
        ok.append(_check("n_dropped_ambiguous > 0",
                          s["n_dropped_ambiguous"] > 0))
        ok.append(_check("n_dropped_mismatched > 0",
                          s["n_dropped_mismatched"] > 0))
        ok.append(_check("post-harmonize notice present",
                          s.get("_notice_post_harmonize") is not None))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_verify_aligned():
    print("\n--- Test 7: verify_aligned reports order match correctly ---")
    workdir = tempfile.mkdtemp(prefix="v130_t7_")
    try:
        afr, eur = _stage_synthetic_pair(workdir, n_snps=80,
                                          n_drop_eur=5, n_swap=0,
                                          n_mismatch=0)
        r = server.prep_data(operation="alias_root", data_path=afr)
        sid = r["session_id"]
        server.prep_data(operation="alias_root", data_path=eur,
                          session_id=sid)
        server.prep_data(operation="rename_columns", session_id=sid,
                          alias="AFR", mapping={"SNP": "rsid"})
        # Before subset: lengths differ -> not aligned
        r = server.prep_data(operation="verify_aligned", session_id=sid,
                              aliases=["AFR", "EUR"])
        ok = []
        ok.append(_check("pre-subset: same_order False",
                          not r["summary"]["same_order"]))
        # After subset: aligned
        server.prep_data(operation="subset_to_common_snps",
                          session_id=sid,
                          aliases=["AFR", "EUR"])
        r = server.prep_data(operation="verify_aligned", session_id=sid,
                              aliases=["AFR", "EUR"])
        ok.append(_check("post-subset: same_order True",
                          r["summary"]["same_order"]))
        ok.append(_check("post-subset: n_in_common == 75",
                          r["summary"]["n_in_common"] == 75))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_reshape_to_matrix():
    print("\n--- Test 8: reshape_to_matrix ---")
    workdir = tempfile.mkdtemp(prefix="v130_t8_")
    try:
        _, eur = _stage_synthetic_pair(workdir, n_snps=50, n_drop_eur=0)
        r = server.prep_data(operation="alias_root", data_path=eur)
        sid = r["session_id"]
        r = server.prep_data(operation="reshape_to_matrix",
                              session_id=sid, alias="EUR",
                              value_col="coef", id_col="rsid")
        ok = []
        ok.append(_check("status ok", r.get("status") == "ok"))
        if r.get("status") != "ok":
            return False
        ok.append(_check("out_alias auto-generated",
                          r["summary"]["alias_out"] == "EUR_matrix"))
        ok.append(_check("matrix has correct shape (50 x 1)",
                          r["summary"]["nrow"] == 50 and
                          r["summary"]["ncol"] == 1))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_assemble_and_persist():
    print("\n--- Test 9: assemble + persist (with prep_meta marker) ---")
    workdir = tempfile.mkdtemp(prefix="v130_t9_")
    try:
        afr, eur = _stage_synthetic_pair(workdir, n_snps=60, n_drop_eur=0,
                                          n_swap=0, n_mismatch=0)
        r = server.prep_data(operation="alias_root", data_path=afr)
        sid = r["session_id"]
        server.prep_data(operation="alias_root", data_path=eur,
                          session_id=sid)
        server.prep_data(operation="rename_columns", session_id=sid,
                          alias="AFR", mapping={"SNP": "rsid"})
        server.prep_data(operation="assemble", session_id=sid,
                          bundle={"target_sumstats": "AFR",
                                   "external_coefs": "EUR"})
        out = os.path.join(workdir, "ready.rds")
        r = server.prep_data(operation="persist", session_id=sid,
                              alias="assembled", output_path=out)
        ok = []
        ok.append(_check("persist status ok",
                          r.get("status") == "ok"))
        ok.append(_check("output file exists", os.path.exists(out)))
        # Read it back and confirm prep_meta is present
        r2 = subprocess.run(
            [_rscript(), "--no-save", "--no-restore", "--no-init-file",
             "-e",
             f'obj <- readRDS("{out}"); '
             'cat(!is.null(obj$.prep_meta$prep_session_id), "\\n", '
             '    obj$.prep_meta$prep_session_id, "\\n")'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        lines = r2.stdout.strip().split("\n")
        ok.append(_check(".prep_meta$prep_session_id present in file",
                          "TRUE" in lines[0]))
        if len(lines) >= 2:
            ok.append(_check("embedded id matches session id",
                              sid in lines[1]))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_audit_log():
    print("\n--- Test 10: prep_data_log returns the audit trail ---")
    workdir = tempfile.mkdtemp(prefix="v130_t10_")
    try:
        afr, _ = _stage_synthetic_pair(workdir, n_snps=40)
        r = server.prep_data(operation="alias_root", data_path=afr)
        sid = r["session_id"]
        server.prep_data(operation="rename_columns", session_id=sid,
                          alias="AFR", mapping={"SNP": "rsid"})
        server.prep_data(operation="derive_corr_from_pvalue",
                          session_id=sid, alias="AFR")
        log = server.prep_data_log(session_id=sid)
        ok = []
        ok.append(_check("status ok", log.get("status") == "ok"))
        ok.append(_check("3 records",
                          len(log["log"]) == 3))
        ops = [r["operation"] for r in log["log"]]
        ok.append(_check("ops in order: alias_root, rename_columns, "
                          "derive_corr_from_pvalue",
                          ops == ["alias_root", "rename_columns",
                                   "derive_corr_from_pvalue"]))
        # Unknown session
        log2 = server.prep_data_log(session_id="prep_does_not_exist_xyz")
        ok.append(_check("unknown session -> error",
                          log2.get("status") == "error"))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_integration_with_fit_and_summarize():
    print("\n--- Test 11: prep_data -> fit -> summarize_fit integration ---")
    workdir = tempfile.mkdtemp(prefix="v130_t11_")
    try:
        rds_in = os.path.join(workdir, "raw.rds")
        subprocess.run(
            [_rscript(), "--no-save", "--no-restore", "--no-init-file",
             "-e",
             "suppressPackageStartupMessages(library(BRIER)); "
             f"data(Data_BRIERi); saveRDS(Data_BRIERi, '{rds_in}')"],
            capture_output=True, stdin=subprocess.DEVNULL)
        r = server.prep_data(operation="alias_root", data_path=rds_in,
                              alias="bi")
        sid = r["session_id"]
        out = os.path.join(workdir, "prepared.rds")
        server.prep_data(operation="persist", session_id=sid,
                          alias="bi", output_path=out)
        fit = server.brier_i(data_path=out,
                              X_expr="prepared$target$train$X",
                              y_expr="prepared$target$train$y",
                              beta_external_expr="prepared$beta.external[, 1, drop=FALSE]",
                              family="gaussian", eta_list=[0, 1])
        ok = []
        ok.append(_check("fit status ok",
                          fit.get("status") == "ok",
                          detail=str(fit.get("message", ""))))
        if fit.get("status") != "ok":
            return False
        sel = server.brier_i_selection(fit_id=fit["fit_id"],
                                        criteria="BIC")
        ok.append(_check("selection ok", sel["status"] == "ok"))
        rep = server.summarize_fit(
            selection_id=sel["selection_id"],
            output_dir=os.path.join(workdir, "rep"))
        ok.append(_check("summarize_fit ok", rep["status"] == "ok"))
        if rep["status"] != "ok":
            return all(ok)
        with open(rep["report_html_path"]) as f:
            html = f.read()
        ok.append(_check("HTML has 'Data preparation steps' section",
                          "Data preparation steps" in html))
        ok.append(_check("HTML mentions the prep session id",
                          sid in html))
        ok.append(_check("HTML lists alias_root operation",
                          "alias_root" in html))
        ok.append(_check("HTML lists persist operation",
                          "persist" in html))
        return all(ok)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    print("BRIER MCP v0.13 prep_data test suite")
    all_pass = True
    all_pass &= test_alias_root_rds()
    all_pass &= test_unknown_operation_rejected()
    all_pass &= test_rename_columns()
    all_pass &= test_derive_corr_from_pvalue()
    all_pass &= test_subset_to_common_snps()
    all_pass &= test_harmonize_alleles()
    all_pass &= test_verify_aligned()
    all_pass &= test_reshape_to_matrix()
    all_pass &= test_assemble_and_persist()
    all_pass &= test_audit_log()
    all_pass &= test_integration_with_fit_and_summarize()
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
