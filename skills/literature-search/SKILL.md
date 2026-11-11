---
name: literature-search
description: Use when a published gene needs its PubMed literature search run or re-run, so every hit is recorded for triage.
---

# literature-search

Read the atlas checkout's `curator/curator-rules.md` first. Model tier: none. This step runs one command and
uses no judgment, so any agent or person can run it.

## Inputs

- A gene symbol. The approved symbol, an alias or a previous symbol all work; the file is
  written under the approved symbol.
- Optional: `NCBI_API_KEY` in the environment, which raises the PubMed rate limit. Never
  write a key into any file.
- Network access to PubMed.

## Steps

1. From the atlas checkout's root, run the search:

   ```bash
   chd-curator search <SYMBOL>
   ```

   Pass `--run-on YYYY-MM-DD` only to record a different date. The atlas builds the query
   from a fixed template (the approved symbol and its previous symbols, the CHD term block,
   the gene's in-scope disease names, and no preprints or reviews); the command asks
   `chd-atlas literature target` for it and records it. Do not write or edit a query.
2. Read the output line: it gives the search id, the hits and the new hits, and the path
   written to, `curation/literature/<SYMBOL>.yaml`.
3. Check the file. Its `searches` list has one entry per run. Every PMID the search had
   not seen before is now in `triage` with `decision: pending`. A re-run adds only the new
   PMIDs and leaves earlier triage and approvals as they were.

   ```yaml
   triage:
     - pmid: PMID:10000001
       search: 1
       decision: pending
   ```
4. Run `.venv/bin/chd-atlas validate`. Pending entries give one `LIT002` warning per file;
   that is expected here and cleared by triage.

## Done when

- The command printed a search line and exited 0.
- `.venv/bin/chd-atlas validate` reports no error caused by the search. The file's new
  output is the `LIT002` warning for the pending hits (none if there were no new hits).
- You tell the curator the hit count, the new-hit count, and that the next step is the
  `literature-triage` skill, then `.venv/bin/chd-atlas literature review <SYMBOL>` once
  findings exist.

## Never

- Never run a query of your own or add a PMID from memory.
- Never edit `searches` or any `pmid` or `search` value by hand.
- Never run this for a gene outside the publication gate: the command refuses it with
  exit 2.
- If the command exits 1 (PubMed or the network failed, or the existing file could not be
  read) or 2 (a bad argument, the atlas refused the symbol, or the write was refused: the
  schema, a YAML anchor or alias, an approval, or the file changed while the search ran),
  it wrote nothing. The error line says which. Report it; do not retry in a loop.
