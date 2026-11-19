"""Tests run against a real atlas checkout, or not at all.

curator never imports ``chd_atlas``; it runs the atlas's CLI and reads its exported
schemas. A suite that silently skipped those tests when the atlas was missing would be
green while checking nothing, so a missing variable stops the run instead.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

REQUIRED = ("CHD_ATLAS_ROOT", "CHD_ATLAS_CLI", "CHD_ATLAS_PYTHON")


def pytest_configure(config: pytest.Config) -> None:
    unset = [name for name in REQUIRED if not os.environ.get(name)]
    if unset:
        raise pytest.UsageError(
            "the atlas is required to run curator's tests; unset: " + ", ".join(unset)
        )


@pytest.fixture(scope="session")
def atlas_root() -> Path:
    return Path(os.environ["CHD_ATLAS_ROOT"])


@pytest.fixture(scope="session")
def atlas_cli() -> Path:
    return Path(os.environ["CHD_ATLAS_CLI"])


@pytest.fixture(scope="session")
def atlas_python() -> Path:
    return Path(os.environ["CHD_ATLAS_PYTHON"])


_STUB = """\
import json
import sys

ANSWERS = {
    "TBX5": ("HGNC:11604", "TBX5", True),
    "HOS": ("HGNC:11604", "TBX5", True),
    "GATA4": ("HGNC:4173", "GATA4", True),
    "BRCA1": ("HGNC:1100", "BRCA1", False),
}
REFUSALS = {
    "NOPE": "'NOPE' is not a known gene symbol",
    "MADH7": "'MADH7' is claimed by HGNC:6772, HGNC:6773",
}
FRAMES = {
    "TBX5": ("NM_181486.4", "NP_852259.1", "12", "-"),
    "GATA4": ("NM_002052.5", "NP_002043.2", "8", "+"),
}
if sys.argv[1] == "validate":
    print("warning SCP003 [curation/chd_scope.yaml]: stub candidate report")
    print("warning SCP007 [curation/chd_scope.yaml]: stub Orphanet digest")
    print()
    print("0 error(s), 3 warning(s)")
    sys.exit(0)
subcommand, symbol = sys.argv[2], sys.argv[3]
if symbol in REFUSALS:
    print("error: " + REFUSALS[symbol])
    sys.exit(2)
gene, approved, published = ANSWERS[symbol]
if subcommand == "digests":
    if approved != "TBX5":
        print("error: no literature file for " + approved)
        sys.exit(2)
    print(json.dumps({
        "format": 1, "gene": gene, "symbol": approved,
        "literature_sha256": "sha256:" + "a" * 64,
        "findings": [
            {"id": "CHDA:LIT:0000901", "finding_digest": "sha256:" + "1" * 64,
             "shown_digest": "sha256:" + "2" * 64, "approved": True, "verified": True,
             "approval_void": False},
            {"id": "CHDA:LIT:0000902", "finding_digest": "sha256:" + "3" * 64,
             "shown_digest": "sha256:" + "4" * 64, "approved": False, "verified": False,
             "approval_void": False},
            {"id": "CHDA:LIT:0000903", "finding_digest": "sha256:" + "5" * 64,
             "shown_digest": "sha256:" + "6" * 64, "approved": True, "verified": False,
             "approval_void": True},
        ],
    }, sort_keys=True))
    sys.exit(0)
if subcommand == "frame":
    if approved not in FRAMES:
        print("error: " + approved + " has no MANE Select in the pinned frame")
        sys.exit(2)
    transcript, protein, chrom, strand = FRAMES[approved]
    print(json.dumps({
        "format": 1, "gene": gene, "symbol": approved, "transcript": transcript,
        "protein": protein, "chrom": chrom, "strand": strand,
    }, sort_keys=True))
    sys.exit(0)
print(json.dumps({
    "format": 1,
    "gene": gene,
    "published": published,
    "query": "(\\"" + approved + "\\"[tiab]) AND stub-query",
    "symbol": approved,
}, sort_keys=True))
"""

STUB_QUERIES = {"TBX5": '("TBX5"[tiab]) AND stub-query', "GATA4": '("GATA4"[tiab]) AND stub-query'}


@pytest.fixture
def stub_atlas(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An executable standing in for ``chd-atlas``, answering ``literature target``,
    ``literature frame``, ``literature digests`` (TBX5 only, with made-up digests) and
    ``validate`` (a summary line of ``0 error(s), 3 warning(s)``).

    TBX5 (also typed as its previous symbol HOS) and GATA4 are published, BRCA1 is
    not, and NOPE and MADH7 are refused with the atlas's real messages.
    """
    path = tmp_path_factory.mktemp("stub") / "chd-atlas"
    path.write_text(f"#!{sys.executable}\n{_STUB}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path
