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
   `chd-atlas literature frame <SYMBOL>` first: it prints the MANE Select transcript and protein.
   - Write one entry per distinct variant per genotype context (the partners it is carried
     with, by their `as_reported`, and the `phase`): one variant in two contexts is two entries.
   - `as_reported` is verbatim: the text at the locator; where the paper prints `c.` and `p.`
     in separate columns, both in the order printed, joined by one space. `kind` is among the
     finding's `variant_classes`. Put each name the paper gives into `reported` (`c`, `p`,
     `rsid`, `transcript` only when the paper states one) as written, and never choose
     between the paper's own names, even when they disagree: `chd-curator normalise` checks them.
   - `placement`, by the first rule that fits:
     - `structural` for a CNV or chromosomal change, even written in `c.` notation;
     - `mane` whenever the paper gives a `c.` name within the exons +/-50 bp, with or without
       an rsID. A name printed without `c.` ("456delC", "998G>T") is still a `c.` name:
       `mane`, `reported.c` as printed, and `normalise` decides; never mark it `unplaced`;
     - `outside_region` for a variant the paper places outside the exons +/-50 bp (an intron
       beyond 50, `c.1234+51`; upstream or promoter; downstream; an enhancer), whatever names
       it: a `c.`, an rsID, a `g.` position or a description. Position beats naming, and a
       position is never looked up (dbSNP, a genome browser and your own knowledge are not
       sources);
     - `protein` for a `p.` name alone: set `hgvs_p` to that change on the protein
       `literature frame` prints, in three-letter code in parentheses (the paper's `p.R279X`
       is `NP_852259.1:p.(Arg279Ter)`);
     - `unplaced` for a variant named by an rsID or a `g.` name alone that the paper does not
       place outside the exons (inside them, or of unstated position): set `review_status:
       pending_review`, a `review_note` stating what the paper gives and `review_settled_by:
       atlas_model`. A `protein` variant whose residue validate refuses, or whose `p.` names
       none, is `unplaced` too: remove `hgvs_p`, state only the mismatch in the note and set
       `review_settled_by: authors` (`atlas_model` when its `p.` names no residue).
   - Not a placement: if `validate` refuses a `reported.p` only for its notation (LIT025,
     measured: a lower-case `ter` or residue code, or `*` printed as the U+2217 asterisk),
     write the minimal fix in `reported` (capital `Ter`, ASCII `*`) and keep the printed text
     in `as_reported`. A missing `p.` is not refused. Never change a residue, position or base.
   - `carriers` per variant, in the finding's `count_unit`. On a rare-variant design name no
     variant the paper gives no carrier for; the variants' `de_novo` and `inherited` carriers
     may not exceed the finding's, nor their total, a `with` group counted once. A
     `common_variant_association` names each variant it tested, with no `carriers`.
   - `zygosity` as stated. A variant carried with another variant in this gene lists their
     ids in `with`; links are mutual, each is `heterozygous`, every entry of a group carries
     the same total, and `phase` (`trans`, `cis`, `not_stated`, `unclear`) is set exactly when
     `with` is. A `with` group whose phase is `unclear` carries one identical `unclear.phase`
     entry on every member.
   - `phenotypes` only where the paper ties a defect to these carriers, each among the finding's.
6. Choose among three ways to leave a value plain. `not_stated` only when the paper is
   silent about it. `unclear` with its reason when the paper says something one value
   cannot hold (heterozygous in 2 carriers, homozygous in 1) or two incompatible things:
   only `zygosity` (`heterozygous`, `homozygous`, `hemizygous`, `not_stated`, `unclear`) and
   `phase` take it. Set the field to `unclear` and give `unclear.<field>` a `reason` and a
   `settled_by`. A review mark (step 9) is a question about the record as a whole; ask
   whether it would go away if this one field had a value the atlas can hold, and if so use
   `unclear`. Name what would settle it, in every `settled_by` and every
   `review_settled_by`:
   - `full_text`: only the abstract was read (never on a `not_accessible` mark);
   - `authors`: the paper contradicts itself, or disagrees with the reference sequence;
   - `expert`: it needs a domain judgement the paper cannot supply;
   - `atlas_model`: the paper is clear and the atlas's record cannot hold it.
7. Add a `locators` entry for every number (`kind: in_text` with `element`, or
   `kind: supplementary` with `file`, `sheet`, `row`). Set `cohorts` from
   `curation/cohorts.yaml`, and `shares_probands_with` to the ids of findings that share
   probands with this one. `[]` asserts that no proband is shared with any other finding;
   write it only where the paper states its cohort is new. Otherwise write `unknown`. The
   curator checks independence by reading this field in the `review` listing.
8. A paper you cannot read stays in: record what its abstract supports, with locator
   element `Abstract`, `review_status: not_accessible` and a `review_note` naming what was
   not read; it takes no settler. If it supports nothing, the curator excludes it as `full_text_not_accessible`.
9. Where the paper disagrees with a source the finding rests on, or contradicts itself, set
   `review_status: pending_review`, state only the measured disagreement in
   `review_note` and name what would settle it. Write the mark on a finding already in
   the file with
   `chd-atlas literature review <SYMBOL> --pending-review <id> --note "<...>" --settled-by <full_text|authors|expert|atlas_model>`;
   `--settled-by` is required. `not_accessible` takes no settler.
10. Set `extracted_by: agent:literature-extract/<model>`; leave `verified_by`, `approved_by`
   and `approved_on` unset.
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
   stays out of `publications.json` while findings cite it and none is approved (a record
   no finding cites publishes), so say in your hand-off which `study_type` you chose and
   why: the curator checks it and `own_lab` (the default `false`; never guess it) in the
   `review` listing. Exit 1 means the PMID is already in `curation/publications.yaml`
   (nothing to add), PubMed failed or has no record of it, or the file is missing;
   exit 2, a bad argument or a schema refusal; the error says which.
12. Run `.venv/bin/chd-atlas validate`. Before `normalise`, a `mane` variant is expected to
    fail with "a mane variant needs hgvs_c", "a mane variant needs grch38", "a mane variant
    needs normalisation" and, when `reported.p` names a residue, "the paper names <p.> but
    the variant has no hgvs_p". A finding approved before you added its variants also fails
    "approved content changed since approval", which only a named person clears: never run
    `--approve`. Leave those; fix every other error, then hand off to verification.
13. After verification, once `chd-curator diff <SYMBOL> --against ... --mark-verified ...` has
    marked the agreeing findings, the curator runs `chd-curator normalise <SYMBOL>`, validates
    and fixes what remains. It fills `hgvs_c`, `hgvs_p`, `grch38` and `normalisation` of each
    `mane` variant; one it cannot place becomes `unplaced` with its own `pending_review`
    mark (`review_settled_by: expert`). Never write `hgvs_c` or `grch38` yourself, nor
    `hgvs_p` on a `mane` variant.

An illustrative finding (every value is made up), in the layout the tooling writes
(entries at indent 2, fields at 4, nested lists at 6). Its first variant is as extracted,
before `normalise`; its second records carriers that differ in zygosity as `unclear`:

```yaml
findings:
  - id: CHDA:LIT:0000001
    publication: PMID:10000001
    design: cohort_screen
    methods: [targeted_sequencing]
    count_unit: probands
    screened: 40
    carriers: {de_novo: 1, inherited: 0, origin_unknown: 3}
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
      - id: 2
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
    locators: [{kind: in_text, element: Table 1}, {kind: in_text, element: Table 2}]
    summary: One proband carried a de novo nonsense variant; three carried one missense variant, two heterozygous and one homozygous.
    extracted_by: agent:literature-extract/<model>
```

## Done when

- Every included paper has a finding and a Publication record: `.venv/bin/chd-atlas validate`
  reports no `LIT003` or `LIT011`, and only the errors step 12 lists.
- Hand off to the `literature-verify` skill, then the curator runs
  `.venv/bin/chd-atlas literature review <SYMBOL>`.

## Never

- Never state more than the paper does, or a number without its locator and unit.
- Never re-classify a variant or call a gene definitive.
- Never fill `verified_by` or `approved_by`, or run `literature review --approve`.
- Never invent an id outside the counter, a PMID, a cohort or an HPO term.
