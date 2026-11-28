"""``integrate``: a package enters the atlas only onto the base it started from.

Each refusal hashes every file of the target atlas (and its file list) before and after.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chd_curator.cli import app
from test_contract import _plain
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


def test_integrate_refuses_a_text_changed_in_the_atlas_since_the_base(
    packaged: tuple[Path, Path, Path],
) -> None:
    atlas, pkg, _ = packaged
    text = atlas / "local/literature/texts/91000002/fulltext.txt"
    text.write_text("a different copy of the paper")
    before = snapshot(atlas)
    code, out = integrate(pkg, atlas)
    assert code == 1 and "local/literature/texts/91000002: changed" in out
    assert snapshot(atlas) == before


def _refetched(tmp_path: Path, atlas_root: Path, stub_atlas: Path, base_edit: str = "") -> Path:
    """A package whose run re-fetched PMID:91000002's texts: a new full text, a new file, and
    no longer a file the earlier fetch had. `base_edit` drops that folder from the base, as if
    the atlas had not held it when the run started."""
    atlas = make_atlas(tmp_path, atlas_root)
    (atlas / "local/literature/texts/91000002/stale.txt").write_text("the earlier fetch's")
    base = base_json(atlas, tmp_path / "base.json")
    if base_edit == "absent at base":
        digests = base["digests"]
        assert isinstance(digests, dict)
        del digests["local/literature/texts/91000002"]
        (tmp_path / "base.json").write_text(json.dumps(base))
    work = tmp_path / "work"
    scratch(atlas, work)
    folder = work / "local/literature/texts/91000002"
    keep = {f.relative_to(folder): f.read_bytes() for f in folder.rglob("*") if f.is_file()}
    shutil.rmtree(folder)  # `texts` replaces the folder whole
    for rel, data in keep.items():
        if rel.as_posix() != "stale.txt":
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(data)
    (folder / "fulltext.txt").write_text("the re-fetched full text")
    (folder / "extra.txt").write_text("new")
    shutil.copy(folder / "fulltext.txt", tmp_path / "expected.txt")
    assert package(work, tmp_path / "base.json", tmp_path / "pkg")[0] == 0
    shutil.rmtree(atlas / RUN)
    return atlas


def test_integrate_replaces_a_text_the_run_refetched_when_the_atlas_kept_its_base(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path
) -> None:
    """R-SWA-47: `texts` replaces a folder when a supplied PDF appears or with --refresh; such
    a text could never integrate while every differing text was refused."""
    atlas = _refetched(tmp_path, atlas_root, stub_atlas)
    code, out = integrate(tmp_path / "pkg", atlas)
    assert code == 0, out
    folder = atlas / "local/literature/texts/91000002"
    assert (folder / "fulltext.txt").read_text() == "the re-fetched full text"
    assert (folder / "extra.txt").read_text() == "new"
    assert not (folder / "stale.txt").exists()  # the folder is the package's, whole
    assert (folder / "supplements/s1.txt").is_file()
    assert "1 text folder(s) replaced" in out
    assert not list(folder.parent.glob(".*"))  # no staging left behind


@pytest.mark.parametrize("case", ["absent at base", "gone now", "absent in both"])
def test_integrate_judges_a_text_folder_against_its_base(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, case: str
) -> None:
    edit = "absent at base" if case != "gone now" else ""
    atlas = _refetched(tmp_path, atlas_root, stub_atlas, edit)
    folder = atlas / "local/literature/texts/91000002"
    if case != "absent at base":
        shutil.rmtree(folder)
    before = snapshot(atlas)
    code, out = integrate(tmp_path / "pkg", atlas)
    if case == "absent in both":  # copied, as before
        assert code == 0, out
        assert (folder / "fulltext.txt").read_text() == "the re-fetched full text"
        return
    assert code == 1 and "local/literature/texts/91000002" in out
    expected = "present now" if case == "absent at base" else "gone now"
    assert expected in out
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


def test_integrate_refuses_a_text_target_that_is_a_directory(
    packaged: tuple[Path, Path, Path],
) -> None:
    atlas, pkg, _ = packaged
    target = atlas / "local/literature/texts/91000002/fulltext.txt"
    target.unlink()
    target.mkdir()
    before = snapshot(atlas)
    code, out = integrate(pkg, atlas)
    assert code == 1 and "91000002/fulltext.txt" in out and "not a file" in out
    assert "Traceback" not in out
    assert snapshot(atlas) == before and not (atlas / RUN).exists()


def test_integrate_reports_an_io_error_mid_copy_without_a_traceback(
    packaged: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from chd_curator import package as package_module

    atlas, pkg, _ = packaged
    real = package_module.copy_file
    calls = {"n": 0}

    def flaky(source: Path, target: Path) -> None:
        calls["n"] += 1
        if str(target).startswith(str(atlas / RUN)) and calls["n"] > 1:
            raise OSError("disk full")
        real(source, target)

    monkeypatch.setattr(package_module, "copy_file", flaky)
    code, out = integrate(pkg, atlas)
    assert code == 1 and "disk full" in out and "partial" in out and RUN in out


def test_the_help_says_what_a_refusal_names_and_not_every_file(  # R-SWA-62 (1)
) -> None:
    """A refusal names the files of the first check that fails, and the README says so; the
    help said "every file named", which a run that fails two checks does not do. ANSI is
    stripped: with GITHUB_ACTIONS=true typer/Rich keeps bold and dim styles under NO_COLOR."""
    result = runner.invoke(app, ["integrate", "--help"], env={"NO_COLOR": "1"})
    flat = " ".join(_plain(result.output).split())
    assert "every file named" not in flat
    assert "naming the files of the first check that fails" in flat


def test_integrate_skips_a_text_folder_absent_at_base_that_is_byte_identical(  # R-SWA-62 (2)
    tmp_path: Path, atlas_root: Path, stub_atlas: Path
) -> None:
    """Someone put the very bytes the package carries where the run found nothing: nothing
    differs, so nothing is refused (R-SWA-21's rule for identical texts) and nothing is copied."""
    atlas = _refetched(tmp_path, atlas_root, stub_atlas, "absent at base")
    folder = atlas / "local/literature/texts/91000002"
    shutil.rmtree(folder)
    shutil.copytree(tmp_path / "pkg/texts/91000002", folder)
    code, out = integrate(tmp_path / "pkg", atlas)
    assert code == 0, out
    assert "0 text folder(s) replaced" in out and "(0 already present)" not in out
    assert (folder / "fulltext.txt").read_text() == "the re-fetched full text"
    assert not list(folder.parent.glob(".*"))


def _interrupted(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> Path:
    """An atlas whose `integrate` stopped at `where` (R-SWA-62 (4)): `staging` (before the first
    rename), `swap` (between the two renames) or `cleanup` (after both, before the old folder
    was removed)."""
    from chd_curator import package as package_module

    atlas = _refetched(tmp_path, atlas_root, stub_atlas)
    real_rename, real_rmtree, calls = os.rename, shutil.rmtree, []

    def rename(src: object, dst: object) -> None:
        calls.append(str(dst))
        if where == "swap" and len(calls) == 2 and str(dst).endswith("/91000002"):
            raise OSError("interrupted")
        real_rename(src, dst)  # type: ignore[arg-type]

    def rmtree(path: object, *args: object, **kwargs: object) -> None:
        if where == "cleanup" and str(path).endswith(".91000002.replaced") and not kwargs:
            raise OSError("interrupted")
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    if where == "staging":
        real_copy = package_module.copy_file
        seen: list[Path] = []

        def copy_file(source: Path, target: Path) -> None:
            seen.append(target)
            if len(seen) == 2:
                raise OSError("interrupted")
            real_copy(source, target)

        monkeypatch.setattr(package_module, "copy_file", copy_file)
    monkeypatch.setattr(package_module.os, "rename", rename)
    monkeypatch.setattr(package_module.shutil, "rmtree", rmtree)
    code, out = integrate(tmp_path / "pkg", atlas)
    assert code == 1 and "interrupted" in out
    monkeypatch.undo()
    return atlas


@pytest.mark.parametrize(
    ("where", "must_say"),
    [
        ("staging", ["texts/.91000002.integrate", "unfinished copy", "rm -rf"]),
        (
            "swap",
            [
                "texts/.91000002.replaced",
                "texts/.91000002.integrate",
                "is absent",
                "mv local/literature/texts/.91000002.integrate local/literature/texts/91000002",
                "rm -rf local/literature/texts/.91000002.replaced",
            ],
        ),  # fmt: skip
        ("cleanup", ["texts/.91000002.replaced", "is the package's", "rm -rf"]),
    ],
)
def test_a_leftover_of_an_interrupted_integrate_is_named_with_the_way_to_finish(  # R-SWA-62 (4)
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    must_say: list[str],
) -> None:
    """`base` used to print the leftover as a text folder, and `package` then exited 2 on a name
    it could not place. Now `base` exits 2 naming each leftover, the state the folder is in
    (read from which of the two renames happened) and the commands that finish; nothing is
    removed for the curator."""
    atlas = _interrupted(tmp_path, atlas_root, stub_atlas, monkeypatch, where)
    before = snapshot(atlas)
    result = runner.invoke(app, ["base", "--atlas", str(atlas), "--gene", "TBX5"])
    assert result.exit_code == 2
    flat = " ".join(result.output.split())
    for phrase in must_say:
        assert phrase in flat, (phrase, flat)
    assert snapshot(atlas) == before
