# AGENTS.md - working guide for agentic BRIER analysis

This file guides an agent through a BRIER transfer-learning analysis on data
that lives on a remote server. It is a **template/example**: copy it into your
own preprocessing project, adjust the project-specific notes at the bottom, and
remove anything that does not apply.

`AGENTS.md` is the cross-tool convention for agent instructions: OpenAI Codex
reads it automatically at session start, as do several other agents. Claude Code
does not read `AGENTS.md` directly, but a one-line `CLAUDE.md` that imports this
file (`@AGENTS.md`) gives Claude Code the same auto-loaded guidance. A
`CLAUDE.md.example` shim shipped alongside this file does exactly that. The
content below is agent-agnostic; it works the same whichever client drives the
tools.

It assumes the BRIER MCP server is registered and connected (so `brier` tools
are available), and that a containment profile is in place (so destructive and
network actions are gated). See SETUP.md, REMOTE.md, and CONTAINMENT.md in this
repo.

This document is *guidance*, not enforcement. It shapes how the agent works; the
permission profile is what actually blocks dangerous actions. The two are
complementary: this sets the rhythm, the profile sets the walls.

---

## Core working principles (read first)

1. **Work one stage at a time. Never chain multiple analysis stages into one
   script or one run.** A single monolithic script that inspects, cleans,
   preprocesses, fits, and reports in one shot is the most common failure mode:
   a bug in any stage wastes the whole run and is hard to localize. Do one
   stage, save its output, show a sanity summary, and STOP for review.

2. **STOP and wait for the user at every checkpoint marked below.** "Stop" means
   present what you found or produced, state your proposed next step, and wait
   for the user to confirm before continuing. Do not proceed past a checkpoint on
   your own.

3. **Ask when the data is genuinely ambiguous.** Real data is often unlabeled or
   contains distractor files. If you are unsure which file is the target, what a
   file's role is, or which approach fits, ASK rather than guessing. Stating a
   confident wrong assumption is worse than asking.

4. **Tool policy: `prep_auto` first; improvise only where it genuinely cannot
   reach, and then match its contract exactly.** The STANDARD preprocessing path
   is one call to `prep_auto` (see Phase 2 for what it now does -- it is a lot
   more than it used to be). Improvisation is allowed, but it is a LAST resort
   and it is BOUNDED, in this order:

   1. **`prep_auto`** -- one call. Handles matching, allele flips, strand
      ambiguity, `corr` derivation, impute-0 alignment, LD construction from a
      reference panel, FITTING a raw external, standardization, the intercept
      row, and the val/test splits.
   2. **`prep_data`** -- composable ops on a cached session
      (`alias_root`, `derive_corr_from_pvalue`, `harmonize_alleles`,
      `subset_to_common_snps`, `verify_aligned`, `reshape_to_matrix`,
      `assemble`, `persist`) for wrangling `prep_auto` does not cover. Its
      `persist` returns the SAME `{prepared_path, expr_hints}` contract, so the
      fitter consumes it identically.
   3. **Raw R, outside the tools** -- only if neither of the above can express the
      step. The MCP server NEVER executes free-typed R: it will not run your
      script. So this means you write and run R yourself (if your client has a
      shell), and hand the fitter an `.rds` via `data_path` + `*_expr`.

   **Whatever route you take, the OUTPUT CONTRACT is the same** -- see "The
   prepared-object contract" below. The fitters do not care how the object was
   built; they care that it is correct. A hand-rolled object that violates the
   contract (a BRIERs `beta.external` carrying an intercept row, a raw-scale `X`
   against a standardized external, an intersected panel where alignment was
   required) fails SILENTLY: no error, just wrong numbers.

   If you find yourself needing raw R for a step that sounds standard (deriving
   `corr`, flipping alleles, building an LD, fitting an external), STOP -- a tool
   almost certainly covers it, and a hand-rolled formula is a silent-error risk.
   The validated fitting/evaluation tools (`brier_i`, `brier_s`, `brier_full`,
   the selection tools, `brier_evaluate`, `score_external_prs`, `summarize_fit`)
   should ALWAYS be used; never hand-compute a fit or a metric.

5. **Inspecting all files needs the right surface for each type.** Use
   `inspect_data` for R data objects (`.rds`, `.rda`, `.RData`) and
   `inspect_user_data` for tabular and text files (`.csv`, `.tsv`, `.txt`, and
   their gzipped `.gz` forms) as well as genotype binaries (`.pgen/.psam/.pvar`,
   `.bed/.bim/.fam`, `.sample`, inspected via their companion files). Use
   `list_data_directory` to enumerate a folder. Prefer `inspect_user_data` for
   anything that is not a single R-binary object; if unsure which inspector
   applies, prefer `inspect_user_data`. Fall back to raw `ls`/`head`/`wc` only
   for file types the inspectors do not cover, and do not assume the MCP listing
   is the complete directory; reconcile with `ls` so no file is overlooked.

---

## How to measure performance (referenced by Phases 3, 4, 5)

Whenever performance is evaluated (a single fit, a baseline comparison, or the
final report), judge it **comprehensively with a set of complementary metrics
matched to the outcome type**, never a single number. A single metric can
mislead; complementary metrics catch different failure modes (calibration vs.
discrimination).

- **Continuous (gaussian):** `gaussian.rsq` (R^2) **and** `gaussian.mspe` (MSPE).
- **Binary (binomial):** `binomial.auc` (AUC) **and** `binomial.dev` (deviance).
- **Count (poisson):** `poisson.dev` (deviance) -- its primary metric.

Call `brier_evaluate` (or `score_external_prs`) ONCE PER METRIC; never
hand-compute one.

**Read the two metrics differently, because they are not the same kind of
number.** `gaussian.rsq` is `cor^2`: SCALE-INVARIANT, so it is unchanged if y is
rescaled. `gaussian.mspe` is `mean((y - pred)^2)`: SCALE-DEPENDENT. When the
outcome is standardized (which it is whenever `standardize=TRUE` for a Gaussian
fit), **MSPE = 1.0 IS the null model** -- so an MSPE of 0.995 means the model
explains about half a percent of the variance, however impressive its R^2 may look
in isolation. Reporting both is what makes that visible.

**When metrics disagree** about whether something "improved" (e.g. R^2 says an
external helps but MSPE says it hurts), do NOT silently pick one or average them
away. Report all of them, state the disagreement explicitly, and let the user
judge. This is consistent with the propose-not-decide stance of the decision
layer.

---

## Phase 1: Inspect and understand  [STOP at end]

Goal: know exactly what data is present and what each file is for, before
deciding anything.

- List the data directory with both `ls` (all files) and the `brier`
  inspection tools: `inspect_data` for R objects, `inspect_user_data` for
  tabular/text/gz files and genotype binaries. Reconcile the two so no file is
  overlooked.
- For each file, determine: what it is (individual-level genotypes? phenotype?
  summary statistics? pretrained coefficient vector? LD reference?), and its
  likely role in a BRIER analysis.
- Identify the **target** and any **external** information. Note explicitly
  whether individual-level phenotype data exists, because that determines which
  modules are even possible (see Phase 3).
- Watch for **distractor / ambiguous files**: more than one candidate target,
  files whose role is unclear, or sources that may overlap with the target
  sample (which would violate independence if used as an external).
- **If PRS is the goal and published external weights could help:** propose
  searching for usable published weights (e.g. the PGS Catalog) and ASK before
  fetching anything. Note: fetching from the web is gated by the containment
  profile (network egress is denied by default), so this step needs explicit
  user approval, and the fetch may need to be done by the user rather than the
  agent.

**STOP.** Present: a table of files and their roles; which is the target; what
external info is available; any ambiguities or distractors; and a proposed
analysis approach with reasoning. Ask the user to confirm or correct before
proceeding. Do not start cleaning until the user approves.

---

## Phase 2: Clean and preprocess, one stage at a time  [STOP between stages]

Goal: turn the raw inputs into a canonical, aligned, fit-ready analysis set.

### The standard path: one `prep_auto` call

Give it the target `shape` (`brier_i` / `brier_s` / `brier_full`), the `data_dir`,
and a `roles` map of logical role -> filename. It does all of this in ONE call:

- **Matches** each external to the target -- by coordinate (CHR/BP/REF/ALT) when
  the variant map carries them, else by predictor NAME.
- **Corrects allele flips** (sign-flips the coefficient / `corr`), and **RESOLVES
  strand-ambiguous palindromes** (A/T, C/G) by allele frequency instead of
  dropping them. For a palindrome the allele LETTERS carry no orientation
  information, so the rule is: negate iff AF is nearer `1 - AF_ref` than
  `AF_ref`, by a margin. (Older BRIER preprocessing dropped every palindrome
  unconditionally -- about 15% of common variants.)
- **Aligns the external TO the target, not by intersection.** Every target
  predictor is kept (the target panel defines `p`); a target predictor the
  external does not cover gets coefficient **0**; an external-only predictor is
  **dropped**. This is deliberate: for pretrained-coefficient transfer, a missing
  external coefficient just means no transfer contribution, so intersecting would
  throw away target signal for nothing. **`brier_full` is the exception** --
  pooling raw genotypes cannot impute a missing genotype, so it takes the
  INTERSECTION of the cohorts.
- **Derives `corr`** for a summary target whose GWAS ships none (from p, N, and
  the sign of the effect).
- **Builds the LD** when only a reference panel is supplied: pass
  `target_ld_panel` + `ld_ancestry` (AFR/EUR/EAS) + `ld_build` (hg19/hg38) and it
  constructs the block-wise sparse LD. Do NOT orchestrate `get_ldb` -> `cal_ld`
  yourself. **Confirm the ancestry and build with the user**: neither is inferable
  from the data (a BP position looks identical in either build), and the wrong one
  silently produces an LD with the right dimensions, the right rownames, and the
  wrong contents.
- **FITS a raw external.** If the external is raw data rather than a pretrained
  coefficient vector, prep_auto fits it (the matching fitter at eta=0, on the
  external's own data) and integrates the result. You do NOT fit the external
  yourself.
    - summary external: `external_sumstats` + `external_ld_panel` +
      `external_snp_info`, with `external_ld_ancestry` / `external_ld_build`.
    - individual external: `external_X` + `external_y` (+ optional
      `external_X_val` / `external_y_val`, which tune it on held-out data instead
      of an information criterion).
    - several externals: number the roles (`_1`, `_2`, ...). Each becomes one
      coefficient column.
- **Standardizes conditionally**, adds the BRIERi intercept row, subsets X / the
  LD to the surviving predictors, and aligns the val/test splits to the TRAINING
  scale.

Decisions that stay with you: `standardize` (TRUE whenever the target must match a
standardized-scale external -- a pretrained one usually is, a fitted one always
is), `standardize_method` (`sd` / `maf`), `outcome_family` (only Gaussian y is
standardized). `predictor_type` is DETECTED (a map with CHR + BP is a genome);
set it to `generic` for non-genotype predictors -- they match by name, have no LD
blocks, and their "LD" is a plain correlation, so omit ancestry/build.

Read the returned `report` (every step it performed) as your sanity summary, and
`external_fits` (nonzero coefficients and selection criterion for each externally
fitted model) to confirm a fitted external is not degenerate. **An all-zero
external makes the transfer a silent no-op**; prep_auto hard-errors on one rather
than passing it on, and a near-zero one (a handful of nonzero coefficients) is a
DATA/SIZE signal, not something to work around by swapping selection criteria.

### If `prep_auto` cannot do it

Fall back to `prep_data` (composable ops, `persist` at the end), and only then to
raw R outside the tools. Whatever you do, the result MUST satisfy the contract
below -- the fitters consume every route identically, and a violation fails
silently.

**Note a divergence if you hand-wrangle:** `prep_data`'s `harmonize_alleles`
DROPS strand-ambiguous variants by default, whereas `prep_auto` RESOLVES them by
allele frequency. If you improvise via `prep_data`, you are not reproducing
`prep_auto`'s semantics; say so in your summary rather than letting the difference
pass unremarked.

**Write outputs to a separate writable directory, never the raw-data directory.**
After each sub-stage: save, summarize, STOP.

---

## The prepared-object contract

Every route -- `prep_auto`, `prep_data` + `persist`, or hand-rolled R -- must end
in ONE object the fitter can consume. `prep_auto` and `prep_data`'s `persist`
return `{prepared_path, expr_hints}`; the fitter loads `data_path` and evaluates
the `*_expr` arguments against it. If you build the object yourself, you must
satisfy the same conventions.

The fitter now CHECKS them, whoever built the object, and REFUSES with the violated
clause named: predictor alignment (names must exist and match in ORDER, not merely in
count), the beta shape per shape, a non-degenerate external, a sparse LD, and scale
consistency between the fit and the splits it is evaluated on. One thing it deliberately
CANNOT check is allele ORIENTATION: a flipped panel matches by name, fits, and reports a
number with every sign inverted, so if you align an external yourself, that correctness is
yours to own.

Two things that check cannot do for you. It cannot verify ALLELE ORIENTATION: a flipped
panel matches by name, fits, and reports a number with every coefficient's SIGN
inverted. And it cannot make a degenerate fit meaningful: an all-zero external is a DATA
signal (too few samples, no validation split), never something to fix by swapping the
selection criterion.

| shape | required members | convention |
|---|---|---|
| `brier_i` | `X` (n x p), `y` (n), `beta_external` | `beta_external` is **(p+1) x M**: the FIRST ROW is the intercept slot (0 when the external has none), then one column per external. |
| `brier_s` | `sumstats` (with a `corr` column), `XtX`, `beta_external` | `beta_external` is **p x M with NO intercept row**. `XtX` is a **sparse** p x p LD on the surviving panel. |
| `brier_full` | `X` (stacked), `y` (stacked), `cohort` | Cohorts are row-bound and labelled by a cohort indicator (target = 0). The panel is the **INTERSECTION** of the cohorts. |

The asymmetry between `brier_i` (intercept row) and `brier_s` (no intercept row)
is a documented silent-failure trap: get it wrong and the fit runs and returns
numbers.

Also invariant across routes:

- **The panel**: for `brier_i`/`brier_s`, the surviving predictors are the
  TARGET's, with 0 imputed where an external does not cover one. Not the
  intersection.
- **The scale**: if the external is on the standardized scale, `X` must be too --
  and the val/test splits must be standardized with the TRAINING constants, not
  their own.
- **`y` for a Gaussian outcome** is standardized alongside `X`; a binary or
  Poisson `y` never is.
- **Order**: `beta_external`, the `X` columns, and the `XtX` rows/columns are all
  in the same panel order.

---

## Phase 3: Fit  [STOP at end]

Goal: fit the model with the `brier` tools (never hand-rolled R).

**Choose the module from what the data IS, not what is convenient:**

- `brier_i` -- an individual-level target (genotypes **and** a phenotype) plus a
  pretrained external coefficient model.
- `brier_s` -- a **summary-statistics** target (a GWAS) plus an LD matrix. A
  genotype matrix in a summary case is an **LD reference panel**, not
  individual-level target data: a summary case has no target phenotype. Do not
  invent one.
- `brier_full` -- **pooled** raw individual-level cohorts (target + externals),
  labelled by a cohort indicator. It requires >= 2 cohorts, so it cannot fit a
  target-only baseline; that baseline is a separate single-cohort `brier_i`.

A **summary EXTERNAL does not make the TARGET summary-level**, and several
external cohorts do not mean `brier_full` -- each external is fit into a
coefficient column and the target keeps its own shape.

**The eta grid: OMIT `eta_list`.** The fit tools build a principled log-spaced
grid that already contains `eta = 0` (the no-transfer baseline), so you get the
baseline for free. A hand-written grid is arbitrary AND it overrides
`eta_ceiling`, which disables the escalation below. Pass `eta_list` only to pin a
single fixed eta (`[0]` for a baseline or an external-only comparator).

**Eta-ceiling escalation is not optional.** If a `*_selection` result carries
`_notice_eta_boundary`, the selected eta sits at the TOP of the grid. That is a
BOUNDARY, not an optimum: the best eta lies outside the grid, the model is
truncated, and its test numbers are not the model's. Do NOT report it. Refit the
same model with `eta_ceiling` raised (about 5x the current top, still no
`eta_list`) and select again.

**`multi_method` (M > 1 externals) defaults to `auto`**, resolved from M: `ind`
up to M = 2, `stacking` from M = 3. `ind` tunes one eta PER SOURCE (so it can lean
on a strong external and ignore a weak one) but its grid is n^M and does not
scale; `stacking` collapses the sources into one predictor first and cannot weight
them separately. An explicit value always wins. The value actually used comes back
as `multi_method_used`.

**Penalty knobs: OMIT them** (`alpha`, `penalty`, `gamma`, `penalty_factor_expr`)
unless the user explicitly asks. The default is LASSO / `alpha = 1` / all
predictors penalized. Set them only on request: `penalty="MCP"`/`"SCAD"` (+
`gamma`), `alpha` in (0, 1] for elastic net, `penalty_factor_expr` (a length-p 0/1
vector, 0 = an unpenalized covariate).

**Selection.** With a validation split, select on it with the family-default
criterion (`gaussian.mspe` / `binomial.dev` / `poisson.dev`). With none, use an
information criterion (`BIC` for the individual-level shapes; `GIC`/`Cp` for the
summary shape -- BRIERs does not accept `BIC`). Never select on the test split.
**`BRIERfull` accepts ONLY validation-set criteria**, never an IC.

**STOP.** Present: the selected eta/lambda, the number of nonzero weights, any
`_notice_*` the tools emitted, and performance per "How to measure performance".

---

## Phase 4: Decision / evaluation layer  [STOP - propose, do not decide alone]

Goal: empirically check that the chosen approach and external sources are
actually helping, and propose adjustments. **This layer PROPOSES; the user
approves any exclusion or module switch.**

Compute explicit baselines so recommendations rest on evidence, not assumption.
Evaluate every baseline and the integrated fit using the full metric set in
"How to measure performance" above (matched to the outcome type), not a single
metric:

1. **Local-alone** baseline: the target data fit with no external information
   (a fit with eta=0).
2. **External-alone** baseline: each external model's performance on its own,
   evaluated on the target test set. HOW depends on the external's form:
   - Pretrained COEFFICIENTS (`brier_i` / `brier_s`): score each external
     coefficient vector directly on the target test set with
     `score_external_prs` (no fitting). With M>1 externals, score each column
     separately and report one metric per external.
   - RAW individual-level cohorts (`brier_full`): there is NO coefficient vector,
     so FIT an external-only model, one per external cohort: fit at eta=0 on that
     cohort's own data (`brier_i` with the per-cohort `X_ext_k`/`y_ext_k` and the
     zero `beta_external` that prep_auto exposes), select lambda by an
     information criterion, and evaluate on the target test set.
3. **Integrated**: the BRIER fit that borrows from the external(s).

Then evaluate, and propose (do not unilaterally act):

- **Per-external evaluation (each external on its own).** Evaluate each external
  source individually: fit the integrated model with the target plus that ONE
  external alone (target + ext1, then separately target + ext2, then separately
  target + ext3, and so on). Do NOT add them cumulatively (ext1, then ext1+ext2,
  then ext1+ext2+ext3); evaluate each in isolation so each external's own
  contribution is measured against the local-alone and external-alone baselines.
  An external that **degrades** performance relative to local-alone is a
  negative-transfer source and should be flagged for exclusion.
- **Propose the combined set.** Based on the per-external results, propose which
  externals to keep (the ones that individually help) and which to drop (the ones
  that individually hurt), and propose a final integrated fit on the kept set.
  Present the per-external contribution table and your recommendation; let the
  user approve the final set before fitting it.
- **Module reconsideration.** If the integrated fit does not beat the
  external-alone baseline (i.e. borrowing is not buying anything), consider
  whether a different module would do better. For example, if `brier_i` beats
  local-alone but not external-alone, deriving summary statistics from the local
  data and switching to `brier_s` may perform better. **Propose** the switch with
  the comparative evidence; do not switch modules without user approval, since it
  is a substantive methodological change.

Use the full metric set from "How to measure performance" for every comparison,
and surface any disagreement between metrics explicitly rather than collapsing it
to one number.

**STOP.** Present the baseline table, per-external contributions, and any
proposed exclusions or module switch, with the evidence. Wait for the user to
approve before finalizing.

---

## Phase 5: Report

Goal: produce the final report only after the approach is settled.

- Use `summarize_fit` to generate the report (and its reproduce script).
- Summarize: the data, the chosen module and why, which externals were kept or
  dropped and on what evidence, the baselines, and the final selected model.
  Report performance with the full metric set from "How to measure performance"
  (matched to the outcome type), not a single number.
- Make the methodological decisions and their justifications explicit, so the
  analysis is reproducible and the reasoning is auditable.

---

## Project-specific notes (fill in per project)

- Raw-data directory (read-only): `FILL IN`
- Working/output directory (writable): `FILL IN`
- Target: `FILL IN`
- Known externals: `FILL IN`
- Anything to ignore (distractors, overlapping sources): `FILL IN`
- Outcome type for this project (continuous / binary / count), which sets the
  metric panel per "How to measure performance": `FILL IN`
