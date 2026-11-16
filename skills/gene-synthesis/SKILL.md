---
name: gene-synthesis
description: Use when a gene has approved literature findings and its page needs a short drafted narrative that a person will approve.
---

# gene-synthesis

Read the atlas checkout's `curator/curator-rules.md` first, its "Synthesis" section above all.
Model tier: the top one (for example Opus). A synthesis claims how the records fit together,
and a weaker model states that with more confidence than the records carry.

## Inputs

- A gene symbol with approved findings: `findings` in `curation/literature/<SYMBOL>.yaml`
  whose `approved_by` is set.
- The model name you run as, for `drafted_by`.

## Steps

1. Read only what a reader of the gene's page also sees: the findings of
   `curation/literature/<SYMBOL>.yaml` that carry `approved_by`, and the lesion assertions in
   `curation/assertions/<SYMBOL>.yaml`. Skip every finding with no `approved_by`, every triage
   entry and every abstract; none of them is on the page.
2. Write for a clinical geneticist who has a minute. Cover, where the records bear on it:
   - the lesion spectrum;
   - inheritance, de novo against inherited, as the findings observed it;
   - syndromic against isolated presentation;
   - genotype and phenotype observations;
   - how much of the evidence is abstract only (`not_accessible`), so a reader can weigh it.
3. Do not restate the validity grade, the burden results or the curated mechanism: the card
   states them above the text. Keep to the rules file: one to five sentences, at most 120
   words in all, and every sentence cites at least one record id in `cites` (an empty `cites`
   is a schema error; a cite that is not an approved finding of the file or an assertion of
   the gene is `SYN001`). A sentence rests only on
   the records it cites, so cite the findings and assertions that say it.
4. Write no number in any form (`SYN002`): no digits, no number words, and no counts hidden
   in other words. Say "in families" or "in affected individuals" and leave the count to the
   card's line. Gene names and variants (TBX5, 22q11.2, p.Gly80Arg) are fine. `SYN002` checks
   digits and number words only. Years, decades, ages and words such as "several" or "a
   handful" pass it, and this skill forbids them anyway: that rule is the skill's own and
   stricter. A few ordinary terms do trip `SYN002`; the rules file names them with the wording
   to use instead.
5. Write the draft to `local/literature/<SYMBOL>.synthesis.yaml` (`local/` is gitignored).
   It holds exactly `sentences` and `drafted_by`:

   ```yaml
   sentences:
     - text: Carriers' lesions include atrial and ventricular septal defects.
       cites: [CHDA:LIT:0000001, CHDA:AST:0000001]
     - text: Reports describe it in both syndromic and isolated disease.
       cites: [CHDA:LIT:0000002]
   drafted_by: agent:gene-synthesis/<model>
   ```
6. Write it into the gene's file. This adds the block, or replaces an unapproved one, and
   refuses any approval field in the draft:

   ```bash
   chd-curator synthesis <SYMBOL> --draft local/literature/<SYMBOL>.synthesis.yaml
   ```
7. Run `.venv/bin/chd-atlas validate` and fix every error by redrafting, not only
   the `SYN` ones: a citation that is not an approved finding or assertion of this gene
   (`SYN001`), a number (`SYN002`), a length over the limit (`SYN003`), or a `SCHEMA001` when
   the atlas cannot load the draft at all (it then reports no `SYN` line). Report a warning you
   cannot explain.

## Done when

- `.venv/bin/chd-atlas validate` reports **0 errors**. Hand off: the person
  runs `.venv/bin/chd-atlas literature review <SYMBOL>`, which lists the text and what each
  sentence cites, and then `--approve-synthesis --by <their name>`.
- Say which sentences lean on abstract-only findings, so the person weighs them.

## Never

- Never write `approved_by`, `approved_on` or `approved_digest`, and never run
  `literature review --approve-synthesis`. A named person approves, in the atlas.
- Never delete an approval to redraft. If `chd-curator synthesis` reports that one exists,
  stop and tell the person: removing it is theirs to do, by hand.
- Never cite or draw on a finding with no `approved_by`, or on anything outside the page's
  own records (no paper, review or memory of the field). Never write a number.
- Never edit an approved finding or assertion to make a sentence fit: that voids the
  approval of the whole synthesis (`SYN004`).
