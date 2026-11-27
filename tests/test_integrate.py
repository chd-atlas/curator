"""``integrate``: a package enters the atlas only onto the base it started from.

Each refusal hashes every file of the target atlas (and its file list) before and after.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chd_curator.cli import app
from test_merge import _pubmed
from test_package import base_json, package
from test_scratch import MUTABLE_TBX5, make_atlas, merge, scratch, snapshot

runner = CliRunner()
RUN = "local/literature/runs/TBX5/r1"


def integrate(pkg: Path, atlas: Path) -> tuple[int, str]:
    result = runner.invoke(
        app, ["integrate", str(pkg), "--atlas", str(atlas)], catch_exceptions=False
    )
    return result.exit_code, result.output


@pytest.fixture
def packaged(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """(the atlas the run started from, the package, a target that lacks the run folder)."""
    atlas = make_atlas(tmp_path, atlas_root)
    base_json(atlas, tmp_path / "base.json")
    scratch(atlas, tmp_path / "work")
    _pubmed(monkeypatch)
    merge(tmp_path / "work", stub_atlas)
    assert package(tmp_path / "work", tmp_path / "base.json", tmp_path / "pkg")[0] == 0
    # The run folder is local-only, so the target an integrate lands on does not hold it.
    shutil.rmtree(atlas / RUN)
    return atlas, tmp_path / "pkg", tmp_path / "work"


def test_integrate_copies_the_proposal_when_the_base_matches(
    packaged: tuple[Path, Path, Path],
) -> None:
    atlas, pkg, work = packaged
    code, out = integrate(pkg, atlas)
    assert code == 0, out
    for rel in MUTABLE_TBX5:
        assert (atlas / rel).read_bytes() == (work / rel).read_bytes()
    assert (atlas / RUN / "merge.json").read_bytes() == (work / RUN / "merge.json").read_bytes()
    assert (atlas / "local/literature/texts/91000002/supplements/s1.txt").is_file()
    assert "integrated TBX5/r1" in out


def test_integrate_refuses_and_names_a_file_changed_since_the_base(
    packaged: tuple[Path, Path, Path],
) -> None:
    atlas, pkg, _ = packaged
    for rel in (MUTABLE_TBX5[0], MUTABLE_TBX5[2]):
        with (atlas / rel).open("a") as handle:
            handle.write("# a curator's edit, made meanwhile\n")
    before = snapshot(atlas)
    code, out = integrate(pkg, atlas)
    assert code == 1
    assert MUTABLE_TBX5[0] in out and MUTABLE_TBX5[2] in out  # every differing file
    assert MUTABLE_TBX5[1] not in out
    assert snapshot(atlas) == before


def test_integrate_refuses_an_existing_run_folder(packaged: tuple[Path, Path, Path]) -> None:
    atlas, pkg, _ = packaged
    (atlas / RUN).mkdir(parents=True)
    (atlas / RUN / "keep.txt").write_text("mine")
    before = snapshot(atlas)
    code, out = integrate(pkg, atlas)
    assert code == 1 and RUN in out
    assert snapshot(atlas) == before


def test_integrate_refuses_a_differing_text(packaged: tuple[Path, Path, Path]) -> None:
    atlas, pkg, _ = packaged
    text = atlas / "local/literature/texts/91000002/fulltext.txt"
    text.write_text("a different copy of the paper")
    before = snapshot(atlas)
    code, out = integrate(pkg, atlas)
    assert code == 1 and "91000002/fulltext.txt" in out
    assert snapshot(atlas) == before


def test_integrate_refuses_a_proposal_missing_a_file_the_checkout_holds(
    packaged: tuple[Path, Path, Path],
) -> None:
    atlas, pkg, _ = packaged
    (pkg / "proposal" / MUTABLE_TBX5[1]).unlink()
    before = snapshot(atlas)
    code, out = integrate(pkg, atlas)
    assert code == 1 and MUTABLE_TBX5[1] in out
    assert snapshot(atlas) == before


@pytest.mark.parametrize("damage", ["missing", "not-json", "bad-symbol"])
def test_integrate_refuses_an_unusable_package(
    packaged: tuple[Path, Path, Path], damage: str
) -> None:
    atlas, pkg, _ = packaged
    meta = pkg / "package.json"
    if damage == "missing":
        meta.unlink()
    elif damage == "not-json":
        meta.write_text("{")
    else:
        meta.write_text(meta.read_text().replace('"symbol": "TBX5"', '"symbol": "../x"'))
    before = snapshot(atlas)
    code, _ = integrate(pkg, atlas)
    assert code == 2
    assert snapshot(atlas) == before
