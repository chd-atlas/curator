"""Clients for the free-copy sources: Europe PMC, PMC efetch, OpenAlex, publisher pages.

**Every client takes its transport as an argument** (`Get`: URL in, `Response` out), so
tests inject canned responses and nothing here reaches the network on its own.
`urllib_get` is the one real transport.

**No request identifies a person.** `HEADERS` carries two headers, a User-Agent of
`chd-curator/<version>` and `Accept: */*`; no client sends an email or a name. The one
NCBI key is passed in by the caller and never appears in an error message: no message is
built from a URL the module requested (a host stands for it), and `pmc_efetch` writes
`<key>` over the key (as given and URL-quoted) and over every `api_key=` value in NCBI's
notice, which can echo the request. A `SourceError` message holds a host, a status or
exception type, the PMID or PMCID the caller passed (in quotes when it is not a PMC id),
the PMID of another paper Europe PMC answered for, and, for an efetch answer without an
article, the text of the `ERROR` elements and in-root comments of a well-formed
`pmc-articleset` / `eFetchResult` body, the key written over, cut to 120 characters.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from importlib.metadata import version
from typing import Any, Final, Literal
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse, urlsplit

from chd_curator import pubmed

HEADERS: Final = {"User-Agent": f"chd-curator/{version('chd-curator')}", "Accept": "*/*"}
TIMEOUT_SECONDS: Final = 60.0
RETRY_STATUSES: Final = frozenset({500, 502, 503, 504})
RETRY_DELAYS: Final = (2.0, 4.0)
EUTILS_HOST: Final = "eutils.ncbi.nlm.nih.gov"
DEFAULT_GAP: Final = 2.0

EPMC_BASE: Final = "https://www.ebi.ac.uk/europepmc/webservices/rest/"
OPENALEX_BASE: Final = "https://api.openalex.org/works/"

# The literature pilot's challenge pattern (publisher_pass.py). The pilot read it in the first
# 20,000 characters of the decoded page, and only when the decoded page was under 200,000
# characters. Here a page is a bot check when the pattern is found in the first
# CHALLENGE_CHARS characters of the decoded page and the page as served is under
# CHALLENGE_PAGE_BYTES bytes, so a long article page loading a captcha for a comment form is
# not one.
CHALLENGE: Final = re.compile(
    r"captcha|just a moment|are you a robot|unusual traffic|access denied|cf-chl"
    r"|challenge-platform|verify you are human",
    re.I,
)
CHALLENGE_CHARS: Final = 20_000
CHALLENGE_PAGE_BYTES: Final = 200_000
BOT_STATUSES: Final = frozenset({403, 429, 503})
SUPPLEMENT_EXTENSIONS: Final = (".xlsx", ".xls", ".docx", ".doc", ".pdf", ".zip", ".csv", ".txt")
SUPPLEMENT_WORDS: Final = re.compile(
    r"supplement|suppl|moesm|appendix|additional file|(?<![a-z])esm(?![a-z])", re.I
)


class SourceError(Exception):
    """A source could not be reached or answered something unusable."""


@dataclass(frozen=True)
class Response:
    status: int
    url: str
    content_type: str
    body: bytes


Get = Callable[[str], Response]


@dataclass(frozen=True)
class EpmcRecord:
    pmid: str
    pmcid: str | None
    open_access: bool
    in_pmc: bool
    licence: str | None
    doi: str | None
    title: str
    year: str


@dataclass(frozen=True)
class FreeCopy:
    url: str
    kind: Literal["pdf", "landing"]
    licence: str | None
    host: str


@dataclass(frozen=True)
class BotCheck:
    host: str
    why: str


def readable_url(url: str) -> bool:
    """False for a URL `urllib.parse` refuses (an open IPv6 bracket, a host invalid under
    NFKC normalisation), which no client here can request."""
    try:
        urlsplit(url)
    except ValueError:
        return False
    return True


def _host(url: str) -> str:
    """The URL's host, "" when it has none or cannot be read."""
    return (urlparse(url).hostname or "") if readable_url(url) else ""


# The API hosts and their spacing in seconds. These (and NCBI eutils, spaced by `pubmed`'s
# throttle) are the only hosts whose 500/502/503/504 answers are retried.
API_GAPS: Final = {
    "www.ebi.ac.uk": 1.0,
    "europepmc.org": 1.0,
    "www.europepmc.org": 1.0,
    "api.openalex.org": 0.2,
}


def _gap(host: str) -> float:
    return API_GAPS.get(host, DEFAULT_GAP)


class _HostThrottle:
    """Spaces requests to one host by that host's gap; NCBI goes through `pubmed`'s throttle."""

    def __init__(self, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = _host(url)
        if host == EUTILS_HOST:
            pubmed._throttle.wait(url)
            return
        last = self._last.get(host)
        if last is not None:
            remaining = last + _gap(host) - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()


_throttle = _HostThrottle(time.monotonic, time.sleep)


def urllib_get(url: str, *, sleep: Callable[[float], None] = time.sleep) -> Response:
    """GET `url`: 60 s timeout, spaced per host.

    A 500/502/503/504 from an API host (the `API_GAPS` hosts and NCBI eutils) is
    retried after 2 s, then 4 s. Any other host, a publisher above all, gets one attempt.

    A 4xx (and a 5xx still failing after the retries) is returned as a `Response`. A
    network failure raises `SourceError` naming the host and the exception type, never
    the URL: it can carry the NCBI key; a URL `readable_url` refuses raises `SourceError`
    before any request. `sleep` paces the retries only.
    """
    if not readable_url(url):
        raise SourceError("a URL that cannot be read")
    host = _host(url)
    delays: tuple[float | None, ...] = (*RETRY_DELAYS, None) if _is_api(host) else (None,)
    for delay in delays:
        _throttle.wait(url)
        response = _once(url, host)
        if response.status not in RETRY_STATUSES or delay is None:
            return response
        sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _is_api(host: str) -> bool:
    return host == EUTILS_HOST or host in API_GAPS


def _once(url: str, host: str) -> Response:
    request = urllib.request.Request(url, headers=HEADERS)  # noqa: S310
    try:
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:  # noqa: S310
                return Response(
                    reply.status, reply.url, reply.headers.get("Content-Type", ""), reply.read()
                )
        except urllib.error.HTTPError as exc:
            with exc:
                return Response(
                    exc.code, exc.geturl(), exc.headers.get("Content-Type", ""), exc.read()
                )
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise SourceError(f"could not reach {host}: {type(exc).__name__}") from None


def _expect(response: Response, what: str, *, missing_ok: bool) -> bool:
    """True for a 200; False for a 404 when `missing_ok`; anything else raises."""
    if response.status == 200:
        return True
    if response.status == 404 and missing_ok:
        return False
    raise SourceError(f"{_host(response.url)} answered HTTP {response.status} for {what}")


def _json(response: Response, what: str) -> Any:
    try:
        return json.loads(response.body)
    except ValueError:
        raise SourceError(f"{_host(response.url)}: {what} is not JSON") from None


def _flag(value: object) -> bool:
    return value == "Y"


def europepmc_record(pmid: str, get: Get) -> EpmcRecord | None:
    """The Europe PMC core record of a PubMed article, or None when it has no hit."""
    url = (
        f"{EPMC_BASE}search?query=EXT_ID:{quote(pmid)}%20AND%20SRC:MED&resultType=core&format=json"
    )
    response = get(url)
    _expect(response, "a search", missing_ok=False)
    payload = _json(response, "the search answer")
    try:
        results = payload["resultList"]["result"]
        if not results:
            return None
        hit = results[0]
        if str(hit["pmid"]) != pmid:
            raise SourceError(f"europepmc.org answered a record for PMID {hit['pmid']}, not {pmid}")
        for key in ("pmcid", "doi", "license"):
            if not isinstance(hit.get(key), str | None):
                raise SourceError(f"europepmc.org: the record's {key} is not a string")
        if not isinstance(hit["title"], str):
            raise SourceError("europepmc.org: the record's title is not a string")
        return EpmcRecord(
            pmid=pmid,
            pmcid=hit.get("pmcid"),
            open_access=_flag(hit.get("isOpenAccess")),
            in_pmc=_flag(hit.get("inPMC")),
            licence=hit.get("license"),
            doi=hit.get("doi"),
            title=hit["title"],
            year=str(hit["pubYear"]),
        )
    except (KeyError, TypeError, IndexError):
        raise SourceError(
            "europepmc.org: the search answer lacks resultList.result[0] fields"
        ) from None


ZIP_SIGNATURE: Final = b"PK\x03\x04"
NOTICE_LIMIT: Final = 120
API_KEY_PARAM: Final = re.compile(r"api_key=[^&\s]+")


def _has_article(body: bytes) -> bool:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return False
    return any(el.tag == "article" for el in root.iter())


def _is_comment(node: ET.Element) -> bool:
    return node.tag is ET.Comment  # type: ignore[comparison-overlap]


def _element_text(element: ET.Element) -> str:
    """The text and tails inside `element`, in document order; a comment gives only its tail.

    Walked with a stack, not recursion, so no depth of nesting raises `RecursionError`.
    """
    chunks: list[str] = []
    stack: list[ET.Element | str] = [element]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            chunks.append(item)
            continue
        if not _is_comment(item):
            chunks.append(item.text or "")
        for child in reversed(item):
            stack += [child.tail or "", child]
    return "".join(chunks)


def _notice(body: bytes, api_key: str | None) -> str:
    """The reason an efetch answer holds no article, cut to `NOTICE_LIMIT` characters.

    Only a body that parses as XML with a `pmc-articleset` or `eFetchResult` root yields
    text: that of its `ERROR` elements (that exact case; an `ERROR` inside another is read
    once, as part of the outer one's text, and a comment inside one adds no text of its own
    to it) and of each comment inside the root, read once, all as parsed nodes, in document
    order.
    Anything else (HTML, a truncated body, another root, a comment outside the root,
    comment-like text inside CDATA) gives "no article in the answer", so no text from
    outside those nodes reaches the message. Those nodes can echo the request, so before
    the cut `<key>` is written over the value after every `api_key=`, whatever it is, and
    over every occurrence of the key's value, as given and URL-quoted (as the request sent it).
    """
    fallback = "no article in the answer"
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        parser.feed(body)
        root = parser.close()
    except ET.ParseError:
        return fallback
    if root.tag not in ("pmc-articleset", "eFetchResult"):
        return fallback
    parts: list[str] = []
    stack: list[tuple[ET.Element, bool]] = [(root, False)]
    while stack:
        el, in_error = stack.pop()
        if el.tag == "ERROR" and not in_error:
            parts.append(_element_text(el))  # holds the text of any ERROR inside it
        elif _is_comment(el):
            parts.append(el.text or "")
        inside = in_error or el.tag == "ERROR"
        stack += [(child, inside) for child in reversed(el)]
    text = API_KEY_PARAM.sub("api_key=<key>", " ".join(" ".join(parts).split()))
    if api_key:
        for form in (api_key, quote(api_key)):
            text = text.replace(form, "<key>")
    return text[:NOTICE_LIMIT] or fallback


def europepmc_fulltext(pmcid: str, get: Get) -> bytes | None:
    """The JATS full text Europe PMC serves for `pmcid`, or None on a 404."""
    response = get(f"{EPMC_BASE}{quote(pmcid)}/fullTextXML")
    if not _expect(response, "full text", missing_ok=True):
        return None
    if not _has_article(response.body):
        raise SourceError(f"{_host(response.url)}: the full text of {pmcid} holds no <article>")
    return response.body


def europepmc_supplements(pmcid: str, get: Get) -> bytes | None:
    """The zip of supplementary files Europe PMC serves for `pmcid`, or None on a 404."""
    response = get(f"{EPMC_BASE}{quote(pmcid)}/supplementaryFiles")
    if not _expect(response, "supplementary files", missing_ok=True):
        return None
    if not response.body.startswith(ZIP_SIGNATURE):
        raise SourceError(f"{_host(response.url)}: the supplements of {pmcid} are not a zip")
    return response.body


def pmc_efetch(pmcid: str, get: Get, api_key: str | None) -> bytes | None:
    """The PMC efetch XML of `pmcid` (`PMC123` or `123`), or None on a 404.

    NCBI answers 200 with a notice (publisher-restricted XML) or an ERROR element for
    other bodies. A body that does not parse as XML holding an `<article>` raises
    `SourceError`: its message carries the text of the `ERROR` elements and in-root
    comments of a well-formed `pmc-articleset` / `eFetchResult` body, with `<key>` over the
    key's value (as given and URL-quoted) and over the value after every `api_key=` (120
    characters at most), else "no article in the answer". An `<article>` with no `<body>`
    is returned: judging it is the caller's job.

    Sends `tool=chd-curator` and never `email`; `api_key` is added only when given.
    """
    digits = pmcid.removeprefix("PMC")
    if not digits.isdigit():
        raise SourceError(f"not a PMC id: {pmcid!r}")
    url = f"{pubmed.BASE_URL}efetch.fcgi?db=pmc&id={digits}&tool={pubmed.TOOL}"
    if api_key:
        url += f"&api_key={quote(api_key)}"
    response = get(url)
    if not _expect(response, "PMC efetch", missing_ok=True):
        return None
    if not _has_article(response.body):
        raise SourceError(f"{pmcid}: {_notice(response.body, api_key)}")
    return response.body


def _copy(location: object) -> FreeCopy | None:
    if not isinstance(location, dict):
        raise SourceError("api.openalex.org: a location of the work is not an object")
    pdf, landing = location.get("pdf_url"), location.get("landing_page_url")
    licence = location.get("license")
    for value in (pdf, landing, licence):
        if not isinstance(value, str | None):
            raise SourceError("api.openalex.org: a location's URL or licence is not a string")
    if pdf:
        return FreeCopy(pdf, "pdf", licence, _host(pdf))
    if landing:
        return FreeCopy(landing, "landing", licence, _host(landing))
    return None


def openalex_free_copies(pmid: str, get: Get) -> tuple[FreeCopy, ...]:
    """OpenAlex's open-access copies of a paper: `best_oa_location` first, then the rest.

    The rest are the `locations` OpenAlex marks `is_oa`; `best_oa_location` is listed
    without that check. A copy appearing twice is listed once. A 404 (OpenAlex does not
    know the paper) gives (); a work, location, URL or licence of the wrong type raises
    `SourceError`.
    """
    response = get(f"{OPENALEX_BASE}pmid:{quote(pmid)}")
    if not _expect(response, "a work", missing_ok=True):
        return ()
    work = _json(response, "the work")
    if not isinstance(work, dict):
        raise SourceError("api.openalex.org: the work is not an object")
    listed = work.get("locations") or []
    if not isinstance(listed, list) or not all(isinstance(loc, dict) for loc in listed):
        raise SourceError("api.openalex.org: the work's locations are not a list of objects")
    locations: list[object] = []
    if work.get("best_oa_location"):
        locations.append(work["best_oa_location"])
    locations += [loc for loc in listed if loc.get("is_oa")]
    copies: list[FreeCopy] = []
    for location in locations:
        copy = _copy(location)
        if copy is not None and copy not in copies:
            copies.append(copy)
    return tuple(copies)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def supplement_name(url: str) -> str:
    """The file name a supplement link names: the last part of its path when that ends in a
    `SUPPLEMENT_EXTENSIONS` extension, else the last part of the first query value that does
    (Wiley's `downloadSupplement?...&file=<name>`), else "". A URL `readable_url` refuses is
    read as text: its file name is the last part of what precedes its first `?` or `#`, and
    its query is not read."""
    if readable_url(url):
        path, query = urlparse(url).path, urlparse(url).query
    else:
        path, query = re.split(r"[?#]", url, maxsplit=1)[0], ""
    name = unquote(path).rsplit("/", 1)[-1]
    if name.lower().endswith(SUPPLEMENT_EXTENSIONS):
        return name
    for _, value in parse_qsl(query):
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        if name.lower().endswith(SUPPLEMENT_EXTENSIONS):
            return name
    return ""


def doi_url(doi: str) -> str:
    """`https://doi.org/<doi>`, the DOI percent-encoded but for `/ : ; ( )`, so a `#`, `?`,
    `%`, space or `<>` in it is sent rather than read as URL syntax."""
    return f"https://doi.org/{quote(doi, safe='/:;()')}"


def publisher_supplements(doi: str, get: Get) -> tuple[str, ...] | BotCheck:
    """Supplement links on a DOI's landing page: one GET, sorted, absolute, de-duplicated.

    A link counts when `supplement_name` finds a file name in it (its path, or a query value,
    ends in a document extension) and its href or text names a supplement. A link whose URL
    `readable_url` refuses is listed as written in the page, for the caller to record as a
    supplement not read. A 403/429/503
    gives a `BotCheck`, as does a page under `CHALLENGE_PAGE_BYTES` bytes whose first
    `CHALLENGE_CHARS` characters match `CHALLENGE`; any other non-200 raises `SourceError`.
    """
    response = get(doi_url(doi))
    host = _host(response.url) or "doi.org"
    if response.status in BOT_STATUSES:
        return BotCheck(host, f"HTTP {response.status}")
    _expect(response, "a landing page", missing_ok=False)
    html = response.body.decode("utf-8", errors="replace")
    if len(response.body) < CHALLENGE_PAGE_BYTES and CHALLENGE.search(html[:CHALLENGE_CHARS]):
        return BotCheck(host, "the page is a bot check")
    parser = _Links()
    parser.feed(html)
    found: set[str] = set()
    for href, text in parser.links:
        absolute = urljoin(response.url, href) if readable_url(href) else href
        if not supplement_name(absolute):
            continue
        if SUPPLEMENT_WORDS.search(href) or SUPPLEMENT_WORDS.search(text):
            found.add(absolute)
    return tuple(sorted(found))
