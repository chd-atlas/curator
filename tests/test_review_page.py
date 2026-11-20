"""``chd-curator review-page``: the page it writes, and the decisions file the page produces.

The page is one HTML file whose behaviour is JavaScript, so its tests run it: headless Chrome
opens ``review.html#selftest`` (file mode: no ``window.claude``), the page drives itself
through a decision of each kind and writes the decisions JSON into ``#selftest-output``, and
the test validates that JSON against the atlas's exported schema. A missing Chrome fails the
test; it is never skipped, because a skipped page test would leave the page unchecked while
the suite stayed green.

Fixture: tests/fixtures/review (TBX5, run r1; the stub atlas answers its digests and
``validate``). ``STUB_VALIDATE_ERRORS`` makes the stub's ``validate`` report errors.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from chd_curator import review_page
from chd_curator.atlas import schema_validator
from chd_curator.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "review"
G, J, A = "CHDA:LIT:0000901", "CHDA:LIT:0000902", "CHDA:LIT:0000903"


@pytest.fixture
def root(tmp_path: Path, atlas_root: Path) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "root")
    shutil.copytree(atlas_root / "schemas", tmp_path / "root" / "schemas")
    return tmp_path / "root"


def build(root: Path, stub: Path, *extra: str) -> tuple[int, str]:
    if "--reviewer" not in extra:
        extra = ("--reviewer", "E.A.M", *extra)
    result = CliRunner().invoke(
        app,
        [
            "review-page",
            "TBX5",
            "--run",
            "r1",
            "--root",
            str(root),
            "--atlas-cli",
            str(stub),
            *extra,
        ],
    )
    return result.exit_code, result.output


def inline_data(page: str) -> dict[str, Any]:
    match = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
    assert match
    data: dict[str, Any] = json.loads(match.group(1))
    return data


def chrome() -> str:
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if found is None:
        pytest.fail("headless Chrome (google-chrome or chromium) is required for the page tests")
    return found


def dump_dom(page: Path, fragment: str = "selftest", budget: int = 5000) -> str:
    done = subprocess.run(
        [
            chrome(),
            "--headless=new",
            "--disable-gpu",
            f"--virtual-time-budget={budget}",
            "--dump-dom",
            f"file://{page}#{fragment}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-500:]
    return done.stdout


def visible(dom: str) -> str:
    """The dumped DOM without its scripts, whose source holds every string the page can show."""
    return re.sub(r"<script.*?</script>", "", dom, flags=re.S)


def quote_marks(dom: str) -> list[str]:
    """The marks on every rendered quote, not the script's own strings holding the same words."""
    out: list[str] = []
    for quote in re.findall(r'<div class="q(?: unchecked| over)*">(.*?)</div>', visible(dom), re.S):
        out += re.findall(r'<span class="flag(?: warn)?">(.*?)</span>', quote)
    return out


def selftest_output(dom: str) -> dict[str, Any]:
    match = re.search(r'<pre id="selftest-output"[^>]*>(.*?)</pre>', dom, re.S)
    assert match, "the page wrote no #selftest-output"
    out: dict[str, Any] = json.loads(html.unescape(match.group(1)))
    return out


def data_of(page: Path) -> dict[str, Any]:
    return inline_data(page.read_text(encoding="utf-8"))


def second_opinion_file(path: Path, **overrides: Any) -> Path:
    doc: dict[str, Any] = {
        "format": 1,
        "gene": "HGNC:11604",
        "symbol": "TBX5",
        "page": {
            "built_on": "2026-11-19T12:00:00+00:00",
            "atlas_commit": None,
            "literature_sha256": "sha256:" + "a" * 64,
        },
        "reviewer": {"name": "Dr X", "role": "second_opinion"},
        "session": {"started_at": None, "finished_at": "2026-11-19T13:00:00Z", "active_ms": 5},
        "findings": [
            {
                "id": J,
                "shown_digest": "sha256:" + "4" * 64,
                "decision": "fix",
                "note": "the carrier count is wrong",
                "active_ms": 10,
                "decided_at": "2026-11-19T13:00:00Z",
            },
            {
                "id": G,
                "shown_digest": "sha256:" + "2" * 64,
                "decision": "approve",
                "note": "",
                "active_ms": 10,
                "decided_at": "2026-11-19T13:00:00Z",
            },
        ],
        **overrides,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_the_page_inlines_its_data_and_cannot_be_broken_out_of(
    root: Path, stub_atlas: Path
) -> None:
    hostile = "</script><script>alert(1)</script> <!-- x"
    lit = root / "curation/literature/TBX5.yaml"
    text = lit.read_text(encoding="utf-8")
    swapped, n = re.subn(
        r"^    summary: One family with Holt-Oram.*$",
        f"    summary: {json.dumps(hostile)}",
        text,
        flags=re.M,
    )
    assert n == 1
    lit.write_text(swapped, encoding="utf-8")
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = (root / "local/literature/runs/TBX5/r1/review.html").read_text(encoding="utf-8")
    assert out.strip().endswith("review.html")
    # `</` and `<!--` are the two ways text ends or double-escapes a script element
    assert page.count("</script>") == 2 and "<!--" not in page
    assert hostile in next(c["summary"] for c in inline_data(page)["findings"] if c["id"] == G)
    assert "__DATA__" not in page and "claude:viewport-fit-sides" in page


@pytest.mark.parametrize("role", ["owner", "second_opinion"])
def test_selftest_download_is_a_valid_decisions_file(
    root: Path, stub_atlas: Path, atlas_root: Path, role: str
) -> None:
    code, out = build(
        root, stub_atlas, "--reviewer", "E.A.M" if role == "owner" else "Dr X", "--role", role
    )
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    assert page.name == ("review.html" if role == "owner" else "review-dr-x.html")
    dom = dump_dom(page)
    decisions = selftest_output(dom)
    errors = [
        e.message for e in schema_validator(atlas_root, "review_decisions").iter_errors(decisions)
    ]
    assert errors == []
    shown = {
        c["id"]: c["shown_digest"]
        for c in inline_data(page.read_text(encoding="utf-8"))["findings"]
    }
    first, second = decisions["findings"]
    assert (first["id"], first["decision"], first["shown_digest"]) == (J, "approve", shown[J])
    assert (second["id"], second["decision"], second["note"]) == (A, "fix", "selftest")
    assert shown[A] == second["shown_digest"]
    assert decisions["triage"]["decision"] == "approve"
    assert decisions["triage"]["flips"][0]["note"] == "selftest"
    assert decisions["session"]["finished_at"] is not None
    assert decisions["reviewer"] == {"name": "E.A.M" if role == "owner" else "Dr X", "role": role}
    # the selftest also presses fix with no note and records a flip with no reason: neither lands
    assert [f["id"] for f in decisions["findings"]] == [J, A]
    assert [f["pmid"] for f in decisions["triage"]["flips"]] == [data_of(page)["triage"][0]["pmid"]]
    assert decisions["papers_seen"] == ["PMID:90000002"] and decisions["format"] == 1
    status = json.loads(
        html.unescape(re.search(r'<pre id="selftest-status"[^>]*>(.*?)</pre>', dom, re.S).group(1))
    )  # type: ignore[union-attr]
    assert any("this browser" in m for m in status)
    assert not [m for m in status if "saved to the page's store" in m.lower()]
    assert "saved to the page's store" not in dom.lower()
    button = "Agree" if role == "second_opinion" else "Approve"
    shown_dom = visible(dom)
    assert f">{button}</button>" in shown_dom
    assert ">Agree</button>" not in shown_dom or role == "second_opinion"
    # a second-opinion page renders no approval state at all
    assert (">already approved<" in shown_dom) is (role == "owner")
    assert (">approval void: re-approve<" in shown_dom) is (role == "owner")


def test_the_papers_needed_card_and_every_quote_state_render(root: Path, stub_atlas: Path) -> None:
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    data = inline_data(page.read_text(encoding="utf-8"))
    assert [p["pmid"] for p in data["papers_needed"]] == ["PMID:90000002"]
    quotes = [
        q
        for c in data["findings"]
        for holder in (*c["fields"], *c["variants"])
        for q in holder["quotes"]
    ]
    assert any(q["over_limit"] for q in quotes) and any(
        q["checked"] and not q["found"] for q in quotes
    )
    dom = dump_dom(page, "")
    marks = quote_marks(dom)
    assert any(m.startswith("over 30 words") for m in marks) and "not found in text" in marks
    assert "Papers whose full text the readers did not have" in dom
    # a quote naming no row is listed, not dropped
    data["findings"][0]["other_quotes"] = [
        {
            "quote": "loose words",
            "where": "Intro",
            "checked": True,
            "found": True,
            "over_limit": False,
            "words": 2,
        }
    ]
    loose = visible(dump_dom(tmp_page(page, data), ""))
    assert "Quotes that match no field above" in loose and "loose words" in loose
    # an unchecked quote is "not checked: no text", never "not found" (R-P1-26): turning one
    # not-found quote into an unchecked one moves exactly one mark from the second to the first
    missing = next(q for q in quotes if q["checked"] and not q["found"])
    missing.update(checked=False, found=False)
    after = quote_marks(dump_dom(tmp_page(page, data), ""))
    assert "not checked: no text" not in marks
    assert after.count("not checked: no text") == 1
    assert after.count("not found in text") == marks.count("not found in text") - 1


def tmp_page(page: Path, data: dict[str, Any]) -> Path:
    out = page.with_name("altered.html")
    out.write_text(review_page.render(data), encoding="utf-8")
    return out


def test_im_done_is_refused_while_a_flip_has_no_triage_verdict(
    root: Path, stub_atlas: Path, atlas_root: Path
) -> None:
    """R-P1-33: a flip never stands in for a verdict, and finishing needs the verdict."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    dom = dump_dom(Path(out.strip().splitlines()[-1]), "selftest-noverdict")
    decisions = selftest_output(dom)
    assert decisions["session"]["finished_at"] is None
    assert decisions["triage"] is None
    assert [
        e.message for e in schema_validator(atlas_root, "review_decisions").iter_errors(decisions)
    ] == []
    banner = re.search(r'<div id="banner"([^>]*)>(.*?)</div>', visible(dom), re.S)
    assert banner and "hidden" not in banner.group(1)
    assert "Give the triage card a verdict" in banner.group(2)


STALE_DIGEST = "sha256:" + "9" * 64


def _banner(dom: str) -> tuple[bool, str]:
    match = re.search(r'<div id="banner"([^>]*)>(.*?)</div>', visible(dom), re.S)
    assert match
    return "hidden" not in match.group(1), match.group(2)


def test_a_decision_made_on_another_version_of_a_card_is_never_counted_or_downloaded(
    root: Path, stub_atlas: Path
) -> None:
    """R-P1-34: one stored decision has another digest, one has none; neither is decided."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    shown = {c["id"]: c["shown_digest"] for c in data_of(page)["findings"]}
    asks = "decided on an earlier version: decide again"

    before = dump_dom(page, "selftest-stale")
    assert selftest_output(before)["findings"] == []
    assert visible(before).count(f">{asks}<") == 4  # chip and card line, on each of two cards

    # opening and closing a stale card saves its time, and must not re-stamp its digest
    looked = dump_dom(page, "selftest-stale-open")
    assert selftest_output(looked)["findings"] == []
    assert visible(looked).count(f">{asks}<") == 4

    after = dump_dom(page, "selftest-stale-click")
    decisions = selftest_output(after)
    # a new click carries the card's current digest; the other stale decision stays out
    assert [(f["id"], f["shown_digest"]) for f in decisions["findings"]] == [(J, shown[J])]
    assert visible(after).count(f">{asks}<") == 2


def test_changing_a_decision_after_im_done_clears_finished_at(root: Path, stub_atlas: Path) -> None:
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    assert selftest_output(dump_dom(page, "selftest"))["session"]["finished_at"] is not None
    redone = selftest_output(dump_dom(page, "selftest-redecide"))
    assert redone["session"]["finished_at"] is None
    assert [f["decision"] for f in redone["findings"]] == ["approve", "fix", "later"]


def test_a_flip_after_the_triage_verdict_clears_it_and_asks_again(
    root: Path, stub_atlas: Path, atlas_root: Path
) -> None:
    code, out = build(root, stub_atlas)
    assert code == 0, out
    dom = dump_dom(Path(out.strip().splitlines()[-1]), "selftest-reflip")
    decisions = selftest_output(dom)
    assert decisions["triage"] is None and decisions["session"]["finished_at"] is None
    shown, text = _banner(dom)
    assert shown and "earlier triage verdict was cleared" in text
    # item 5: the same line says the download leaves the flips out
    assert "Download my review leaves the flips out" in text
    errors = schema_validator(atlas_root, "review_decisions").iter_errors(decisions)
    assert [e.message for e in errors] == []


def test_a_store_that_never_answers_falls_back_to_file_mode_after_ten_seconds(
    root: Path, stub_atlas: Path
) -> None:
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    stuck = page.with_name("stuck.html")
    marker = '<script type="application/json" id="data">'
    text = page.read_text(encoding="utf-8")
    assert text.count(marker) == 1
    stuck.write_text(
        text.replace(
            marker, "<script>window.claude={use:()=>new Promise(()=>{})}</script>" + marker
        ),
        encoding="utf-8",
    )
    dom = dump_dom(stuck, "", budget=20000)
    status = re.search(r'id="store-state">(.*?)</span>', dom, re.S)
    assert status and "kept in this browser" in status.group(1)


def test_a_second_opinion_page_refuses_other_verdicts(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    opinion = second_opinion_file(tmp_path / "x.json")
    code, out = build(
        root, stub_atlas, "--reviewer", "Dr Y", "--role", "second_opinion", "--with", str(opinion)
    )
    assert code == 2 and "drop --with" in out
    assert not list((root / "local/literature/runs/TBX5/r1").glob("review*.html"))


def test_the_owner_page_shows_a_clinicians_verdict_on_its_card(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    opinion = second_opinion_file(tmp_path / "x.json")
    code, out = build(root, stub_atlas, "--with", str(opinion))
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    data = inline_data(page.read_text(encoding="utf-8"))
    assert data["second_opinions"][J] == [
        {"name": "Dr X", "decision": "fix", "note": "the carrier count is wrong"}
    ]
    dom = dump_dom(page, "")
    assert "Dr X: needs a fix: the carrier count is wrong" in dom
    assert "Dr X: agrees" in dom


@pytest.mark.parametrize(
    ("override", "why"),
    [
        (
            {"reviewer": {"name": "E.A.M", "role": "owner"}},
            "its reviewer's role is not second_opinion",
        ),
        ({"gene": "HGNC:4173"}, "--with"),
        ({"symbol": "GATA4"}, "GATA4"),
        ({"format": 2}, "format"),
    ],
)
def test_a_with_file_must_be_a_second_opinion_on_this_gene(
    root: Path, stub_atlas: Path, tmp_path: Path, override: dict[str, Any], why: str
) -> None:
    opinion = second_opinion_file(tmp_path / "x.json", **override)
    code, out = build(root, stub_atlas, "--with", str(opinion))
    assert code == 2 and why in out
    assert not list((root / "local/literature/runs/TBX5/r1").glob("review*.html"))


def test_review_page_refuses_to_build_when_validate_reports_an_error(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_VALIDATE_ERRORS", "2")
    code, out = build(root, stub_atlas)
    assert code == 1 and "2 error(s)" in out
    assert not list((root / "local/literature/runs/TBX5/r1").glob("review*.html"))


def test_the_page_header_carries_the_validate_line_and_provenance(
    root: Path, stub_atlas: Path
) -> None:
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1]).read_text(encoding="utf-8")
    page_info = inline_data(page)["page"]
    assert page_info["validate"] == "0 error(s), 3 warning(s)"
    assert page_info["literature_sha256"] == "sha256:" + "a" * 64


def test_a_decisions_file_with_a_malformed_date_time_fails_the_schema(
    atlas_root: Path, tmp_path: Path, root: Path, stub_atlas: Path
) -> None:
    opinion = json.loads(second_opinion_file(tmp_path / "x.json").read_text(encoding="utf-8"))
    validator = schema_validator(atlas_root, "review_decisions")
    assert list(validator.iter_errors(opinion)) == []
    opinion["session"]["finished_at"] = "x"
    assert [list(e.path) for e in validator.iter_errors(opinion)] == [["session", "finished_at"]]
    # the page's own form, with milliseconds and a Z, is accepted
    opinion["session"]["finished_at"] = "2026-11-19T13:45:27.860Z"
    assert list(validator.iter_errors(opinion)) == []
