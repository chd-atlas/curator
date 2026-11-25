"""The atlas-shaped fixture the phase 2 run commands share.

It is the run ``TBX5/r1`` just before ``merge``, over synthetic papers (PMIDs 9100000n, no real
paper text) and gene TBX5 (HGNC:11604). Two directories:

``tests/fixtures/run/`` (an atlas root)
  - ``curation/literature/TBX5.yaml``: triage PMID:91000001 (with its one approved finding,
    CHDA:LIT:0009001, the already-curated paper a backfill would read) and PMID:91000002,
    91000003, 91000004, all ``included`` with no findings. ``curation/publications.yaml`` holds
    91000001 only. ``curation/.id_registry.yaml`` counts LIT to 9001.
  - ``local/literature/texts/<digits>/``: 91000001 and 91000002 each have ``fulltext.txt`` and
    ``abstract.txt``; 91000002 also ``supplements/s1.txt``; 91000003 and 91000004 only
    ``abstract.txt``. Years 2019, 2021, 2022, 2023, so the (year, PMID) order of the papers a
    run reads is 91000002, 91000003, 91000004.
  - Run ``local/literature/runs/TBX5/r1/``: ``texts.json`` (all four papers; title and year
    for the three ``publications.yaml`` lacks), ``batches.json``, ``extract/`` and ``verify/``
    (per batch: ``findings.yaml``, ``evidence.yaml``, ``papers.json``, ``report.md``). Nothing
    a later command writes is in it: no ``merge.json``, ``evidence/``, ``comparison.json``,
    ``judging/``, ``judge/`` or ``assemble.json``.

``tests/fixtures/run-judge/`` (a judge round, copied into a run by ``copy_judge_round``)
  ``judging/judge-01.findings.yaml`` and ``judge/judge-01.decisions.yaml``.

Batches (mode new). ``text_bytes`` counts the full text where there is one, else the abstract:
91000002's ``fulltext.txt`` is 283 bytes, 91000003's abstract 203, 91000004's 151. Adding
papers in (year, PMID) order while the sum stays within the budget: 283 fits; 283 + 203 = 486
exceeds any budget below 486; the budget is 354, so 91000003 starts batch 02, and 203 + 151 =
354 fits. Any budget from 354 to 485 gives the same two batches: batch 01 = [91000002]
(283 bytes), batch 02 = [91000003, 91000004] (354 bytes).

Local ids are ``CHDA:LIT:09NNnnn`` (extract), ``08NNnnn`` (verify) and ``07NNnnn`` (judge),
NN the batch. Batch 01 (91000002): extract 0901001 (case report, two variants) and 0901002 (a
family); verify 0801001 equals 0901001 in every compared field, 0801002 differs from 0901002
in ``screened`` only (2, not 1), and 0801003 is a finding the first reader did not report (the
supplement's third carrier). Batch 02: extract 0902001 on 91000003; verify 0802001 differs
from it only in the variant's ``as_reported`` span. ``papers.json`` gives 91000002 and
91000003 a study type and excludes 91000004 (``not_human_genetics``). Every extract finding
carries ``extracted_by`` ``agent:literature-extract/claude-sonnet-5-5``, every verify finding
``agent:literature-verify/claude-opus-5-5``; none carries ``verified_by`` or an approval.

Merge numbers from the counter, 9001, in (year, PMID number, batch, file position) order:
0901001 -> CHDA:LIT:0009002, 0901002 -> CHDA:LIT:0009003, 0902001 -> CHDA:LIT:0009004. The
judge round is written against those: ``judge-01.findings.yaml`` holds the corrected whole
finding CHDA:LIT:0009003 and the added CHDA:LIT:0701001 (from the blind reader's 0801003);
``judge-01.decisions.yaml`` has a decision for 0009003's differing field and one for 0701001.

The approved finding's ``approved_digest`` is the atlas's own. The atlas needs the gene
registry to compute digests, which the fixture does not carry, so ``atlas_digest_root`` builds
a temporary root (the fixture plus the atlas's ``mirrors/genes.tsv``). To recompute it by hand::

    cp -r tests/fixtures/run /tmp/root && mkdir /tmp/root/mirrors
    cp $CHD_ATLAS_ROOT/mirrors/genes.tsv /tmp/root/mirrors/
    $CHD_ATLAS_CLI literature digests TBX5 --root /tmp/root     # finding_digest of 0009001
"""

from __future__ import annotations

import shutil
from pathlib import Path

from chd_curator.run import RunPaths

RUN_FIXTURE = Path(__file__).parent / "fixtures" / "run"
JUDGE_FIXTURE = Path(__file__).parent / "fixtures" / "run-judge"


def run_paths(root: Path = RUN_FIXTURE) -> RunPaths:
    return RunPaths(root, "TBX5", "r1")


def copy_run_fixture(destination: Path) -> RunPaths:
    """A writable copy of the fixture under ``destination / 'root'``."""
    shutil.copytree(RUN_FIXTURE, destination / "root")
    return run_paths(destination / "root")


def copy_judge_round(p: RunPaths) -> None:
    """Copy the judge round (``judging/`` and ``judge/``) into the run."""
    shutil.copytree(JUDGE_FIXTURE / "judging", p.judging)
    shutil.copytree(JUDGE_FIXTURE / "judge", p.judge)


def atlas_digest_root(destination: Path, atlas_root: Path) -> Path:
    """A temporary root the atlas can compute digests over: the fixture plus the gene mirror."""
    root = destination / "digest-root"
    shutil.copytree(RUN_FIXTURE, root)
    (root / "mirrors").mkdir()
    shutil.copy(atlas_root / "mirrors" / "genes.tsv", root / "mirrors" / "genes.tsv")
    return root
