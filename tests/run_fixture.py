"""The atlas-shaped fixture the phase 2 run commands share: ``tests/fixtures/run/``.

Everything in it is synthetic: PMIDs 9100000n, no real paper text, gene TBX5 (HGNC:11604).

- ``curation/literature/TBX5.yaml``: one triage entry (PMID:91000001, included) and one
  approved finding, CHDA:LIT:0009001; ``curation/publications.yaml`` holds PMID:91000001 only,
  so PMID:91000002 is a paper the run reads that the atlas does not know yet;
  ``curation/.id_registry.yaml`` counts LIT to 9001; ``ontologies/`` has the HPO excerpt.
- ``local/literature/texts/<digits>/``: ``abstract.txt`` for both papers, ``fulltext.txt`` for
  91000001 (the layout ``texts.py`` writes, without ``raw/`` or ``fetch.json``).
- Run ``TBX5/r1``: ``texts.json``, ``batches.json`` (mode new; batch 01 = paper 1, batch 02 =
  paper 2), ``extract/`` and ``verify/`` (per batch: ``findings.yaml``, ``evidence.yaml``,
  ``papers.json``, ``report.md``), ``comparison.json``, ``judging/judge-01.findings.yaml`` and
  ``judge/judge-01.decisions.yaml``.
- Local ids: extract CHDA:LIT:0901nnn, verify CHDA:LIT:0801nnn, judge-added CHDA:LIT:0701nnn.
  The extract and verify passes pair 0901001-0801001 and 0901002-0801002; the verify pass also
  reports 0801003, which the first reader did not (``only_second``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from chd_curator.run import RunPaths

RUN_FIXTURE = Path(__file__).parent / "fixtures" / "run"


def run_paths(root: Path = RUN_FIXTURE) -> RunPaths:
    return RunPaths(root, "TBX5", "r1")


def copy_run_fixture(destination: Path) -> RunPaths:
    """A writable copy of the fixture under ``destination / 'root'``."""
    shutil.copytree(RUN_FIXTURE, destination / "root")
    return run_paths(destination / "root")
