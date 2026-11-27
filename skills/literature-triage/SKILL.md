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
   to reread earlier decisions. Its `Publication types` line shows an erratum
   (`Published Erratum`), a review (`Review`, `Systematic Review`) or a preprint
   (`Preprint`) even when the abstract does not; the rules' reason table decides, not the type alone.
2. Decide each PMID from its title and abstract, then edit its entry in the YAML by hand.
   No command of this skill writes a decision (`triage-apply` applies a pipeline run's).
   Change only `decision` and the fields below; never change `pmid` or `search`.
3. Decide as the role file `src/chd_curator/workflow/roles/triage-check.md` directs (a path in
   curator's repository and in the installed package; no command prints it). It is the capable
   tier's task, which you fill; the local tier's is
   `src/chd_curator/workflow/roles/triage-screen.md`. The rules both cite, the include rule,
   the reason table and the treatment of a paper that cannot be read, are in the `## Triage`
   section of the rules file. A decision is written as:

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
4. `same_as` names a PMID that is `included` in this same file. Set the other paper's
   decision first.
5. Run `.venv/bin/chd-atlas validate`.

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
