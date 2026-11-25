"""``chd-curator texts``, over a fake PubMed, a fake web and the stub atlas.

Five synthetic papers (PMIDs 9100001 to 9100005), one per way a text arrives:

- 9100001: an open-access PMC paper, with a supplement zip holding a workbook, a text file and
  an image;
- 9100002: a PMC author manuscript (Europe PMC lists it as not open access), no supplements;
- 9100003: a closed paper whose OpenAlex copy is a PDF, a case report;
- 9100004: a closed paper whose OpenAlex copy is a landing page, and whose publisher serves a
  workbook and a PDF that does not exist;
- 9100005: a paper with a PDF supplied by hand.

Two more are in the triage but not fetched: 9100006 (excluded, reason other) and 9100007
(pending). Nothing here touches the network and no text is a real paper's.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from chd_curator import cli, pubmed
from chd_curator import convert as convert_module
from chd_curator.cli import app
from chd_curator.quotes import check_quotes
from chd_curator.run import RunPaths, load_texts
from chd_curator.sources import EPMC_BASE, OPENALEX_BASE, Response, SourceError
from fake_http import FakeWeb, ok
from fake_pubmed import FakePubMed
from test_convert import JATS, LOGIN, _pdf, _words, _xlsx, _zip

runner = CliRunner()
ROOT_FILES = Path(__file__).parent / "fixtures" / "texts" / "responses"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SECRET = "k3y-SECRET-0f9a"


def search_url(pmid: str) -> str:
    return f"{EPMC_BASE}search?query=EXT_ID:{pmid}%20AND%20SRC:MED&resultType=core&format=json"


def efetch_url(pmcid: str) -> str:
    return f"{pubmed.BASE_URL}efetch.fcgi?db=pmc&id={pmcid.removeprefix('PMC')}&tool=chd-curator"


def search(
    pmid: str, pmcid: str | None = None, oa: bool = False, licence: str | None = None
) -> Response:
    hit: dict[str, Any] = {
        "pmid": pmid,
        "title": f"Title of {pmid}",
        "pubYear": "2001",
        "isOpenAccess": "Y" if oa else "N",
        "inPMC": "Y" if pmcid else "N",
    }
    if pmcid:
        hit["pmcid"] = pmcid
    if licence:
        hit["license"] = licence
    return ok(search_url(pmid), json.dumps({"resultList": {"result": [hit]}}).encode())


def not_found(url: str) -> Response:
    return Response(404, url, "text/html", b"")


def jats(title: str) -> bytes:
    return JATS.replace(b"A synthetic study", title.encode())


SUPPLIED = _pdf([" ".join(f"supplied{i}" for i in range(1000))])
OPENALEX_PDF = _pdf([_words(1000)])
PAPER_PAGE = (ROOT_FILES / "publisher_page_two_supplements.html").read_bytes()
PUBLISHER_URL = "https://pub.example.com/article/2"
WORKBOOK_URL = "https://pub.example.com/article/2/MediaObjects/41588_2099_Article_MOESM1_ESM.xlsx"
APPENDIX_URL = "https://static.example.com/files/appendix.pdf"
NO_LINKS = b"<html><body><p>An article page with no supplement links.</p></body></html>"


def plain_page(pmid: str) -> Response:
    return Response(200, f"https://pub.example.com/article/{pmid}", "text/html", NO_LINKS)


def routes() -> dict[str, Response | Exception]:
    openalex_landing = {
        "best_oa_location": {
            "is_oa": True,
            "landing_page_url": "https://repo.example.org/record/4",
            "pdf_url": None,
            "license": "cc-by-nc",
        },
        "locations": [],
    }
    openalex_pdf = {
        "best_oa_location": {
            "is_oa": True,
            "landing_page_url": "https://repo.example.org/record/3",
            "pdf_url": "https://repo.example.org/files/3.pdf",
            "license": "cc-by",
        },
        "locations": [],
    }
    zipped = _zip([("T1.xlsx", _xlsx()), ("notes.txt", b"Plain notes line."), ("fig1.png", PNG)])
    return {
        search_url("9100001"): search("9100001", "PMC9100001", True, "cc by"),
        f"{EPMC_BASE}PMC9100001/fullTextXML": ok("u", jats("An open study"), "text/xml"),
        f"{EPMC_BASE}PMC9100001/supplementaryFiles": ok("u", zipped, "application/zip"),
        search_url("9100002"): search("9100002", "PMC9100002"),
        efetch_url("PMC9100002"): ok("u", jats("An author manuscript"), "text/xml"),
        f"{EPMC_BASE}PMC9100002/supplementaryFiles": not_found("u"),
        "https://doi.org/10.1000/t9100002": plain_page("9100002"),
        search_url("9100003"): search("9100003"),
        "https://doi.org/10.1000/t9100003": plain_page("9100003"),
        f"{OPENALEX_BASE}pmid:9100003": ok("u", json.dumps(openalex_pdf).encode()),
        "https://repo.example.org/files/3.pdf": ok("u", OPENALEX_PDF, "application/pdf"),
        search_url("9100004"): search("9100004"),
        f"{OPENALEX_BASE}pmid:9100004": ok("u", json.dumps(openalex_landing).encode()),
        "https://repo.example.org/record/4": ok("u", LOGIN, "text/html"),
        "https://doi.org/10.1000/t9100004": Response(200, PUBLISHER_URL, "text/html", PAPER_PAGE),
        WORKBOOK_URL: ok("u", _xlsx()),
        APPENDIX_URL: not_found(APPENDIX_URL),
        search_url("9100005"): search("9100005"),
        "https://doi.org/10.1000/t9100005": plain_page("9100005"),
    }


def pubmed_papers() -> dict[str, dict[str, object]]:
    def paper(pmid: str, year: int, types: list[str]) -> dict[str, object]:
        return {
            "title": f"Title of {pmid}",
            "year": year,
            "abstract": f"Abstract of {pmid}.",
            "types": types,
        }

    return {
        "9100001": paper("9100001", 2001, ["Journal Article"]),
        "9100002": paper("9100002", 2002, ["Journal Article"]),
        "9100003": paper("9100003", 2003, ["Case Reports", "Journal Article"]),
        "9100004": paper("9100004", 2004, ["Journal Article"]),
        "9100005": paper("9100005", 2005, ["Journal Article"]),
    }


TRIAGE = """\
gene: HGNC:11604
searches: []
triage:
  - {pmid: 'PMID:9100001', search: 1, decision: included}
  - {pmid: 'PMID:9100002', search: 1, decision: included}
  - {pmid: 'PMID:9100003', search: 1, decision: excluded, reason: full_text_not_accessible}
  - {pmid: 'PMID:9100004', search: 1, decision: included}
  - {pmid: 'PMID:9100005', search: 1, decision: included}
  - {pmid: 'PMID:9100006', search: 1, decision: excluded, reason: other, note: x}
  - {pmid: 'PMID:9100007', search: 1, decision: pending}
  - {pmid: 'PMID:9100001', search: 2, decision: included}
findings:
  - {id: 'CHDA:LIT:0000001', publication: 'PMID:9100001'}
  - {id: 'CHDA:LIT:0000002', publication: 'PMID:9100003'}
  - {id: 'CHDA:LIT:0000003', publication: 'PMID:9100004'}
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "curation/literature").mkdir(parents=True)
    (tmp_path / "curation/literature/TBX5.yaml").write_text(TRIAGE)
    supplied = tmp_path / "local/literature/supplied"
    supplied.mkdir(parents=True)
    (supplied / "9100005.pdf").write_bytes(SUPPLIED)
    return tmp_path


class World:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        papers: dict[str, dict[str, object]],
        web_routes: dict[str, Response | Exception],
    ) -> None:
        self.pubmed = FakePubMed(papers)
        self.web = FakeWeb(web_routes)
        monkeypatch.setattr(cli, "_fetch", self.pubmed)
        monkeypatch.setattr(cli, "_get", self.web)
        monkeypatch.delenv("NCBI_API_KEY", raising=False)

    @property
    def requests(self) -> int:
        return len(self.pubmed.urls) + len(self.web.urls)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch, pubmed_papers(), routes())


def texts(stub: Path, root: Path, run: str = "r1", *flags: str, symbol: str = "TBX5") -> Any:
    return runner.invoke(
        app, ["texts", symbol, "--run", run, "--root", str(root), "--atlas-cli", str(stub), *flags]
    )


def texts_json(root: Path, run: str = "r1") -> dict[str, Any]:
    data = json.loads((root / f"local/literature/runs/TBX5/{run}/texts.json").read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def by_pmid(root: Path, run: str = "r1") -> dict[str, dict[str, Any]]:
    return {p["pmid"]: p for p in texts_json(root, run)["papers"]}


def attempt(via: str, url: str, outcome: str) -> dict[str, str]:
    return {"via": via, "url": url, "outcome": outcome}


T = "local/literature/texts"


def literal_papers() -> list[dict[str, Any]]:
    epmc = f"{EPMC_BASE}"
    return [
        {
            "pmid": "PMID:9100004",
            "source": "abstract",
            "text": None,
            "abstract": f"{T}/9100004/abstract.txt",
            "supplements": [f"{T}/9100004/supplements/41588_2099_Article_MOESM1_ESM.xlsx.txt"],
            "needed": True,
            "why": (
                "no free full text found (europepmc found, no PMC id, not open access; "
                "openalex free copies: 1; "
                "openalex rejected: a landing page: 13 words, no methods heading, "
                "no results heading); PubMed types: Journal Article; "
                "supplements not read (appendix.pdf: publisher failed: HTTP 404)"
            ),
            "title": "Title of 9100004",
            "year": "2004",
            "doi": "10.1000/t9100004",
            "licence": None,
            "attempts": [
                attempt("europepmc", search_url("9100004"), "found, no PMC id, not open access"),
                attempt("openalex", f"{OPENALEX_BASE}pmid:9100004", "free copies: 1"),
                attempt(
                    "openalex",
                    "https://repo.example.org/record/4",
                    "rejected: a landing page: 13 words, no methods heading, no results heading",
                ),
                attempt("publisher", "https://doi.org/10.1000/t9100004", "supplement links: 2"),
                attempt("publisher", WORKBOOK_URL, "converted, 20 words"),
                attempt("publisher", APPENDIX_URL, "failed: HTTP 404"),
            ],
        },
        {
            "pmid": "PMID:9100001",
            "source": "full_text",
            "text": f"{T}/9100001/fulltext.txt",
            "abstract": f"{T}/9100001/abstract.txt",
            "supplements": [
                f"{T}/9100001/supplements/T1.xlsx.txt",
                f"{T}/9100001/supplements/notes.txt.txt",
            ],
            "needed": False,
            "why": (
                "supplements not read (fig1.png: europepmc rejected: fig1.png: a PNG image, "
                "not text)"
            ),
            "title": "Title of 9100001",
            "year": "2001",
            "doi": "10.1000/t9100001",
            "licence": "cc by",
            "attempts": [
                attempt("europepmc", search_url("9100001"), "found, PMC9100001, open access"),
                attempt("europepmc", f"{epmc}PMC9100001/fullTextXML", "accepted, 35 words"),
                attempt("europepmc", f"{epmc}PMC9100001/supplementaryFiles", "zip files: 3"),
                attempt(
                    "europepmc",
                    f"{epmc}PMC9100001/supplementaryFiles#T1.xlsx",
                    "converted, 20 words",
                ),
                attempt(
                    "europepmc",
                    f"{epmc}PMC9100001/supplementaryFiles#notes.txt",
                    "converted, 3 words",
                ),
                attempt(
                    "europepmc",
                    f"{epmc}PMC9100001/supplementaryFiles#fig1.png",
                    "rejected: fig1.png: a PNG image, not text",
                ),
            ],
        },
        {
            "pmid": "PMID:9100002",
            "source": "full_text",
            "text": f"{T}/9100002/fulltext.txt",
            "abstract": f"{T}/9100002/abstract.txt",
            "supplements": [],
            "needed": False,
            "why": "",
            "title": "Title of 9100002",
            "year": "2002",
            "doi": "10.1000/t9100002",
            "licence": None,
            "attempts": [
                attempt("europepmc", search_url("9100002"), "found, PMC9100002, not open access"),
                attempt("pmc", efetch_url("PMC9100002"), "accepted, 35 words"),
                attempt("europepmc", f"{epmc}PMC9100002/supplementaryFiles", "not available (404)"),
                attempt("publisher", "https://doi.org/10.1000/t9100002", "supplement links: 0"),
            ],
        },
        {
            "pmid": "PMID:9100003",
            "source": "full_text",
            "text": f"{T}/9100003/fulltext.txt",
            "abstract": f"{T}/9100003/abstract.txt",
            "supplements": [],
            "needed": False,
            "why": "",
            "title": "Title of 9100003",
            "year": "2003",
            "doi": "10.1000/t9100003",
            "licence": "cc-by",
            "attempts": [
                attempt("europepmc", search_url("9100003"), "found, no PMC id, not open access"),
                attempt("openalex", f"{OPENALEX_BASE}pmid:9100003", "free copies: 1"),
                attempt("openalex", "https://repo.example.org/files/3.pdf", "accepted, 1000 words"),
                attempt("publisher", "https://doi.org/10.1000/t9100003", "supplement links: 0"),
            ],
        },
        {
            "pmid": "PMID:9100005",
            "source": "supplied_pdf",
            "text": f"{T}/9100005/fulltext.txt",
            "abstract": f"{T}/9100005/abstract.txt",
            "supplements": [],
            "needed": False,
            "why": "",
            "title": "Title of 9100005",
            "year": "2005",
            "doi": "10.1000/t9100005",
            "licence": None,
            "attempts": [
                attempt(
                    "supplied", "local/literature/supplied/9100005.pdf", "accepted, 1000 words"
                ),
                attempt("europepmc", search_url("9100005"), "found, no PMC id, not open access"),
                attempt("publisher", "https://doi.org/10.1000/t9100005", "supplement links: 0"),
            ],
        },
    ]


def test_the_run_writes_the_whole_texts_document(
    stub_atlas: Path, root: Path, world: World
) -> None:
    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == (
        "5 papers: 4 full text (1 Europe PMC, 1 PMC, 1 OpenAlex, 1 supplied), "
        "1 abstract only, 0 neither; 3 supplement files converted, 2 rejected; 1 needed"
    )
    assert texts_json(root) == {"format": 1, "papers": literal_papers()}
    assert not any("email" in url for url in [*world.pubmed.urls, *world.web.urls])
    # 9100006 (excluded, other) and 9100007 (pending) are never asked about
    assert not any("9100006" in url or "9100007" in url for url in world.pubmed.urls)
    # the duplicate triage entry for 9100001 is fetched once
    assert sum(search_url("9100001") == url for url in world.web.urls) == 1


def test_the_files_under_each_paper_hold_the_texts_and_the_raw_bytes(
    stub_atlas: Path, root: Path, world: World
) -> None:
    texts(stub_atlas, root)
    base = root / T

    assert (base / "9100003/abstract.txt").read_text("utf-8") == (
        "Title of 9100003\nYear: 2003\nPublication types: Case Reports; Journal Article\n"
        "\nAbstract of 9100003.\n"
    )
    assert (base / "9100001/raw/fulltext.xml").read_bytes() == jats("An open study")
    assert (base / "9100003/raw/fulltext.pdf").read_bytes() == OPENALEX_PDF
    assert (base / "9100005/raw/fulltext.pdf").read_bytes() == SUPPLIED
    assert (base / "9100001/supplements/notes.txt.txt").read_text("utf-8") == "Plain notes line."
    assert (base / "9100001/raw/supplements/T1.xlsx").read_bytes() == _xlsx()
    assert not (base / "9100001/raw/supplements/fig1.png").exists()
    assert not (base / "9100001/supplements/fig1.png.txt").exists()
    # the landing page that failed the verdict is kept nowhere
    assert not (base / "9100004/fulltext.txt").exists()
    assert not (base / "9100004/raw/fulltext.html").exists()
    assert json.loads((base / "9100004/fetch.json").read_text("utf-8"))["source"] == "abstract"


def test_the_phase_one_loader_reads_the_run(stub_atlas: Path, root: Path, world: World) -> None:
    texts(stub_atlas, root)

    loaded = load_texts(RunPaths(root, "TBX5", "r1"))

    assert [(p.pmid, p.source, p.needed) for p in loaded] == [
        ("PMID:9100004", "abstract", True),
        ("PMID:9100001", "full_text", False),
        ("PMID:9100002", "full_text", False),
        ("PMID:9100003", "full_text", False),
        ("PMID:9100005", "supplied_pdf", False),
    ]
    first = loaded[1]
    assert first.text == root / T / "9100001/fulltext.txt"
    assert first.licence == "cc by" and first.doi == "10.1000/t9100001" and first.year == "2001"
    assert [a.via for a in first.attempts][:2] == ["europepmc", "europepmc"]
    assert loaded[0].text is None


def test_a_landing_page_copy_is_an_attempt_with_its_reason_and_the_paper_is_needed(
    stub_atlas: Path, root: Path, world: World
) -> None:
    texts(stub_atlas, root)

    paper = by_pmid(root)["PMID:9100004"]

    assert paper["needed"] is True and paper["source"] == "abstract" and paper["text"] is None
    assert (
        attempt(
            "openalex",
            "https://repo.example.org/record/4",
            "rejected: a landing page: 13 words, no methods heading, no results heading",
        )
        in paper["attempts"]
    )
    assert "a landing page: 13 words" in paper["why"]
    assert "PubMed types: Journal Article" in paper["why"]


def test_a_second_run_asks_nobody_and_refresh_asks_everybody_again(
    stub_atlas: Path, root: Path, world: World
) -> None:
    texts(stub_atlas, root, "r1")
    asked = (len(world.pubmed.urls), len(world.web.urls))
    first = texts_json(root, "r1")

    second = texts(stub_atlas, root, "r2")

    assert second.exit_code == 0, second.stdout
    assert (len(world.pubmed.urls), len(world.web.urls)) == asked
    assert texts_json(root, "r2") == first

    third = texts(stub_atlas, root, "r3", "--refresh")

    assert third.exit_code == 0, third.stdout
    assert len(world.pubmed.urls) == 2 * asked[0] and len(world.web.urls) == 2 * asked[1]
    assert texts_json(root, "r3") == first


def test_a_run_that_has_its_texts_is_refused_before_any_request(
    stub_atlas: Path, root: Path, world: World
) -> None:
    texts(stub_atlas, root)
    before = (root / "local/literature/runs/TBX5/r1/texts.json").read_bytes()
    asked = world.requests

    again = texts(stub_atlas, root, "r1", "--refresh")

    assert again.exit_code == 2
    assert "texts.json exists" in again.stdout
    assert world.requests == asked
    assert (root / "local/literature/runs/TBX5/r1/texts.json").read_bytes() == before


def test_a_source_that_fails_for_one_paper_is_recorded_and_the_others_complete(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = routes()
    web[search_url("9100001")] = Response(500, search_url("9100001"), "text/html", b"")
    web[f"{OPENALEX_BASE}pmid:9100001"] = not_found("u")
    web["https://doi.org/10.1000/t9100001"] = Response(
        404, "https://doi.org/10.1000/t9100001", "text/html", b""
    )
    World(monkeypatch, pubmed_papers(), web)

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    papers = by_pmid(root)
    assert papers["PMID:9100001"]["needed"] is True
    assert papers["PMID:9100001"]["attempts"][0] == attempt(
        "europepmc", search_url("9100001"), "failed: www.ebi.ac.uk answered HTTP 500 for a search"
    )
    assert [p["source"] for k, p in papers.items() if k != "PMID:9100001"] == [
        "abstract",
        "full_text",
        "full_text",
        "supplied_pdf",
    ]
    assert papers["PMID:9100004"]["needed"] is True


def test_the_ncbi_key_is_sent_to_ncbi_and_is_in_no_file(
    stub_atlas: Path, root: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NCBI_API_KEY", SECRET)

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    sent = [u for u in [*world.pubmed.urls, *world.web.urls] if SECRET in u]
    assert len(sent) == len(world.pubmed.urls) + 1  # every PubMed request and the PMC efetch
    assert any("efetch.fcgi?db=pmc" in u for u in sent)
    files = [f for f in (root / "local").rglob("*") if f.is_file()]
    assert files
    assert [f for f in files if SECRET.encode() in f.read_bytes()] == []
    assert SECRET not in result.stdout
    assert by_pmid(root)["PMID:9100002"]["attempts"][1]["url"] == efetch_url("PMC9100002")


def test_needed_papers_come_first_non_case_reports_first_then_newest_then_pmid(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    papers = {
        "9100015": ("2015", ["Journal Article"]),
        "9100009": ("2009", ["Journal Article"]),
        "9100013": ("2020", ["Case Reports", "Journal Article"]),
        "9100012": ("2000", ["Journal Article"]),
        "9100011": ("2010", ["Journal Article"]),
        "9100010": ("2010", ["Journal Article"]),
    }
    (tmp_path / "curation/literature").mkdir(parents=True)
    triage = "".join(f"  - {{pmid: 'PMID:{p}', search: 1, decision: included}}\n" for p in papers)
    (tmp_path / "curation/literature/TBX5.yaml").write_text(
        f"gene: HGNC:11604\ntriage:\n{triage}findings: []\n"
    )
    web = {
        f"{EPMC_BASE}search?": ok("u", b'{"resultList": {"result": []}}'),
        OPENALEX_BASE: not_found("u"),
        "https://doi.org/": Response(404, "https://doi.org/x", "text/html", b""),
    }
    for pmid in ("9100009", "9100015"):
        web[search_url(pmid)] = search(pmid, f"PMC{pmid}", True)
        web[f"{EPMC_BASE}PMC{pmid}/fullTextXML"] = ok("u", JATS, "text/xml")
        web[f"{EPMC_BASE}PMC{pmid}/supplementaryFiles"] = not_found("u")
    World(
        monkeypatch,
        {
            p: {"title": f"T{p}", "year": int(y), "abstract": "An abstract.", "types": t}
            for p, (y, t) in papers.items()
        },
        web,
    )

    result = texts(stub_atlas, tmp_path)

    assert result.exit_code == 0, result.stdout
    assert [(p["pmid"], p["needed"]) for p in texts_json(tmp_path)["papers"]] == [
        ("PMID:9100010", True),
        ("PMID:9100011", True),
        ("PMID:9100012", True),
        ("PMID:9100013", True),
        ("PMID:9100009", False),
        ("PMID:9100015", False),
    ]


def test_every_quote_of_a_hand_made_evidence_file_is_found_in_the_fetched_texts(
    stub_atlas: Path, root: Path, world: World
) -> None:
    texts(stub_atlas, root)
    run = RunPaths(root, "TBX5", "r1")
    run.evidence.mkdir()
    (run.evidence / "batch.yaml").write_text(
        "CHDA:LIT:0000001:\n"
        "  - {field: design, quote: We read forty records, where: Methods}\n"
        "  - {field: table, quote: Gene Change GENEA c.1A>G, where: Table 1}\n"
        "  - {field: workbook, quote: Gene Count GENEA 7, where: T1.xlsx}\n"
        "  - {field: note, quote: Plain notes line, where: notes.txt}\n"
        "CHDA:LIT:0000002:\n"
        "  - {field: abstract, quote: Abstract of 9100003, where: abstract}\n"
        "  - {field: pdf, quote: term10 term11 term12, where: p1}\n"
        "CHDA:LIT:0000003:\n"
        "  - {field: abstract, quote: Abstract of 9100004, where: abstract}\n"
        "  - {field: publisher workbook, quote: Gene Count GENEA 7, where: MOESM1}\n"
        "  - {field: invented, quote: a sentence no text holds, where: nowhere}\n"
    )

    checks = check_quotes(run)

    assert [(c.finding, c.field, c.checked, c.found) for c in checks] == [
        ("CHDA:LIT:0000001", "design", True, True),
        ("CHDA:LIT:0000001", "table", True, True),
        ("CHDA:LIT:0000001", "workbook", True, True),
        ("CHDA:LIT:0000001", "note", True, True),
        ("CHDA:LIT:0000002", "abstract", True, True),
        ("CHDA:LIT:0000002", "pdf", True, True),
        ("CHDA:LIT:0000003", "abstract", True, True),
        ("CHDA:LIT:0000003", "publisher workbook", True, True),
        ("CHDA:LIT:0000003", "invented", True, False),
    ]


def test_pubmed_failing_writes_nothing_and_exits_one(
    stub_atlas: Path, root: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(url: str) -> bytes:
        raise pubmed.PubMedError("PubMed answered HTTP 500 Server Error")

    monkeypatch.setattr(cli, "_fetch", broken)

    result = texts(stub_atlas, root)

    assert result.exit_code == 1
    assert "PubMed answered HTTP 500" in result.stdout
    assert world.web.urls == []
    assert not (root / "local/literature/texts").exists()
    assert not (root / "local/literature/runs").exists()


def test_a_pmid_pubmed_does_not_know_is_a_failure_not_an_empty_paper(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    papers = pubmed_papers()
    del papers["9100004"]
    world = World(monkeypatch, papers, routes())

    result = texts(stub_atlas, root)

    assert result.exit_code == 1
    assert "no record for PMID:9100004" in result.stdout
    assert world.web.urls == []
    assert not (root / "local/literature/runs").exists()


GONE = "local/literature/texts/9100001/gone.txt"


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        (lambda d: d.pop("licence"), "lacks the key(s) licence"),
        (lambda d: d.update(extra=1), "has the unexpected key(s) extra"),
        (lambda d: d.update(source="scan"), "key 'source' is 'scan', not one of full_text"),
        (
            lambda d: d["attempts"].append({"via": "x"}),
            "key 'attempts' entry number 7 is {'via': 'x'}, not an object",
        ),
        (lambda d: d.update(rejected="0"), "key 'rejected' is '0', not a count"),
        (lambda d: d.update(rejected=True), "key 'rejected' is True, not a count"),
        (lambda d: d["supplements"].append(GONE), "key 'supplements' needs "),
        (lambda d: d.update(year="19x9"), "key 'year' is '19x9', not a year in digits"),
        (lambda d: d.update(year=""), "key 'year' is '', not a year in digits"),
        (lambda d: d.update(format=True), "key 'format' is True, not the integer 1"),
        (lambda d: d.update(format=2), "key 'format' is 2, not the integer 1"),
        (lambda d: d.update(pmid="PMID:1"), "key 'pmid' is 'PMID:1', not 'PMID:9100001'"),
        (lambda d: d.update(supplied_sha256="ab"), "key 'supplied_sha256' is 'ab', not a sha256"),
        (
            lambda d: d.update(via="supplied"),
            "key 'via' is 'supplied', not one of europepmc, pmc, openalex for source 'full_text'",
        ),
        (
            lambda d: d.update(via=None),
            "key 'via' is None, not one of europepmc, pmc, openalex for source 'full_text'",
        ),
        (
            lambda d: d.update(source="abstract"),
            "key 'via' is 'europepmc', not null for source 'abstract'",
        ),
    ],
    ids=[
        "missing key",
        "extra key",
        "bad source",
        "bad attempt",
        "bad type",
        "bool count",
        "file gone",
        "year not digits",
        "year empty",
        "bool format",
        "other format",
        "other pmid",
        "bad sha256",
        "via of another source",
        "full text without via",
        "abstract with via",
    ],
)
def test_an_unusable_fetch_record_is_refused_naming_the_file_the_key_and_refresh(
    stub_atlas: Path, root: Path, world: World, damage: Any, said: str
) -> None:
    texts(stub_atlas, root, "r1")
    path = root / T / "9100001/fetch.json"
    data = json.loads(path.read_text("utf-8"))
    damage(data)
    path.write_text(json.dumps(data))
    asked = world.requests

    result = texts(stub_atlas, root, "r2")

    assert result.exit_code == 1
    message = " ".join(result.stdout.split())
    assert message.startswith(f"error: {path}: {said}"), message
    assert message.endswith("; run again with --refresh to fetch this paper anew")
    assert world.requests == asked
    assert not (root / "local/literature/runs/TBX5/r2").exists()
    path.write_text("not json")
    assert texts(stub_atlas, root, "r2").exit_code == 1


def test_the_argument_errors_exit_two_and_ask_nobody(
    stub_atlas: Path, root: Path, world: World
) -> None:
    assert texts(stub_atlas, root, "r1", symbol="NOPE").exit_code == 2
    assert texts(stub_atlas, root, "../x").exit_code == 2
    assert texts(stub_atlas, root, "..").exit_code == 2
    (root / "curation/literature/TBX5.yaml").unlink()
    missing = texts(stub_atlas, root)
    assert missing.exit_code == 2 and "TBX5.yaml does not exist" in missing.stdout
    (root / "curation/literature/TBX5.yaml").write_text(
        "gene: HGNC:11604\ntriage:\n  - {pmid: 5}\n"
    )
    assert texts(stub_atlas, root).exit_code == 2
    assert world.requests == 0
    assert not (root / "local/literature/runs").exists()


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
LONG = "a" * 300


def one_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pmid: str, triage: str | None = None
) -> tuple[Path, World]:
    """A root whose triage names only `pmid`, over the standard routes."""
    (tmp_path / "curation/literature").mkdir(parents=True)
    (tmp_path / "curation/literature/TBX5.yaml").write_text(
        f"gene: HGNC:11604\ntriage:\n  - {{pmid: 'PMID:{pmid}', search: 1, decision: included}}\n"
        "findings: []\n"
    )
    return tmp_path, World(monkeypatch, pubmed_papers(), routes())


def supplement_files(root: Path, pmid: str) -> list[str]:
    base = root / T / pmid
    return sorted(
        f.relative_to(base).as_posix()
        for d in ("supplements", "raw/supplements")
        for f in (base / d).rglob("*")
        if f.is_file()
    )


def test_a_login_page_served_for_a_publisher_workbook_is_rejected_not_kept(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    world.web.routes[WORKBOOK_URL] = ok("u", LOGIN, "text/html")

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    paper = by_pmid(root)["PMID:9100004"]
    name = "41588_2099_Article_MOESM1_ESM.xlsx"
    assert paper["supplements"] == []
    assert (
        attempt("publisher", WORKBOOK_URL, f"rejected: an HTML page served for {name}")
        in paper["attempts"]
    )
    assert supplement_files(root, "9100004") == []
    assert "0 supplement files converted, 2 rejected" in result.stdout


def test_an_html_member_of_a_supplement_zip_is_rejected_but_an_html_named_one_is_kept(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    zipped = _zip([("login.xlsx", LOGIN), ("page.html", LOGIN)])
    world.web.routes[f"{EPMC_BASE}PMC9100001/supplementaryFiles"] = ok("u", zipped)

    texts(stub_atlas, root)

    paper = by_pmid(root)["PMID:9100001"]
    assert paper["supplements"] == [f"{T}/9100001/supplements/page.html.txt"]
    assert [a["outcome"] for a in paper["attempts"][3:]] == [
        "rejected: an HTML page served for login.xlsx",
        "converted, 13 words",
    ]


def test_a_zip_whose_every_member_is_not_text_writes_nothing_and_rejects_each_member(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    zipped = _zip([("a.png", PNG), ("b.jpg", JPEG)])
    world.web.routes[f"{EPMC_BASE}PMC9100001/supplementaryFiles"] = ok("u", zipped)

    result = texts(stub_atlas, root)

    paper = by_pmid(root)["PMID:9100001"]
    assert paper["supplements"] == []
    assert [a["outcome"] for a in paper["attempts"][2:]] == [
        "zip files: 2",
        "rejected: a.png: a PNG image, not text",
        "rejected: b.jpg: a JPEG image, not text",
    ]
    assert supplement_files(root, "9100001") == []
    assert "0 supplement files converted, 2 rejected" in result.stdout


def test_a_refresh_that_raises_leaves_the_papers_old_directory_as_it_was(
    stub_atlas: Path, root: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    texts(stub_atlas, root, "r1")
    base = root / T / "9100002"
    before = {f.name: f.read_bytes() for f in base.rglob("*") if f.is_file()}

    def down(url: str) -> Response:
        if "9100002" in url:
            raise OSError("disk went away")
        return world.web(url)

    monkeypatch.setattr(cli, "_get", down)

    result = texts(stub_atlas, root, "r2", "--refresh")

    assert result.exit_code == 1
    assert {f.name: f.read_bytes() for f in base.rglob("*") if f.is_file()} == before
    assert not (root / T / "9100002.tmp").exists()
    assert not (root / "local/literature/runs/TBX5/r2").exists()


def test_a_very_long_supplement_name_is_cut_keeping_its_extension(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    url = f"https://pub.example.com/files/{LONG}_Supplement.xlsx"
    page = f'<a href="{url}">Supplementary Table</a>'.encode()
    world.web.routes["https://doi.org/10.1000/t9100004"] = Response(
        200, PUBLISHER_URL, "text/html", page
    )
    world.web.routes[url] = ok("u", _xlsx())

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    (kept,) = by_pmid(root)["PMID:9100004"]["supplements"]
    name = Path(kept).name
    assert name.endswith(".xlsx.txt") and name.startswith("a" * 90) and len(name) <= 104
    assert (root / kept).is_file()


def test_a_paper_with_no_doi_makes_no_publisher_attempt(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    plain = world.pubmed

    def without_doi(url: str) -> bytes:
        body = plain(url)
        if "esummary" in url:
            data = json.loads(body)
            for record in data["result"].values():
                if isinstance(record, dict):
                    record["articleids"] = []
            body = json.dumps(data).encode()
        return body

    monkeypatch.setattr(cli, "_fetch", without_doi)

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    paper = by_pmid(root)["PMID:9100004"]
    assert paper["doi"] == "" and paper["needed"] is True
    assert [a["via"] for a in paper["attempts"]] == ["europepmc", "openalex", "openalex"]
    assert not any("doi.org" in u for u in world.web.urls)
    cached = json.loads((root / T / "9100004/fetch.json").read_text("utf-8"))
    assert cached["doi"] is None


def test_a_supplied_pdf_that_does_not_convert_is_an_attempt_and_the_next_source_is_tried(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = one_paper(tmp_path, monkeypatch, "9100003")
    supplied = root / "local/literature/supplied"
    supplied.mkdir(parents=True)
    (supplied / "9100003.pdf").write_bytes(b"%PDF-1.4 not a pdf")

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    paper = by_pmid(root)["PMID:9100003"]
    assert paper["attempts"][0]["via"] == "supplied"
    assert paper["attempts"][0]["url"] == "local/literature/supplied/9100003.pdf"
    assert paper["attempts"][0]["outcome"].startswith("rejected: a PDF that pypdf cannot read")
    assert paper["source"] == "full_text"
    assert (
        attempt("openalex", "https://repo.example.org/files/3.pdf", "accepted, 1000 words")
        in (paper["attempts"])
    )


def refreshed(
    stub_atlas: Path, root: Path, world: World, search_answer: Response, publisher: Response
) -> tuple[Any, dict[str, bytes]]:
    """Run r1, then refresh as r2 with Europe PMC answering `search_answer` for 9100001."""
    texts(stub_atlas, root, "r1")
    base = root / T / "9100001"
    before = {f.name: f.read_bytes() for f in base.rglob("*") if f.is_file()}
    world.web.routes[search_url("9100001")] = search_answer
    world.web.routes[f"{OPENALEX_BASE}pmid:9100001"] = not_found("u")
    world.web.routes["https://doi.org/10.1000/t9100001"] = publisher
    return texts(stub_atlas, root, "r2", "--refresh"), before


def test_a_refresh_while_europe_pmc_is_down_keeps_the_earlier_full_text(
    stub_atlas: Path, root: Path, world: World
) -> None:
    result, before = refreshed(
        stub_atlas,
        root,
        world,
        Response(500, search_url("9100001"), "text/html", b""),
        Response(404, "https://doi.org/10.1000/t9100001", "text/html", b""),
    )

    assert result.exit_code == 0, result.stdout
    after = {f.name: f.read_bytes() for f in (root / T / "9100001").rglob("*") if f.is_file()}
    assert after == before and b"An open study" in after["fulltext.txt"]
    assert not (root / T / "9100001.tmp").exists()
    lines = result.stdout.strip().splitlines()
    assert lines[0] == (
        "PMID:9100001: kept the earlier texts, the new fetch has no full text and no T1.xlsx, "
        "notes.txt (europepmc failed: www.ebi.ac.uk answered HTTP 500 for a search; publisher "
        "failed: doi.org answered HTTP 404 for a landing page)"
    )
    assert len(lines) == 2 and lines[1].startswith("5 papers: 4 full text")
    paper = by_pmid(root, "r2")["PMID:9100001"]
    assert paper["source"] == "full_text" and paper["needed"] is False
    assert paper == by_pmid(root, "r1")["PMID:9100001"]


def test_a_refresh_that_finds_no_copy_replaces_the_earlier_full_text(
    stub_atlas: Path, root: Path, world: World
) -> None:
    result, _ = refreshed(
        stub_atlas,
        root,
        world,
        ok(search_url("9100001"), b'{"resultList": {"result": []}}'),
        Response(200, PUBLISHER_URL, "text/html", b"<html><body>No supplements.</body></html>"),
    )

    assert result.exit_code == 0, result.stdout
    assert "kept the earlier" not in result.stdout
    assert not (root / T / "9100001/fulltext.txt").exists()
    paper = by_pmid(root, "r2")["PMID:9100001"]
    assert paper["source"] == "abstract" and paper["needed"] is True
    assert [a["outcome"] for a in paper["attempts"]] == [
        "no record",
        "free copies: 0",
        "supplement links: 0",
    ]


# --- Stage B fix wave -----------------------------------------------------------------------


def server_error(url: str) -> Response:
    return Response(500, url, "text/html", b"")


def openalex_work(*locations: dict[str, Any]) -> Response:
    work = {"best_oa_location": locations[0] if locations else None, "locations": locations}
    return ok("u", json.dumps(work).encode())


def files_under(root: Path, pmid: str) -> dict[str, bytes]:
    base = root / T / pmid
    return {f.relative_to(base).as_posix(): f.read_bytes() for f in base.rglob("*") if f.is_file()}


def test_openalex_is_asked_when_europe_pmc_states_a_licence_but_its_copies_fail(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: Europe PMC's licence used to stand in for "a full text was found", so OpenAlex was
    never asked. The licence written is that of the copy kept."""
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    fulltext = f"{EPMC_BASE}PMC9100001/fullTextXML"
    world.web.routes[fulltext] = server_error(fulltext)
    world.web.routes[efetch_url("PMC9100001")] = server_error(efetch_url("PMC9100001"))
    pdf = {"is_oa": True, "pdf_url": "https://repo.example.org/files/1.pdf", "license": "cc-by"}
    world.web.routes[f"{OPENALEX_BASE}pmid:9100001"] = openalex_work(pdf)
    world.web.routes["https://repo.example.org/files/1.pdf"] = ok("u", OPENALEX_PDF)

    assert texts(stub_atlas, root).exit_code == 0
    paper = by_pmid(root)["PMID:9100001"]
    assert (paper["source"], paper["licence"]) == ("full_text", "cc-by")
    assert (
        attempt("openalex", "https://repo.example.org/files/1.pdf", "accepted, 1000 words")
        in (paper["attempts"])
    )


def test_with_no_full_text_the_licence_is_europe_pmcs(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    fulltext = f"{EPMC_BASE}PMC9100001/fullTextXML"
    world.web.routes[fulltext] = not_found(fulltext)
    world.web.routes[efetch_url("PMC9100001")] = not_found("u")
    world.web.routes[f"{OPENALEX_BASE}pmid:9100001"] = not_found("u")

    assert texts(stub_atlas, root).exit_code == 0
    paper = by_pmid(root)["PMID:9100001"]
    assert (paper["source"], paper["licence"]) == ("abstract", "cc by")


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        (_pdf([_words(900)]), "accepted, 900 words"),
        (_pdf(["A short letter."]), "accepted, 3 words"),
        (_pdf([""]), "rejected: a PDF with no text (0 words), scanned pages"),
        (LOGIN, "rejected: a html file, not a PDF"),
    ],
)
def test_a_supplied_pdf_is_taken_at_any_length_as_the_curators_word(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes, outcome: str
) -> None:
    """F2 (R-P2-38): the verdict judges only copies the tool found. A supplied file is still
    refused when it is not a PDF, or a PDF with no text to read."""
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    world.web.routes[search_url("9100004")] = search("9100004", licence="cc by")
    supplied = root / "local/literature/supplied"
    supplied.mkdir(parents=True)
    (supplied / "9100004.pdf").write_bytes(body)

    assert texts(stub_atlas, root).exit_code == 0
    paper = by_pmid(root)["PMID:9100004"]
    assert paper["attempts"][0] == attempt(
        "supplied", "local/literature/supplied/9100004.pdf", outcome
    )
    accepted = outcome.startswith("accepted")
    assert paper["source"] == ("supplied_pdf" if accepted else "abstract")
    # a supplied PDF states no licence; with no full text the licence is Europe PMC's
    assert paper["licence"] == (None if accepted else "cc by")


def test_a_cached_paper_is_fetched_again_when_its_supplied_pdf_appears_changes_or_goes(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 (R-P2-39): fetch.json holds the supplied PDF's sha256; the cache is used only while
    it matches. A text from a supplied PDF since removed is not kept, though the refetch has
    no full text and Europe PMC failed (R-P2-50: the gone PDF lifts the full-text keep)."""
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    pdf = root / "local/literature/supplied/9100004.pdf"
    record = root / T / "9100004/fetch.json"

    texts(stub_atlas, root, "r1")
    assert json.loads(record.read_text("utf-8"))["supplied_sha256"] is None
    asked = world.requests
    texts(stub_atlas, root, "r2")
    assert world.requests == asked  # nothing changed, nothing asked

    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(_pdf(["first copy of the letter"]))
    texts(stub_atlas, root, "r3")
    assert world.requests > asked
    assert by_pmid(root, "r3")["PMID:9100004"]["source"] == "supplied_pdf"
    assert json.loads(record.read_text("utf-8"))["supplied_sha256"] == (
        hashlib.sha256(pdf.read_bytes()).hexdigest()
    )

    pdf.write_bytes(_pdf(["second copy of the letter"]))
    texts(stub_atlas, root, "r4")
    assert (root / T / "9100004/fulltext.txt").read_text("utf-8") == "second copy of the letter"

    pdf.unlink()
    world.web.routes[search_url("9100004")] = server_error(search_url("9100004"))
    world.web.routes["https://doi.org/10.1000/t9100004"] = plain_page("9100004")
    result = texts(stub_atlas, root, "r5")
    assert "kept the earlier" not in result.stdout
    paper = by_pmid(root, "r5")["PMID:9100004"]
    assert paper["source"] == "abstract" and not (root / T / "9100004/fulltext.txt").exists()
    assert json.loads(record.read_text("utf-8"))["supplied_sha256"] is None


@pytest.mark.parametrize(
    ("answer", "lost", "publisher_asked"),
    [
        (
            ok("u", _zip([("notes.txt", b"Plain notes line."), ("movie.mp4", b"\x00" * 600)])),
            "movie.mp4: europepmc rejected: over 500 bytes uncompressed",
            False,
        ),
        (
            ok("u", b"PK\x03\x04damaged"),
            "Europe PMC's archive: europepmc rejected: not a readable zip archive",
            True,
        ),
        (
            Response(500, "https://www.ebi.ac.uk/x", "", b""),
            "Europe PMC's archive: europepmc failed: www.ebi.ac.uk answered HTTP 500 for "
            "supplementary files",
            True,
        ),
    ],
    ids=["member over the cap", "damaged archive", "failed request"],
)
def test_every_supplement_loss_is_counted_and_named_for_a_paper_with_a_full_text(
    stub_atlas: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: Response,
    lost: str,
    publisher_asked: bool,
) -> None:
    """F5 (R-P2-41). An archive that opened gave its files, whatever became of them; one that
    failed or did not open gave nothing, so the publisher page is read (R-P2-45)."""
    monkeypatch.setattr(convert_module, "MAX_MEMBER_BYTES", 500)
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    world.web.routes[f"{EPMC_BASE}PMC9100001/supplementaryFiles"] = answer
    world.web.routes["https://doi.org/10.1000/t9100001"] = plain_page("9100001")

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    assert "1 rejected; 0 needed" in result.stdout
    paper = by_pmid(root)["PMID:9100001"]
    assert paper["needed"] is False
    assert paper["why"].startswith(f"supplements not read ({lost}")
    assert ("https://doi.org/10.1000/t9100001" in world.web.urls) is publisher_asked


def test_a_refresh_that_finds_fewer_supplements_because_a_request_failed_keeps_the_earlier(
    stub_atlas: Path, root: Path, world: World
) -> None:
    """F6 (R-P2-42)."""
    texts(stub_atlas, root, "r1")
    before = files_under(root, "9100001")
    zip_url = f"{EPMC_BASE}PMC9100001/supplementaryFiles"
    world.web.routes[zip_url] = Response(503, "https://www.ebi.ac.uk/x", "", b"")
    world.web.routes["https://doi.org/10.1000/t9100001"] = plain_page("9100001")

    result = texts(stub_atlas, root, "r2", "--refresh")

    assert result.exit_code == 0, result.stdout
    assert files_under(root, "9100001") == before
    assert result.stdout.splitlines()[0] == (
        "PMID:9100001: kept the earlier texts, the new fetch has no T1.xlsx, notes.txt "
        "(europepmc failed: www.ebi.ac.uk answered HTTP 503 for supplementary files)"
    )
    assert by_pmid(root, "r2")["PMID:9100001"] == by_pmid(root, "r1")["PMID:9100001"]


def test_a_zip_supplement_is_opened_one_level_and_a_zip_inside_it_is_not(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7 (R-P2-43): a publisher's zip, and a zip member of Europe PMC's archive."""
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    zip_url = "https://pub.example.com/suppl/Supplementary_Data.zip"
    page = f'<a href="{zip_url}">Supplementary data</a>'.encode()
    world.web.routes["https://doi.org/10.1000/t9100004"] = Response(
        200, PUBLISHER_URL, "text/html", page
    )
    inner = _zip([("deep.xlsx", _xlsx())])
    world.web.routes[zip_url] = ok("u", _zip([("TableS1.xlsx", _xlsx()), ("inner.zip", inner)]))

    assert texts(stub_atlas, root).exit_code == 0
    paper = by_pmid(root)["PMID:9100004"]
    assert paper["supplements"] == [
        f"{T}/9100004/supplements/Supplementary_Data.zip__TableS1.xlsx.txt"
    ]
    assert [a["outcome"] for a in paper["attempts"][-4:]] == [
        "supplement links: 1",
        "zip files: 2",
        "converted, 20 words",
        "rejected: a zip inside a zip, not opened",
    ]
    assert paper["attempts"][-1]["url"] == f"{zip_url}#inner.zip"

    root2, world2 = one_paper(tmp_path / "epmc", monkeypatch, "9100001")
    archive = _zip([("S1.zip", _zip([("T.xlsx", _xlsx()), ("in.zip", inner)]))])
    world2.web.routes[f"{EPMC_BASE}PMC9100001/supplementaryFiles"] = ok("u", archive)
    result = texts(stub_atlas, root2)
    assert result.exit_code == 0, result.stdout
    assert by_pmid(root2)["PMID:9100001"]["supplements"] == [
        f"{T}/9100001/supplements/S1.zip__T.xlsx.txt"
    ]
    assert "1 supplement files converted, 1 rejected" in result.stdout


def test_footnote_marks_reach_the_fetched_text(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8: a de novo mark in a table row reaches fulltext.txt with its footnote."""
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    marked = JATS.replace(
        b"<td>c.1A&gt;G</td>", b'<td>c.1A&gt;G<xref ref-type="table-fn" rid="a">a</xref></td>'
    ).replace(
        b"<fn><p>Counted once per family.</p></fn>",
        b'<fn id="a"><label>a</label><p>De novo.</p></fn>',
    )
    world.web.routes[f"{EPMC_BASE}PMC9100001/fullTextXML"] = ok("u", marked)

    texts(stub_atlas, root)

    text = (root / T / "9100001/fulltext.txt").read_text("utf-8").splitlines()
    assert "GENEA | c.1A>G [a]" in text and "[TABLE FOOTNOTE] [a] De novo." in text


def test_an_openalex_copy_paper_gets_its_publisher_supplements(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10 (R-P2-45): no PMC id, so Europe PMC's archive gave nothing; the paper has a full
    text from OpenAlex, and its publisher page is still read for supplement links."""
    root, world = one_paper(tmp_path, monkeypatch, "9100003")
    world.web.routes["https://doi.org/10.1000/t9100003"] = Response(
        200, PUBLISHER_URL, "text/html", PAPER_PAGE
    )

    assert texts(stub_atlas, root).exit_code == 0
    paper = by_pmid(root)["PMID:9100003"]
    assert paper["source"] == "full_text"
    assert paper["supplements"] == [
        f"{T}/9100003/supplements/41588_2099_Article_MOESM1_ESM.xlsx.txt"
    ]


@pytest.mark.parametrize(
    ("name", "written"),
    [
        ("表" * 90 + ".xlsx", "表" * 31 + ".xlsx"),
        ("suppl%00ementary\x07.xlsx", "suppl_ementary_.xlsx"),
    ],
)
def test_a_name_that_cannot_be_a_file_is_made_one_and_the_run_completes(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, written: str
) -> None:
    """F12: 90 CJK characters are 270 UTF-8 bytes, which with `.xlsx.txt` and a temporary
    suffix overran 255; a `%00` in a link became a NUL in the path."""
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    url = f"https://pub.example.com/files/{name}".replace("\x07", "%07")
    page = f'<a href="{url}">Supplementary Table</a>'.encode()
    world.web.routes["https://doi.org/10.1000/t9100004"] = Response(
        200, PUBLISHER_URL, "text/html", page
    )
    world.web.routes[url] = ok("u", _xlsx())

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    (kept,) = by_pmid(root)["PMID:9100004"]["supplements"]
    assert kept == f"{T}/9100004/supplements/{written}.txt"
    assert (root / kept).is_file() and (root / T / "9100004/raw/supplements" / written).is_file()
    assert len(written.encode()) <= 100


@pytest.mark.parametrize(
    "body",
    [
        b"<article><front/><body/></article>",
        b"<article><front/><body><!-- the full text is not available here --></body></article>",
    ],
)
def test_a_jats_record_with_no_body_element_leaves_the_paper_needed(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    """F14 (matrix survivor B16)."""
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    world.web.routes[f"{EPMC_BASE}PMC9100001/fullTextXML"] = ok("u", body)
    world.web.routes[efetch_url("PMC9100001")] = ok("u", body)
    world.web.routes[f"{OPENALEX_BASE}pmid:9100001"] = not_found("u")

    assert texts(stub_atlas, root).exit_code == 0
    paper = by_pmid(root)["PMID:9100001"]
    assert paper["needed"] is True
    reason = "rejected: no body text: the record has no <body>, or its <body> holds no element"
    assert [a["outcome"] for a in paper["attempts"][1:3]] == [reason, reason]


def test_a_publisher_bot_check_is_a_failure_that_keeps_the_earlier_full_text(
    stub_atlas: Path, root: Path, world: World
) -> None:
    """F15: the bot check is this refresh's only failed attempt."""
    result, before = refreshed(
        stub_atlas,
        root,
        world,
        ok(search_url("9100001"), b'{"resultList": {"result": []}}'),
        Response(403, PUBLISHER_URL, "text/html", b"no"),
    )

    assert result.exit_code == 0, result.stdout
    after = {f.name: f.read_bytes() for f in (root / T / "9100001").rglob("*") if f.is_file()}
    assert after == before
    assert result.stdout.splitlines()[0].endswith(
        "(publisher bot check at pub.example.com: HTTP 403)"
    )


def test_a_refresh_whose_only_find_is_a_rejected_copy_replaces_the_earlier_full_text(
    stub_atlas: Path, root: Path, world: World
) -> None:
    """F15: a rejected copy is an answer, not a failure."""
    landing = {"is_oa": True, "landing_page_url": "https://repo.example.org/record/4"}
    texts(stub_atlas, root, "r1")
    world.web.routes[search_url("9100001")] = ok(
        search_url("9100001"), b'{"resultList": {"result": []}}'
    )
    world.web.routes[f"{OPENALEX_BASE}pmid:9100001"] = openalex_work(landing)
    world.web.routes["https://doi.org/10.1000/t9100001"] = plain_page("9100001")

    result = texts(stub_atlas, root, "r2", "--refresh")

    assert result.exit_code == 0, result.stdout
    assert "kept the earlier" not in result.stdout
    paper = by_pmid(root, "r2")["PMID:9100001"]
    assert paper["source"] == "abstract" and paper["supplements"] == []
    assert paper["attempts"][2]["outcome"].startswith("rejected: a landing page")


def test_an_openalex_copy_that_fails_is_an_attempt_and_the_next_copy_is_tried(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F15: a copy answering non-200, and one whose host cannot be reached."""
    root, world = one_paper(tmp_path, monkeypatch, "9100003")
    copies = [
        {"is_oa": True, "pdf_url": "https://a.example.org/1.pdf"},
        {"is_oa": True, "pdf_url": "https://b.example.org/1.pdf"},
        {"is_oa": True, "pdf_url": "https://c.example.org/1.pdf", "license": "cc0"},
    ]
    world.web.routes[f"{OPENALEX_BASE}pmid:9100003"] = openalex_work(*copies)
    world.web.routes["https://a.example.org/1.pdf"] = not_found("https://a.example.org/1.pdf")
    world.web.routes["https://b.example.org/1.pdf"] = SourceError(
        "could not reach b.example.org: TimeoutError"
    )
    world.web.routes["https://c.example.org/1.pdf"] = ok("u", OPENALEX_PDF)

    assert texts(stub_atlas, root).exit_code == 0
    paper = by_pmid(root)["PMID:9100003"]
    assert paper["attempts"][1:5] == [
        attempt("openalex", f"{OPENALEX_BASE}pmid:9100003", "free copies: 3"),
        attempt("openalex", "https://a.example.org/1.pdf", "failed: HTTP 404"),
        attempt(
            "openalex",
            "https://b.example.org/1.pdf",
            "failed: could not reach b.example.org: TimeoutError",
        ),
        attempt("openalex", "https://c.example.org/1.pdf", "accepted, 1000 words"),
    ]
    assert (paper["source"], paper["licence"]) == ("full_text", "cc0")


def test_a_publisher_download_that_cannot_be_reached_is_a_lost_supplement(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F15."""
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    world.web.routes[WORKBOOK_URL] = SourceError("could not reach pub.example.com: TimeoutError")

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    paper = by_pmid(root)["PMID:9100004"]
    lost = attempt(
        "publisher", WORKBOOK_URL, "failed: could not reach pub.example.com: TimeoutError"
    )
    assert lost in paper["attempts"] and paper["supplements"] == []
    assert "0 supplement files converted, 2 rejected" in result.stdout
    assert paper["why"].endswith(
        "supplements not read (41588_2099_Article_MOESM1_ESM.xlsx: publisher failed: could not "
        "reach pub.example.com: TimeoutError; appendix.pdf: publisher failed: HTTP 404)"
    )


def test_an_attempt_that_sent_no_request_has_no_url(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F18: efetch refuses an id that is not a PMC id before sending anything; the attempt
    used to carry the URL of Europe PMC's search, the request before it."""
    root, world = one_paper(tmp_path, monkeypatch, "9100002")
    world.web.routes[search_url("9100002")] = search("9100002", "NIHMS9100002")
    world.web.routes[f"{OPENALEX_BASE}pmid:9100002"] = not_found("u")
    world.web.routes[f"{EPMC_BASE}NIHMS9100002/supplementaryFiles"] = not_found("u")

    assert texts(stub_atlas, root).exit_code == 0
    paper = by_pmid(root)["PMID:9100002"]
    assert paper["attempts"][1] == attempt("pmc", "", "failed: not a PMC id: 'NIHMS9100002'")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_supplied_pdf_that_cannot_be_read_stops_the_run_naming_it(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    pdf = root / "local/literature/supplied/9100004.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(SUPPLIED)
    pdf.chmod(0)
    try:
        result = texts(stub_atlas, root)
    finally:
        pdf.chmod(0o644)

    assert result.exit_code == 1
    assert " ".join(result.stdout.split()).startswith(f"error: {pdf}: cannot be read: ")
    assert not (root / "local/literature/runs").exists()


# --- Stage B residual fix -------------------------------------------------------------------


def test_a_copy_declaring_a_codec_that_is_not_text_is_rejected_and_the_run_completes(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: charset=hex raised LookupError out of convert and killed the run."""
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    page = b'<!DOCTYPE html><html><head><meta charset="hex"></head><body><p>a b c</p></body></html>'
    world.web.routes["https://repo.example.org/record/4"] = ok("u", page, "text/html")

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    paper = by_pmid(root)["PMID:9100004"]
    assert paper["needed"] is True
    assert (
        attempt(
            "openalex",
            "https://repo.example.org/record/4",
            "rejected: HTML that declares the charset 'hex', which is not a text encoding",
        )
        in paper["attempts"]
    )


def test_an_office_package_in_the_archive_is_one_supplement_not_read(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-52: a pptx opened as an archive gave one loss per part."""
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    pptx = _zip(
        [("[Content_Types].xml", b"<Types/>")] + [(f"ppt/s{i}.xml", b"<s/>") for i in range(12)]
    )
    ods = _zip(
        [("mimetype", b"application/vnd.oasis.opendocument.spreadsheet"), ("content.xml", b"<c/>")]
    )
    archive = _zip([("Figure S1.pptx", pptx), ("Table S2.ods", ods)])
    world.web.routes[f"{EPMC_BASE}PMC9100001/supplementaryFiles"] = ok("u", archive)

    result = texts(stub_atlas, root)

    assert "0 supplement files converted, 2 rejected" in result.stdout
    assert by_pmid(root)["PMID:9100001"]["why"] == (
        "supplements not read (Figure S1.pptx: europepmc rejected: Figure S1.pptx: a package of "
        "type pptx, not read; Table S2.ods: europepmc rejected: Table S2.ods: a package of type "
        "ods, not read)"
    )


def links_page(*names: str) -> bytes:
    links = "".join(f'<a href="/s/{n}">Supplementary {n}</a>' for n in names)
    return f"<html><body>{links}</body></html>".encode()


def openalex_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *names: str
) -> tuple[Path, World]:
    """9100003 (no PMC id): an OpenAlex PDF and a publisher page linking `names`."""
    root, world = one_paper(tmp_path, monkeypatch, "9100003")
    world.web.routes["https://doi.org/10.1000/t9100003"] = Response(
        200, PUBLISHER_URL, "text/html", links_page(*names)
    )
    world.web.routes["https://pub.example.com/s/"] = ok("u", _xlsx())
    return root, world


def kept_names(root: Path, run: str) -> list[str]:
    return [Path(s).name for s in by_pmid(root, run)["PMID:9100003"]["supplements"]]


def test_a_full_text_failure_does_not_keep_a_supplement_the_page_dropped(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-50 (q8): a dead OpenAlex link failed, but the full text was found; the supplement
    step answered with one link fewer and nothing failed in it, so the new fetch replaces."""
    root, world = openalex_paper(tmp_path, monkeypatch, "S1.xlsx", "S2.xlsx")
    texts(stub_atlas, root, "r1")
    dead = {"is_oa": True, "pdf_url": "https://dead.example.org/3.pdf"}
    live = {"is_oa": True, "pdf_url": "https://repo.example.org/files/3.pdf", "license": "cc-by"}
    world.web.routes[f"{OPENALEX_BASE}pmid:9100003"] = openalex_work(dead, live)
    world.web.routes["https://dead.example.org/3.pdf"] = not_found("https://dead.example.org/3.pdf")
    world.web.routes["https://doi.org/10.1000/t9100003"] = Response(
        200, PUBLISHER_URL, "text/html", links_page("S1.xlsx")
    )

    result = texts(stub_atlas, root, "r2", "--refresh")

    assert "kept the earlier" not in result.stdout
    assert kept_names(root, "r1") == ["S1.xlsx.txt", "S2.xlsx.txt"]
    assert kept_names(root, "r2") == ["S1.xlsx.txt"]


def test_a_supplement_lost_to_a_failure_keeps_the_earlier_texts_though_the_count_holds(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-50 (q9): S1's download fails and S3 is new; compared by count, two files against
    two hid the loss of S1."""
    root, world = openalex_paper(tmp_path, monkeypatch, "S1.xlsx", "S2.xlsx")
    texts(stub_atlas, root, "r1")
    world.web.routes["https://doi.org/10.1000/t9100003"] = Response(
        200, PUBLISHER_URL, "text/html", links_page("S1.xlsx", "S2.xlsx", "S3.xlsx")
    )
    world.web.routes["https://pub.example.com/s/S1.xlsx"] = server_error("u")

    result = texts(stub_atlas, root, "r2", "--refresh")

    assert result.stdout.splitlines()[0] == (
        "PMID:9100003: kept the earlier texts, the new fetch has no S1.xlsx (publisher failed: "
        "HTTP 500)"
    )
    assert kept_names(root, "r2") == ["S1.xlsx.txt", "S2.xlsx.txt"]


def test_a_gone_supplied_pdf_lifts_only_the_full_text_keep(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-50 (N5): the supplied PDF is removed while Europe PMC's archive fails; the
    earlier supplements are kept (and the earlier directory with them)."""
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    pdf = root / "local/literature/supplied/9100001.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(SUPPLIED)
    texts(stub_atlas, root, "r1")
    assert by_pmid(root, "r1")["PMID:9100001"]["source"] == "supplied_pdf"
    pdf.unlink()
    zip_url = f"{EPMC_BASE}PMC9100001/supplementaryFiles"
    world.web.routes[zip_url] = Response(503, "https://www.ebi.ac.uk/x", "", b"")
    world.web.routes["https://doi.org/10.1000/t9100001"] = plain_page("9100001")

    result = texts(stub_atlas, root, "r2")

    assert result.stdout.splitlines()[0].startswith(
        "PMID:9100001: kept the earlier texts, the new fetch has no T1.xlsx, notes.txt (europepmc "
        "failed: www.ebi.ac.uk answered HTTP 503 for supplementary files)"
    )
    assert (
        by_pmid(root, "r2")["PMID:9100001"]["supplements"]
        == (by_pmid(root, "r1")["PMID:9100001"]["supplements"])
    )


def test_a_link_or_a_copy_whose_url_cannot_be_read_is_recorded_and_the_run_completes(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 6: an open IPv6 bracket raised ValueError and killed the run."""
    root, world = one_paper(tmp_path, monkeypatch, "9100003")
    bad = "http://[x/supplementary.xlsx"
    copies = [
        {"is_oa": True, "pdf_url": "http://[x/3.pdf"},
        {"is_oa": True, "pdf_url": "https://repo.example.org/files/3.pdf"},
    ]
    world.web.routes[f"{OPENALEX_BASE}pmid:9100003"] = openalex_work(*copies)
    page = f'<a href="{bad}">Supplementary data</a>'.encode()
    world.web.routes["https://doi.org/10.1000/t9100003"] = Response(
        200, PUBLISHER_URL, "text/html", page
    )

    result = texts(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    paper = by_pmid(root)["PMID:9100003"]
    assert (
        attempt("openalex", "http://[x/3.pdf", "failed: a URL that cannot be read")
        in (paper["attempts"])
    )
    assert paper["source"] == "full_text"
    assert paper["why"] == (
        "supplements not read (supplementary.xlsx: publisher rejected: a link whose URL cannot "
        "be read)"
    )
    assert not any(u.startswith("http://[") for u in world.web.urls)


def test_a_supplement_failure_does_not_keep_a_full_text_no_source_still_offers(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-50: the full text is gone because every full-text source answered "none"; the only
    failure is the publisher page, a supplement step, and the paper had no supplements."""
    root, world = one_paper(tmp_path, monkeypatch, "9100002")
    texts(stub_atlas, root, "r1")
    assert by_pmid(root, "r1")["PMID:9100002"]["source"] == "full_text"
    world.web.routes[search_url("9100002")] = ok(
        search_url("9100002"), b'{"resultList": {"result": []}}'
    )
    world.web.routes[f"{OPENALEX_BASE}pmid:9100002"] = not_found("u")
    world.web.routes["https://doi.org/10.1000/t9100002"] = Response(
        403, PUBLISHER_URL, "text/html", b"no"
    )

    result = texts(stub_atlas, root, "r2", "--refresh")

    assert "kept the earlier" not in result.stdout
    assert by_pmid(root, "r2")["PMID:9100002"]["source"] == "abstract"


# --- R-P2-54: a keep never discards a gain --------------------------------------------------


def test_a_supplied_pdf_added_while_the_publisher_bot_checks_is_read_and_s1_is_named(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-54: the supplement keep held the earlier abstract-only directory on every run, so
    the curator's PDF was never read and no line said so."""
    root, world = openalex_paper(tmp_path, monkeypatch, "S1.xlsx")
    world.web.routes[f"{OPENALEX_BASE}pmid:9100003"] = not_found("u")
    texts(stub_atlas, root, "r1")
    assert by_pmid(root, "r1")["PMID:9100003"]["needed"] is True
    pdf = root / "local/literature/supplied/9100003.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(SUPPLIED)
    world.web.routes["https://doi.org/10.1000/t9100003"] = Response(
        403, PUBLISHER_URL, "text/html", b"no"
    )

    result = texts(stub_atlas, root, "r2")

    assert result.stdout.splitlines()[0] == (
        "PMID:9100003: replaced the earlier texts, the new fetch has a full text where there was "
        "none and a new or changed supplied PDF; not carried over: S1.xlsx (publisher bot check "
        "at pub.example.com: HTTP 403)"
    )
    paper = by_pmid(root, "r2")["PMID:9100003"]
    assert (paper["source"], paper["supplements"]) == ("supplied_pdf", [])
    assert paper["why"] == (
        "supplements not read (the publisher page: publisher bot check at pub.example.com: "
        "HTTP 403; S1.xlsx: publisher not carried over: the new fetch replaced the earlier one "
        "and did not read this file)"
    )
    assert "1 rejected" not in result.stdout and "2 rejected" in result.stdout
    asked = world.requests
    texts(stub_atlas, root, "r3")
    assert world.requests == asked  # the new record is the cache now


def test_a_new_open_access_full_text_replaces_the_earlier_while_the_archive_fails(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-54: the earlier fetch had no full text but the archive's files; now Europe PMC
    serves the paper open access and the archive answers 503."""
    root, world = one_paper(tmp_path, monkeypatch, "9100001")
    fulltext = f"{EPMC_BASE}PMC9100001/fullTextXML"
    world.web.routes[search_url("9100001")] = search("9100001", "PMC9100001")
    world.web.routes[efetch_url("PMC9100001")] = not_found("u")
    world.web.routes[f"{OPENALEX_BASE}pmid:9100001"] = not_found("u")
    texts(stub_atlas, root, "r1")
    assert by_pmid(root, "r1")["PMID:9100001"]["needed"] is True
    world.web.routes[search_url("9100001")] = search("9100001", "PMC9100001", True, "cc by")
    world.web.routes[fulltext] = ok("u", jats("Now open"), "text/xml")
    zip_url = f"{EPMC_BASE}PMC9100001/supplementaryFiles"
    world.web.routes[zip_url] = Response(503, "https://www.ebi.ac.uk/x", "", b"")
    world.web.routes["https://doi.org/10.1000/t9100001"] = plain_page("9100001")

    result = texts(stub_atlas, root, "r2", "--refresh")

    assert result.stdout.splitlines()[0] == (
        "PMID:9100001: replaced the earlier texts, the new fetch has a full text where there was "
        "none; not carried over: T1.xlsx, notes.txt (europepmc failed: www.ebi.ac.uk answered "
        "HTTP 503 for supplementary files)"
    )
    paper = by_pmid(root, "r2")["PMID:9100001"]
    assert paper["source"] == "full_text" and paper["supplements"] == []
    assert b"Now open" in (root / T / "9100001/fulltext.txt").read_bytes()
    assert "T1.xlsx: europepmc not carried over" in paper["why"]
    assert "notes.txt: europepmc not carried over" in paper["why"]


def test_a_supplement_link_served_from_doi_org_is_named_by_its_file_not_as_the_page(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publisher page is the first publisher attempt, whatever its URL; a link on doi.org
    was labelled "the publisher page" by its URL prefix."""
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    link = "https://doi.org/10.1000/t9100004/suppl/S1.xlsx"
    world.web.routes["https://doi.org/10.1000/t9100004"] = Response(
        200, PUBLISHER_URL, "text/html", f'<a href="{link}">Supplementary S1</a>'.encode()
    )
    world.web.routes[link] = server_error(link)

    texts(stub_atlas, root)

    assert by_pmid(root)["PMID:9100004"]["why"].endswith(
        "supplements not read (S1.xlsx: publisher failed: HTTP 500)"
    )


def test_an_earlier_file_the_new_fetch_tried_again_is_named_once(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-54: S1's download failed in the replacing fetch, so S1 is named by that failure and
    not again as not carried over."""
    root, world = openalex_paper(tmp_path, monkeypatch, "S1.xlsx")
    world.web.routes[f"{OPENALEX_BASE}pmid:9100003"] = not_found("u")
    texts(stub_atlas, root, "r1")
    pdf = root / "local/literature/supplied/9100003.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(SUPPLIED)
    world.web.routes["https://pub.example.com/s/S1.xlsx"] = server_error("u")

    result = texts(stub_atlas, root, "r2")

    assert "not carried over: S1.xlsx (publisher failed: HTTP 500)" in result.stdout
    assert by_pmid(root, "r2")["PMID:9100003"]["why"] == (
        "supplements not read (S1.xlsx: publisher failed: HTTP 500)"
    )


def test_a_file_not_carried_over_is_named_by_its_link_when_the_page_was_not_asked(
    stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-54: the replacing fetch has no DOI, so it asks no publisher page; the earlier
    publisher workbook's record is the paper's only publisher attempt and is not the page."""
    root, world = one_paper(tmp_path, monkeypatch, "9100004")
    texts(stub_atlas, root, "r1")
    plain = world.pubmed

    def without_doi(url: str) -> bytes:
        body = plain(url)
        if "esummary" in url:
            data = json.loads(body)
            for record in data["result"].values():
                if isinstance(record, dict):
                    record["articleids"] = []
            body = json.dumps(data).encode()
        return body

    monkeypatch.setattr(cli, "_fetch", without_doi)
    world.web.routes[search_url("9100004")] = search("9100004", "PMC9100004", True)
    world.web.routes[f"{EPMC_BASE}PMC9100004/fullTextXML"] = ok("u", JATS, "text/xml")
    world.web.routes[f"{EPMC_BASE}PMC9100004/supplementaryFiles"] = server_error("u")

    texts(stub_atlas, root, "r2", "--refresh")

    paper = by_pmid(root, "r2")["PMID:9100004"]
    assert paper["source"] == "full_text"
    assert paper["why"].endswith(
        "41588_2099_Article_MOESM1_ESM.xlsx: publisher not carried over: the new fetch replaced "
        "the earlier one and did not read this file)"
    )
