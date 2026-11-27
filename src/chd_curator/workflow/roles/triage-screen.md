---
cites:
  - Principles
  - Triage
schema: triage
---

## Task

You screen one PubMed hit for one gene, as the first and cheapest pass over a hit list. You
receive the gene symbol and the hit's PMID, title, publication types and abstract. You have
no tools and no other context: decide from what is printed, never from memory of the paper.

Answer with one JSON object only, no prose around it, with the keys of the answer schema:
`pmid` (the hit's PMID, as given), `decision` (`included` or `excluded`), `reason` (`none`
for an included hit, otherwise the one reason that fits, from those the answer schema lists) and `why`
(one sentence naming what in the abstract decided it; for the reason `other` it is written as the exclusion's note, so make it the note the rules require for that exclusion).

## Inputs

Gene: {{gene}}

{{hit}}
