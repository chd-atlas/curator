"""``chd-curator review-page``: the page it writes, and the decisions file the page produces.

The page is one HTML file whose behaviour is JavaScript, so its tests run it: headless Chrome
opens ``review.html#selftest`` (file mode: no ``window.claude``), the page drives itself
through a decision of each kind (approve and fix; it makes no "later" decision, except in
``#selftest-redecide``) and writes into ``#selftest-output`` the exact text its own ``download()``
produced, and the test validates that JSON against the atlas's exported schema. A missing
Chrome fails the test; it is never skipped, because a skipped page test would leave the page
unchecked while the suite stayed green.

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
from chd_curator.decisions import decisions_from_export

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
    # persistence is skipped in a selftest, so the page claims nothing was kept: only the
    # connection line at load names the browser (the `persist` tests pin what a real write says)
    assert not [m for m in status if m.lower().startswith("saved")]
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
    assert "Papers the run flags for you to read" in dom
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


def test_a_second_opinion_intro_says_to_press_im_done_before_downloading(
    root: Path, stub_atlas: Path
) -> None:
    """R-P1-46a: downloading without 'I'm done' yields a file `review-page --with` refuses."""
    code, out = build(root, stub_atlas, "--reviewer", "Dr X", "--role", "second_opinion")
    assert code == 0, out
    dom = visible(dump_dom(Path(out.strip().splitlines()[-1]), ""))
    intro = re.search(r'<div class="intro" id="intro">(.*?)</div>', dom, re.S)
    assert intro
    text = html.unescape(re.sub(r"<[^>]+>", "", intro.group(1)))
    assert (
        "When you are done, press I'm done first, then Download my review, "
        "then send the file back." in text
    )


def test_the_papers_needed_card_words_every_text_source(root: Path, stub_atlas: Path) -> None:
    """R-P1-46b: a raw texts.json token such as full_text must never reach the card."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    data = inline_data(page.read_text(encoding="utf-8"))
    said = {}
    for source in ("full_text", "supplied_pdf", "abstract", "none"):
        data["papers_needed"][0]["source"] = source
        dom = visible(dump_dom(tmp_page(page, data), ""))
        card = re.search(r'<div class="static-card" id="needed">(.*?)</div></div>', dom, re.S)
        assert card
        said[source] = re.search(r"The readers had: (.*?)\.", card.group(1))
        assert said[source] and "_" not in said[source].group(1)
    assert [m.group(1) for m in said.values() if m] == [
        "the full text",
        "a PDF supplied for the run",
        "the abstract only",
        "no text",
    ]
    # a token the run contract does not allow is still never printed raw
    data["papers_needed"][0]["source"] = "odd_token"
    odd = visible(dump_dom(tmp_page(page, data), ""))
    assert "odd_token" not in odd and "The readers had: an unrecognised text source." in odd


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
        {"name": "Dr X", "decision": "fix", "note": "the carrier count is wrong", "current": True}
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
        ({"session": {"started_at": None, "finished_at": None, "active_ms": 0}}, "not finished"),
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


def test_two_files_from_one_reviewer_are_refused(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    first = second_opinion_file(tmp_path / "a.json")
    again = second_opinion_file(
        tmp_path / "b.json", reviewer={"name": " dr x", "role": "second_opinion"}
    )
    code, out = build(root, stub_atlas, "--with", str(first), "--with", str(again))
    assert code == 2 and "already gave" in out and "a.json" in out
    assert not list((root / "local/literature/runs/TBX5/r1").glob("review*.html"))


def test_a_verdict_on_an_earlier_version_of_a_card_reads_as_one(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    opinion = second_opinion_file(tmp_path / "x.json")
    doc = json.loads(opinion.read_text(encoding="utf-8"))
    doc["findings"][0]["shown_digest"] = "sha256:" + "9" * 64  # J's card has changed since
    opinion.write_text(json.dumps(doc), encoding="utf-8")
    code, out = build(root, stub_atlas, "--with", str(opinion))
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    assert (
        inline_data(page.read_text(encoding="utf-8"))["second_opinions"][J][0]["current"] is False
    )
    text = plain(card_html(dump_dom(page, ""), J))
    assert "Dr X: needs a fix on an earlier version: the carrier count is wrong" in text
    assert "Dr X: agrees" in plain(card_html(dump_dom(page, ""), G))  # G's digest still matches


@pytest.mark.parametrize("errors", [None, "2"])
def test_validate_is_asked_before_the_atlas_digests_a_gene(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch, errors: str | None
) -> None:
    """E3: the stub cannot digest GATA4 (no literature file); validate's errors come first."""
    if errors:
        monkeypatch.setenv("STUB_VALIDATE_ERRORS", errors)
    result = CliRunner().invoke(
        app,
        ["review-page", "GATA4", "--run", "r1", "--reviewer", "E.A.M", "--root", str(root)]
        + ["--atlas-cli", str(stub_atlas)],
    )
    assert result.exit_code == (1 if errors else 2), result.output
    assert ("2 error(s)" in result.output) is bool(errors)
    assert ("no literature file" in result.output) is not bool(errors)


def card_html(dom: str, fid: str) -> str:
    """One finding card's markup, so an assertion cannot be satisfied by another card's text."""
    match = re.search(
        rf'<details class="card" data-id="{re.escape(fid)}"[^>]*>(.*?)</details>',
        visible(dom),
        re.S,
    )
    assert match, f"no card {fid}"
    return match.group(1)


def plain(markup: str) -> str:
    """Visible text of a fragment: tags become spaces, entities are read, whitespace collapsed."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def edit_literature(root: Path, edit: Any) -> None:
    from ruamel.yaml import YAML

    path = root / "curation/literature/TBX5.yaml"
    doc = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    edit({f["id"]: f for f in doc["findings"]})
    out = YAML(typ="safe")
    out.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        out.dump(doc, fh)


def test_what_each_card_claims_is_what_the_data_says_card_by_card(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    """R-P1-38/39/41, B2, B4, B8, E5, A2 and the judge, row and variant detail, per card.

    Every assertion is made on one card's own markup (CLAUDE.md section 4.19): the same words
    on another card must not satisfy it.
    """

    def tweak(by_id: dict[str, Any]) -> None:
        g, j = by_id[G], by_id[J]
        g["variants"][0].update(
            hgvs_p="p.(Pro362Argfs*8)",
            review_status="pending_review",
            review_note="check the exon",
            review_settled_by="E.A.M",
            unclear={"zygosity": {"reason": "paper silent", "settled_by": "expert"}},
        )
        j["variants"][0].update(placement="mane", hgvs_c="c.100C>T", hgvs_p="p.(Arg34Ter)")

    edit_literature(root, tweak)
    run = root / "local/literature/runs/TBX5/r1"
    comparison = json.loads((run / "comparison.json").read_text(encoding="utf-8"))
    comparison["agreed"] = []  # G is covered by the run (its pair) but in no list: one reader
    (run / "comparison.json").write_text(json.dumps(comparison), encoding="utf-8")
    ev = run / "evidence/batch-01.yaml"
    ev.write_text(
        ev.read_text(encoding="utf-8")
        + '  - {field: "stray", quote: "a proband ... septal defect", where: "Abstract"}\n',
        encoding="utf-8",
    )
    pubs = root / "curation/publications.yaml"
    pubs.write_text(
        pubs.read_text(encoding="utf-8").replace(
            "    study_type: family_linkage\n",
            "    study_type: family_linkage\n    own_lab: true\n",
        ),
        encoding="utf-8",
    )
    opinion = second_opinion_file(tmp_path / "x.json")
    doc = json.loads(opinion.read_text(encoding="utf-8"))
    doc["findings"][0]["shown_digest"] = "sha256:" + "9" * 64
    opinion.write_text(json.dumps(doc), encoding="utf-8")
    code, out = build(root, stub_atlas, "--with", str(opinion))
    assert code == 0, out
    dom = dump_dom(Path(out.strip().splitlines()[-1]), "")
    g, j, a = (plain(card_html(dom, i)) for i in (G, J, A))

    # G: a row, a variant's other fields, its review mark, its label, its id, its quotes
    assert "design agreed family segregation" in g
    assert 'locator {"element": "Results - Patients", "kind": "in_text"}' in g
    assert 'unclear {"zygosity": {"reason": "paper silent", "settled_by": "expert"}}' in g
    assert "pending_review: check the exon (settled by E.A.M)" in g
    assert "variant 1" in g  # E5: the variant's id, which the shown digest binds
    assert "Recorded in the finding, not by VariantValidator: p.(Pro362Argfs*8)" in g
    assert "VariantValidator:" not in g.replace("not by VariantValidator", "")
    assert "family linkage · own lab" in g and "read from the full text" in g
    assert "1 quote not found in text" in g
    assert "over 30 words (40 words; first 30 shown)" in g
    assert "one reader only" in g and "readers agreed" not in g
    # J: the judge's rule, a variant's diff, and the VariantValidator label for a mane variant
    assert "Judge: chose reader 1. Phenotypes are the carrier's own lesions" in j
    assert (
        "variant c.100C>T, p.(Arg34Ter) phase reader 1: trans · reader 2: not stated · "
        "judge: chose reader 1: trans. A phase the paper states is kept." in j
    )
    assert "VariantValidator: c.100C>T → p.(Arg34Ter)" in j and "Recorded in the finding" not in j
    assert "found in 2 parts" in j and "Dr X: needs a fix on an earlier version:" in j
    assert "readers agreed" not in j and "one reader only" not in j
    # A: span-only counts as agreed; abstract-only source
    assert "readers agreed" in a and "read from the abstract only" in a
    assert "Dr X" not in a and "Dr X: agrees" in g  # a verdict sits on its own card only
    assert "Only one reader reported this" in visible(dom)


def _persist_page(page: Path, head: str) -> Path:
    marker = '<script type="application/json" id="data">'
    text = page.read_text(encoding="utf-8")
    out = page.with_name("persist.html")
    out.write_text(text.replace(marker, f"<script>{head}</script>" + marker), encoding="utf-8")
    return out


def _status(dom: str) -> list[str]:
    match = re.search(r'<pre id="selftest-status"[^>]*>(.*?)</pre>', dom, re.S)
    assert match
    messages: list[str] = json.loads(html.unescape(match.group(1)))
    return messages


@pytest.mark.parametrize("blocked", [False, True])
def test_a_choice_the_browser_could_not_keep_is_not_reported_as_saved(
    root: Path, stub_atlas: Path, blocked: bool
) -> None:
    """R-P1-40: persistence is live in `#selftest-persist`; blocked storage must be admitted."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    head = "Storage.prototype.setItem=function(){throw new Error('blocked')};" if blocked else ""
    status = _status(dump_dom(_persist_page(page, head), "selftest-persist"))
    kept = [m for m in status if m.startswith("Saved in this browser")]
    refused = [m for m in status if m.startswith("NOT saved in this browser")]
    assert (len(kept) == 0, len(refused) > 0) == (blocked, blocked)
    assert bool(kept) is not blocked and all("Download my review" in m for m in kept + refused)


def test_restoring_from_the_browser_takes_only_this_pages_own_current_decisions(
    root: Path, stub_atlas: Path
) -> None:
    """D2: a stale entry does not count and another reviewer's or page's entry is not read."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    shown = {c["id"]: c["shown_digest"] for c in data_of(page)["findings"]}
    sha = data_of(page)["page"]["literature_sha256"]

    def entry(**decisions: dict[str, Any]) -> str:
        return json.dumps(
            {
                "decisions": {
                    i: {
                        "decision": "approve",
                        "note": "",
                        "decided_at": "2026-11-19T10:00:00Z",
                        **d,
                    }
                    for i, d in decisions.items()
                },
                "session": {"started_at": "2026-11-19T09:00:00.000Z"},
            }
        )

    mine = f"chd-review:{sha}:owner:e-a-m"
    seeded = {
        mine: entry(
            **{G: {"shown_digest": shown[G]}, J: {"shown_digest": "sha256:" + "9" * 64}, A: {}}
        ),
        f"chd-review:{sha}:owner:dr-x": entry(**{A: {"shown_digest": shown[A]}}),
        f"chd-review:{sha}:second_opinion:e-a-m": entry(**{A: {"shown_digest": shown[A]}}),
        f"chd-review:{sha}": entry(**{A: {"shown_digest": shown[A]}}),
        f"chd-review:sha256:{'b' * 64}:owner:e-a-m": entry(**{A: {"shown_digest": shown[A]}}),
    }
    head = "".join(
        f"localStorage.setItem({json.dumps(k)},{json.dumps(v)});" for k, v in seeded.items()
    )
    dom = dump_dom(_persist_page(page, head), "selftest-restore")
    restored = selftest_output(dom)
    assert [(f["id"], f["decision"], f["shown_digest"]) for f in restored["findings"]] == [
        (G, "approve", shown[G])
    ]
    assert restored["session"]["started_at"] == "2026-11-19T09:00:00.000Z"
    assert visible(dom).count(">decided on an earlier version: decide again<") == 4  # J and A


STORE_FAKE = """
window.__store = {}; window.__subs = [];
const snapDocs = () => Object.entries(window.__store).filter(([p]) => p.startsWith("review/"))
  .map(([p, b]) => ({ id: p.slice(7), data: () => JSON.parse(JSON.stringify(b)) }));
const notify = () => window.__subs.forEach((s) => s());
const fakeDb = {
  doc: (path) => ({
    set: (body) => {
      if (window.__refuse) return Promise.reject({ code: "invalid_argument" });
      window.__store[path] = JSON.parse(JSON.stringify(body));
      setTimeout(notify, 0); return Promise.resolve();
    },
    onSnapshot: (cb) => {
      const f = () => cb({ exists: path in window.__store,
        data: () => JSON.parse(JSON.stringify(window.__store[path])) });
      window.__subs.push(f); setTimeout(f, 0); return () => {};
    },
  }),
  collection: () => ({ onSnapshot: (cb) => {
    const f = () => cb({ docs: snapDocs(), metadata: { hasPendingWrites: false } });
    window.__subs.push(f); setTimeout(f, 0); return () => {};
  } }),
};
window.claude = { use: async () => fakeDb };
"""
STORE_DRIVER = """
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  await wait(300);
  const root = document.getElementById("root");
  const card = (id) => root.querySelector(`details.card[data-id="${id}"]`);
  const ids = [...root.querySelectorAll("details.card")].map((c) => c.dataset.id)
    .filter((i) => i !== "triage");
  card(ids[0]).open = true; await wait(50);
  root.querySelector(`.actions[data-id="${ids[0]}"] [data-act="approve"]`).click(); await wait(50);
  card(ids[1]).open = true; await wait(50);
  document.getElementById("note-" + ids[1]).value = "check n";
  root.querySelector(`.actions[data-id="${ids[1]}"] [data-act="fix"]`).click(); await wait(50);
  root.querySelector('.actions[data-id="triage"] [data-act="approve"]').click(); await wait(50);
  document.getElementById("finish").click(); await wait(300);
  const pre = document.createElement("pre"); pre.id = "store-dump";
  pre.textContent = JSON.stringify({ store: window.__store,
    status: document.getElementById("store-state").textContent });
  document.body.appendChild(pre);
})();
"""


def run_in_store_mode(page: Path, *, refuse: bool = False) -> dict[str, Any]:
    text = page.read_text(encoding="utf-8")
    marker = '<script type="application/json" id="data">'
    harness = page.with_name("store.html")
    refusal = "window.__refuse = true;" if refuse else ""
    harness.write_text(
        text.replace(marker, f"<script>{refusal}{STORE_FAKE}</script>" + marker)
        + f"<script>{STORE_DRIVER}</script>",
        encoding="utf-8",
    )
    dom = dump_dom(harness, "", budget=8000)
    match = re.search(r'<pre id="store-dump">(.*?)</pre>', dom, re.S)
    assert match, "the driver wrote no dump"
    dump: dict[str, Any] = json.loads(html.unescape(match.group(1)))
    return dump


def test_a_store_session_converts_and_what_the_page_says_matches_what_it_saved(
    root: Path, stub_atlas: Path, atlas_root: Path, tmp_path: Path
) -> None:
    """D2: the page in store mode with a fake db, exported in ArtifactData's layout, converted."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    dump = run_in_store_mode(page)
    store = dump["store"]
    assert dump["status"] == "Finished. Tell Claude in the terminal."
    session = store["session/main"]
    data = data_of(page)
    assert session["page"] == {
        "literature_sha256": data["page"]["literature_sha256"],
        "built_on": data["page"]["built_on"],
    }
    assert session["reviewer"] == {"name": "E.A.M", "role": "owner"}
    assert isinstance(session["total_active_ms"], int) and "total_ms" not in session
    export = tmp_path / "export"
    for path, body in store.items():
        target = export / f"{path}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body), encoding="utf-8")
    converted = decisions_from_export(export, page, atlas_root, "TBX5")
    got = converted.decisions
    assert got["session"]["active_ms"] == session["total_active_ms"]
    assert [(f["decision"], f["note"]) for f in got["findings"]] == [
        ("approve", ""),
        ("fix", "check n"),
    ]
    assert converted.stale == () and got["triage"]["decision"] == "approve"


@pytest.mark.parametrize(
    ("role", "refuse", "says"),
    [
        ("owner", False, "Finished. Tell Claude in the terminal."),
        ("second_opinion", False, "Finished. Choose Download my review and send the file back."),
        ("owner", True, "Finished, but nothing was saved"),
        ("second_opinion", True, "Finished, but nothing was saved"),
    ],
)
def test_the_finished_line_names_the_next_step_for_this_role_and_this_mode(
    root: Path, stub_atlas: Path, role: str, refuse: bool, says: str
) -> None:
    name = "E.A.M" if role == "owner" else "Dr X"
    code, out = build(root, stub_atlas, "--reviewer", name, "--role", role)
    assert code == 0, out
    dump = run_in_store_mode(Path(out.strip().splitlines()[-1]), refuse=refuse)
    assert dump["status"].startswith(says)
    assert (dump["store"] == {}) is refuse


def test_taking_back_the_only_flip_after_the_verdict_clears_the_verdict(
    root: Path, stub_atlas: Path
) -> None:
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    assert selftest_output(dump_dom(page, "selftest"))["triage"]["flips"] != []
    decisions = selftest_output(dump_dom(page, "selftest-unflip"))
    assert decisions["triage"] is None and decisions["session"]["finished_at"] is None


def test_the_selftest_publishes_the_text_the_download_made_and_notes_its_approvals(
    root: Path, stub_atlas: Path
) -> None:
    """D1/C3: a `{}` from download() would fail the schema; the approve carries its note."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    dom = dump_dom(Path(out.strip().splitlines()[-1]), "selftest")
    first = selftest_output(dom)["findings"][0]
    assert first["decision"] == "approve" and first["note"] == "selftest"
    assert re.search(r'<pre id="selftest-output"[^>]*>\{\n  "format": 1,\n  "gene"', dom)
