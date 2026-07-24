"""Role-advisor layer (T6): infer the module + canonical prep_auto roles from data.

A removable scaffolding layer (toggle: BRIER_ROLE_ADVISOR, default ON). It inspects the
CONTENT of the case's data files -- not their names -- and works out which BRIER module the
data calls for and how each file maps to a prep_auto role, then emits a short hint the agent
reads before its first turn.

It targets two failure modes observed on the honest (de-leaked) prompts, both on the
raw-external cases: (a) mis-routing an individual target with a raw EUR summary to the
pooling module, and (b) mapping a raw GWAS (BETA/P/N) as if it were a fitted coefficient
vector. Both stem from the file names (external_GWAS, external_X_reference) not matching
prep_auto's role vocabulary (external_sumstats, external_ld_panel); a capable model
(Llama-70B) routes these unaided, so the layer's job is to lift the smaller models to that.

The advisor is deliberately deterministic and content-based: a table with BETA and P is a
GWAS; a wide matrix with an ID column is a genotype panel; a `varnames`+`coef` table is a
fitted coefficient vector. It never reads data VALUES beyond the header + one row.
"""
from __future__ import annotations

import gzip
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---- file-content classification ---------------------------------------------

_KIND_GWAS = "gwas"          # summary statistics: has BETA/OR + P (marginal effects)
_KIND_COEF = "coef"          # a fitted coefficient vector: varnames + coef, no BETA/P
_KIND_GENO = "genotype"      # a wide genotype matrix: ID column + many variant columns
_KIND_PHENO = "phenotype"    # ID + a single outcome column
_KIND_MAP = "variant_map"    # varnames/CHR/BP/REF/ALT only (no coef, no BETA, not wide)
_KIND_SUMSTATS_CORR = "sumstats_corr"  # summary target: has a corr column + N
_KIND_UNKNOWN = "unknown"


def _read_header(path: str) -> Tuple[List[str], int]:
    """Return (lowercased column names, column count) from the first line."""
    opener = gzip.open if str(path).endswith((".gz", ".bgz")) else open
    try:
        with opener(path, "rt", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return [], 0
    # tolerate both tab and whitespace-delimited headers
    cols = re.split(r"[\t ]+", first.strip())
    return [c.strip().strip('"').lower() for c in cols if c != ""], len(cols)


def classify(path: str) -> str:
    """Classify one data file by its header columns (content, not name)."""
    p = str(path)
    if p.endswith((".rds", ".rda", ".RData")):
        return _KIND_UNKNOWN  # a binary R object (LD / prepared) -- leave to prep_auto
    cols, ncol = _read_header(p)
    if not cols:
        return _KIND_UNKNOWN
    cset = set(cols)
    has = lambda *names: any(n in cset for n in names)
    # A wide matrix (an ID column + many predictor columns) is a genotype panel.
    id_like = cols[0] in ("iid", "fid", "deid_patientid", "id", "sample", "sample_id")
    if id_like and ncol > 20:
        return _KIND_GENO
    # A GWAS: a marginal effect (BETA or OR) AND a p-value.
    if has("beta", "or") and has("p", "pval", "pvalue"):
        return _KIND_GWAS
    # A summary-target sumstats that already carries the marginal correlation.
    if has("corr") and has("n", "nmiss"):
        return _KIND_SUMSTATS_CORR
    # A fitted coefficient vector: a coef column, no marginal effect/p.
    if has("coef", "beta_external", "weight") and not has("beta", "or", "p"):
        return _KIND_COEF
    # A phenotype: an ID column + a single value (trait / pheno).
    if id_like and ncol <= 3 and has("trait", "pheno", "phenotype", "y"):
        return _KIND_PHENO
    if id_like and ncol <= 3:
        return _KIND_PHENO
    # A variant map: names + coordinates, nothing else.
    if has("varnames", "snp") and has("chr") and has("bp") and not has("coef", "beta", "or"):
        return _KIND_MAP
    return _KIND_UNKNOWN


# ---- ancestry / build extraction from the prompt (facts, not method) ---------

def _extract_ancestry_build(prompt: str) -> Tuple[Optional[str], Optional[str]]:
    """Pull the external ancestry and genome build from the prompt's stated facts."""
    build = None
    if re.search(r"hg38|grch38", prompt, re.I):
        build = "hg38"
    elif re.search(r"hg19|grch37", prompt, re.I):
        build = "hg19"
    # The external cohort's ancestry (EUR in this benchmark); the target is AFR.
    ext_anc = None
    for anc in ("EUR", "EAS", "SAS", "AMR", "AFR"):
        if re.search(rf"european|{anc}\b", prompt) and anc != "AFR":
            ext_anc = anc
            break
    if ext_anc is None and re.search(r"european", prompt, re.I):
        ext_anc = "EUR"
    return ext_anc, build


# ---- role + module inference --------------------------------------------------

def _is_external(name: str) -> bool:
    n = name.lower()
    return n.startswith("external") or n.startswith("height_eur") or "_eur_" in n


def _split_num(name: str) -> Optional[int]:
    """The trailing instance index of a numbered external (external_GWAS_2 -> 2)."""
    m = re.search(r"_(\d+)(?:\.|$)", name)
    return int(m.group(1)) if m else None


def _which_split(name: str) -> Optional[str]:
    n = name.lower()
    if "_test" in n or "testing" in n:
        return "test"
    if "_val" in n or "valid" in n:
        return "val"
    if "_train" in n or "training" in n:
        return "train"
    return None


def advise(data_dir: str, prompt: str = "") -> Optional[str]:
    """Inspect the case's files and return a routing + role-mapping hint, or None."""
    try:
        files = sorted(
            f for f in os.listdir(data_dir)
            if f != "prompt.md" and os.path.isfile(os.path.join(data_dir, f))
        )
    except OSError:
        return None
    if not files:
        return None
    kind = {f: classify(os.path.join(data_dir, f)) for f in files}
    ext_anc, build = _extract_ancestry_build(prompt)

    roles: Dict[str, str] = {}
    # --- externals ------------------------------------------------------------
    ext_coef, ext_gwas, ext_geno, ext_map, ext_pheno = [], [], [], [], []
    for f in files:
        if not _is_external(f):
            continue
        k = kind[f]
        if k == _KIND_COEF:
            ext_coef.append(f)
        elif k == _KIND_GWAS:
            ext_gwas.append(f)
        elif k == _KIND_GENO:
            ext_geno.append(f)
        elif k == _KIND_MAP:
            ext_map.append(f)
        elif k == _KIND_PHENO:
            ext_pheno.append(f)

    # Raw EUR *cohort* to pool (external_X_k + external_y_k, both train-like) -> brier_full.
    ext_cohort = [f for f in ext_geno if _which_split(f) in (None, "train")
                  and any(_KIND_PHENO == kind[g] and _split_num(g) == _split_num(f)
                          and _which_split(g) in (None, "train") for g in ext_pheno)]

    # --- target ---------------------------------------------------------------
    tgt_geno_train = [f for f in files if not _is_external(f) and kind[f] == _KIND_GENO
                      and _which_split(f) in (None, "train")]
    tgt_sumstats = [f for f in files if not _is_external(f)
                    and kind[f] in (_KIND_SUMSTATS_CORR, _KIND_GWAS)]
    tgt_pheno_train = [f for f in files if not _is_external(f) and kind[f] == _KIND_PHENO
                       and _which_split(f) in (None, "train")]
    snp = [f for f in files if not _is_external(f) and kind[f] == _KIND_MAP]

    # --- module inference -----------------------------------------------------
    if ext_cohort:
        module = "brier_full"
    elif tgt_sumstats and not (tgt_geno_train and tgt_pheno_train):
        module = "brier_s"
    elif tgt_geno_train and tgt_pheno_train:
        module = "brier_i"
    elif tgt_sumstats:
        module = "brier_s"
    else:
        return None  # cannot infer -- stay silent rather than mislead

    # --- assign target roles --------------------------------------------------
    if module in ("brier_i", "brier_full"):
        if tgt_geno_train:
            roles["target_X_train"] = tgt_geno_train[0]
        if tgt_pheno_train:
            roles["target_y_train"] = tgt_pheno_train[0]
    if module == "brier_s":
        if tgt_sumstats:
            roles["target_sumstats"] = tgt_sumstats[0]
        # The LD reference panel is a genotype matrix that is NOT a val/test split: prefer
        # one named reference/panel, else any train-or-unsplit genotype (the summary case's
        # X_val/X_test genotypes are held-out splits, not the LD reference).
        geno = [f for f in files if not _is_external(f) and kind[f] == _KIND_GENO]
        ref = ([f for f in geno if "refer" in f.lower() or "panel" in f.lower()]
               or [f for f in geno if _which_split(f) not in ("val", "test")])
        if ref:
            roles["target_ld_panel"] = ref[0]
    if snp:
        roles["snp_info"] = snp[0]
    # target val/test splits (individual-level, for selection/evaluation)
    for split in ("val", "test"):
        gx = [f for f in files if not _is_external(f) and kind[f] == _KIND_GENO
              and _which_split(f) == split]
        gy = [f for f in files if not _is_external(f) and kind[f] == _KIND_PHENO
              and _which_split(f) == split]
        if gx:
            roles[f"target_X_{split}"] = gx[0]
        if gy:
            roles[f"target_y_{split}"] = gy[0]

    # --- assign external roles ------------------------------------------------
    external_kind = None
    if module == "brier_full":
        external_kind = "raw cohort (pooled)"
        for f in sorted(ext_cohort, key=lambda x: _split_num(x) or 1):
            k = _split_num(f) or 1
            roles[f"external_X_{k}"] = f
            yk = [g for g in ext_pheno if _split_num(g) == _split_num(f)]
            if yk:
                roles[f"external_y_{k}"] = yk[0]
    elif ext_coef:
        external_kind = "pretrained coefficient model(s)"
        if len(ext_coef) == 1:
            roles["external_coef"] = ext_coef[0]
        else:
            for f in sorted(ext_coef, key=lambda x: _split_num(x) or 0):
                k = _split_num(f) or (ext_coef.index(f) + 1)
                roles[f"external_coef_{k}"] = f
    elif ext_gwas:
        external_kind = "raw summary (GWAS) -- FIT internally"
        if len(ext_gwas) == 1:
            roles["external_sumstats"] = ext_gwas[0]
        else:
            for f in sorted(ext_gwas, key=lambda x: _split_num(x) or 0):
                k = _split_num(f) or (ext_gwas.index(f) + 1)
                roles[f"external_sumstats_{k}"] = f
        # a shared EUR reference panel + variant map for the internal fit
        ref = [f for f in ext_geno if _which_split(f) in (None, "train")]
        if ref:
            roles["external_ld_panel"] = ref[0]
        if ext_map:
            roles["external_snp_info"] = ext_map[0]
        # per-external held-out EUR split(s) to tune the internal fit
        exv_x = [f for f in ext_geno if _which_split(f) == "val"]
        exv_y = [f for f in ext_pheno if _which_split(f) == "val"]
        if len(ext_gwas) == 1:
            if exv_x:
                roles["external_X_val"] = exv_x[0]
            if exv_y:
                roles["external_y_val"] = exv_y[0]
        else:
            for f in exv_x:
                k = _split_num(f)
                if k:
                    roles[f"external_X_val_{k}"] = f
            for f in exv_y:
                k = _split_num(f)
                if k:
                    roles[f"external_y_val_{k}"] = f

    return _format_hint(module, external_kind, roles, ext_anc, build)


def _format_hint(module: str, external_kind: Optional[str], roles: Dict[str, str],
                 ext_anc: Optional[str], build: Optional[str]) -> str:
    tgt = "individual-level" if module in ("brier_i", "brier_full") else "summary-level"
    pool = " (two cohorts to POOL)" if module == "brier_full" else ""
    lines = [
        "DATA-ROLE ADVISOR (inferred from file content):",
        f"- Target is {tgt}{pool}; route prep_auto to shape=\"{module}\".",
    ]
    if external_kind:
        lines.append(f"- External is a {external_kind}.")
    lines.append("- Call prep_auto with shape=\"%s\" and these roles:" % module)
    for r in sorted(roles):
        lines.append(f'    {r}: "{roles[r]}"')
    extra = []
    if external_kind and "GWAS" in (external_kind or "") and ext_anc:
        extra.append(f'external_ld_ancestry="{ext_anc}"')
    if external_kind and "GWAS" in (external_kind or "") and build:
        extra.append(f'external_ld_build="{build}"')
    if module in ("brier_s", "brier_full") and "target_ld_panel" in roles:
        # the target LD is built from the reference panel; the target ancestry is AFR here
        extra.append('ld_ancestry="AFR"')
        if build:
            extra.append(f'ld_build="{build}"')
    if extra:
        lines.append("  plus: " + ", ".join(extra) + ".")
    return "\n".join(lines)
