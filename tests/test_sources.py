"""The source clients against recorded shapes with synthetic values; no test uses the network."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from importlib.metadata import version
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from chd_curator import pubmed, sources
from chd_curator.sources import (
    HEADERS,
    BotCheck,
    EpmcRecord,
    FreeCopy,
    Response,
    SourceError,
    europepmc_fulltext,
    europepmc_record,
    europepmc_supplements,
    openalex_free_copies,
    pmc_efetch,
    publisher_supplements,
    urllib_get,
)
from fake_http import FakeWeb, ok

FIXTURES = Path(__file__).parent / "fixtures" / "texts" / "responses"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def search_url(pmid: str) -> str:
    return f"{EPMC}search?query=EXT_ID:{pmid}%20AND%20SRC:MED&resultType=core&format=json"


# --- the clients parse their fixtures -------------------------------------------------


def test_europepmc_record_parses_an_open_and_a_closed_paper() -> None:
    web = FakeWeb(
        {
            search_url("9100001"): ok("u", fixture("europepmc_search_9100001.json")),
            search_url("9100002"): ok("u", fixture("europepmc_search_9100002.json")),
            search_url("9100009"): ok("u", fixture("europepmc_search_none.json")),
        }
    )
    assert europepmc_record("9100001", web) == EpmcRecord(
        pmid="9100001",
        pmcid="PMC9100001",
        open_access=True,
        in_pmc=False,
        licence="cc by",
        doi="10.9999/synthetic.0001",
        title="A synthetic study of a synthetic gene",
        year="2001",
    )
    closed = europepmc_record("9100002", web)
    assert closed is not None
    assert (closed.pmcid, closed.open_access, closed.in_pmc, closed.licence) == (
        None,
        False,
        False,
        None,
    )
    assert europepmc_record("9100009", web) is None


def test_europepmc_record_refuses_a_record_for_another_paper() -> None:
    web = FakeWeb({search_url("9100002"): ok("u", fixture("europepmc_search_9100001.json"))})
    with pytest.raises(SourceError, match="not 9100002"):
        europepmc_record("9100002", web)


def test_europepmc_fulltext_and_supplements_read_their_paths_and_map_404_to_none() -> None:
    web = FakeWeb(
        {
            f"{EPMC}PMC9100001/fullTextXML": ok("u", b"<article/>", "text/xml"),
            f"{EPMC}PMC9100001/supplementaryFiles": ok("u", b"PK\x03\x04", "application/zip"),
            f"{EPMC}PMC9100002/": Response(404, "https://www.ebi.ac.uk/x", "", b""),
        }
    )
    assert europepmc_fulltext("PMC9100001", web) == b"<article/>"
    assert europepmc_supplements("PMC9100001", web) == b"PK\x03\x04"
    assert europepmc_fulltext("PMC9100002", web) is None
    assert europepmc_supplements("PMC9100002", web) is None
    web.routes[f"{EPMC}PMC9100003/"] = Response(500, "https://www.ebi.ac.uk/x", "", b"")
    with pytest.raises(SourceError, match="www.ebi.ac.uk answered HTTP 500"):
        europepmc_fulltext("PMC9100003", web)


def test_pmc_efetch_sends_the_tool_and_the_key_only_when_given() -> None:
    body = fixture("efetch_pmc_9100001.xml")
    web = FakeWeb({pubmed.BASE_URL: ok("u", body, "text/xml")})
    assert pmc_efetch("PMC9100001", web, None) == body
    assert pmc_efetch("9100001", web, "k3y") == body
    keyless, keyed = (parse_qs(urlparse(u).query) for u in web.urls)
    assert keyless == {"db": ["pmc"], "id": ["9100001"], "tool": ["chd-curator"]}
    assert keyed == {**keyless, "api_key": ["k3y"]}
    with pytest.raises(SourceError, match="not a PMC id"):
        pmc_efetch("PMC91x", web, None)


def test_openalex_lists_the_best_location_first_without_repeating_it() -> None:
    web = FakeWeb(
        {
            "https://api.openalex.org/works/pmid:9100002": ok(
                "u", fixture("openalex_work_9100002.json")
            ),
            "https://api.openalex.org/works/pmid:9100009": Response(404, "https://x/", "", b""),
        }
    )
    assert openalex_free_copies("9100002", web) == (
        FreeCopy("https://repo.example.org/files/2.pdf", "pdf", "cc-by", "repo.example.org"),
        FreeCopy("https://preprints.example.net/abs/2", "landing", None, "preprints.example.net"),
    )
    assert openalex_free_copies("9100009", web) == ()


def test_publisher_supplements_keeps_supplement_documents_sorted_and_unique() -> None:
    url = "https://publisher.example.com/article/2"
    web = FakeWeb(
        {
            "https://doi.org/10.9999/synthetic.0002": ok(
                url, fixture("publisher_page_two_supplements.html"), "text/html"
            )
        }
    )
    assert publisher_supplements("10.9999/synthetic.0002", web) == (
        "https://publisher.example.com/article/2/MediaObjects/41588_2099_Article_MOESM1_ESM.xlsx",
        "https://static.example.com/files/appendix.pdf",
    )
    assert web.urls == ["https://doi.org/10.9999/synthetic.0002"]


@pytest.mark.parametrize(
    ("response", "why"),
    [
        (
            ok("https://publisher.example.com/a", fixture("publisher_challenge.html"), "text/html"),
            "bot check",
        ),
        (Response(403, "https://publisher.example.com/a", "text/html", b"no"), "HTTP 403"),
        (Response(429, "https://publisher.example.com/a", "text/html", b"no"), "HTTP 429"),
        (Response(503, "https://publisher.example.com/a", "text/html", b"no"), "HTTP 503"),
    ],
)
def test_a_challenge_page_or_a_blocking_status_is_a_bot_check(response: Response, why: str) -> None:
    web = FakeWeb({"https://doi.org/10.9999/x": response})
    result = publisher_supplements("10.9999/x", web)
    assert isinstance(result, BotCheck)
    assert result.host == "publisher.example.com"
    assert why in result.why


def test_publisher_supplements_refuses_a_missing_landing_page() -> None:
    web = FakeWeb({"https://doi.org/10.9999/x": Response(404, "https://p.example.com/a", "", b"")})
    with pytest.raises(SourceError, match="p.example.com answered HTTP 404"):
        publisher_supplements("10.9999/x", web)


# --- identification --------------------------------------------------------------------


class FakeReply:
    def __init__(self, status: int, url: str, body: bytes, content_type: str) -> None:
        self.status, self.url, self._body = status, url, body
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeReply:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeUrlopen:
    """Answers from a queue of statuses (or exceptions); records every Request and timeout."""

    def __init__(self, *answers: int | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeReply:
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if answer >= 400:
            headers = Message()
            headers["Content-Type"] = "text/plain"
            raise urllib.error.HTTPError(
                request.full_url, answer, "reason", headers, io.BytesIO(b"body %d" % answer)
            )
        return FakeReply(answer, request.full_url, b"body %d" % answer, "text/plain")


class Clock:
    """A throttle's fake time: `sleep` records the request and advances nothing."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(sources, "_throttle", sources._HostThrottle(lambda: fake.now, fake.sleep))
    return fake


def install(monkeypatch: pytest.MonkeyPatch, *answers: int | Exception) -> FakeUrlopen:
    fake = FakeUrlopen(*answers)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def test_no_request_identifies_the_curator(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    web = FakeWeb(
        {
            search_url("9100001"): ok("u", fixture("europepmc_search_9100001.json")),
            f"{EPMC}PMC9100001/fullTextXML": ok("u", b"<article/>"),
            f"{EPMC}PMC9100001/supplementaryFiles": ok("u", b"PK\x03\x04"),
            pubmed.BASE_URL: ok("u", fixture("efetch_pmc_9100001.xml")),
            "https://api.openalex.org/works/": ok("u", fixture("openalex_work_9100002.json")),
            "https://doi.org/": ok("https://p.example.com/a", b"<html></html>"),
        }
    )
    europepmc_record("9100001", web)
    europepmc_fulltext("PMC9100001", web)
    europepmc_supplements("PMC9100001", web)
    pmc_efetch("PMC9100001", web, None)
    openalex_free_copies("9100002", web)
    publisher_supplements("10.9999/x", web)
    assert len(web.urls) == 6
    # The clients see only URLs; the headers are what urllib_get puts on the Request.
    fake = install(monkeypatch, 200)
    urllib_get(web.urls[0])
    sent = dict(fake.requests[0].header_items())
    assert sent == {"User-agent": HEADERS["User-Agent"], "Accept": "*/*"}
    for text in [*web.urls, *HEADERS.values(), *sent.values()]:
        assert not any(w in text.lower() for w in ("@", "email", "mailto"))
    assert HEADERS["User-Agent"] == f"chd-curator/{version('chd-curator')}"


# --- the transport ---------------------------------------------------------------------


def test_a_503_is_retried_after_two_seconds_and_a_503_three_times_is_returned(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    delays: list[float] = []
    fake = install(monkeypatch, 503, 200)
    reply = urllib_get("https://www.ebi.ac.uk/a", sleep=delays.append)
    assert (reply.status, reply.body, reply.content_type) == (200, b"body 200", "text/plain")
    assert delays == [2.0]
    assert fake.timeouts == [60.0, 60.0]

    delays.clear()
    install(monkeypatch, 503, 502, 503)
    reply = urllib_get("https://www.ebi.ac.uk/a", sleep=delays.append)
    assert (reply.status, reply.body) == (503, b"body 503")
    assert delays == [2.0, 4.0]


def test_a_4xx_is_returned_not_raised_and_not_retried(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    fake = install(monkeypatch, 404)
    delays: list[float] = []
    reply = urllib_get("https://www.ebi.ac.uk/a", sleep=delays.append)
    assert (reply.status, reply.body, reply.content_type) == (404, b"body 404", "text/plain")
    assert (len(fake.requests), delays) == (1, [])


def test_a_network_error_names_the_host_and_never_the_url_or_key(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    install(monkeypatch, urllib.error.URLError("secret k3y reason"))
    with pytest.raises(SourceError) as excinfo:
        urllib_get(f"{pubmed.BASE_URL}efetch.fcgi?db=pmc&id=1&api_key=k3y")
    message = str(excinfo.value)
    assert message == "could not reach eutils.ncbi.nlm.nih.gov: URLError"
    assert "k3y" not in message


def test_requests_are_spaced_per_host(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    install(monkeypatch, *[200] * 8)
    for url in [
        "https://www.ebi.ac.uk/a",
        "https://www.ebi.ac.uk/b",
        "https://api.openalex.org/a",
        "https://api.openalex.org/b",
        "https://doi.org/a",
        "https://doi.org/b",
        "https://www.ebi.ac.uk/c",
        "https://other.example.org/a",
    ]:
        urllib_get(url)
    # The first request to each host waits for nothing; the clock never advances, so
    # each repeat waits that host's full gap.
    assert clock.slept == pytest.approx([1.0, 0.2, 2.0, 1.0])


def test_ncbi_requests_wait_on_pubmeds_throttle(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    waited: list[str] = []

    class Spy:
        def wait(self, url: str) -> None:
            waited.append(url)

    monkeypatch.setattr(pubmed, "_throttle", Spy())
    install(monkeypatch, 200, 200)
    urllib_get(f"{pubmed.BASE_URL}efetch.fcgi?db=pmc&id=1")
    urllib_get("https://example.org/a")
    assert waited == [f"{pubmed.BASE_URL}efetch.fcgi?db=pmc&id=1"]
    assert clock.slept == []


# --- bodies that are not the thing asked for -----------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            b"<?xml version='1.0'?><pmc-articleset><!--The publisher does not allow downloading of"
            b" the full text in XML form.--></pmc-articleset>",
            "The publisher does not allow downloading of the full text in XML form.",
        ),
        (b"<pmc-articleset><ERROR>" + b"x" * 200 + b"</ERROR></pmc-articleset>", "x" * 120),
        (b"<pmc-articleset></pmc-articleset>", "no article in the answer"),
        (
            b"<pmc-articleset><article><body><p>Heart defects in a cohort of 40 probands",
            "no article in the answer",
        ),
        (
            b"<html><body>Heart defects in a cohort, api_key=k3y</body></html>",
            "no article in the answer",
        ),
        (b"<other><ERROR>Heart defects</ERROR></other>", "no article in the answer"),
        (
            b"<pmc-articleset><note><![CDATA[<!--paper text in cdata-->]]></note></pmc-articleset>",
            "no article in the answer",
        ),
        (b"<pmc-articleset><error>paper text</error></pmc-articleset>", "no article in the answer"),
        (b"<!--outside--><pmc-articleset></pmc-articleset>", "no article in the answer"),
        (b"<pmc-articleset><ERROR>a<!--c-->b<i>d</i>e</ERROR></pmc-articleset>", "abde c"),
    ],
)
def test_efetch_without_an_article_raises_with_ncbis_words(body: bytes, reason: str) -> None:
    web = FakeWeb({pubmed.BASE_URL: ok("u", body, "text/xml")})
    with pytest.raises(SourceError) as excinfo:
        pmc_efetch("PMC9100001", web, "k3y")
    assert str(excinfo.value) == f"PMC9100001: {reason}"


def test_efetch_returns_an_article_without_a_body() -> None:
    body = b"<pmc-articleset><article><front/></article></pmc-articleset>"
    web = FakeWeb({pubmed.BASE_URL: ok("u", body)})
    assert pmc_efetch("PMC9100001", web, None) == body


def test_europepmc_refuses_a_200_that_is_not_the_asked_for_file() -> None:
    page = "https://www.ebi.ac.uk/x"
    web = FakeWeb(
        {
            f"{EPMC}PMC9100001/fullTextXML": ok(page, b"<html></html>"),
            f"{EPMC}PMC9100002/fullTextXML": ok(page, b"<article><bo"),
            f"{EPMC}PMC9100001/supplementaryFiles": ok(page, b"<html>"),
            f"{EPMC}PMC9100002/supplementaryFiles": ok(page, b"PK\x03"),
        }
    )
    for call in (europepmc_fulltext, europepmc_supplements):
        for pmcid in ("PMC9100001", "PMC9100002"):
            with pytest.raises(SourceError):
                call(pmcid, web)


def test_a_publisher_gets_one_attempt_and_an_api_host_is_retried(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    delays: list[float] = []
    fake = install(monkeypatch, 503)
    assert urllib_get("https://publisher.example.com/a", sleep=delays.append).status == 503
    assert (len(fake.requests), delays) == (1, [])
    for host in ("api.openalex.org", "www.ebi.ac.uk", "eutils.ncbi.nlm.nih.gov"):
        fake = install(monkeypatch, 503, 200)
        assert urllib_get(f"https://{host}/a", sleep=delays.append).status == 200
        assert len(fake.requests) == 2


@pytest.mark.parametrize(
    ("href", "kept"),
    [
        ("/files/Chesmore.pdf", False),
        ("/files/41588_2099_Article_ESM.pdf", True),
        ("/files/ESM_1.pdf", True),
        ("/files/esm1.pdf", True),
    ],
)
def test_esm_matches_only_as_a_word(href: str, kept: bool) -> None:
    page = f'<a href="{href}">Download</a>'.encode()
    web = FakeWeb({"https://doi.org/10.9999/x": ok("https://p.example.com/a", page)})
    assert bool(publisher_supplements("10.9999/x", web)) is kept


def test_an_http_error_after_a_redirect_reports_the_final_url(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    headers = Message()
    headers["Content-Type"] = "text/html"
    error = urllib.error.HTTPError(
        "https://final.example.com/p", 403, "no", headers, io.BytesIO(b"no")
    )
    install(monkeypatch, error)
    assert urllib_get("https://doi.org/10.9999/x").url == "https://final.example.com/p"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            b"<pmc-articleset><ERROR>Request failed for api_key=k3y%2b0</ERROR>"
            b"<!--key k3y+0 (k3y%2B0) is not valid--></pmc-articleset>",
            "Request failed for api_key=<key> key <key> (<key>) is not valid",
        ),
        (
            b"<pmc-articleset><ERROR>" + b"x" * 115 + b" k3y+0</ERROR></pmc-articleset>",
            "x" * 115 + " <key",
        ),
    ],
)
def test_a_notice_never_carries_the_key(body: bytes, reason: str) -> None:
    """The key as given, as sent (URL-quoted) and after `api_key=` in any spelling reads
    `<key>`, before the notice is cut, so a key at the cut leaves no prefix behind."""
    web = FakeWeb({pubmed.BASE_URL: ok("u", body)})
    with pytest.raises(SourceError) as excinfo:
        pmc_efetch("PMC9100001", web, "k3y+0")
    assert str(excinfo.value) == f"PMC9100001: {reason}"
    assert "k3y" not in str(excinfo.value)


def test_a_deeply_nested_notice_is_refused_with_its_text() -> None:
    """3,000 levels inside one ERROR, past the default recursion limit of 1,000, still give a
    `SourceError` with the ERROR's text in document order."""
    levels = b"".join(b"<i>" + b"abcdefghij"[i % 10 : i % 10 + 1] for i in range(3000))
    body = b"<pmc-articleset><ERROR>" + levels + b"</i>" * 3000 + b"</ERROR></pmc-articleset>"
    web = FakeWeb({pubmed.BASE_URL: ok("u", body)})
    with pytest.raises(SourceError) as excinfo:
        pmc_efetch("PMC9100001", web, None)
    assert str(excinfo.value) == "PMC9100001: " + "abcdefghij" * 12


def test_a_notice_never_carries_text_from_outside_error_and_comments() -> None:
    body = b"<pmc-articleset><!--restricted--><note>paper words</note></pmc-articleset>"
    web = FakeWeb({pubmed.BASE_URL: ok("u", body)})
    with pytest.raises(SourceError) as excinfo:
        pmc_efetch("PMC9100001", web, None)
    assert str(excinfo.value) == "PMC9100001: restricted"


def test_an_error_inside_an_error_is_read_once() -> None:
    """The outer ERROR's text holds the inner one's; reading the inner again repeated it (and
    made deep ERROR nesting quadratic). A comment inside either is still read, once."""
    body = b"<eFetchResult><ERROR>a<ERROR>b<!--n-->c</ERROR>d</ERROR></eFetchResult>"
    web = FakeWeb({pubmed.BASE_URL: ok("u", body)})
    with pytest.raises(SourceError) as excinfo:
        pmc_efetch("PMC9100001", web, None)
    assert str(excinfo.value) == "PMC9100001: abcd n"


def _page(*parts: str) -> bytes:
    return ("<html><body>" + "".join(parts) + "</body></html>").encode()


SUPPLEMENT_LINK = '<a href="/suppl/MOESM1_ESM.xlsx">Supplementary Table 1</a>'
RECAPTCHA = '<script src="https://www.google.com/recaptcha/api.js"></script>'


@pytest.mark.parametrize(
    "page",
    [
        # over 200,000 bytes, the challenge word in its first 20,000 characters
        _page(RECAPTCHA, "<p>" + "word " * 50_000 + "</p>", SUPPLEMENT_LINK),
        # under 200,000 bytes, the challenge word past its first 20,000 characters
        _page("<p>" + "word " * 5_000 + "</p>", SUPPLEMENT_LINK, RECAPTCHA),
    ],
)
def test_an_article_page_that_mentions_a_captcha_is_not_a_bot_check(page: bytes) -> None:
    """After the pilot's rule (publisher_pass.py), which read the challenge pattern in the first
    20,000 characters of the decoded page and only when the decoded page was under 200,000
    characters. Here the 20,000 are characters of the decoded page and the 200,000 are bytes
    of the page as served. A long article page loading reCAPTCHA for a comment form keeps its
    supplement links."""
    web = FakeWeb({"https://doi.org/10.9999/x": ok("https://p.example.com/a", page)})
    assert publisher_supplements("10.9999/x", web) == (
        "https://p.example.com/suppl/MOESM1_ESM.xlsx",
    )


def test_a_supplement_named_in_a_query_value_is_a_supplement_link() -> None:
    href = "/action/downloadSupplement?doi=10.1002%2Fajmg.x&file=ajmg-sup-0001-TableS1.xlsx"
    page = _page(f'<a href="{href}">Supporting Information</a>', '<a href="/a?x=y.pdf">PDF</a>')
    web = FakeWeb({"https://doi.org/10.9999/x": ok("https://p.example.com/doi/x", page)})
    (link,) = publisher_supplements("10.9999/x", web)
    assert link == f"https://p.example.com{href}"
    assert sources.supplement_name(link) == "ajmg-sup-0001-TableS1.xlsx"
    assert sources.supplement_name("https://p.example.com/files/T1.xlsx?v=2") == "T1.xlsx"
    assert sources.supplement_name("https://p.example.com/files/") == ""


def test_the_doi_is_percent_encoded_in_the_landing_page_url() -> None:
    web = FakeWeb({"https://doi.org/": Response(404, "https://doi.org/x", "text/html", b"")})
    doi = "10.1002/(SICI)1096-8628(19960202)61:4<330::AID-AJMG6>3.0.CO;2-#"
    with pytest.raises(SourceError):
        publisher_supplements(doi, web)
    with pytest.raises(SourceError):
        publisher_supplements("10.9999/a b?c%d", web)
    assert web.urls == [
        "https://doi.org/10.1002/(SICI)1096-8628(19960202)61:4%3C330::AID-AJMG6%3E3.0.CO;2-%23",
        "https://doi.org/10.9999/a%20b%3Fc%25d",
    ]
    assert urlparse(web.urls[0]).fragment == ""


@pytest.mark.parametrize(
    "work",
    [
        {"best_oa_location": "https://repo.example.org/1.pdf"},
        {"locations": ["https://repo.example.org/1.pdf"]},
        {"locations": [{"is_oa": True, "pdf_url": 7}]},
        {"best_oa_location": {"landing_page_url": ["a"]}},
        {"locations": {"is_oa": True}},
    ],
)
def test_an_openalex_work_of_the_wrong_shape_is_a_source_error(work: object) -> None:
    web = FakeWeb(
        {"https://api.openalex.org/works/pmid:9100002": ok("u", json.dumps(work).encode())}
    )
    with pytest.raises(SourceError, match="api.openalex.org"):
        openalex_free_copies("9100002", web)


@pytest.mark.parametrize("key", ["pmcid", "doi", "license", "title"])
def test_a_europepmc_field_of_the_wrong_type_is_a_source_error(key: str) -> None:
    hit = {"pmid": "9100001", "title": "T", "pubYear": "2001", key: 9}
    body = json.dumps({"resultList": {"result": [hit]}}).encode()
    web = FakeWeb({search_url("9100001"): ok("https://www.ebi.ac.uk/x", body)})
    with pytest.raises(SourceError, match=key):
        europepmc_record("9100001", web)


BAD_LINKS = ("http://[x/supplementary.xlsx", "http://ex\uff0fample.com/supplementary.xlsx")


def test_a_supplement_link_whose_url_cannot_be_read_is_listed_as_written() -> None:
    """An open IPv6 bracket and a host invalid under NFKC raised ValueError out of urljoin and
    killed the run. Such a link is listed as written, for the caller to record unread."""
    page = _page(
        *(f'<a href="{href}">Supplementary data</a>' for href in BAD_LINKS), SUPPLEMENT_LINK
    )
    web = FakeWeb({"https://doi.org/10.9999/x": ok("https://p.example.com/a", page)})
    found = publisher_supplements("10.9999/x", web)
    assert found == tuple(sorted([*BAD_LINKS, "https://p.example.com/suppl/MOESM1_ESM.xlsx"]))
    assert [sources.readable_url(u) for u in found] == [False, False, True]
    assert [sources.supplement_name(u) for u in BAD_LINKS] == ["supplementary.xlsx"] * 2


@pytest.mark.parametrize("url", BAD_LINKS)
def test_an_openalex_copy_whose_url_cannot_be_read_is_listed_with_no_host(url: str) -> None:
    work = {"best_oa_location": {"is_oa": True, "pdf_url": url}}
    web = FakeWeb(
        {"https://api.openalex.org/works/pmid:9100002": ok("u", json.dumps(work).encode())}
    )
    assert openalex_free_copies("9100002", web) == (FreeCopy(url, "pdf", None, ""),)


def test_a_url_the_transport_cannot_read_is_a_source_error(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    fake = install(monkeypatch, 200)
    with pytest.raises(SourceError, match="^a URL that cannot be read$"):
        urllib_get(BAD_LINKS[0])
    assert fake.requests == []
