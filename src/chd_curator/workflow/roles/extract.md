---
cites:
  - Principles
  - Findings
  - Triage
  - Attribution and approval
schema: reader
backfill_schema: reader-backfill
---

## Task

You read one batch of papers for one gene and write down what each reports about it, as
findings. You receive a manifest of the batch and the text of each paper (its full text
where one was reached, else its abstract, with any supplement). You have no tools: read
only what is given.

Answer with one JSON object only, no prose around it.

The `texts` input holds each paper's text under a `--- file: <path> ---` marker, where the
path is exactly the one the manifest names (`text path`, `abstract path`, `supplement`): use
the marker to find the text the manifest describes, and quote only from it.

The `registries` input holds the atlas's cohort and phenotype registries under the same
marker, by their paths in the atlas: where the cited rules send you to one of those files,
read it there. Where they send you to anything else you are not given (the burden mirror, the
transcript frame a command prints), leave that check to the code steps that follow (merge,
normalise), and name in your `report` what you could not check.

When the manifest's title line (`# Batch NN of run ...`) gives `mode new`, answer with the
keys of the answer schema:

- `findings`: the findings, each with a distinct local `id` `CHDA:LIT:09NNnnn`: the digits
  `09`, then `NN`, the batch number from the manifest's title line, then `nnn`, a
  three-digit count that starts at 001 in this batch. The batch number keeps ids of
  different batches apart, and the id is replaced by a final one afterwards;
- `evidence`: for each finding id, the verbatim quotes the text gives for the fields it
  rests on, each as `field`, `quote` (copied character for character from the text) and
  `where` (the section, table or file it comes from);
- `papers`: one entry per paper of the batch, either `pmid`, `study_type` (null where no
  study type fits) and `reading` (what the paper says about its own design), or `pmid` with
  `exclude` giving `reason` and `note` for a paper that supports no finding;
- `report`: Markdown for the curator, naming what you could not read or settle and why.

When the title line gives `mode backfill`, the manifest shows the findings as curated, under
"Findings as curated:", and names the fields to re-read ("Fields to re-read:"). You write no
new finding and no `papers` entry. Answer with the keys of the backfill answer schema:

- `findings`: a mapping from the `id` of a curated finding, as the manifest shows it, to a
  mapping of the fields to fill, each one of the fields to re-read and each one the curated
  finding does not yet hold; leave out a finding for which you find nothing to fill;
- `evidence`: for each of those ids, the verbatim quotes for the fields you filled, as above;
- `report`: Markdown for the curator, as above.

## Inputs

Manifest:

{{manifest}}

Papers:

{{texts}}

Registries:

{{registries}}
