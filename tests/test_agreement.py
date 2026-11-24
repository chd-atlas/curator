"""The agreement report: clinician against owner, per version of a card."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from archive_helpers import decisions_doc, git, make_archive
from chd_curator.agreement import pairs, report, tally
from chd_curator.archive import compact_utc
from chd_curator.archive_read import read_archive
from chd_curator.cli import app
from chd_curator.review_page import slug

G, J, A, U = (f"CHDA:LIT:000090{n}" for n in (1, 2, 3, 4))
D1, D2 = "sha256:" + "2" * 64, "sha256:" + "4" * 64
AT = "2026-11-19T12:00:00Z"


def fnd(fid: str, decision: str, digest: str, note: str = "") -> dict[str, Any]:
    return {
        "id": fid,
        "shown_digest": digest,
        "decision": decision,
        "note": note or ("because" if decision != "approve" else ""),
        "active_ms": 10,
        "decided_at": AT,
    }


def put(
    archive: Path,
    tmp: Path,
    name: str,
    role: str,
    finished: str,
    findings: list[dict[str, Any]],
    symbol: str = "TBX5",
) -> None:
    src = decisions_doc(
        tmp / "src.json",
        name=name,
        role=role,
        finished_at=finished,
        findings=findings,
        symbol=symbol,
    )
    dest = archive / symbol / "r1" / "decisions" / f"{compact_utc(finished)}--{slug(name)}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)


def commit(archive: Path) -> None:
    git(archive, "add", "--", ".")
    git(archive, "commit", "-m", "add")


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return make_archive(tmp_path)


def build(archive: Path, tmp: Path, owner: list[dict[str, Any]], dr: list[dict[str, Any]]) -> None:
    put(archive, tmp, "E.A.M", "owner", "2026-11-19T12:00:00Z", owner)
    put(archive, tmp, "Dr X", "second_opinion", "2026-11-19T13:00:00Z", dr)
    commit(archive)


def test_a_fix_on_version_one_pairs_as_agreement_after_the_owner_approves_version_two(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T12:00:00Z", [fnd(G, "fix", D1, "why")])
    put(archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T13:00:00Z", [fnd(G, "fix", D1)])
    put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T14:00:00Z", [fnd(G, "approve", D2)])
    commit(archive)
    view = read_archive(archive, "TBX5", atlas_root)
    paired, unpaired = pairs(view)
    (t,) = tally([view])
    c = t.genes["TBX5"]
    assert (len(paired), len(unpaired)) == (1, 0)
    assert (c.pairs, c.agreed, len(c.disagreed), c.with_later, c.unpaired) == (1, 1, 0, 0, 0)


def test_the_report_is_exactly_these_lines(archive: Path, tmp_path: Path, atlas_root: Path) -> None:
    build(
        archive,
        tmp_path,
        [
            fnd(G, "approve", D1),
            fnd(J, "fix", D2, "the count is wrong"),
            fnd(A, "later", D1, "ask"),
        ],
        [
            fnd(G, "approve", D1),
            fnd(J, "approve", D2),
            fnd(A, "approve", D1),
            fnd(U, "approve", D1),
        ],
    )
    view = read_archive(archive, "TBX5", atlas_root)
    assert report(tally([view]), ["TBX5"]) == [
        "Dr X: 3 pairs, 1 agreed, 1 disagreed, 1 with ask later; 1 unpaired",
        "  TBX5: 3 pairs, 1 agreed, 1 disagreed, 1 with ask later; 1 unpaired",
        '    disagreed CHDA:LIT:0000902 at sha256:44444444: owner fix "the count is wrong"; '
        'Dr X approve ""',
        "Unpaired: a clinician's verdict on a version of a card that no owner decisions file "
        "covers; approvals made in the terminal have no file.",
    ]


def test_verdicts_on_different_versions_never_pair(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    build(archive, tmp_path, [fnd(G, "approve", D2)], [fnd(G, "fix", D1, "old"), fnd(J, "fix", D1)])
    view = read_archive(archive, "TBX5", atlas_root)
    paired, unpaired = pairs(view)
    assert paired == [] and len(unpaired) == 2
    assert report(tally([view]), ["TBX5"])[:2] == [
        "Dr X: 0 pairs, 0 agreed, 0 disagreed, 0 with ask later; 2 unpaired",
        "  TBX5: 0 pairs, 0 agreed, 0 disagreed, 0 with ask later; 2 unpaired",
    ]


def test_a_clinician_written_two_ways_is_one_line_named_by_the_latest_file(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    put(archive, tmp_path, "E.A.M", "owner", AT, [fnd(G, "approve", D1), fnd(J, "approve", D1)])
    put(
        archive, tmp_path, "Dr X", "second_opinion", "2026-11-19T13:00:00Z", [fnd(G, "approve", D1)]
    )
    put(archive, tmp_path, " dr x", "second_opinion", "2026-11-19T14:00:00Z", [fnd(J, "fix", D1)])
    commit(archive)
    lines = report(tally([read_archive(archive, "TBX5", atlas_root)]), ["TBX5"])
    assert lines[:2] == [
        "dr x: 2 pairs, 1 agreed, 1 disagreed, 0 with ask later; 0 unpaired",
        "  TBX5: 2 pairs, 1 agreed, 1 disagreed, 0 with ask later; 0 unpaired",
    ]
    assert lines[2].endswith('; dr x fix "because"')


def test_no_clinician_and_triage_only_and_two_genes(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    put(archive, tmp_path, "E.A.M", "owner", AT, [fnd(G, "approve", D1)])
    commit(archive)
    assert report(tally([read_archive(archive, "TBX5", atlas_root)]), ["TBX5", "NR2F2"]) == [
        "No second opinion in the archive for TBX5, NR2F2."
    ]
    put(archive, tmp_path, "Dr X", "second_opinion", AT, [fnd(G, "approve", D1)], "NR2F2")
    put(archive, tmp_path, "E.A.M", "owner", AT, [fnd(G, "approve", D1)], "NR2F2")
    commit(archive)
    views = [read_archive(archive, s, atlas_root) for s in ("NR2F2", "TBX5")]
    assert report(tally(views), ["NR2F2", "TBX5"]) == [
        "Dr X: 1 pairs, 1 agreed, 0 disagreed, 0 with ask later; 0 unpaired",
        "  NR2F2: 1 pairs, 1 agreed, 0 disagreed, 0 with ask later; 0 unpaired",
    ]


def test_agreement_writes_nothing_and_json_names_the_commit(
    archive: Path, tmp_path: Path, atlas_root: Path
) -> None:
    build(
        archive,
        tmp_path,
        [fnd(J, "fix", D2, 'say "no"')],
        [fnd(J, "approve", D2)],
    )
    head = git(archive, "rev-parse", "HEAD").strip()
    before = sorted(p.name for p in tmp_path.iterdir())
    cwd_before = sorted(Path(".").iterdir())
    run = CliRunner().invoke(
        app, ["agreement", "--archive", str(archive), "--root", str(atlas_root)]
    )
    assert run.exit_code == 0, run.output
    assert 'owner fix "say \\"no\\""; Dr X approve ""' in run.output
    js = CliRunner().invoke(
        app, ["agreement", "TBX5", "--archive", str(archive), "--root", str(atlas_root), "--json"]
    )
    doc = json.loads(js.output)
    assert doc["archive_commit"] == head[:7] or head.startswith(doc["archive_commit"])
    (cl,) = doc["clinicians"]
    assert cl["genes"]["TBX5"]["disagreed"][0]["shown_digest"] == D2
    assert git(archive, "status", "--porcelain") == ""
    assert git(archive, "rev-parse", "HEAD").strip() == head
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert sorted(Path(".").iterdir()) == cwd_before


def test_a_dirty_archive_is_refused(archive: Path, tmp_path: Path, atlas_root: Path) -> None:
    put(archive, tmp_path, "E.A.M", "owner", AT, [fnd(G, "approve", D1)])
    run = CliRunner().invoke(
        app, ["agreement", "--archive", str(archive), "--root", str(atlas_root)]
    )
    assert run.exit_code == 2
