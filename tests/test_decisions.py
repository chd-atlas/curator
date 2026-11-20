"""``chd-curator review-decisions``: a page's store export becomes a decisions file.

Fixture: tests/fixtures/review/export copies the document shapes and timestamps of the
MMP21 pilot's real export (ArtifactData ``out_dir``, 2026-11-19), re-keyed to the fixture
page's findings, each decided finding with the ``shown_digest`` the fixture page carries.
Notes must come through byte for byte; one has non-ASCII text.
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
def export(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURE / "export", tmp_path / "export")
    return tmp_path / "export"


@pytest.fixture
def page(root: Path, stub_atlas: Path) -> Path:
    result = CliRunner().invoke(
        app,
        ["review-page", "TBX5", "--run", "r1", "--reviewer", "E.A.M", "--root", str(root)]
        + ["--atlas-cli", str(stub_atlas)],
    )
    assert result.exit_code == 0, result.output
    return root / "local/literature/runs/TBX5/r1/review.html"


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
    assert got["papers_seen"] == ["PMID:30000001", "PMID:30000002"]
    assert got["triage"] == {
        "decision": "approve",
        "note": "",
        "active_ms": 14452,
        "flips": [{"pmid": "PMID:30000002", "to": "included", "note": "an included case series"}],
    }


def test_triage_without_a_verdict_gives_no_block_but_its_papers_still_count(
    root: Path, export: Path, page: Path, tmp_path: Path
) -> None:
    edit(export, "review/triage", decision=..., note=..., decided_at=...)
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 0, output
    got = read(out)
    assert got["triage"] is None
    assert got["papers_seen"] == ["PMID:30000001", "PMID:30000002"]


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


@pytest.mark.parametrize("how", ["missing key", "null", "no session file"])
def test_a_review_not_marked_done_exits_1_and_writes_nothing(
    root: Path, export: Path, page: Path, tmp_path: Path, how: str
) -> None:
    if how == "no session file":
        (export / "session/main.json").unlink()
    else:
        edit(export, "session/main", finished_at=... if how == "missing key" else None)
    out = tmp_path / "decisions.json"
    code, output = convert(root, export, out)
    assert code == 1
    assert "has not pressed I'm done" in output
    assert not out.exists()


def stale_digest(export: Path) -> None:
    edit(export, f"review/{J}", shown_digest="sha256:" + "9" * 64)


def missing_digest(export: Path) -> None:
    edit(export, f"review/{J}", shown_digest=...)


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
        (stale_digest, [J, "decide", "again", "I'm done"]),
        (missing_digest, [J, "decide", "again", "I'm done"]),
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


def test_the_stale_message_names_only_the_stale_cards(
    root: Path, export: Path, page: Path, tmp_path: Path
) -> None:
    stale_digest(export)
    code, output = convert(root, export, tmp_path / "decisions.json")
    assert code == 2
    assert J in output and G not in output and A not in output


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
    page.unlink()
    out = tmp_path / "decisions.json"
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
    ``chd-atlas literature apply-review`` then reads it. TBX5's findings are already
    approved, so the selftest's approve is a no-op and its fix is only a printed next round:
    the literature file's bytes must not change.
    """
    root = tmp_path / "atlas"
    for name in ("curation", "mirrors", "ontologies", "schemas"):
        shutil.copytree(atlas_root / name, root / name)
    run = root / "local/literature/runs/TBX5/e2e"
    run.mkdir(parents=True)
    (run / "texts.json").write_text('{"format": 1, "papers": []}', encoding="utf-8")
    literature = root / "curation/literature/TBX5.yaml"
    before = literature.read_bytes()

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

    first, second = (c["id"] for c in page_data(page)["findings"][:2])
    applied = subprocess.run(
        [str(atlas_cli), "literature", "apply-review", "TBX5", "--decisions", str(file)]
        + ["--by", "E.A.M", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert f"already approved {first}" in applied.stdout
    assert f"next round: fix {second}: selftest" in applied.stdout
    assert literature.read_bytes() == before
