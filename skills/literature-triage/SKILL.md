---
name: literature-triage
description: Use when a gene's literature file has pending hits that each need an included or excluded decision from the title and abstract.
---

# literature-triage

Read the atlas checkout's `curator/curator-rules.md` first. Model tier: a cheap model (for example Haiku).
Triage reads an abstract and applies a short rule list.

## Inputs

- A gene symbol whose `curation/literature/<SYMBOL>.yaml` has `decision: pending` entries,
  from the `literature-search` skill.

## Steps

1. Print the pending abstracts:

   ```bash
   chd-curator abstracts <SYMBOL>
   ```

   The default is `--decision pending`. Add `--decision included` or `--decision excluded`
   to reread earlier decisions.
2. Decide each PMID from its title and abstract, then edit its entry in the YAML by hand.
   No command writes a decision. Change only `decision` and the fields below; never change
   `pmid` or `search`.
3. Include when the paper reports human genetic evidence on this gene in CHD. When unsure,
   include. Exclude only for a reason the abstract shows:

   | reason | use when the abstract shows |
   |---|---|
   | `not_peer_reviewed` | a preprint, thesis, a conference or meeting abstract, or other unreviewed item |
   | `review_article` | a review or commentary with no new data |
   | `not_human_genetics` | animal, cell or protein work with no human genetic finding; or DNA methylation or RNA expression measured in human samples with no variant in the gene reported (a paper that also reports a variant in the gene is included) |
   | `no_finding_in_gene` | the gene named only as background, or nothing reported about a variant in it; a gene among a study's reported results is a finding: include |
   | `not_chd` | no congenital heart phenotype |
   | `same_data_as` | the same patients and the same result reported again (add `same_as`); a paper with new results on the same patients is included, and its finding names the other in `shares_probands_with` |
   | `other` | none of the above (add `note` saying why); an erratum or corrigendum is excluded here with `note: erratum to PMID:<the corrected paper>` |

   ```yaml
   - pmid: PMID:10000001
     search: 1
     decision: included
   - pmid: PMID:10000002
     search: 1
     decision: excluded
     reason: same_data_as
     same_as: PMID:10000001
   - pmid: PMID:10000003
     search: 1
     decision: excluded
     reason: no_finding_in_gene
   ```
4. A paper whose abstract is missing or whose full text you cannot reach is not excluded
   for that. Include it; the extract step records it as `not_accessible`. The one
   exception is an erratum, which PubMed usually prints with no abstract and the corrected
   paper's title. Exclude it only when you know it is one (its title says so, or the
   curator told you); a hit with no abstract whose title repeats another hit's is
   included and named in your hand-off as unsure.
5. `same_as` names a PMID that is `included` in this same file. Set the other paper's
   decision first.
6. Run `.venv/bin/chd-atlas validate`.

## Done when

- No entry is `pending`: `.venv/bin/chd-atlas validate` reports no `LIT002` for the file.
- Validate reports no error except `LIT003`, once for each included paper that has no
  finding yet. Those clear at the extract step; name those PMIDs in your hand-off. Any
  other error is a mistake in your edits: fix the edit.
- You tell the curator how many were included and excluded by reason, then hand off to the
  `literature-extract` skill. The curator
  later runs `.venv/bin/chd-atlas literature review <SYMBOL>`.

## Never

- Never exclude on a hunch, on the journal, or on a paper being old or small.
- Never exclude a paper for being unreadable.
- Never add, remove or reorder triage entries, or change a `pmid` or `search`.
- Never change a decision to clear a validator error. `LIT003` on an included paper is
  expected; excluding the paper to remove it throws the paper's evidence away.
- Never decide from memory of a paper; use the printed abstract.
