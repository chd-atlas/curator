"""The archive reader: latest verdicts by instant, one reviewer per key, what is still owed."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from archive_helpers import decisions_doc, git, make_archive
from chd_curator.archive import ArchiveError, compact_utc
from chd_curator.archive_read import (
    ArchiveView,
    files_line,
    latest,
    read_archive,
    state,
)
from chd_curator.cli import app
from chd_curator.review_page import slug

G, J, A = "CHDA:LIT:0000901", "CHDA:LIT:0000902", "CHDA:LIT:0000903"
D1, D2 = "sha256:" + "2" * 64, "sha256:" + "4" * 64
FIXTURE = Path(__file__).parent / "fixtures" / "review"


def finding(fid: str, decision: str, digest: str, at: str, note: str = "") -> dict[str, Any]:
    return {
        "id": fid,
        "shown_digest": digest,
        "decision": decision,
        "note": note or ("because" if decision != "approve" else ""),
        "active_ms": 10,
        "decided_at": at,
    }


def put(
    archive: Path,
    tmp: Path,
    name: str,
    role: str,
    finished: str,
    findings: list[dict[str, Any]],
    triage: dict[str, Any] | None = None,
    run: str = "r1",
) -> Path:
    src = decisions_doc(
        tmp / "src.json",
        name=name,
        role=role,
        finished_at=finished,
        findings=findings,
        triage=triage,
    )
    dest = archive / "TBX5" / run / "decisions" / f"{compact_utc(finished)}--{slug(name)}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    return dest


def commit_all(archive: Path) -> None:
    git(archive, "add", "--", "TBX5")
    git(archive, "commit", "-m", "add")


def view(archive: Path, root: Path) -> ArchiveView:
    return read_archive(archive, "TBX5", root)


@pytest.fixture
def root(tmp_path: Path, atlas_root: Path) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "root")
    shutil.copytree(atlas_root / "schemas", tmp_path / "root" / "schemas")
    return tmp_path / "root"


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return make_archive(tmp_path)


def test_the_latest_verdict_compares_instants_not_strings(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    # As strings "…T13:00:00+02:00" sorts after "…T12:00:00Z"; as instants it is 11:00Z.
    put(
        archive, tmp_path, "E.A.M", "owner", "2026-11-19T13:00:00+02:00",
        [finding(G, "fix", D1, "2026-11-19T13:00:00+02:00")],
    )  # fmt: skip
    put(
        archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z",
        [finding(G, "approve", D1, "2026-11-19T12:00:00Z")],
    )  # fmt: skip
    commit_all(archive)
    got = latest(view(archive, atlas_root).verdicts)
    assert got[("owner", G)].decision == "approve"


def test_one_clinician_written_two_ways_is_one_reviewer(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    put(archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T10:00:00Z",
        [finding(G, "fix", D1, "2026-11-19T10:00:00Z")])  # fmt: skip
    put(archive, tmp_path, " dr x", "second_opinion", "2026-11-19T11:00:00Z",
        [finding(G, "later", D1, "2026-11-19T11:00:00Z")])  # fmt: skip
    put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z",
        [finding(G, "approve", D1, "2026-11-19T12:00:00Z")])  # fmt: skip
    commit_all(archive)
    v = view(archive, atlas_root)
    assert (v.files, v.reviewers) == (3, 2)
    got = latest(v.verdicts)
    assert got[("dr x", G)].decision == "later"
    assert sorted(k for k in got) == [("dr x", G), ("owner", G)]


def test_per_version_latest_keeps_each_digest(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    put(archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T10:00:00Z",
        [finding(G, "fix", D1, "2026-11-19T10:00:00Z")])  # fmt: skip
    put(archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T11:00:00Z",
        [finding(G, "approve", D2, "2026-11-19T11:00:00Z")])  # fmt: skip
    commit_all(archive)
    verdicts = view(archive, atlas_root).verdicts
    assert len(latest(verdicts)) == 1
    both = latest(verdicts, per_version=True)
    assert {k[2] for k in both} == {D1, D2}


def test_state_marks_current_and_pending_from_the_current_files(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    put(
        archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z",
        [
            finding(G, "fix", D1, "2026-11-19T12:00:00Z"),
            finding(J, "fix", D1, "2026-11-19T12:00:00Z"),
            finding(A, "approve", D2, "2026-11-19T12:00:00Z"),
            finding("CHDA:LIT:0000999", "later", D1, "2026-11-19T12:00:00Z"),
        ],
        triage={
            "decision": "fix",
            "note": "two papers",
            "active_ms": 5,
            "flips": [
                {"pmid": "PMID:90000001", "to": "excluded", "note": "an erratum"},
                {"pmid": "PMID:90000002", "to": "excluded", "note": "a review"},
                {"pmid": "PMID:99999999", "to": "excluded", "note": "gone from triage"},
            ],
        },
    )  # fmt: skip
    put(archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T13:00:00Z",
        [finding(G, "later", D1, "2026-11-19T13:00:00Z")])  # fmt: skip
    commit_all(archive)
    got = state(
        view(archive, atlas_root),
        {G: D1, J: D2, A: D2},
        {"PMID:90000001": "included", "PMID:90000002": "excluded"},
    )
    assert set(got) == {"format", "symbol", "gene", "archive_commit", "findings", "triage"}
    by_id = {f["id"]: f for f in got["findings"]}
    assert [f["id"] for f in got["findings"]] == sorted(by_id)
    mark = lambda fid, who: {  # noqa: E731
        k: v for k, v in by_id[fid]["verdicts"][who].items() if k in ("current", "pending")
    }
    assert [v["name"] for v in by_id[G]["verdicts"]] == ["E.A.M", "Dr X"]
    assert mark(G, 0) == {"current": True, "pending": True}
    assert mark(G, 1) == {"current": True, "pending": True}
    assert mark(J, 0) == {"current": False, "pending": False}  # fix on d1, current is d2
    assert mark(A, 0) == {"current": True, "pending": False}  # an approve is never pending
    gone = by_id["CHDA:LIT:0000999"]
    assert (gone["in_file"], gone["current_digest"]) == (False, None)
    assert (by_id[G]["in_file"], by_id[G]["current_digest"]) == (True, D1)
    assert gone["verdicts"][0]["pending"] is False
    flips = got["triage"]["verdicts"][0]["flips"]
    assert [(f["pmid"], f["pending"]) for f in flips] == [
        ("PMID:90000001", True),
        ("PMID:90000002", False),
        ("PMID:99999999", False),
    ]
    assert set(got["triage"]["verdicts"][0]) == {
        "name", "role", "decision", "note", "finished_at", "file", "flips",
    }  # fmt: skip
    assert set(by_id[G]["verdicts"][0]) == {
        "name", "role", "decision", "note", "shown_digest", "finished_at", "decided_at",
        "file", "current", "pending",
    }  # fmt: skip


def test_later_on_the_current_digest_is_pending(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z",
        [finding(G, "later", D1, "2026-11-19T12:00:00Z")])  # fmt: skip
    commit_all(archive)
    got = state(view(archive, atlas_root), {G: D1}, {})
    assert got["findings"][0]["verdicts"][0]["pending"] is True
    assert got["triage"] == {"verdicts": []}


@pytest.mark.parametrize(
    ("case", "phrase"),
    [
        ("dirty", "uncommitted"),
        ("misnamed", "is named"),
        ("othersymbol", "not TBX5"),
        ("unfinished", "not finished"),
        ("twogenes", "two genes"),
        ("notjson", "is not a .json file"),
        ("ownerkey", "keyed as the owner"),
        ("twice", f"decides {G} more than once"),
    ],
)
def test_read_archive_refuses(
    case: str, phrase: str, archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    f = [finding(G, "approve", D1, "2026-11-19T12:00:00Z")]
    first = put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z", f)
    if case == "misnamed":
        first.rename(first.with_name("wrong.json"))
    elif case in ("othersymbol", "unfinished", "twogenes"):
        doc = json.loads(first.read_text("utf-8"))
        if case == "othersymbol":
            doc["symbol"] = "GATA4"
        elif case == "unfinished":
            doc["session"]["finished_at"] = None
        else:
            second = put(archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T13:00:00Z", f)
            other = json.loads(second.read_text("utf-8"))
            other["gene"] = "HGNC:4173"
            second.write_text(json.dumps(other), "utf-8")
        first.write_text(json.dumps(doc), "utf-8")
    elif case == "ownerkey":
        first.unlink()
        put(archive, tmp_path, " Owner", "second_opinion", "2026-11-19T12:00:00Z", f)
    elif case == "twice":
        first.unlink()
        both = [*f, finding(G, "fix", D1, "2026-11-19T12:30:00Z")]
        put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z", both)
    elif case == "notjson":
        (first.parent / "notes.txt").write_text("hello", "utf-8")
    commit_all(archive)
    if case == "dirty":
        (archive / "stray.txt").write_text("x", "utf-8")
    with pytest.raises(ArchiveError, match=phrase):
        view(archive, atlas_root)


@pytest.mark.parametrize(
    ("setup", "phrase"),
    [("showUntrackedFiles", "uncommitted"), ("ignored", "not in HEAD")],
)
def test_read_archive_refuses_a_decisions_file_that_is_not_in_head(
    setup: str, phrase: str, archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    f = [finding(G, "approve", D1, "2026-11-19T12:00:00Z")]
    put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z", f)
    commit_all(archive)
    if setup == "showUntrackedFiles":
        git(archive, "config", "status.showUntrackedFiles", "no")
    else:
        (archive / ".git" / "info" / "exclude").write_text("*.json\n", "utf-8")
    put(archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T13:00:00Z", f)
    with pytest.raises(ArchiveError, match=phrase):
        view(archive, atlas_root)


def test_the_reader_reads_head_not_a_working_copy_git_was_told_to_ignore(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    path = put(
        archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z",
        [finding(G, "approve", D1, "2026-11-19T12:00:00Z")],
    )  # fmt: skip
    commit_all(archive)
    git(archive, "update-index", "--assume-unchanged", str(path.relative_to(archive)))
    doc = json.loads(path.read_text("utf-8"))
    doc["findings"][0]["decision"] = "fix"
    doc["findings"][0]["note"] = "edited"
    path.write_text(json.dumps(doc), "utf-8")
    assert git(archive, "status", "--porcelain") == ""
    got = latest(view(archive, atlas_root).verdicts)
    assert got[("owner", G)].decision == "approve"


def test_an_archive_with_no_file_for_the_symbol_is_empty(archive: Path, atlas_root: Path) -> None:
    v = view(archive, atlas_root)
    assert (v.gene, v.files, v.reviewers, v.verdicts) == (None, 0, 0, ())


def test_files_line_counts_in_words() -> None:
    def one(files: int, reviewers: int) -> str:
        return files_line(ArchiveView("TBX5", None, "abc", files, reviewers, ()))

    assert one(1, 1) == "1 decisions file from 1 reviewer"
    assert one(3, 2) == "3 decisions files from 2 reviewers"


def run_state(
    archive: Path, root: Path, stub: Path, *extra: str, symbol: str = "TBX5"
) -> tuple[int, str]:
    result = CliRunner().invoke(
        app,
        ["archive-state", symbol, "--archive", str(archive), "--root", str(root),
         "--atlas-cli", str(stub), *extra],
    )  # fmt: skip
    return result.exit_code, result.output


def test_archive_state_prints_the_json_contract_and_the_line(
    archive: Path, tmp_path: Path, root: Path, stub_atlas: Path
) -> None:
    put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z",
        [finding(G, "fix", D1, "2026-11-19T12:00:00Z"),
         finding(J, "approve", D2, "2026-11-19T12:00:00Z")],
        triage={"decision": "fix", "note": "n", "active_ms": 1,
                "flips": [{"pmid": "PMID:90000001", "to": "excluded", "note": "x"},
                          {"pmid": "PMID:90000002", "to": "included", "note": "y"}]})  # fmt: skip
    put(archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T13:00:00Z",
        [finding(G, "later", D1, "2026-11-19T13:00:00Z")])  # fmt: skip
    commit_all(archive)
    code, out = run_state(archive, root, stub_atlas, "--json")
    assert code == 0, out
    data = json.loads(out)
    assert set(data) == {"format", "symbol", "gene", "archive_commit", "findings", "triage"}
    assert data["gene"] == "HGNC:11604"
    assert set(data["findings"][0]) == {"id", "in_file", "current_digest", "verdicts"}
    assert set(data["findings"][0]["verdicts"][0]) == {
        "name", "role", "decision", "note", "shown_digest", "finished_at", "decided_at",
        "file", "current", "pending",
    }  # fmt: skip
    datetime.fromisoformat(data["findings"][0]["verdicts"][0]["finished_at"])
    commit = git(archive, "rev-parse", "--short", "HEAD").strip()
    # G: owner fix and Dr X later are both pending; J is an approve; one flip is pending.
    code, out = run_state(archive, root, stub_atlas)
    assert code == 0, out
    assert out == (
        "TBX5: 2 findings with verdicts in 2 decisions files from 2 reviewers; "
        f"2 pending fix or ask later, 1 pending flip (archive {commit})\n"
    )


def test_archive_state_refuses_another_gene_and_a_dirty_archive(
    archive: Path, tmp_path: Path, root: Path, stub_atlas: Path
) -> None:
    f = [finding(G, "approve", D1, "2026-11-19T12:00:00Z")]
    path = put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z", f)
    doc = json.loads(path.read_text("utf-8"))
    doc["gene"] = "HGNC:4173"
    path.write_text(json.dumps(doc), "utf-8")
    commit_all(archive)
    code, out = run_state(archive, root, stub_atlas)
    assert code == 2
    assert "HGNC:4173" in out
    (archive / "stray.txt").write_text("x", "utf-8")
    code, out = run_state(archive, root, stub_atlas)
    assert code == 2
    assert "uncommitted" in out
