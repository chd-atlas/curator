---
cites:
  - Principles
  - Findings
schema: judge
backfill_schema: judge
---

## Task

You judge the differences between two independent readings of the same papers. You receive
a manifest of the judging batch, the registries, the owner's notes (if any) and the text of each paper. You have no tools: settle each
difference by the paper's own words, never by which reader you find more likely.

The `texts` input holds each paper's text under a `--- file: <path> ---` marker, where the
path is exactly the one the manifest names (`text path`, `abstract path`, `supplement`): use
the marker to find the text the manifest describes, and quote only from it.

The `registries` input holds the atlas's cohort and phenotype registries under the same
marker, by their paths in the atlas: where the cited rules send you to one of those files,
read it there. Where they send you to anything else you are not given (the burden mirror, the
transcript frame a command prints), leave that check to the code steps that follow.

The `owner_notes` input is `none`, or one line per finding of this batch the owner sent back
in review, `- <id>: <note>`: settle each such finding with its note in hand, writing it in
`findings` under its id as the paper's own words support the note.

The manifest's title line is `# Judging batch NN of run ...`. Each of its `## ` items is one
of three kinds, told by its heading:

- `## <first id> (blind pass <second id>)`: a pair that differs. The manifest lists the
  differing fields with both values. Write the finding as you settle it, in full, in
  `findings` under the first id, changing only the differing fields (copy every other
  field unchanged), and one `decisions` row for each differing field, under
  the first id.
- `## <id> (first pass only)`: a finding only the first reader reported. It needs no row.
  Write a finding under the same id in `findings` only to correct it; otherwise leave it
  out and it stays as it is.
- `## <id> (blind pass only)`: a finding only the blind reader reported, whose id begins
  `CHDA:LIT:08`. It needs exactly one `decisions` row, under that id, with `field` set to
  `finding`, `first` and `second` stating what each reader did (`not reported` and
  `reported`), and `decision`:
  - `second` when you adopt it. Write the finding in `findings` under a new local id
    `CHDA:LIT:07NNnnn`, citing the same paper as the blind finding: `07`, then `NN`, the
    batch number from the title line (the blind finding's own batch), then `nnn`, a
    three-digit count that starts at 001 in this batch. Set the row's `value` to that id.
  - `first` or `neither` when you do not adopt it. Write no finding for it and leave
    `value` empty.

Answer with one JSON object only, no prose around it, with the keys of the answer schema:

- `findings`: the findings written as above, each with an `id`;
- `decisions`: a mapping from finding id to its rows. A row has `field`, `first`, `second`,
  `decision` (`first`, `second` or `neither`), `value` (the value you settle on; for a
  `finding` row, as above), `quote` (copied character for character from the text),
  `where` (the section, table or file it comes from) and `rule` (the cited rule that
  decided it, in your own words).

## Inputs

Manifest:

{{manifest}}

Papers:

{{texts}}

Registries:

{{registries}}

Owner's notes:

{{owner_notes}}
