"""``chd-curator archive-page`` and ``archive.py``, against a fixture archive under tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from archive_helpers import git, make_archive, page_html
from chd_curator.archive import ArchiveError, check_id, compact_utc
from chd_curator.cli import app

runner = CliRunner()
PAGE = "TBX5/r1/pages/20261119T120000Z--e-a-m.html"


def file_it(
    archive: Path, page: Path, *extra: str, symbol: str = "TBX5", run: str = "r1"
) -> Result:
    return runner.invoke(
        app,
        [
            "archive-page",
            symbol,
            "--run",
            run,
            "--page",
            str(page),
            "--archive",
            str(archive),
            *extra,
        ],
    )


def test_compact_utc_names_the_instant_in_utc() -> None:
    assert compact_utc("2026-11-20T11:27:24.668111+00:00") == "20261120T112724Z"
    assert compact_utc("2026-11-20T13:27:24+02:00") == "20261120T112724Z"
    assert compact_utc("2026-11-20T13:49:25.824Z") == "20261120T134925Z"
    with pytest.raises(ArchiveError):
        compact_utc("2026-11-20T11:27:24")


def test_a_page_is_filed_byte_for_byte_and_committed(tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    page = page_html(tmp_path / "p.html")
    before = git(archive, "rev-parse", "HEAD").strip()
    assert check_id("run id", "r1.2") == "r1.2"
    result = file_it(archive, page)
    assert result.exit_code == 0, result.output
    assert (archive / PAGE).read_bytes() == page.read_bytes()
    assert git(archive, "status", "--porcelain") == ""
    assert git(archive, "log", "-1", "--format=%B").strip() == "file TBX5 r1: page"
    assert git(archive, "diff", "--name-only", before, "HEAD").split() == [PAGE]
    lines = result.output.splitlines()
    assert lines[0] == f"filed {PAGE}"
    assert lines[1].startswith("committed ")


def test_refiling_the_same_page_is_already_filed_and_a_different_one_is_refused(
    tmp_path: Path,
) -> None:
    archive = make_archive(tmp_path)
    page = page_html(tmp_path / "p.html")
    assert file_it(archive, page).exit_code == 0
    head = git(archive, "rev-parse", "HEAD").strip()
    again = file_it(archive, page)
    assert again.exit_code == 0
    assert again.output.strip() == f"already filed {PAGE}"
    assert git(archive, "rev-parse", "HEAD").strip() == head
    other = page_html(tmp_path / "q.html", extra={"groups": ["different"]})
    refused = file_it(archive, other)
    assert refused.exit_code == 2
    assert "already holds" in refused.output
    assert (archive / PAGE).read_bytes() == page.read_bytes()
    assert git(archive, "rev-parse", "HEAD").strip() == head


def test_an_artifact_url_is_filed_beside_the_page_even_after_the_page(tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    page = page_html(tmp_path / "p.html")
    assert file_it(archive, page).exit_code == 0
    head = git(archive, "rev-parse", "HEAD").strip()
    result = file_it(archive, page, "--artifact", "https://claude.ai/artifact/abc")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:2] == [
        f"already filed {PAGE}",
        "filed " + PAGE.replace(".html", ".artifact"),
    ]
    artifact = archive / PAGE.replace(".html", ".artifact")
    assert artifact.read_text(encoding="utf-8") == "https://claude.ai/artifact/abc\n"
    assert git(archive, "rev-list", "--count", f"{head}..HEAD").strip() == "1"
    assert git(archive, "status", "--porcelain") == ""
    different = file_it(archive, page, "--artifact", "https://claude.ai/artifact/other")
    assert different.exit_code == 2
    assert "already holds" in different.output


def test_an_uncommitted_filed_page_is_committed_on_a_refile(tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    page = page_html(tmp_path / "p.html")
    (archive / PAGE).parent.mkdir(parents=True)
    (archive / PAGE).write_bytes(page.read_bytes())
    assert git(archive, "status", "--porcelain").strip() != ""
    result = file_it(archive, page)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == f"already filed {PAGE}"
    assert result.output.splitlines()[1].startswith("committed ")
    assert git(archive, "status", "--porcelain") == ""


def _plain_dir(tmp: Path) -> Path:
    plain = tmp / "plain"
    plain.mkdir()
    return plain


def _no_marker(tmp: Path) -> Path:
    repo = tmp / "nomarker"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "x").write_text("x\n", encoding="utf-8")
    git(repo, "add", "--", "x")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-m", "x")
    return repo


def _subdir(tmp: Path) -> Path:
    sub = make_archive(tmp) / "TBX5"
    sub.mkdir()
    return sub


@pytest.mark.parametrize(
    ("case", "phrase"),
    [
        ("plain", "not a review archive"),
        ("nomarker", "not a review archive"),
        ("subdir", "not the top"),
        ("noidentity", "user.name"),
        ("othergene", "is for GATA4"),
        ("badrun", "run id"),
        ("nodata", "data element"),
        ("badartifact", "https://claude.ai/"),
    ],
)
def test_archive_page_refuses_and_writes_nothing(tmp_path: Path, case: str, phrase: str) -> None:
    page = page_html(tmp_path / "p.html")
    run, extra = "r1", []
    if case == "plain":
        archive = _plain_dir(tmp_path)
    elif case == "nomarker":
        archive = _no_marker(tmp_path)
    elif case == "subdir":
        archive = _subdir(tmp_path)
    else:
        archive = make_archive(tmp_path, identity=case != "noidentity")
    if case == "othergene":
        page = page_html(tmp_path / "g.html", symbol="GATA4")
    elif case == "badrun":
        run = "../x"
    elif case == "nodata":
        page = tmp_path / "n.html"
        page.write_text("<html></html>\n", encoding="utf-8")
    elif case == "badartifact":
        extra = ["--artifact", "http://example.org/a"]
    repo = tmp_path / "archive" if case == "subdir" else archive
    head = git(repo, "rev-parse", "HEAD").strip() if case not in ("plain",) else None
    result = file_it(archive, page, *extra, run=run)
    assert result.exit_code == 2, result.output
    assert phrase in result.output
    if head is not None:
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "rev-parse", "HEAD").strip() == head
    assert not any(p.name == "pages" for p in tmp_path.rglob("pages"))


def test_a_git_failure_is_exit_1(tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    page = page_html(tmp_path / "p.html")
    hook = archive / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    result = file_it(archive, page)
    assert result.exit_code == 1, result.output
    assert "git commit failed" in result.output
    assert git(archive, "log", "--format=%s").strip() == "start the archive"
