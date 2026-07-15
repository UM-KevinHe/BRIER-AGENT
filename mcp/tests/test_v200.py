"""v2.0.0 test: broadened inspection format support.

Exercises inspect_user_data and list_data_directory against the formats
added in v2.0.0:
  * tabular: .csv (already supported; sanity), .xlsx (graceful if readxl absent)
  * genotype binaries: .pgen/.psam/.pvar (PLINK2), .bed/.bim/.fam (PLINK1)

Genotype binaries are inspected via their companion text files only; this
test writes small fake companions (and a stub binary) so no real genotype
data or PLINK install is needed. It checks that dimensions are read from
the companions, not from the binary.

Run:
  cd BRIER-MCP/
  uv run tests/test_v200.py

Prereqs: R + jsonlite (for the round-trip), same as test_inspect_data.py.
The BRIER R package is NOT required.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402


def _check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def _tmpdir() -> Path:
    d = tempfile.mkdtemp(prefix="brier_v200_")
    return Path(d)


def _write_csv(path: Path) -> None:
    # A small individual-level-ish table: a binary y and two rsID columns.
    path.write_text(
        "y,rs123,rs456\n"
        "0,0,1\n"
        "1,1,2\n"
        "0,2,0\n"
        "1,1,1\n"
    )


def _write_plink2_trio(prefix: Path) -> None:
    # .pgen is a stub binary (never read); .pvar/.psam carry the metadata.
    (prefix.with_suffix(".pgen")).write_bytes(b"\x6c\x1b\x02FAKE_PGEN_BINARY")
    # .pvar: two ## meta lines, one #CHROM header, then 3 variant rows.
    prefix.with_suffix(".pvar").write_text(
        "##fileformat=PVARish\n"
        "##source=test\n"
        "#CHROM\tPOS\tID\tREF\tALT\n"
        "1\t1000\trs1\tA\tG\n"
        "1\t2000\trs2\tC\tT\n"
        "1\t3000\trs3\tG\tA\n"
    )
    # .psam: header (#IID ...) then 4 sample rows; include a PHENO1 column.
    prefix.with_suffix(".psam").write_text(
        "#IID\tSEX\tPHENO1\n"
        "S1\t1\t0\n"
        "S2\t2\t1\n"
        "S3\t1\t0\n"
        "S4\t2\t1\n"
    )


def _write_plink1_trio(prefix: Path) -> None:
    (prefix.with_suffix(".bed")).write_bytes(b"\x6c\x1b\x01FAKE_BED_BINARY")
    # .bim: 5 variants, 6 columns (chr, id, cm, bp, a1, a2)
    prefix.with_suffix(".bim").write_text(
        "1\trs1\t0\t1000\tA\tG\n"
        "1\trs2\t0\t2000\tC\tT\n"
        "1\trs3\t0\t3000\tG\tA\n"
        "1\trs4\t0\t4000\tT\tC\n"
        "1\trs5\t0\t5000\tA\tT\n"
    )
    # .fam: 3 samples, 6 cols; col 6 = phenotype (use 1/2 = present, not -9)
    prefix.with_suffix(".fam").write_text(
        "F1 S1 0 0 1 1\n"
        "F2 S2 0 0 2 2\n"
        "F3 S3 0 0 1 1\n"
    )


def test_csv_inspection() -> bool:
    print("\n--- Test 1: inspect_user_data on a .csv ---")
    results = []
    d = _tmpdir()
    csv = d / "target.csv"
    _write_csv(csv)
    try:
        r = server.inspect_user_data(data_paths=[str(csv)])
        results.append(_check(
            "status == 'ok'",
            r.get("status") == "ok",
            detail=f"got {r.get('status')!r}: {r.get('message')}",
        ))
        files = r.get("files") or []
        f0 = files[0] if files else {}
        results.append(_check(
            "format detected as csv",
            f0.get("format") == "csv",
            detail=f"got {f0.get('format')!r}",
        ))
    finally:
        for p in d.iterdir():
            p.unlink(missing_ok=True)
    return all(results)


def test_plink2_inspection() -> bool:
    print("\n--- Test 2: inspect_user_data on a PLINK2 .pgen (companions only) ---")
    results = []
    d = _tmpdir()
    prefix = d / "geno2"
    _write_plink2_trio(prefix)
    pgen = prefix.with_suffix(".pgen")
    try:
        r = server.inspect_user_data(data_paths=[str(pgen)])
        results.append(_check(
            "status == 'ok'",
            r.get("status") == "ok",
            detail=f"got {r.get('status')!r}: {r.get('message')}",
        ))
        f0 = (r.get("files") or [{}])[0]
        results.append(_check(
            "format detected as pgen",
            f0.get("format") == "pgen",
            detail=f"got {f0.get('format')!r}",
        ))
        struct = f0.get("structure") or {}
        results.append(_check(
            "n_variants read from .pvar == 3",
            struct.get("n_variants") == 3,
            detail=f"got {struct.get('n_variants')!r}",
        ))
        results.append(_check(
            "n_samples read from .psam == 4",
            struct.get("n_samples") == 4,
            detail=f"got {struct.get('n_samples')!r}",
        ))
        results.append(_check(
            "phenotype column detected in .psam",
            struct.get("has_phenotype") is True,
            detail=f"got {struct.get('has_phenotype')!r}",
        ))
    finally:
        for p in d.iterdir():
            p.unlink(missing_ok=True)
    return all(results)


def test_plink1_inspection() -> bool:
    print("\n--- Test 3: inspect_user_data on a PLINK1 .bed (companions only) ---")
    results = []
    d = _tmpdir()
    prefix = d / "geno1"
    _write_plink1_trio(prefix)
    bed = prefix.with_suffix(".bed")
    try:
        r = server.inspect_user_data(data_paths=[str(bed)])
        f0 = (r.get("files") or [{}])[0]
        results.append(_check(
            "format detected as bed",
            f0.get("format") == "bed",
            detail=f"got {f0.get('format')!r}",
        ))
        struct = f0.get("structure") or {}
        results.append(_check(
            "n_variants read from .bim == 5",
            struct.get("n_variants") == 5,
            detail=f"got {struct.get('n_variants')!r}",
        ))
        results.append(_check(
            "n_samples read from .fam == 3",
            struct.get("n_samples") == 3,
            detail=f"got {struct.get('n_samples')!r}",
        ))
    finally:
        for p in d.iterdir():
            p.unlink(missing_ok=True)
    return all(results)


def test_list_directory_broadened() -> bool:
    print("\n--- Test 4: list_data_directory lists new formats ---")
    results = []
    d = _tmpdir()
    _write_csv(d / "a.csv")
    _write_plink2_trio(d / "geno2")
    _write_plink1_trio(d / "geno1")
    try:
        r = server.list_data_directory(dir_path=str(d))
        names = {f["name"] for f in (r.get("files") or [])}
        results.append(_check(
            "lists the .csv",
            "a.csv" in names,
            detail=f"got {sorted(names)}",
        ))
        results.append(_check(
            "lists the .pgen",
            "geno2.pgen" in names,
            detail=f"got {sorted(names)}",
        ))
        results.append(_check(
            "lists the .bed",
            "geno1.bed" in names,
            detail=f"got {sorted(names)}",
        ))
    finally:
        for p in d.iterdir():
            p.unlink(missing_ok=True)
    return all(results)


def test_xlsx_graceful() -> bool:
    print("\n--- Test 5: xlsx degrades gracefully if readxl absent ---")
    # We do not write a real .xlsx (that would need a writer). Instead we
    # confirm the format is recognized and that a missing/unreadable xlsx
    # yields a structured error rather than a crash. A bogus .xlsx path
    # exercises the error path deterministically.
    results = []
    d = _tmpdir()
    fake = d / "book.xlsx"
    fake.write_bytes(b"not a real xlsx")
    try:
        r = server.inspect_user_data(data_paths=[str(fake)])
        # Either: readxl absent -> per-file error message about readxl,
        # or readxl present -> a read error on the bogus file. Both are
        # handled (no crash), which is what we assert.
        ok = r.get("status") == "ok"  # tool itself should not crash
        f0 = (r.get("files") or [{}])[0]
        recognized = f0.get("format") == "xlsx" or "error" in f0
        results.append(_check(
            "tool did not crash on xlsx input",
            ok,
            detail=f"got status={r.get('status')!r}",
        ))
        results.append(_check(
            "xlsx recognized or errored cleanly per-file",
            recognized,
            detail=f"got file entry keys={list(f0.keys())}",
        ))
    finally:
        for p in d.iterdir():
            p.unlink(missing_ok=True)
    return all(results)


def main() -> int:
    print("BRIER MCP v2.0.0: broadened inspection format tests")
    print(f"  Rscript: {server._find_rscript()}")

    all_pass = True
    all_pass &= test_csv_inspection()
    all_pass &= test_plink2_inspection()
    all_pass &= test_plink1_inspection()
    all_pass &= test_list_directory_broadened()
    all_pass &= test_xlsx_graceful()

    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
