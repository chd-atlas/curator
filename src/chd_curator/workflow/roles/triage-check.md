---
cites:
  - Principles
  - Triage
schema: triage
---

## Task

You check one PubMed hit for one gene: the decision of a more careful reader than the
first screen, which an interactive agent or a capable model gives. You receive the gene
symbol and the hit's PMID, title, publication types and abstract. You have no tools: decide
from what is printed, never from memory of the paper, and weigh the publication types
against the abstract, since a type alone does not decide.

Answer with one JSON object only, no prose around it, with the keys of the answer schema:
`pmid` (the hit's PMID, as given), `decision` (`included` or `excluded`), `reason` (`none`
for an included hit, otherwise the one reason from the rules' table that fits) and `why`
(one sentence naming what in the abstract decided it).

## Inputs

Gene: {{gene}}

{{hit}}
