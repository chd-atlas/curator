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
5. Record the variants the paper names under `variants:` on the finding. Run
   `chd-atlas literature frame <SYMBOL>` first: it prints the MANE Select transcript and
   protein a name is placed on.
   - Write one entry per distinct variant per genotype context. A context is the partners'
     row keys (their `as_reported` when unkeyed) plus `phase`: one variant in two contexts is
     two entries, in one context one entry (validate refuses a repeat).
   - `as_reported` is verbatim, the paper's own text, with a `locator`; `kind` is among the
     finding's `variant_classes`. Put each name the paper gives into `reported` (`c`, `p`,
     `rsid`, `transcript` only when the paper states one) as written, and never choose
     between the paper's own names, even when they disagree: `chd-curator normalise` checks
     them.
   - `placement`: `mane` when the paper gives a `c.` name; `protein` for a `p.` name alone
     (then set `hgvs_p`); `structural` for a CNV or chromosomal change; `outside_region` for
     an rsID or a position beyond the exons +/-50 bp. Leave `hgvs_c`, `hgvs_p`, `grch38` and
     `normalisation` of a `mane` variant to `normalise` (step 13).
   - `carriers` per variant, in the finding's `count_unit` (refused on a
     `common_variant_association`; the total may not exceed the finding's, a `with` group
     counted once). Name no variant the paper gives no carrier for.
   - `zygosity` as stated. A variant carried with another variant in this gene lists their
     ids in `with`; links are mutual, each is `heterozygous`, and `phase` (`trans`, `cis`,
     `not_stated`, `unclear`) is set exactly when `with` is. A `with` group whose phase is
     `unclear` carries one identical `unclear.phase` entry on every member.
   - `phenotypes` only where the paper ties a defect to these carriers, each among the
     finding's.
6. Choose among three ways to leave a value plain. `not_stated` only when the paper is
   silent about it. `unclear` with its reason when the paper says something one value
   cannot hold (heterozygous in 2 carriers, homozygous in 1) or two incompatible things:
   set the field to `unclear` and give `unclear.<field>` a `reason` and a `settled_by`. A
   review mark (step 9) is a question about the record as a whole; ask whether it would go
   away if this one field had a value the atlas can hold, and if so use `unclear`. Name
   what would settle it, in every `settled_by` and every `review_settled_by`:
   - `full_text`: only the abstract was read;
   - `authors`: the paper contradicts itself, or disagrees with the reference sequence;
   - `expert`: it needs a domain judgement the paper cannot supply;
   - `atlas_model`: the paper is clear and the atlas's record cannot hold it.
7. Add a `locators` entry for every number (`kind: in_text` with `element`, or
   `kind: supplementary` with `file`, `sheet`, `row`). Set `cohorts` from
   `curation/cohorts.yaml`, and `shares_probands_with` to the ids of findings that share
   probands with this one. `[]` asserts that no proband is shared with any other
   finding; write it only where the paper states its cohort is new. Otherwise write
   `unknown`. The curator checks independence by reading this
   field in the `review` listing.
8. A paper you cannot read stays in: record what its abstract supports, with locator
   element `Abstract`, `review_status: not_accessible` and a `review_note` naming what was
   not read. If it supports nothing, the curator excludes it as `full_text_not_accessible`.
9. Where the paper disagrees with a source the finding rests on, set
   `review_status: pending_review`, state only the measured disagreement in
   `review_note` and name what would settle it. Write the mark on a finding already in
   the file with
   `chd-atlas literature review <SYMBOL> --pending-review <id> --note "<...>" --settled-by <full_text|authors|expert|atlas_model>`;
   `--settled-by` is required. `not_accessible` takes no settler.
10. Set `extracted_by: agent:literature-extract/<model>`. Leave `verified_by`,
   `approved_by` and `approved_on` unset.
11. Add each paper's record the first time it gets a finding, reading the study type from
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
12. Run `.venv/bin/chd-atlas validate`. Before `normalise` the only errors allowed are "a mane
    variant needs hgvs_c", "a mane variant needs grch38" and "a mane variant needs
    normalisation"; fix every other error. Then hand off to verification.
13. After verification, once `chd-curator diff <SYMBOL> --against ... --mark-verified ...` has
    marked the agreeing findings, run `chd-curator normalise <SYMBOL>`, then validate again:
    no error may remain. `normalise` places each `mane` variant and fills `hgvs_c`, `hgvs_p`,
    `grch38` and `normalisation`; one it cannot place becomes `unplaced` with a
    `pending_review` mark it writes, never edited by hand. Never write `hgvs_c` or `grch38`
    yourself, nor `hgvs_p` on a `mane` variant; a `protein` variant holds its own `hgvs_p`.

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
    locators: [{kind: in_text, element: Table 1}]
    summary: One proband with an atrial septal defect carried a de novo nonsense variant.
    extracted_by: agent:literature-extract/<model>
```

Before `normalise` runs a `mane` variant has no `hgvs_c`, `grch38` or `normalisation`, as
above. Carriers differing in zygosity, recorded `unclear`:

```yaml
findings:
  - id: CHDA:LIT:0000002
    publication: PMID:10000002
    design: cohort_screen
    methods: [targeted_sequencing]
    count_unit: probands
    screened: 40
    carriers: {de_novo: 0, inherited: 0, origin_unknown: 3}
    variant_classes: [snv_indel]
    variants:
      - id: 1
        as_reported: c.512A>G (p.N171S)
        locator: {kind: in_text, element: Table 2}
        kind: snv_indel
        reported: {c: c.512A>G, p: p.N171S}
        placement: mane
        zygosity: unclear
        unclear:
          zygosity:
            reason: Table 2 lists two carriers as heterozygous and one as homozygous; the atlas records one zygosity per variant.
            settled_by: atlas_model
        carriers: {de_novo: 0, inherited: 0, origin_unknown: 3}
    phenotypes: [HP:0001631]
    lesion_groups: [septal]
    syndromic: isolated
    shares_probands_with: unknown
    locators: [{kind: in_text, element: Table 2}]
    summary: Three of 40 probands carried one missense variant, two heterozygous and one homozygous.
    extracted_by: agent:literature-extract/<model>
```

## Done when

- Every included paper has a finding and a Publication record: validate reports no
  `LIT003` or `LIT011`, and after verification `normalise` then validate report no variant issue.
- `.venv/bin/chd-atlas validate` reports no error. Hand off to the `literature-verify`
  skill, then the curator runs `.venv/bin/chd-atlas literature review <SYMBOL>`.

## Never

- Never state more than the paper does, or a number without its locator and unit.
- Never re-classify a variant or call a gene definitive.
- Never fill `verified_by` or `approved_by`, or run `literature review --approve`.
- Never write `not_stated` for a value the paper states but one value cannot hold.
- Never invent an id outside the counter, a PMID, a cohort or an HPO term.
