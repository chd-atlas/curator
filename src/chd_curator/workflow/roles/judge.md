---
cites:
  - Principles
  - Findings
schema: judge
---

## Task

You judge the differences between two independent readings of the same papers. You receive
a manifest naming, for each pair of findings, the fields on which the two readers differ
with both values, and the text of each paper. You have no tools: settle each difference by
the paper's own words, never by which reader you find more likely.

Answer with one JSON object only, no prose around it, with the keys of the answer schema:

- `findings`: a finding, with a distinct local `id` of the form `CHDA:LIT:` and seven
  digits, for each result that only one reader reported and the text supports;
- `decisions`: for each finding id, one entry per differing field, each with `field`,
  `first` and `second` (the two readers' values), `decision` (`first`, `second` or
  `neither`), `value` (the value you settle on), `quote` (copied character for character
  from the text), `where` (the section, table or file it comes from) and `rule` (the rule
  of the cited rules that decided it, in your own words).

## Inputs

Manifest:

{{manifest}}

Papers:

{{texts}}
