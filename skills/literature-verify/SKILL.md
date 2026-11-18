---
name: literature-verify
description: Use when a gene's extracted findings need an independent blind second extraction compared against them, by a different model in a fresh context.
---

# literature-verify

Read the atlas checkout's `curator/curator-rules.md` first. Model tier: a model different from the one that
extracted, in a fresh context. The point is independence: you extract without seeing the
first pass.

## Inputs

- A gene symbol whose findings were written by `literature-extract`.
- The model name you run as, for `--mark-verified`. It must differ from the model that
  extracted; ask the curator which one that was rather than opening the findings.

## Steps

1. List the papers to extract:

   ```bash
   chd-curator abstracts <SYMBOL> --decision included
   ```

2. Extract each paper from scratch, as the `literature-extract` skill describes (steps 2,
   4, 5, 6, 7 and 8), but always set `shares_probands_with: unknown` (it names finding ids and
   yours are local, so `diff` does not compare it), and write to `local/literature/<SYMBOL>.verify.yaml`, not to `curation/`.
   Create `local/literature/` if it is missing; `local/` is gitignored. The file has one
   top-level key, `findings:`, holding full findings. Ids are local to this file
   (`CHDA:LIT:0000001`, `CHDA:LIT:0000002`, ...); do not take them from the registry and
   do not add `Publication` records.
3. Extract the variants blind, by `literature-extract` step 5 and its rules: one entry per
   distinct variant per genotype context, `as_reported` verbatim, each name in `reported`
   as written, and never choose between the paper's own names. Write `not_stated` only when
   the paper is silent and `unclear` with its reason only when the paper says something one
   value cannot hold (`literature-extract` step 6); a `pending_review` mark carries
   `review_settled_by`. Leave `hgvs_c`, `grch38` and `normalisation` unset, and `hgvs_p` on a
   `mane` variant: both passes are compared before `chd-curator normalise` places anything.
   A `protein` variant carries `hgvs_p` as `literature-extract` step 5 writes it. Place each
   variant by where the paper puts it, as step 5 rules: position beats naming (a variant the
   paper places outside the exons is `outside_region` whatever names it), a `c.` name printed
   without `c.` is `mane`, and a notation-only fix goes in `reported` with `as_reported` kept
   as printed. Never copy a placement or a name from the first pass.
4. Leave `verified_by`, `approved_by` and `approved_on` unset. Set
   `extracted_by: agent:literature-verify/<model>`.

   ```yaml
   findings:
     - id: CHDA:LIT:0000001
       publication: PMID:10000001
       design: case_report
       methods: [targeted_sequencing]
       count_unit: probands
       screened: 1
       carriers: {de_novo: 1, inherited: 0, origin_unknown: 0}
       variant_classes: [snv_indel]
       variants:
         - id: 1
           as_reported: c.835C>T (p.R279X)
           locator: {kind: in_text, element: Table 1}
           kind: snv_indel
           reported: {c: c.835C>T, p: p.R279X}
           placement: mane
           zygosity: heterozygous
           carriers: {de_novo: 1, inherited: 0, origin_unknown: 0}
           phenotypes: [HP:0001631]
       phenotypes: [HP:0001631]
       lesion_groups: [septal]
       syndromic: isolated
       shares_probands_with: unknown
       locators:
         - kind: in_text
           element: Table 1
       summary: Illustrative text.
       extracted_by: agent:literature-verify/<model>
   ```
5. Compare. This writes nothing and exits 0 even when findings differ:

   ```bash
   chd-curator diff <SYMBOL> --against local/literature/<SYMBOL>.verify.yaml
   ```

   It lists agreed findings, findings in one pass only, and each differing field with both
   values. Two findings pair when they share a PMID, a design and the same cohorts; a key
   carried twice in either pass is never paired and is listed as one-pass only. Summaries
   and locators are not compared; a curator reads them. A variant is compared on `as_reported` (character for
   character), `reported`, `placement`, `kind`, `hgvs_c`, `hgvs_p`, `grch38`, `zygosity`, `phase`, `carriers` and
   `phenotypes`; not on its `id`, `with`, `normalisation`, `locator`, `unclear` entries or
   review mark.
6. Mark the agreed findings. This sets `verified_by` only where it is unset, and only on
   findings with no field difference:

   ```bash
   chd-curator diff <SYMBOL> --against local/literature/<SYMBOL>.verify.yaml \
     --mark-verified agent:literature-verify/<model>
   ```
7. Report every difference and every one-pass-only finding to the curator, with both
   values. Do not edit the first pass to settle one.

## Done when

- The verify file is written, `diff` has run, the agreeing findings are marked, and every
  difference and one-pass-only finding is reported to the curator with both values.
- Hand off: the curator resolves the differences, runs `chd-curator normalise <SYMBOL>` and
  `.venv/bin/chd-atlas validate`, then `.venv/bin/chd-atlas literature review <SYMBOL>`.

## Never

- Never read the first pass's `findings` before your own file is written.
- Never mark a finding verified by hand or edit a differing field to make it agree.
- Never run `literature review --approve`, and never commit `local/`.
