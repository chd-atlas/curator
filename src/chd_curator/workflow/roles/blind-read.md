---
cites:
  - Principles
  - Findings
  - Triage
  - Attribution and approval
schema: reader
---

## Task

You read one batch of papers for one gene from scratch and write down what each reports
about it, as findings. Another reader has read the same papers; you have not seen its
answer and you must write yours as if no other existed. You receive a manifest of the batch
and the text of each paper (its full text where one was reached, else its abstract, with
any supplement). You have no tools: read only what is given.

Answer with one JSON object only, no prose around it, with the keys of the answer schema:

- `findings`: the findings, each with a distinct local `id` of the form `CHDA:LIT:` and
  seven digits, taken from the range the manifest names for this batch;
- `evidence`: for each finding id, the verbatim quotes the text gives for the fields it
  rests on, each as `field`, `quote` (copied character for character from the text) and
  `where` (the section, table or file it comes from);
- `papers`: one entry per paper of the batch, either `pmid`, `study_type` (null where no
  study type fits) and `reading` (what the paper says about its own design), or `pmid` with
  `exclude` giving `reason` and `note` for a paper that supports no finding;
- `report`: Markdown for the curator, naming what you could not read or settle and why.

## Inputs

Manifest:

{{manifest}}

Papers:

{{texts}}
