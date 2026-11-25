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

from archive_helpers import commit_all, finding, flip, make_archive, put, tri
from chd_curator import review_page
from chd_curator.atlas import schema_validator
from chd_curator.cli import app
from chd_curator.decisions import decisions_from_export

FIXTURE = Path(__file__).parent / "fixtures" / "review"
G, J, A = "CHDA:LIT:0000901", "CHDA:LIT:0000902", "CHDA:LIT:0000903"
D1, D2 = "sha256:" + "2" * 64, "sha256:" + "4" * 64
T_X = "2026-11-19T13:00:00Z"
P1, P2, P3 = "PMID:90000001", "PMID:90000002", "PMID:90000003"


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
    """R-P1-46a: downloading without 'I'm done' yields a file `archive-decisions` refuses."""
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


def filed_by_dr_x(archive: Path, tmp_path: Path, j_digest: str = D2) -> None:
    put(
        archive, tmp_path, "Dr X", "second_opinion", T_X,
        [finding(J, "fix", j_digest, T_X, "the carrier count is wrong"),
         finding(G, "approve", D1, T_X)],
    )  # fmt: skip
    commit_all(archive)


def no_page(root: Path) -> bool:
    return not list((root / "local/literature/runs/TBX5/r1").glob("review*.html"))


def test_a_second_opinion_page_refuses_an_archive(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path)
    code, out = build(
        root, stub_atlas, "--reviewer", "Dr Y", "--role", "second_opinion",
        "--archive", str(archive),
    )  # fmt: skip
    assert code == 2 and "a second-opinion page reads no archive" in out
    assert no_page(root)


def test_with_is_gone(root: Path, stub_atlas: Path, tmp_path: Path) -> None:
    opinion = second_opinion_file(tmp_path / "x.json")
    code, out = build(root, stub_atlas, "--with", str(opinion))
    assert code == 2 and "--with" in out
    assert no_page(root)


def test_a_dirty_archive_refuses_the_page(root: Path, stub_atlas: Path, tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    filed_by_dr_x(archive, tmp_path)
    (archive / "stray.txt").write_text("x", encoding="utf-8")
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 2 and "uncommitted changes" in out
    assert no_page(root)


def test_the_owner_page_shows_a_clinicians_verdict_on_its_card(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path)
    filed_by_dr_x(archive, tmp_path)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    data = inline_data(page.read_text(encoding="utf-8"))
    assert data["second_opinions"][J] == [
        {
            "name": "Dr X",
            "decision": "fix",
            "note": "the carrier count is wrong",
            "current": True,
            "date": "2026-11-19",
        }
    ]
    dom = dump_dom(page, "")
    assert "Dr X: needs a fix: the carrier count is wrong" in dom
    assert "Dr X: agrees" in dom


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"gene": "HGNC:4173"}, "HGNC:4173"),
        ({"symbol": "GATA4"}, "GATA4"),
        ({"format": 2}, "format"),
        ({"session": {"started_at": None, "finished_at": None, "active_ms": 0}}, "finished"),
    ],
)
def test_a_filed_file_that_is_not_a_finished_second_opinion_on_this_gene_refuses_the_page(
    root: Path, stub_atlas: Path, tmp_path: Path, override: dict[str, Any], why: str
) -> None:
    """Committed by hand, since filing would refuse it: the reader and the gene check must."""
    archive = make_archive(tmp_path)
    opinion = second_opinion_file(tmp_path / "x.json", **override)
    dest = archive / "TBX5" / "r1" / "decisions" / "20261119T130000Z--dr-x.json"
    dest.parent.mkdir(parents=True)
    shutil.copy(opinion, dest)
    commit_all(archive)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 2 and why in out
    assert no_page(root)


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


def test_two_files_from_one_reviewer_are_one_reviewer_and_only_the_later_verdict_shows(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path)
    put(archive, tmp_path, "Dr X", "second_opinion", T_X, [finding(J, "fix", D2, T_X, "wrong")])
    later = "2026-11-19T14:00:00Z"
    put(archive, tmp_path, " dr x", "second_opinion", later, [finding(J, "approve", D2, later)])
    commit_all(archive)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    (only,) = data_of(page)["second_opinions"][J]
    assert (only["name"], only["decision"]) == (" dr x", "approve")
    text = plain(card_html(dump_dom(page, ""), J))
    assert "dr x: agrees" in text and "needs a fix" not in text


def test_a_verdict_on_an_earlier_version_of_a_card_reads_as_one(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path)
    filed_by_dr_x(archive, tmp_path, j_digest="sha256:" + "9" * 64)  # J's card has changed since
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    assert data_of(page)["second_opinions"][J][0]["current"] is False
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
    archive = make_archive(tmp_path)
    filed_by_dr_x(archive, tmp_path, j_digest="sha256:" + "9" * 64)
    code, out = build(root, stub_atlas, "--archive", str(archive))
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


@pytest.mark.parametrize(
    ("act", "decision", "note", "active_ms"),
    [
        ("window.__open(); await wait(60);", "approve", "kept", None),
        (
            "window.__open(); await wait(60); window.__close(); await wait(10);",
            "approve",
            "kept",
            5001,
        ),
        ("window.__open(); await wait(60); window.__decide('fix', 'newer');", "fix", "newer", None),
    ],
    ids=["open", "open-and-close", "decide"],
)
def test_a_card_touched_before_the_restore_does_not_lose_its_saved_decision(
    root: Path, stub_atlas: Path, act: str, decision: str, note: str, active_ms: int | None
) -> None:
    """Opening a card now creates an entry: the restore must merge into it, not skip the saved one.

    `window.__beforeRestore` (selftest only) runs while the page still awaits the store, which is
    when a real user can open a card whose decision an earlier visit saved in this browser."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    shown = {c["id"]: c["shown_digest"] for c in data_of(page)["findings"]}
    sha = data_of(page)["page"]["literature_sha256"]
    first = "2026-11-19T08:00:00.000Z"
    saved = {
        "decisions": {
            G: {
                "decision": "approve",
                "note": "kept",
                "shown_digest": shown[G],
                "decided_at": "2026-11-19T10:00:00Z",
                "active_ms": 5000,
                "first_opened_at": first,
            }
        },
        "session": {"started_at": "2026-11-19T09:00:00.000Z"},
    }
    head = (
        f"localStorage.setItem({json.dumps(f'chd-review:{sha}:owner:e-a-m')},"
        f"{json.dumps(json.dumps(saved))});"
        "window.__beforeRestore = async () => {"
        "const wait = (ms) => new Promise((r) => setTimeout(r, ms));"
        f"const card = () => document.querySelector('details.card[data-id=\"{G}\"]');"
        "window.__open = () => { card().open = true; };"
        "window.__close = () => { card().open = false; };"
        "window.__decide = (act, note) => {"
        f"document.getElementById('note-{G}').value = note;"
        f"document.querySelector('.actions[data-id=\"{G}\"] [data-act=\"' + act + '\"]')"
        ".click(); };"
        f"{act} }};"
    )
    dom = dump_dom(_persist_page(page, head), "selftest-restore", budget=8000)
    got = next(f for f in selftest_output(dom)["findings"] if f["id"] == G)
    assert (got["decision"], got["note"], got["shown_digest"]) == (decision, note, shown[G])
    assert got["first_opened_at"] == first
    if active_ms is not None:
        assert got["active_ms"] >= active_ms
    assert visible(dom).count(">decided on an earlier version: decide again<") == 0


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
FILE_FAKE = "window.claude = { use: async () => null };"

# One millisecond per clock read, so that time added after a save is never rounded away.
TICKING_CLOCK = "{ let t = Date.now(); Date.now = () => ++t; }"
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
  // looking at a decided card again must keep the time it was first opened
  window.__firstSaved = (window.__store["review/" + ids[0]] || {}).first_opened_at;
  card(ids[0]).open = true; await wait(80); card(ids[0]).open = false; await wait(50);
  card(ids[1]).open = true; await wait(50);
  document.getElementById("note-" + ids[1]).value = "check n";
  root.querySelector(`.actions[data-id="${ids[1]}"] [data-act="fix"]`).click(); await wait(50);
  root.querySelector('.actions[data-id="triage"] [data-act="approve"]').click(); await wait(50);
  document.getElementById("finish").click(); await wait(300);
  const status = document.getElementById("store-state").textContent;
  document.getElementById("download").click(); await wait(300);
  const pre = document.createElement("pre"); pre.id = "store-dump";
  pre.textContent = JSON.stringify({ store: window.__store, download: window.__lastDownload,
    firstSaved: window.__firstSaved, firstId: ids[0], status });
  document.body.appendChild(pre);
})();
"""


def run_in_store_mode(page: Path, *, refuse: bool = False) -> dict[str, Any]:
    text = page.read_text(encoding="utf-8")
    marker = '<script type="application/json" id="data">'
    harness = page.with_name("store.html")
    refusal = "window.__refuse = true;" if refuse else ""
    harness.write_text(
        text.replace(marker, f"<script>{refusal}{TICKING_CLOCK}{STORE_FAKE}</script>" + marker)
        + f"<script>{STORE_DRIVER}</script>",
        encoding="utf-8",
    )
    dom = dump_dom(harness, "", budget=8000)
    match = re.search(r'<pre id="store-dump">(.*?)</pre>', dom, re.S)
    assert match, "the driver wrote no dump"
    dump: dict[str, Any] = json.loads(html.unescape(match.group(1)))
    return dump


FILE_DRIVER = """
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  await wait(300);
  const root = document.getElementById("root");
  const card = (id) => root.querySelector(`details.card[data-id="${id}"]`);
  const id = [...root.querySelectorAll("details.card")].map((c) => c.dataset.id)
    .find((i) => i !== "triage");
  card(id).open = true; await wait(80);
  root.querySelector(`.actions[data-id="${id}"] [data-act="approve"]`).click(); await wait(50);
  document.getElementById("download").click(); await wait(100);
  const first = window.__lastDownload;
  card(id).open = true; await wait(80); card(id).open = false; await wait(50);
  document.getElementById("download").click(); await wait(100);
  const pre = document.createElement("pre"); pre.id = "file-dump";
  pre.textContent = JSON.stringify({ first, second: window.__lastDownload });
  document.body.appendChild(pre);
})();
"""


def test_looking_at_a_decided_card_again_keeps_the_time_it_was_first_opened(
    root: Path, stub_atlas: Path
) -> None:
    """R-P1-49: without a store, a later look must not restamp first_opened_at.

    Measured: with the open-time stamp made unconditional, the store-mode test above still passes
    (1 passed), because the store's snapshots replace the in-memory entry; this test fails."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    text = page.read_text(encoding="utf-8")
    marker = '<script type="application/json" id="data">'
    harness = page.with_name("file.html")
    harness.write_text(
        text.replace(marker, f"<script>{FILE_FAKE}</script>" + marker)
        + f"<script>{FILE_DRIVER}</script>",
        encoding="utf-8",
    )
    # `restore` is the selftest variant that drives nothing itself: the driver has the page alone
    dom = dump_dom(harness, "selftest-restore", budget=8000)
    match = re.search(r'<pre id="file-dump">(.*?)</pre>', dom, re.S)
    assert match, "the driver wrote no dump"
    dump = json.loads(html.unescape(match.group(1)))
    first = json.loads(dump["first"])["findings"][0]
    second = json.loads(dump["second"])["findings"][0]
    assert first["first_opened_at"] < first["decided_at"]
    assert second["first_opened_at"] == first["first_opened_at"]
    assert second["active_ms"] > first["active_ms"]


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
    # the time a card was first opened is kept across a later look, and precedes its decision
    assert store["review/" + dump["firstId"]]["first_opened_at"] == dump["firstSaved"]
    cards = {p[7:]: b for p, b in store.items() if p.startswith("review/")}
    for card_id, body in cards.items():
        assert body["first_opened_at"] < body.get("decided_at", "9"), card_id
    # the store and the download say the same of each decided card, and the cards sum to the session
    downloaded = json.loads(dump["download"])
    assert len(downloaded["findings"]) == 2
    for f in downloaded["findings"]:
        assert f["active_ms"] == cards[f["id"]]["active_ms"]
        assert f["first_opened_at"] == cards[f["id"]]["first_opened_at"]
    assert downloaded["triage"]["active_ms"] == cards["triage"]["active_ms"]
    assert sum(b["active_ms"] for b in cards.values()) == session["total_active_ms"]
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


SAVE_FAKE = """
window.__saves = [];
window.__downloadsTimeoutMs = 50;
window.claude = { use: async (name) => {
  if (name !== "downloads") return null;
  %s
} };
"""
SAVE_DRIVER = """
(async () => {
  await new Promise((r) => setTimeout(r, 3000));
  const pre = document.createElement("pre"); pre.id = "save-dump";
  pre.textContent = JSON.stringify({ saves: window.__saves,
    last: window.__lastDownload,
    status: JSON.parse(document.getElementById("selftest-status").textContent) });
  document.body.appendChild(pre);
})();
"""
SAVE_ACCEPTS = (
    "return { save: async (r) => { window.__saves.push(r); return { status: 'saved' }; } };"
)
SAVE_REFUSES = (
    "return { save: async (r) => { window.__saves.push(r); throw { code: '%s', message: 'x' }; } };"
)


def run_with_downloads(page: Path, body: str) -> dict[str, Any]:
    """The selftest page with `claude.use("downloads")` answering ``body``; what it recorded."""
    marker = '<script type="application/json" id="data">'
    harness = page.with_name("downloads.html")
    harness.write_text(
        page.read_text(encoding="utf-8").replace(
            marker, f"<script>{SAVE_FAKE % body}</script>" + marker
        )
        + f"<script>{SAVE_DRIVER}</script>",
        encoding="utf-8",
    )
    dom = dump_dom(harness, "selftest", budget=8000)
    match = re.search(r'<pre id="save-dump">(.*?)</pre>', dom, re.S)
    assert match, "the driver wrote no dump"
    dump: dict[str, Any] = json.loads(html.unescape(match.group(1)))
    dump["output"] = selftest_output(dom)
    return dump


def test_download_is_saved_through_the_downloads_capability_and_its_refusal_is_shown(
    root: Path, stub_atlas: Path
) -> None:
    """R-P1-48: claude.ai never lets a page download by itself; `save` is the only way out."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    saved = run_with_downloads(page, SAVE_ACCEPTS)
    assert len(saved["saves"]) == 1
    assert saved["saves"][0]["filename"] == "TBX5-review-e-a-m.json"
    assert saved["saves"][0]["data"] == saved["last"]
    assert json.loads(saved["saves"][0]["data"]) == saved["output"]
    assert saved["status"][-1] == "Review saved as TBX5-review-e-a-m.json."
    for code_, words in {
        "declined": "you declined it.",
        "unavailable": "saving a file is unavailable in this view.",
        "too_large": "the file is too large to save.",
        "rate_limited": "try again shortly.",
        "bad_request": "the save failed (bad_request).",
    }.items():
        refused = run_with_downloads(page, SAVE_REFUSES % code_)
        assert len(refused["saves"]) == 1, "a refusal must not be retried"
        line = refused["status"][-1]
        assert line.startswith("The review was not saved: ") and words in line
        # each code gets its own wording, never another's
        for other, other_words in {"declined": "declined", "unavailable": "unavailable in"}.items():
            assert (other_words in line) is (other == code_)
        assert not [m for m in refused["status"] if "Review saved" in m]


def test_a_page_opened_as_a_file_is_not_given_the_claude_ai_message(
    root: Path, stub_atlas: Path
) -> None:
    """Without window.claude the page shows no saved / not-saved line and still writes its output.

    It does not prove the Blob link's click: a selftest returns before it.
    """
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    # no window.claude at all: the page was opened as a file, as a selftest does by default
    dom = dump_dom(page, "selftest", budget=8000)
    assert selftest_output(dom)["format"] == 1
    status = re.search(r'<pre id="selftest-status"[^>]*>(.*?)</pre>', dom, re.S)
    assert status and "Review saved" not in status.group(1) and "not saved" not in status.group(1)


@pytest.mark.parametrize(
    "body",
    ["return null;", "throw { code: 'unavailable', message: 'x' };"],
    ids=["null", "rejects"],
)
def test_on_claude_ai_a_downloads_capability_that_is_null_or_rejects_says_nothing_was_saved(
    root: Path, stub_atlas: Path, body: str
) -> None:
    """A Blob link does nothing on claude.ai: a null or rejected capability must not fall to it."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    got = run_with_downloads(page, body)
    line = got["status"][-1]
    assert line.startswith("The review was not saved: ") and "cannot save a file" in line
    assert "open this page as a file" in line.lower()
    assert got["saves"] == [] and not [m for m in got["status"] if "Review saved" in m]


def test_the_page_title_names_the_gene(root: Path, stub_atlas: Path) -> None:
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    assert "<title>TBX5 literature review</title>" in page.read_text(encoding="utf-8")


def test_a_span_only_card_with_judged_rows_carries_the_chip_of_its_class_only(
    root: Path, stub_atlas: Path
) -> None:
    """Stage B re-review residual 4: 'readers agreed' beside 'N judged' says two opposite things.

    The comparison calls A span-only, so it is agreed; a judge decision on a real field of it
    (the run is inconsistent, but the card must still say one thing) leaves its row shown.
    """
    judge = root / "local/literature/runs/TBX5/r1/judge/judge-01.decisions.yaml"
    judge.write_text(
        judge.read_text(encoding="utf-8")
        + f"""{A}:
  - field: summary
    first: "one"
    second: "two"
    decision: first
    value: "one"
    quote: ""
    where: ""
    rule: "Reader 1 states it."
""",
        encoding="utf-8",
    )
    code, out = build(root, stub_atlas)
    assert code == 0, out
    dom = dump_dom(Path(out.strip().splitlines()[-1]), "")
    chips = re.search(r'<div class="sum-chips">(.*?)</div></summary>', card_html(dom, A), re.S)
    text = plain(chips.group(1)) if chips else ""
    assert "readers agreed" in text and "judged" not in text


def test_a_downloads_capability_that_never_answers_says_nothing_was_saved(
    root: Path, stub_atlas: Path
) -> None:
    """On claude.ai a Blob link does nothing, so a timeout must not fall through to it silently."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    hang = run_with_downloads(page, "return new Promise(() => {});")
    line = hang["status"][-1]
    assert line.startswith("The review was not saved: ") and "did not answer in time" in line
    assert "open this page as a file" in line and "Download my review to try again" in line
    assert hang["saves"] == [] and not [m for m in hang["status"] if "Review saved" in m]


@pytest.mark.parametrize(
    "act, low, high",
    [
        ("", 4019, 4019 + 3000),
        ("window.__open(); await wait(60); window.__close(); await wait(10);", 4019, 4019 + 3000),
    ],
    ids=["restored", "merged-with-an-opened-card"],
)
def test_a_stored_active_time_that_is_a_string_is_added_not_joined(
    root: Path, stub_atlas: Path, act: str, low: int, high: int
) -> None:
    """`"4019" + 0` is "40190": a time kept in storage as text must still count as a number."""
    code, out = build(root, stub_atlas)
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    shown = {c["id"]: c["shown_digest"] for c in data_of(page)["findings"]}
    sha = data_of(page)["page"]["literature_sha256"]
    saved = {
        "decisions": {
            G: {
                "decision": "approve",
                "note": "kept",
                "shown_digest": shown[G],
                "decided_at": "2026-11-19T10:00:00Z",
                "active_ms": "4019",
            }
        },
        "session": {"started_at": "2026-11-19T09:00:00.000Z"},
    }
    head = (
        f"localStorage.setItem({json.dumps(f'chd-review:{sha}:owner:e-a-m')},"
        f"{json.dumps(json.dumps(saved))});"
        "window.__beforeRestore = async () => {"
        "const wait = (ms) => new Promise((r) => setTimeout(r, ms));"
        f"const card = () => document.querySelector('details.card[data-id=\"{G}\"]');"
        "window.__open = () => { card().open = true; };"
        "window.__close = () => { card().open = false; };"
        f"{act} }};"
    )
    dom = dump_dom(_persist_page(page, head), "selftest-restore", budget=8000)
    got = next(f for f in selftest_output(dom)["findings"] if f["id"] == G)
    assert isinstance(got["active_ms"], int) and low <= got["active_ms"] <= high


def test_a_placement_reading_is_rendered_in_its_variant_card_as_text(
    root: Path, stub_atlas: Path
) -> None:
    """The reading a run's normalise.json gives a variant reaches the rendered card, and markup
    inside it is shown as text. The other findings' cards carry no such line."""
    reading = "read NM_1 as NM_1.1 (CP-D8: <b>bold</b> & more)"
    run = root / "local/literature/runs/TBX5/r1"
    (run / "normalise.json").write_text(
        json.dumps(
            {
                "format": 1,
                "normalisation": 1,
                "variants": [
                    {
                        "finding": G,
                        "variant": 1,
                        "outcome": "placed",
                        "line": "x",
                        "reading": reading,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    code, out = build(root, stub_atlas)
    assert code == 0, out

    dom = dump_dom(Path(out.strip().splitlines()[-1]), "")

    mine = card_html(dom, G)
    placed = re.findall(r'<div class="placed">(.*?)</div>', mine, re.S)
    assert any(plain(p) == reading for p in placed), placed
    assert "<b>bold</b>" not in mine and "&lt;b&gt;bold&lt;/b&gt;" in mine
    assert "CP-D8" not in card_html(dom, J)


def test_the_owner_page_shows_the_owners_earlier_word_on_its_card(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path)
    old = "sha256:" + "9" * 64
    put(archive, tmp_path, "E.A.M", "owner", T_X, [
        finding(J, "fix", D2, T_X, "the count is wrong"),
        finding(A, "later", old, T_X, "ask the author"),
        finding(G, "approve", D1, T_X, "fine"),
    ])  # fmt: skip
    commit_all(archive)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    dom = dump_dom(Path(out.strip().splitlines()[-1]), "")
    assert "Your note, 2026-11-19: Needs a fix: the count is wrong" in plain(card_html(dom, J))
    assert "Your note, 2026-11-19, on an earlier version: Ask later: ask the author" in plain(
        card_html(dom, A)
    )
    assert "Your note, 2026-11-19: Approve: fine" in plain(card_html(dom, G))


def test_a_verdict_with_no_card_reaches_the_fold_with_where_it_is(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    run = root / "local/literature/runs/TBX5/r1"
    comparison = json.loads((run / "comparison.json").read_text(encoding="utf-8"))
    del comparison["pairs"][A]
    comparison["span_only"].remove(A)
    (run / "comparison.json").write_text(json.dumps(comparison), encoding="utf-8")
    gone = "CHDA:LIT:0000999"
    archive = make_archive(tmp_path)
    put(archive, tmp_path, "Dr X", "second_opinion", T_X, [
        finding(A, "fix", D1, T_X, "check the proband count"),
        finding(gone, "later", D1, T_X, "ask"),
    ])  # fmt: skip
    put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T14:00:00Z", [
        finding(A, "approve", D1, "2026-11-19T14:00:00Z"),
        finding(gone, "fix", D1, "2026-11-19T14:00:00Z", "wrong gene"),
    ])  # fmt: skip
    commit_all(archive)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    dom = visible(dump_dom(Path(out.strip().splitlines()[-1]), ""))
    fold = re.search(r'<details class="static-card" id="not-on-page">(.*?)</details>', dom, re.S)
    assert fold
    text = plain(fold.group(1))
    assert text.startswith("Earlier verdicts on findings not on this page (3)")
    assert (
        f"{A}, on another page of this gene: Dr X: needs a fix: check the proband count "
        "(2026-11-19)"
    ) in text
    assert (
        f"{gone}, no longer in the gene's file: You: needs a fix: wrong gene (2026-11-19)" in text
    )
    assert f"{gone}, no longer in the gene's file: Dr X: ask later: ask (2026-11-19)" in text
    assert "You: approved" not in text  # the owner's plain approve of an off-page id


def test_the_fold_is_absent_when_nothing_is_off_the_page(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path)
    filed_by_dr_x(archive, tmp_path)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    assert "not-on-page" not in visible(dump_dom(Path(out.strip().splitlines()[-1]), ""))


def test_the_page_says_what_archive_it_read(root: Path, stub_atlas: Path, tmp_path: Path) -> None:
    archive = make_archive(tmp_path)
    filed_by_dr_x(archive, tmp_path)
    sha = subprocess.run(
        ["git", "-C", str(archive), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    meta = plain(visible(dump_dom(page, "")))
    assert f"Review archive read at {sha}: 1 decisions file from 1 reviewer" in meta
    code, out = build(root, stub_atlas)
    assert code == 0, out
    meta = plain(visible(dump_dom(Path(out.strip().splitlines()[-1]), "")))
    assert "No review archive read: earlier notes and second opinions are not shown" in meta
    code, out = build(root, stub_atlas, "--reviewer", "Dr Y", "--role", "second_opinion")
    assert code == 0, out
    dom = visible(dump_dom(Path(out.strip().splitlines()[-1]), ""))
    assert "Review archive" not in dom and "No review archive" not in dom


RULE = (
    "Your flips are ticked again below and kept unless you undo them. Deciding this card "
    "closes every other flip listed here that you do not make yourself; Ask later closes none."
)


def flipped_archive(tmp_path: Path) -> Path:
    """Dr X has flipped P1 and the owner has flipped P2; both are still open."""
    archive = make_archive(tmp_path)
    put(
        archive,
        tmp_path,
        "Dr X",
        "second_opinion",
        "2026-11-19T10:00:00Z",
        [],
        tri("fix", flip(P1)),
    )
    put(archive, tmp_path, "E.A.M", "owner", "2026-11-19T11:00:00Z", [], tri("fix", flip(P2)))
    commit_all(archive)
    return archive


def triage_rows(dom: str) -> dict[str, str]:
    card = card_html(dom, "triage")
    return {
        re.search(r"PMID:\d+", row).group(0): plain(row)  # type: ignore[union-attr]
        for row in re.findall(r"<tr>(.*?)</tr>", card, re.S)
        if "PMID:" in row
    }


def test_the_triage_card_lists_earlier_verdicts_and_open_flips_and_ticks_only_the_owners(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    archive = flipped_archive(tmp_path)
    put(archive, tmp_path, "Dr Y", "second_opinion", T_X, [], tri("approve"))
    put(archive, tmp_path, "Dr Z", "second_opinion", T_X, [], tri("later"))
    commit_all(archive)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    assert [
        (f["name"], f["role"], f["pmid"]) for f in data_of(page)["triage_earlier"]["flips"]
    ] == [
        ("E.A.M", "owner", P2),
        ("Dr X", "second_opinion", P1),
    ]
    dom = dump_dom(page, "")
    text = plain(card_html(dom, "triage"))
    assert "Your note, 2026-11-19: Needs a fix: n" in text
    assert "Dr Y: agreed" in text and "Dr Z: ask later: n" in text
    assert "Your flip, 2026-11-19: PMID:90000002 to excluded: because" in text
    assert "Dr X flipped PMID:90000001 to excluded: because" in text
    assert text.count(RULE) == 1
    rows = triage_rows(dom)
    assert "flipped to excluded because Undo flip" in rows[P2]
    assert "flipped to" not in rows[P1] and "Exclude instead" in rows[P1]
    assert "flipped to" not in rows[P3]
    assert RULE not in plain(visible(dom).replace(card_html(dom, "triage"), ""))


def test_the_rule_sentence_is_absent_when_no_flip_is_open(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path)
    put(archive, tmp_path, "Dr Y", "second_opinion", T_X, [], tri("approve"))
    commit_all(archive)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    text = plain(card_html(dump_dom(Path(out.strip().splitlines()[-1]), ""), "triage"))
    assert "Dr Y: agreed" in text and "ticked again" not in text


@pytest.mark.parametrize(
    ("mode", "flips", "finished", "banner"),
    [
        ("earlier-approve", [P2], True, False),
        ("earlier-undo", [], True, False),
        ("earlier-undecided", None, True, False),
        ("earlier-add", None, False, True),
        ("earlier-flip", [P2, P1], True, False),
    ],
)
def test_ticked_again_flips_reach_the_file_until_the_owner_undoes_them(
    root: Path,
    stub_atlas: Path,
    tmp_path: Path,
    atlas_root: Path,
    mode: str,
    flips: list[str] | None,
    finished: bool,
    banner: bool,
) -> None:
    archive = flipped_archive(tmp_path)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    dom = dump_dom(Path(out.strip().splitlines()[-1]), f"selftest-{mode}")
    decisions = selftest_output(dom)
    assert [
        e.message for e in schema_validator(atlas_root, "review_decisions").iter_errors(decisions)
    ] == []
    if flips is None:
        assert decisions["triage"] is None
    else:
        assert [f["pmid"] for f in decisions["triage"]["flips"]] == flips
        assert decisions["triage"]["decision"] == "approve"
    assert (decisions["session"]["finished_at"] is not None) is finished
    shown = visible(dom)
    hidden = re.search(r'<div id="banner"[^>]*\bhidden', shown) is not None
    assert hidden is not banner
    if banner:
        # the ticked-again flip and the one made here: both are on the card, neither has a verdict
        assert "The triage card carries 2 flipped papers and no verdict." in plain(shown)


@pytest.mark.parametrize("owner_flips_the_paper", [False, True])
def test_the_page_and_the_reader_agree_on_which_flips_a_decision_closed(
    root: Path,
    stub_atlas: Path,
    tmp_path: Path,
    atlas_root: Path,
    owner_flips_the_paper: bool,
) -> None:
    """The contract: the page's own data is what the reader reads back as 'shown'."""
    archive = flipped_archive(tmp_path)
    code, out = build(root, stub_atlas, "--archive", str(archive))
    assert code == 0, out
    page = Path(out.strip().splitlines()[-1])
    runner = CliRunner()
    filed = runner.invoke(
        app, ["archive-page", "TBX5", "--run", "r1", "--page", str(page), "--archive", str(archive)]
    )
    assert filed.exit_code == 0, filed.output
    mode = "earlier-flip" if owner_flips_the_paper else "earlier-approve"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        json.dumps(selftest_output(dump_dom(page, f"selftest-{mode}"))), encoding="utf-8"
    )
    done = runner.invoke(
        app,
        ["archive-decisions", "TBX5", "--run", "r1", "--decisions", str(decisions)]
        + ["--archive", str(archive), "--root", str(root)],
    )
    assert done.exit_code == 0, done.output
    got = runner.invoke(
        app,
        ["archive-state", "TBX5", "--archive", str(archive), "--root", str(root), "--json"]
        + ["--atlas-cli", str(stub_atlas)],
    )
    assert got.exit_code == 0, got.output
    rows = {r["name"]: r for r in json.loads(got.output)["triage"]["flips"]}
    x = rows["Dr X"]
    assert x["pending"] is owner_flips_the_paper
    assert (x["closed_by"] is not None) is not owner_flips_the_paper
    if x["closed_by"] is not None:
        assert x["closed_by"].endswith("--e-a-m.json") and "/decisions/" in x["closed_by"]
    assert rows["E.A.M"]["pending"] is True  # the owner's own flip was kept, so it is still open
