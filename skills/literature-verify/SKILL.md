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
   4, 5 and 6), but always set `shares_probands_with: unknown` (it names finding ids and
   yours are local, so `diff` does not compare it), and write to `local/literature/<SYMBOL>.verify.yaml`, not to `curation/`.
   Create `local/literature/` if it is missing; `local/` is gitignored. The file has one
   top-level key, `findings:`, holding full findings. Ids are local to this file
   (`CHDA:LIT:0000001`, `CHDA:LIT:0000002`, ...); do not take them from the registry and
   do not add `Publication` records.
3. Leave `verified_by`, `approved_by` and `approved_on` unset. Set
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
4. Compare. This writes nothing and exits 0 even when findings differ:

   ```bash
   chd-curator diff <SYMBOL> --against local/literature/<SYMBOL>.verify.yaml
   ```

   It lists agreed findings, findings in one pass only, and each differing field with both
   values. Two findings pair when they share a PMID, a design and the same cohorts; a key
   carried twice in either pass is never paired and is listed as one-pass only. Summaries
   and locators are not compared; a curator reads them.
5. Mark the agreed findings. This sets `verified_by` only where it is unset, and only on
   findings with no field difference:

   ```bash
   chd-curator diff <SYMBOL> --against local/literature/<SYMBOL>.verify.yaml \
     --mark-verified agent:literature-verify/<model>
   ```
6. Report every difference and every one-pass-only finding to the curator, with both
   values. Do not edit the first pass to settle one.
7. Run `.venv/bin/chd-atlas validate`.

## Done when

- The `diff` output is summarised for the curator: counts agreed, differing and one-pass.
- `.venv/bin/chd-atlas validate` reports no error. Hand off: the curator resolves the
  differences, then runs `.venv/bin/chd-atlas literature review <SYMBOL>`.

## Never

- Never read the first pass's `findings` before your own file is written.
- Never mark a finding verified by hand or edit a differing field to make it agree.
- Never run `literature review --approve`, and never commit `local/`.
