"""A small PubMed (NCBI E-utilities) client for the literature curator tooling.

Copied from the atlas's ``chd_atlas/pubmed.py`` at b4c8191 and changed in three
places: ``TOOL`` is ``chd-curator``, ``publication_record`` returns a plain mapping
where the atlas built a pydantic ``Publication``, and the docstrings that named
the atlas's modules.

Standard library only. Two jobs: page through esearch without ever truncating,
and read esummary/efetch into the fields a curator and a publication record
need. The search query is not built here: the atlas owns it and curator takes it
from ``chd-atlas literature target``.

**Every function takes its transport as an argument** (`Fetch`: URL in, bytes
out), so tests inject recorded or fake responses and nothing here reaches the
network on its own. `urllib_fetch` is the one real transport.

**PMIDs in this module are bare digit strings** (`"8988165"`), as PubMed writes
them. The `PMID:` prefix the atlas uses is added by the callers.

**The API key is passed in, never read here**, and it is never part of an error
message: `urllib_fetch` builds its messages from the HTTP status, not the URL.
No `email` parameter is sent, so no contact address can reach a request; the one
identifying parameter is `tool=chd-curator`.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

Fetch = Callable[[str], bytes]

BASE_URL: Final = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL: Final = "chd-curator"
PAGE_SIZE: Final = 500
BATCH_SIZE: Final = 200
TIMEOUT_SECONDS: Final = 30.0

# Measured in the atlas, 2026-11-02, against the live service: esearch with `retstart=9999`
# answers "'retstart' cannot be larger than 9998. For PubMed, ESearch can only
# retrieve the first 9,999 records matching the query", while `retstart=9998`
# returns the 9,999th record. Past this count a search cannot be paged without
# the history server, so `esearch_all` refuses instead of returning a prefix.
MAX_PAGEABLE_HITS: Final = 9999


class PubMedError(Exception):
    """PubMed gave an answer the curator tooling cannot safely use."""


@dataclass(frozen=True)
class PaperMeta:
    """The esummary fields a publication record is built from."""

    pmid: str
    title: str
    journal: str
    year: int
    authors: tuple[str, ...]
    doi: str | None
    pmcid: str | None


def _url(endpoint: str, params: dict[str, str], api_key: str | None) -> str:
    query = {**params, "tool": TOOL}
    if api_key:
        query["api_key"] = api_key
    return f"{BASE_URL}{endpoint}.fcgi?{urlencode(query)}"


def _json_object(body: bytes, what: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise PubMedError(f"{what}: the response is not JSON ({body[:60]!r}...)") from exc
    if not isinstance(parsed, dict):
        raise PubMedError(f"{what}: the response is not a JSON object")
    return parsed


def _esearch_page(
    query: str, start: int, fetch: Fetch, api_key: str | None
) -> tuple[int, list[str]]:
    url = _url(
        "esearch",
        {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": str(PAGE_SIZE),
            "retstart": str(start),
        },
        api_key,
    )
    result = _json_object(fetch(url), "esearch").get("esearchresult")
    if not isinstance(result, dict):
        raise PubMedError("esearch: no esearchresult in the response")
    if "ERROR" in result:
        raise PubMedError(f"esearch: PubMed reported an error: {result['ERROR']}")
    try:
        count = int(result["count"])
        ids = [str(i) for i in result["idlist"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise PubMedError("esearch: the response carries no usable count and idlist") from exc
    return count, ids


def esearch_all(query: str, fetch: Fetch, *, api_key: str | None) -> tuple[int, list[str]]:
    """Every PMID matching `query`, with the hit count, or `PubMedError`.

    Refuses rather than returning a partial answer: a count above what esearch
    can page, a count that differs between pages (the index moved mid-search),
    and a page set that does not add up to the count.
    """
    count, ids = _esearch_page(query, 0, fetch, api_key)
    if count > MAX_PAGEABLE_HITS:
        raise PubMedError(
            f"esearch: {count} hits is more than the {MAX_PAGEABLE_HITS:,} PubMed lets "
            f"esearch page without the history server; narrow the query"
        )
    found = list(ids)
    for start in range(PAGE_SIZE, count, PAGE_SIZE):
        page_count, page = _esearch_page(query, start, fetch, api_key)
        if page_count != count:
            raise PubMedError(
                f"esearch: the hit count changed from {count} to {page_count} between pages"
            )
        found.extend(page)
    if len(found) != count or len(set(found)) != count:
        raise PubMedError(
            f"esearch: {len(found)} ids ({len(set(found))} distinct) for a count of {count}"
        )
    return count, found


def _batches(pmids: Sequence[str]) -> list[list[str]]:
    return [list(pmids[i : i + BATCH_SIZE]) for i in range(0, len(pmids), BATCH_SIZE)]


def _year(record: dict[str, Any]) -> int | None:
    for key in ("pubdate", "epubdate"):
        found = re.match(r"\d{4}", str(record.get(key) or ""))
        if found:
            return int(found.group())
    return None


def esummary(
    pmids: Sequence[str], fetch: Fetch, *, api_key: str | None = None
) -> dict[str, PaperMeta]:
    """esummary for each PMID. A PMID PubMed did not return is absent from the result."""
    found: dict[str, PaperMeta] = {}
    for batch in _batches(pmids):
        url = _url(
            "esummary",
            {"db": "pubmed", "id": ",".join(batch), "retmode": "json"},
            api_key,
        )
        result = _json_object(fetch(url), "esummary").get("result")
        if not isinstance(result, dict):
            raise PubMedError("esummary: no result in the response")
        try:
            for uid in result.get("uids", []):
                record = result[uid]
                year = _year(record)
                if year is None:
                    raise PubMedError(f"esummary: PMID {uid} has no publication year")
                ids = {a.get("idtype"): a.get("value") for a in record.get("articleids", [])}
                found[str(uid)] = PaperMeta(
                    pmid=str(uid),
                    title=" ".join(str(record.get("title", "")).split()),
                    journal=str(record.get("fulljournalname", "")),
                    year=year,
                    authors=tuple(
                        str(a["name"]) for a in record.get("authors", []) if a.get("name")
                    ),
                    doi=ids.get("doi") or None,
                    pmcid=ids.get("pmc") or None,
                )
        except (KeyError, AttributeError, TypeError) as exc:
            raise PubMedError(
                f"esummary: an unexpected record shape ({type(exc).__name__})"
            ) from None
    return found


def _text(element: ET.Element | None) -> str:
    """All text under an element, inline markup (`<i>`) flattened."""
    return "" if element is None else "".join(element.itertext())


def efetch_abstracts(
    pmids: Sequence[str], fetch: Fetch, *, api_key: str | None = None
) -> dict[str, tuple[str, str]]:
    """`{pmid: (title, abstract)}`. A paper with no abstract gets `""`.

    A structured abstract keeps its section labels (`BACKGROUND: ...`), one per
    line. A PMID PubMed did not return is absent from the result.
    """
    found: dict[str, tuple[str, str]] = {}
    for batch in _batches(pmids):
        url = _url(
            "efetch",
            {"db": "pubmed", "id": ",".join(batch), "rettype": "abstract", "retmode": "xml"},
            api_key,
        )
        try:
            root = ET.fromstring(fetch(url))
        except ET.ParseError as exc:
            raise PubMedError("efetch: the response is not XML") from exc
        for article in root.findall("PubmedArticle"):
            pmid = _text(article.find("MedlineCitation/PMID")).strip()
            title = " ".join(_text(article.find("MedlineCitation/Article/ArticleTitle")).split())
            parts = []
            for node in article.findall("MedlineCitation/Article/Abstract/AbstractText"):
                text = " ".join(_text(node).split())
                label = node.get("Label")
                parts.append(f"{label}: {text}" if label else text)
            found[pmid] = (title, "\n".join(parts))
    return found


def publication_record(meta: PaperMeta, study_type: str) -> dict[str, Any]:
    """The ``publications.yaml`` record for a paper, as ``records.with_publication`` writes it.

    `study_type` is the curator's reading. ``doi`` and ``pmcid`` are left out when
    PubMed has none, as ``with_publication`` leaves them out.
    """
    record: dict[str, Any] = {
        "id": f"PMID:{meta.pmid}",
        "title": meta.title,
        "journal": meta.journal,
        "year": meta.year,
        "authors": list(meta.authors),
        "study_type": study_type,
    }
    if meta.doi is not None:
        record["doi"] = meta.doi
    if meta.pmcid is not None:
        record["pmcid"] = meta.pmcid
    return record


class _Throttle:
    """Spaces requests: three a second without an API key, ten with one."""

    def __init__(self, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self, url: str) -> None:
        gap = 1 / 10 if "api_key=" in url else 1 / 3
        if self._last is not None:
            remaining = self._last + gap - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


_throttle = _Throttle(time.monotonic, time.sleep)


def urllib_fetch(url: str) -> bytes:
    """GET `url` with a timeout, spaced to NCBI's rate limits.

    Failures become `PubMedError` built from the status or reason alone: the URL
    carries the API key and must never reach a message.
    """
    _throttle.wait(url)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as exc:
        raise PubMedError(f"PubMed answered HTTP {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise PubMedError(f"could not reach PubMed: {type(exc).__name__}") from None
