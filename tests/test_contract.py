"""curator against the REAL atlas: ``CHD_ATLAS_CLI`` and a copy of ``CHD_ATLAS_ROOT``.

The unit tests use a stub atlas, so nothing there shows that curator's output is
what the atlas accepts. These tests run the atlas's own ``literature target`` and
``validate`` over a throwaway copy of the checkout. PubMed is still faked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator import cli
from chd_curator.cli import app
from fake_pubmed import FakePubMed, papers

runner = CliRunner()
# .git, .venv, local, dist, .superpowers, CLAUDE.md, .claude and superpowers are
# gitignored local files or symlinks on a development machine and absent on CI.
IGNORED = (".git", ".venv", "local", "dist", ".superpowers", "CLAUDE.md", ".claude", "superpowers")


def _atlas(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.environ["CHD_ATLAS_CLI"], *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def atlas_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "atlas"
    shutil.copytree(
        os.environ["CHD_ATLAS_ROOT"],
        copy,
        symlinks=False,
        ignore=shutil.ignore_patterns(*IGNORED),
    )
    return copy


def _validate(root: Path) -> None:
    run = _atlas("validate", "--root", str(root))
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_real_target_answers_for_tbx5() -> None:
    run = _atlas("literature", "target", "TBX5", "--root", os.environ["CHD_ATLAS_ROOT"])
    assert run.returncode == 0, run.stdout + run.stderr
    answer = json.loads(run.stdout)
    assert answer["gene"] == "HGNC:11604"
    assert answer["published"] is True
    assert answer["query"].startswith('("TBX5"[tiab]')


def test_a_search_on_a_copy_of_the_atlas_validates(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = None
    for symbol in ("GATA4", "NKX2-5", "GATA6"):
        run = _atlas("literature", "target", symbol, "--root", str(atlas_copy))
        answer = json.loads(run.stdout) if run.returncode == 0 else {}
        file = atlas_copy / "curation" / "literature" / f"{answer.get('symbol', symbol)}.yaml"
        if answer.get("published") is True and not file.exists():
            chosen = (symbol, answer, file)
            break
    assert chosen is not None, "none of GATA4, NKX2-5, GATA6 is published without a literature file"
    symbol, answer, file = chosen
    monkeypatch.setattr(cli, "_fetch", FakePubMed(papers("9", "25", "100")))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "search",
            symbol,
            "--run-on",
            "2026-11-11",
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 0, result.stdout
    written = YAML(typ="safe").load(file.read_text())
    assert written["gene"] == answer["gene"]
    assert written["searches"][0]["query"] == answer["query"]
    assert [t["pmid"] for t in written["triage"]] == ["PMID:9", "PMID:25", "PMID:100"]
    _validate(atlas_copy)


def test_add_publication_on_a_copy_of_the_atlas_validates(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = atlas_copy / "curation" / "publications.yaml"
    pmid = "9"
    assert f"PMID:{pmid}\n" not in path.read_text()
    monkeypatch.setattr(cli, "_fetch", FakePubMed(papers(pmid)))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "add-publication",
            pmid,
            "--study-type",
            "case_report",
            "--reading",
            "A single-family report in the abstract.",
            "--root",
            str(atlas_copy),
        ],
    )

    assert result.exit_code == 0, result.stdout
    ids = [p["id"] for p in YAML(typ="safe").load(path.read_text())["publications"]]
    assert ids[-1] == "PMID:9"
    _validate(atlas_copy)
