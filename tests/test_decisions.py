"""``chd-curator review-decisions``: a page's store export becomes a decisions file.

Fixture: tests/fixtures/review/export copies the document shapes and timestamps of the
MMP21 pilot's real export (ArtifactData ``out_dir``, 2026-11-19), re-keyed to the fixture
page's findings, each decided finding with the ``shown_digest`` the fixture page carries.
Notes must come through byte for byte; one has non-ASCII text. The export's flips and
acknowledgements name the fixture's own papers (PMID:90000001 is included on the page, and
PMID:90000002 is the one it asks to be read), because the converter refuses any other. The
fixture's ``session/main.json`` carries no page identity, since ``built_on`` is the build's own
clock: the ``export`` fixture stamps it from the page, as the page itself does.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from chd_curator.atlas import schema_validator
from chd_curator.cli import app
from test_review_page import dump_dom, selftest_output

FIXTURE = Path(__file__).parent / "fixtures" / "review"
G, J, A = "CHDA:LIT:0000901", "CHDA:LIT:0000902", "CHDA:LIT:0000903"
NOTE = "Größe der Kohorte prüfen — n=12"


@pytest.fixture
def root(tmp_path: Path, atlas_root: Path) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "root", ignore=shutil.ignore_patterns("export"))
    shutil.copytree(atlas_root / "schemas", tmp_path / "root" / "schemas")
    return tmp_path / "root"


@pytest.fixture
def page(root: Path, stub_atlas: Path) -> Path:
    result = CliRunner().invoke(
        app,
        ["review-page", "TBX5", "--run", "r1", "--reviewer", "E.A.M", "--root", str(root)]
        + ["--atlas-cli", str(stub_atlas)],
    )
    assert result.exit_code == 0, result.output
    return root / "local/literature/runs/TBX5/r1/review.html"


@pytest.fixture
def export(tmp_path: Path, page: Path) -> Path:
    shutil.copytree(FIXTURE / "export", tmp_path / "export")
    stamp(tmp_path / "export", page)
    return tmp_path / "export"


def convert(root: Path, export: Path, out: Path, *extra: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        app,
        ["review-decisions", "TBX5", "--run", "r1", "--export", str(export)]
        + ["--out", str(out), "--root", str(root), *extra],
    )
    return result.exit_code, result.output


def read(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def edit(export: Path, name: str, **changes: Any) -> None:
    path = export / f"{name}.json"
    doc = read(path)
    for key, value in changes.items():
        if value is ...:
            doc.pop(key, None)
        else:
            doc[key] = value
    path.write_text(json.dumps(doc), encoding="utf-8")


def stamp(export: Path, page: Path) -> None:
    """Write into session/main the identity the page itself writes there (R-P1-36)."""
    data = page_data(page)
    identity = {
        "page": {k: data["page"][k] for k in ("literature_sha256", "built_on")},
        "reviewer": data["reviewer"],
    }
    edit(export, "session/main", **identity)
    edit(export, "review/triage", built_on=data["page"]["built_on"])  # the page stamps its triage


def page_data(page: Path) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="data">(.*?)</script>',
        page.read_text(encoding="utf-8"),
        re.S,
    )
    assert match
    data: dict[str, Any] = json.loads(match.group(1).replace("<\\/", "</"))
    return data


def test_a_real_shaped_export_becomes_a_schema_valid_decisions_file(
    root: Path, export: Path, page: Path, atlas_root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 0, output
    raw = out.read_text(encoding="utf-8")
    got = json.loads(raw)
    assert list(schema_validator(atlas_root, "review_decisions").iter_errors(got)) == []
    # sorted keys, UTF-8 not escaped: the note reaches the file as typed
    assert raw == json.dumps(got, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    assert NOTE in raw

    data = page_data(page)
    docs = {i: read(export / "review" / f"{i}.json") for i in (G, J, A)}
    # in the page's card order, which is the order a reviewer saw
    assert [f["id"] for f in got["findings"]] == [c["id"] for c in data["findings"]]
    assert sorted(f["id"] for f in got["findings"]) == [G, J, A]
    for finding in got["findings"]:
        d = docs[finding["id"]]
        assert finding == {
            "id": finding["id"],
            "shown_digest": d["shown_digest"],
            "decision": d["decision"],
            "note": d["note"],
            "active_ms": d["active_ms"],
            "first_opened_at": d["first_opened_at"],
            "decided_at": d["decided_at"],
        }
    by_id = {f["id"]: f for f in got["findings"]}
    assert by_id[J]["note"] == docs[J]["note"] == NOTE
    assert by_id[J]["active_ms"] == 75733
    assert (got["gene"], got["symbol"]) == (data["page"]["gene"], data["page"]["symbol"])
    assert got["page"] == {
        k: data["page"][k] for k in ("built_on", "atlas_commit", "literature_sha256")
    }
    assert got["reviewer"] == {"name": "E.A.M", "role": "owner"}
    assert got["session"] == {
        "started_at": "2026-11-19T13:39:38.956Z",
        "finished_at": "2026-11-19T13:45:27.860Z",
        "active_ms": 323438,
    }
    assert got["papers_seen"] == ["PMID:90000002"]
    assert got["triage"] == {
        "decision": "approve",
        "note": "",
        "active_ms": 14452,
        "flips": [
            {
                "pmid": "PMID:90000001",
                "to": "excluded",
                "note": "a review article, not a case series",
            }
        ],
    }
    # the line a wrong --export directory would show: what was written, against the page
    assert output.splitlines()[-2:] == [
        "3 decisions: 2 approve, 1 fix, 0 later; triage approve; 1 papers seen; "
        "3 cards on the page",
        str(out),
    ]


def test_triage_without_a_verdict_gives_no_block_but_its_papers_still_count(
    root: Path, export: Path, page: Path, tmp_path: Path
) -> None:
    edit(export, "review/triage", decision=..., note=..., decided_at=...)
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 0, output
    got = read(out)
    assert got["triage"] is None
    assert got["papers_seen"] == ["PMID:90000002"]
    assert "triage none; 1 papers seen" in output


def test_a_card_opened_but_never_decided_is_not_a_decision(
    root: Path, export: Path, page: Path, tmp_path: Path
) -> None:
    edit(export, f"review/{J}", decision=..., note=..., decided_at=..., shown_digest=...)
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 0, output
    order = [c["id"] for c in page_data(page)["findings"] if c["id"] != J]
    assert [f["id"] for f in read(out)["findings"]] == order
    assert sorted(order) == [G, A]


@pytest.mark.parametrize("how", ["missing key", "null"])
def test_a_review_not_marked_done_exits_1_and_writes_nothing(
    root: Path, export: Path, page: Path, tmp_path: Path, how: str
) -> None:
    edit(export, "session/main", finished_at=... if how == "missing key" else None)
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 1
    assert "has not pressed I'm done" in output
    assert not out.exists()


def foreign_id(export: Path) -> None:
    shutil.copy(export / f"review/{G}.json", export / "review/CHDA:LIT:0000999.json")


def bad_decision(export: Path) -> None:
    edit(export, f"review/{G}", decision="reject")


def bad_triage_decision(export: Path) -> None:
    edit(export, "review/triage", decision="reject")


def fix_without_a_note(export: Path) -> None:
    edit(export, f"review/{J}", note="  ")


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (foreign_id, ["CHDA:LIT:0000999", "not on the page"]),
        (bad_decision, [G, "reject", "is not one of approve|fix|later"]),
        (bad_triage_decision, ["triage", "reject", "is not one of approve|fix|later"]),
        (fix_without_a_note, ["note"]),
    ],
    ids=lambda v: getattr(v, "__name__", None),
)
def test_a_refused_export_exits_2_names_the_cause_and_writes_nothing(
    root: Path, export: Path, page: Path, tmp_path: Path, mutate: Any, expected: list[str]
) -> None:
    mutate(export)
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 2, output
    for fragment in expected:
        assert fragment in output
    assert not out.exists()


@pytest.mark.parametrize("how", ["another digest", "no digest"])
def test_a_card_decided_on_an_earlier_version_is_left_out_and_named(
    root: Path, export: Path, page: Path, tmp_path: Path, how: str
) -> None:
    """R-P1-44: the page's own download leaves it out; so does this converter, and says so."""
    edit(
        export,
        f"review/{J}",
        shown_digest="sha256:" + "9" * 64 if how == "another digest" else ...,
    )
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 0, output
    assert sorted(f["id"] for f in read(out)["findings"]) == [G, A]
    lines = output.splitlines()
    assert lines[0] == f"left out, decided on an earlier version: {J}"
    assert (
        "2 decisions: 2 approve, 0 fix, 0 later" in lines[1] and "3 cards on the page" in lines[1]
    )
    assert G not in lines[0] and A not in lines[0]


def test_a_choice_changed_after_im_done_exits_1_and_writes_nothing(
    root: Path, export: Path, page: Path, tmp_path: Path
) -> None:
    for name, key in ((f"review/{J}", "decided_at"), ("review/triage", "decided_at")):
        edit(export, name, **{key: "2026-11-19T13:45:27.861Z"})  # a millisecond after finished_at
        out = tmp_path / "decisions.json"
        code, output = convert(root, export, out)
        assert code == 1 and "changed after I'm done" in output and not out.exists()
        edit(export, name, **{key: "2026-11-19T13:45:27.860Z"})  # at finished_at: allowed
    assert convert(root, export, tmp_path / "decisions.json")[0] == 0


def test_a_missing_session_file_names_the_directory(
    root: Path, export: Path, page: Path, tmp_path: Path
) -> None:
    (export / "session/main.json").unlink()
    code, output = convert(root, export, tmp_path / "decisions.json")
    assert code == 1 and f"no session/main.json in {export}" in output


def other_reviewer(export: Path) -> None:
    edit(export, "session/main", reviewer={"name": "Dr X", "role": "second_opinion"})


def other_literature(export: Path) -> None:
    edit(export, "session/main", page={"literature_sha256": "sha256:" + "b" * 64, "built_on": "x"})


def no_identity(export: Path) -> None:
    edit(export, "session/main", page=..., reviewer=...)


def foreign_flip(export: Path) -> None:
    edit(export, "review/triage", flips=[{"pmid": "PMID:30000002", "to": "excluded", "note": "n"}])


def flip_to_the_same_decision(export: Path) -> None:
    edit(export, "review/triage", flips=[{"pmid": "PMID:90000001", "to": "included", "note": "n"}])


def foreign_paper_seen(export: Path) -> None:
    edit(export, "review/triage", papers_seen=["PMID:90000001"])  # on the page, but not needed


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (other_reviewer, ["not from this page", "reviewer"]),
        (other_literature, ["not from this page", "page"]),
        (no_identity, ["not from this page", "page"]),
        (foreign_flip, ["PMID:30000002", "not a paper of this page's triage"]),
        (flip_to_the_same_decision, ["PMID:90000001", "'included'", "excluded"]),
        (foreign_paper_seen, ["PMID:90000001", "does not ask to be read"]),
    ],
    ids=lambda v: getattr(v, "__name__", None),
)
def test_an_export_that_is_not_this_pages_is_refused_whole(
    root: Path, export: Path, page: Path, tmp_path: Path, mutate: Any, expected: list[str]
) -> None:
    """R-P1-36: the identity the page writes into its own store, and papers it holds."""
    mutate(export)
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 2, output
    for fragment in expected:
        assert fragment in output
    assert not out.exists()


def test_an_export_for_another_gene_is_refused(
    root: Path, export: Path, page: Path, tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        app,
        ["review-decisions", "GATA4", "--run", "r1", "--export", str(export)]
        + ["--out", str(tmp_path / "d.json"), "--root", str(root), "--page", str(page)],
    )
    assert result.exit_code == 2 and "page for TBX5, not GATA4" in result.output


@pytest.mark.parametrize("html", ["<html></html>", "twice"])
def test_a_page_without_exactly_one_data_element_exits_2(
    root: Path, export: Path, page: Path, tmp_path: Path, html: str
) -> None:
    text = page.read_text(encoding="utf-8")
    page.write_text(
        "<html></html>"
        if html != "twice"
        else text + text[text.index('<script type="application/json"') :],
        encoding="utf-8",
    )
    code, output = convert(root, export, tmp_path / "decisions.json")
    assert code == 2, output
    assert "data" in output and not (tmp_path / "decisions.json").exists()


def test_page_defaults_to_the_owner_page_and_can_be_pointed_elsewhere(
    root: Path, export: Path, page: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        app,
        ["review-page", "TBX5", "--run", "r1", "--reviewer", "Dr X", "--role", "second_opinion"]
        + ["--root", str(root), "--atlas-cli", str(stub_atlas)],
    )
    assert result.exit_code == 0, result.output
    second = page.parent / "review-dr-x.html"
    out = tmp_path / "decisions.json"
    stamp(export, second)  # a second opinion's store
    # the owner page against that export (R-P1-36): refused, not written with the owner's name
    code, output = convert(root, export, out)
    assert code == 2 and "not from this page" in output and not out.exists()
    page.unlink()
    code, output = convert(root, export, out)
    assert code == 2 and "review.html" in output
    code, output = convert(root, export, out, "--page", str(second))
    assert code == 0, output
    assert read(out)["reviewer"] == {"name": "Dr X", "role": "second_opinion"}


def test_a_page_download_validates_and_the_real_atlas_applies_it(
    tmp_path: Path, atlas_root: Path, atlas_cli: Path
) -> None:
    """Spec section 9, end to end on a copy of the real atlas (never the checkout itself).

    The page's own selftest downloads a decisions file in headless Chrome; the real
    ``chd-atlas literature apply-review`` then reads it. One approval is stripped in the copy,
    from a finding the verifier has compared, so "the page's digest is the atlas's digest, so
    the atlas approves it" runs end to end: the approve is applied (``approved <id>``) and the
    file carries the same ``approved_digest`` the stripped approval had. The page's second card
    is a fix, which apply-review only prints.
    """
    root = tmp_path / "atlas"
    for name in ("curation", "mirrors", "ontologies", "schemas"):
        shutil.copytree(atlas_root / name, root / name)
    run = root / "local/literature/runs/TBX5/e2e"
    run.mkdir(parents=True)
    (run / "texts.json").write_text('{"format": 1, "papers": []}', encoding="utf-8")
    ids = ["CHDA:LIT:0000019", "CHDA:LIT:0000020", "CHDA:LIT:0000021"]
    (run / "comparison.json").write_text(
        json.dumps(
            {
                "format": 1,
                "pairs": {i: i for i in ids},
                "agreed": ids,
                "span_only": [],
                "differs": {},
            }
        ),
        encoding="utf-8",
    )
    literature = root / "curation/literature/TBX5.yaml"
    text = literature.read_text(encoding="utf-8")
    block = re.search(
        r"(  - id: CHDA:LIT:0000019\n(?:    .*\n|\n)*?)"
        r"    approved_by: .*\n    approved_on: .*\n    approved_digest: (sha256:\w+)\n",
        text,
    )
    assert block and "verified_by:" in block.group(1)
    digest = block.group(2)
    stripped = text.replace(block.group(0), block.group(1))
    assert stripped != text
    literature.write_text(stripped, encoding="utf-8")
    others = {i: stripped.count(i) for i in ids}

    built = CliRunner().invoke(
        app,
        ["review-page", "TBX5", "--run", "e2e", "--reviewer", "E.A.M", "--root", str(root)]
        + ["--atlas-cli", str(atlas_cli)],
    )
    assert built.exit_code == 0, built.output
    page = run / "review.html"
    decisions = selftest_output(dump_dom(page))
    file = tmp_path / "decisions.json"
    file.write_text(json.dumps(decisions), encoding="utf-8")
    assert list(schema_validator(root, "review_decisions").iter_errors(decisions)) == []
    assert [c["id"] for c in page_data(page)["findings"]] == ids

    applied = subprocess.run(
        [str(atlas_cli), "literature", "apply-review", "TBX5", "--decisions", str(file)]
        + ["--by", "E.A.M", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert f"approved {ids[0]} (E.A.M, " in applied.stdout
    assert f"note on {ids[0]}: selftest" in applied.stdout  # a stray selftest file shows itself
    assert f"next round: fix {ids[1]}: selftest" in applied.stdout
    after = literature.read_text(encoding="utf-8")
    assert after.count(f"approved_digest: {digest}") == 1  # the digest the stripped approval had
    assert {i: after.count(i) for i in ids} == others


def test_an_export_with_no_finding_documents_still_prints_its_count_line(
    root: Path, export: Path, page: Path, tmp_path: Path
) -> None:
    """A reviewer who decided nothing gets the usual count line, with 0 decisions."""
    for doc_id in (G, J, A):
        (export / "review" / f"{doc_id}.json").unlink()
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 0, output
    assert read(out)["findings"] == []
    assert "0 decisions: 0 approve, 0 fix, 0 later" in output.splitlines()[-2]
    assert "3 cards on the page" in output


@pytest.mark.parametrize("stamped", ["this page", "another page", "unstamped"])
def test_a_triage_document_not_made_on_this_page_exports_only_the_papers_seen(
    root: Path, export: Path, page: Path, tmp_path: Path, stamped: str
) -> None:
    """R-P4-18/21: the conversion matches the page. A triage verdict made on another page (or by a
    page that stamped nothing) is left out, with a line saying so, and its papers_seen is kept
    only for the papers this page asks to be read; it never closes a flip this page did not show."""
    built = page_data(page)["page"]["built_on"]
    stamp_value = {"this page": built, "another page": "2020-01-01T00:00:00+00:00"}.get(
        stamped, ...
    )
    seen = ["PMID:90000002"] + ([] if stamped == "this page" else ["PMID:99999999"])
    edit(export, "review/triage", built_on=stamp_value, papers_seen=seen)
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 0, output
    got = read(out)
    line = "left out a triage verdict that was not made on this page"
    if stamped == "this page":
        assert got["triage"] is not None and got["triage"]["decision"] == "approve"
        assert line not in output
    else:
        assert got["triage"] is None
        assert got["papers_seen"] == ["PMID:90000002"]
        assert output.count(line) == 1
