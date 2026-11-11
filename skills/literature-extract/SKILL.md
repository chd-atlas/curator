---
name: literature-extract
description: Use when a gene's included papers need findings written, one per distinct result, each number with a locator, and a Publication record added.
---

# literature-extract

Read the atlas checkout's `curator/curator-rules.md` first. Model tier: a mid-tier model (for example Sonnet).

## Inputs

- A gene symbol with no `pending` triage entries (the `literature-triage` skill is done).
- The model name you run as, for `extracted_by`.

## Steps

1. List the papers to extract:

   ```bash
   chd-curator abstracts <SYMBOL> --decision included
   ```

   Read each paper in full where PubMed Central or Europe PMC has open-access text, and any
   erratum triage excluded for it. Extract only its own variant results, none it cites.
2. For each paper write one finding per distinct result under `findings:` in
   `curation/literature/<SYMBOL>.yaml`. Two designs, or two separate cohorts, are two
   findings. Choose the design (`case_report`, `family_segregation`, `cohort_screen`,
   `rare_variant_association`, `common_variant_association`) by what the study observed.
3. Take the id: read the `LIT` counter under `prefixes` in `curation/.id_registry.yaml`,
   use the next ordinal as `CHDA:LIT:<7 digits>`, and set the counter to the last ordinal
   you used, in the same edit.
4. Fill the counts by design. Rare-variant designs need `count_unit` (`probands` or
   `families`), `screened`, `carriers` (`de_novo`, `inherited`, `origin_unknown`) and
   `variant_classes`. Carriers are counted in `count_unit`: with `probands` a carrier is
   a person; with `families`, `screened` is families and each origin count is carrier
   families, so put how many relatives carry the variant in the `summary`, with a
   locator. A `rare_variant_association` also needs a `comparator`
   (`control_cohort` or `mutation_model`); a case series is a `cohort_screen`. A
   `common_variant_association` needs `cases` and `controls`, takes `p_value` and
   `odds_ratio` when reported, and has no carrier counts. If the study's rows are already
   in `mirrors/burden.tsv`, set `burden_study` to its PMID.
5. Add a `locators` entry for every number (`kind: in_text` with `element`, or
   `kind: supplementary` with `file`, `sheet`, `row`). Set `cohorts` from
   `curation/cohorts.yaml`, and `shares_probands_with` to the ids of findings that share
   probands with this one. `[]` asserts that no proband is shared with any other
   finding; write it only where the paper states its cohort is new. Otherwise write
   `unknown`. The curator checks independence by reading this
   field in the `review` listing.
6. A paper you cannot read stays in: record what its abstract supports, with locator
   element `Abstract`, `review_status: not_accessible` and a `review_note` naming what was
   not read. If it supports nothing, the curator excludes it as `full_text_not_accessible`.
7. Where the paper disagrees with a source the finding rests on, set
   `review_status: pending_review` and state only the measured disagreement in
   `review_note`.
8. Set `extracted_by: agent:literature-extract/<model>`. Leave `verified_by`,
   `approved_by` and `approved_on` unset.
9. Add each paper's record the first time it gets a finding, reading the study type from
   the paper's first finding and the paper itself:

   | Design | StudyType |
   |---|---|
   | `case_report` | `case_report` |
   | `family_segregation` | `family_linkage` |
   | `cohort_screen` | `cohort_sequencing` |
   | `rare_variant_association` with `control_cohort` | `case_control` |
   | `rare_variant_association` with `mutation_model` and `exome` | `trio_wes` |
   | `rare_variant_association` with `mutation_model` and `genome` | `trio_wgs` |
   | `rare_variant_association` with `mutation_model` and any other method | ask the curator; no StudyType is guessed |
   | `common_variant_association` with `genotyping_array` | `gwas` |
   | `common_variant_association` otherwise | `candidate_variant` |

   ```bash
   chd-curator add-publication <PMID> --study-type <StudyType> --reading "<what the paper says about its design>"
   ```

   `--reading` is required and is written as a comment beside the record. The record
   stays out of `publications.json` while findings cite it and none is approved
   (a record no finding cites publishes), so say in your hand-off which `study_type` you chose and why: the curator checks it and
   `own_lab` (the default `false`; never guess it) in the `review` listing. Exit 1 means
   the PMID is already in `curation/publications.yaml` (nothing to add), PubMed failed or
   has no record of it, or the file is missing; exit 2, a bad argument or a schema refusal; the error says which.
10. Run `.venv/bin/chd-atlas validate` and fix every error it names.

An illustrative finding (every value is made up), in the layout the tooling writes
(entries at indent 2, fields at 4, nested lists at 6):

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
    summary: One proband with an atrial septal defect carried a de novo missense variant.
    extracted_by: agent:literature-extract/<model>
```

## Done when

- Every included paper has a finding and a Publication record: validate reports no
  `LIT003` or `LIT011`.
- `.venv/bin/chd-atlas validate` reports no error. Hand off to the `literature-verify`
  skill, then the curator runs `.venv/bin/chd-atlas literature review <SYMBOL>`.

## Never

- Never state more than the paper does, or a number without its locator and unit.
- Never re-classify a variant or call a gene definitive.
- Never fill `verified_by` or `approved_by`, or run `literature review --approve`.
- Never invent an id outside the counter, a PMID, a cohort or an HPO term.
