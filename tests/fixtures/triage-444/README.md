Decisions and counts from the CPU triage benchmark of 2026-11-26 (run 3: DeepSeek V4 Flash,
thinking on), for `tests/test_qualify.py`. Three genes (TBX5 400, NR2F2 32, MMP21 12 hits; 444
in all), one file set per gene because PMID:24479926 is a hit of two of them. No abstract, title,
model wording or reasoning is kept.

- `gold.json`: per hit, `gene`, `pmid`, `decision` and `reason` (the final triage decision; `none`
  for an inclusion) and `findings` (the count of approved findings), from the benchmark's gold.
- `screen-<GENE>.jsonl`: run 3's decision and reason per hit as `ask` answer rows
  (`{key, parsed: {pmid, decision, reason, why}}`); `why` is the placeholder `(withheld)`, because
  the reader requires a non-empty one and the model's own is not kept.
- `check-<GENE>.jsonl`: the benchmark's abstract-stage decisions (the reference that produced
  the gold) in the same shape, standing for the capable check. One stage reason, `same_data_as`,
  is outside the triage vocabulary and is written `other`.

Built from the benchmark's `gold.json` and `results/ds-think1/answers.jsonl` by a script kept
outside this repository.

The benchmark was scored pooled over the 444 hits; the test scores per gene: each gene's hybrid
is scored on its own hits with the audit seeded `"<seed>:<gene>"` (for example `run-a:TBX5`). The 82, 93 and 86 figures are sums
of per-hit quantities over the three genes.
