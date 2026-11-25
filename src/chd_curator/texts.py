"""``chd-curator texts``: gather a gene's free full texts and whole supplements into ``local/``.

Per paper, under ``<root>/local/literature/texts/<PMID digits>/``: ``abstract.txt`` (title, year,
publication types, abstract) always; ``fulltext.txt`` and ``raw/fulltext.<ext>`` for the first
full text found, in this order: a supplied PDF (``<root>/local/literature/supplied/<digits>.pdf``)
that converts as a PDF with at least one word, at any length; then Europe PMC's JATS, PMC's
efetch JATS and OpenAlex's free copies, each kept only when ``convert.full_text_verdict``
accepts it.
``supplements/<name>.txt`` (raw bytes in ``raw/supplements/<name>``) for each supplement file
that converted to text; and ``fetch.json``, written last, holding everything the paper's entry
in ``texts.json`` needs.

Supplements come whole, never filtered by gene or sheet, whichever source gave the full text:
Europe PMC's archive when the paper has a PMC id, and, when that gave nothing (no PMC id, no
archive, a failed request or an archive that does not open) and the paper has a DOI, the
supplement links on the DOI's landing page. A supplement that is a zip is opened one level, its
members named ``<zip>__<member>``; a zip inside it is not opened. A file name is made safe for
the disk (``_file_name``). A supplement not read (not text, HTML under a non-HTML name, an
Office or OpenDocument package that is not xlsx or docx, a zip inside a zip, an archive member
skipped by ``convert.unpack``, a damaged archive, a failed request, a link whose URL cannot be
read, a publisher page that could not be read, an earlier file a replacing fetch did not read
again) is an attempt, counted in the paper's ``rejected`` and named, with its outcome, in its
``why`` (``_loss_name``).

A paper with a ``fetch.json`` is not asked about again (PubMed included) unless ``--refresh``
is given or its supplied PDF appeared, changed or disappeared since (``fetch.json`` holds the
PDF's sha256). When a paper is fetched again and its earlier record is usable, the earlier
directory is kept when the step that has less failed (``_keep_reasons``): a full-text attempt
failed and the new fetch has no full text where there was one (not when that text came from a
supplied PDF since changed or gone), or a supplement attempt failed, a supplement file of the
earlier fetch, by name, is missing from the new one, and the new fetch gained nothing (a full
text where there was none, or a supplied PDF that appeared or changed). Every other new fetch
replaces it; one that replaces it though a supplement attempt failed records each earlier file
it did not read again as ``NOT_CARRIED_OVER``, a loss named in ``why``, and prints a line
naming them.

Each request is an entry of the paper's ``attempts`` (``via``, ``url``, ``outcome``), as are a
supplied PDF (its path for a URL) and each member of a supplement zip (the zip's URL and
``#<member>``); PubMed's requests are not attempts. An attempt's URL has its ``api_key``
parameter dropped, so no key reaches a file; a source attempt that sent no request (efetch
refusing an id that is not a PMC id) has the URL ``""``. Outcomes start
``accepted``, ``converted``, ``rejected:`` (a copy or file read and refused; an Office or
OpenDocument package; a zip inside a zip; an archive member ``convert.unpack`` skipped unread,
over a cap, past the total or unreadable; an archive that does not open; a link whose URL cannot
be read), ``failed:``, ``bot check`` or ``not carried over:`` (``NOT_CARRIED_OVER``, a record
written without a request), or state what a source answered (``found, ...``,
``no record``, ``not available (404)``, ``free copies: N``, ``supplement links: N``,
``zip files: N``). A source failing for one paper is an attempt, never fatal; the run stops,
exit 1, when PubMed fails or has no record for a PMID, a ``fetch.json`` is unusable, a supplied
PDF cannot be read or a file cannot be written.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final

from chd_curator import convert, pubmed, sources
from chd_curator.fs import write_bytes_atomically
from chd_curator.pubmed import Fetch
from chd_curator.run import RUN_FORMAT, Attempt, RunPaths
from chd_curator.sources import Get, SourceError

FETCH_FORMAT: Final = 1
CASE_REPORT: Final = "Case Reports"
NAME_LIMIT: Final = 100  # UTF-8 bytes of a supplement file name, before ``.txt``
_HTML_NAMES: Final = (".htm", ".html")
_SOURCES: Final = ("full_text", "supplied_pdf", "abstract", "none")
_VIAS: Final = {
    "full_text": ("europepmc", "pmc", "openalex"),
    "supplied_pdf": ("supplied",),
}
_KEY_PARAM: Final = "api_key="
_RAW_EXTENSIONS: Final = {"jats": "xml", "pdf": "pdf", "html": "html"}
_FETCH_KEYS: Final = frozenset(
    {
        "format",
        "pmid",
        "title",
        "year",
        "doi",
        "types",
        "licence",
        "has_abstract",
        "source",
        "via",
        "supplements",
        "rejected",
        "supplied_sha256",
        "attempts",
    }
)
_ATTEMPT_KEYS: Final = frozenset({"via", "url", "outcome"})
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_UNSAFE: Final = re.compile(r"[\x00-\x1f\x7f/\\]")


class TextsError(Exception):
    """A refusal: ``code`` is the exit code (1 for a failed fetch, an unusable cache or a file
    that cannot be written, 2 for an argument or a file the command was told to use)."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Fetched:
    """What one paper's ``fetch.json`` records."""

    pmid: str  # "PMID:<digits>"
    title: str
    year: str
    doi: str | None
    types: tuple[str, ...]
    licence: str | None
    has_abstract: bool
    source: str  # full_text | supplied_pdf | abstract | none
    via: str | None  # supplied | europepmc | pmc | openalex; None without a full text
    supplements: tuple[str, ...]  # paths relative to the root
    rejected: int  # len(supplement_losses(attempts)), counted when fetched
    supplied_sha256: str | None  # of the supplied PDF found by this fetch, None when none was
    attempts: tuple[Attempt, ...]

    @property
    def digits(self) -> str:
        return self.pmid.removeprefix("PMID:")

    @property
    def needed(self) -> bool:
        return self.source in ("abstract", "none")


def paper_dir(root: Path, digits: str) -> Path:
    return root / "local/literature/texts" / digits


def stage_dir(root: Path, digits: str) -> Path:
    return root / "local/literature/texts" / f"{digits}.tmp"


def supplied_pdf(root: Path, digits: str) -> Path:
    return root / "local/literature/supplied" / f"{digits}.pdf"


def _read_supplied(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise TextsError(f"{path}: cannot be read: {exc}", 1) from None


def supplied_sha256(root: Path, digits: str) -> str | None:
    """The sha256 of the paper's supplied PDF, or None when there is none."""
    path = supplied_pdf(root, digits)
    return hashlib.sha256(_read_supplied(path)).hexdigest() if path.is_file() else None


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def triage_pmids(literature: dict[str, Any], path: Path) -> list[str]:
    """The digits of the papers to fetch, in triage order, each once: every `included` entry and
    every `excluded` one whose reason is `full_text_not_accessible`."""
    triage = literature.get("triage")
    if not isinstance(triage, list):
        raise TextsError(f"{path}: has no triage list", 2)
    found: list[str] = []
    for index, entry in enumerate(triage):
        pmid = entry.get("pmid") if isinstance(entry, dict) else None
        if not isinstance(pmid, str) or not re.fullmatch(r"PMID:[0-9]+", pmid):
            raise TextsError(f"{path}: triage entry number {index + 1} has no PMID:<digits>", 2)
        decision, reason = entry.get("decision"), entry.get("reason")
        if decision == "included" or (
            decision == "excluded" and reason == "full_text_not_accessible"
        ):
            digits = pmid.removeprefix("PMID:")
            if digits not in found:
                found.append(digits)
    return found


# --- the cache ------------------------------------------------------------------------------


def _fetch_json(fetched: Fetched) -> bytes:
    record = {
        "format": FETCH_FORMAT,
        "pmid": fetched.pmid,
        "title": fetched.title,
        "year": fetched.year,
        "doi": fetched.doi,
        "types": list(fetched.types),
        "licence": fetched.licence,
        "has_abstract": fetched.has_abstract,
        "source": fetched.source,
        "via": fetched.via,
        "supplements": list(fetched.supplements),
        "rejected": fetched.rejected,
        "supplied_sha256": fetched.supplied_sha256,
        "attempts": [{"via": a.via, "url": a.url, "outcome": a.outcome} for a in fetched.attempts],
    }
    return (json.dumps(record, sort_keys=True, ensure_ascii=False, indent=1) + "\n").encode()


def _shown(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _check_fetch_record(data: dict[str, Any], digits: str) -> str | None:
    """Why the record cannot be used, naming the key and its value; None when it can."""
    checks: list[tuple[str, Callable[[Any], bool], str]] = [
        ("format", lambda v: type(v) is int and v == FETCH_FORMAT, f"the integer {FETCH_FORMAT}"),
        ("pmid", lambda v: v == f"PMID:{digits}", f"'PMID:{digits}'"),
        ("title", lambda v: isinstance(v, str), "a string"),
        ("year", lambda v: isinstance(v, str) and v.isascii() and v.isdigit(), "a year in digits"),
        ("doi", lambda v: v is None or isinstance(v, str), "a string or null"),
        ("types", _str_list, "a list of strings"),
        ("licence", lambda v: v is None or isinstance(v, str), "a string or null"),
        ("has_abstract", lambda v: type(v) is bool, "true or false"),
        ("source", lambda v: v in _SOURCES, f"one of {', '.join(_SOURCES)}"),
        ("supplements", _str_list, "a list of strings"),
        ("rejected", lambda v: type(v) is int and v >= 0, "a count (an integer, 0 or more)"),
        (
            "supplied_sha256",
            lambda v: v is None or (isinstance(v, str) and _SHA256.fullmatch(v) is not None),
            "a sha256 in lowercase hex, or null",
        ),
    ]
    for key, ok, expected in checks:
        if not ok(data[key]):
            return f"key {key!r} is {_shown(data[key])}, not {expected}"
    source, via = data["source"], data["via"]
    allowed = _VIAS.get(source)
    if (via is not None) if allowed is None else (via not in allowed):
        expected = "null" if allowed is None else "one of " + ", ".join(allowed)
        return f"key 'via' is {_shown(via)}, not {expected} for source {source!r}"
    attempts = data["attempts"]
    if not isinstance(attempts, list):
        return f"key 'attempts' is {_shown(attempts)}, not a list"
    for number, a in enumerate(attempts, 1):
        if not (
            isinstance(a, dict)
            and set(a) == _ATTEMPT_KEYS
            and all(isinstance(v, str) for v in a.values())
        ):
            return (
                f"key 'attempts' entry number {number} is {_shown(a)}, not an object with the "
                "string keys outcome, url, via"
            )
    return None


def load_cached(root: Path, digits: str) -> Fetched | None:
    """The paper's ``fetch.json``, or None when it has none. An unusable one raises
    ``TextsError`` (exit 1) naming the file and ``--refresh``, and: the reading or JSON error's
    text for a file that cannot be read as JSON; the value it holds (its repr, cut to 80
    characters) for one that is not a JSON object; the key for one that lacks a key or has
    another; the key and its value for a value of the wrong type; the key and the file for a
    listed file that is gone."""
    path = paper_dir(root, digits) / "fetch.json"
    if not path.is_file():
        return None

    def refuse(why: str) -> TextsError:
        return TextsError(f"{path}: {why}; run again with --refresh to fetch this paper anew", 1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise refuse(f"cannot be read as JSON ({exc})") from None
    if not isinstance(data, dict):
        raise refuse(f"holds {_shown(data)}, not a JSON object")
    missing, extra = sorted(_FETCH_KEYS - set(data)), sorted(set(data) - _FETCH_KEYS)
    if missing:
        raise refuse(f"lacks the key(s) {', '.join(missing)}")
    if extra:
        raise refuse(f"has the unexpected key(s) {', '.join(extra)}")
    why = _check_fetch_record(data, digits)
    if why is not None:
        raise refuse(why)
    fetched = Fetched(
        pmid=data["pmid"],
        title=data["title"],
        year=data["year"],
        doi=data["doi"],
        types=tuple(data["types"]),
        licence=data["licence"],
        has_abstract=data["has_abstract"],
        source=data["source"],
        via=data["via"],
        supplements=tuple(data["supplements"]),
        rejected=data["rejected"],
        supplied_sha256=data["supplied_sha256"],
        attempts=tuple(Attempt(a["via"], a["url"], a["outcome"]) for a in data["attempts"]),
    )
    listed = [("abstract", paper_dir(root, digits) / "abstract.txt")]
    listed += [("supplements", root / s) for s in fetched.supplements]
    if not fetched.needed:
        listed.append(("source", paper_dir(root, digits) / "fulltext.txt"))
    for key, file in listed:
        if not file.is_file():
            raise refuse(f"key {key!r} needs {file}, which does not exist")
    return fetched


# --- one paper ------------------------------------------------------------------------------


def _without_key(url: str) -> str:
    base, _, query = url.partition("?")
    kept = [p for p in query.split("&") if p and not p.startswith(_KEY_PARAM)]
    return base + ("?" + "&".join(kept) if kept else "")


class _Recorder:
    """Passes requests to the transport and remembers the URL of the last one sent."""

    def __init__(self, get: Get) -> None:
        self._get = get
        self._last = ""
        self._unrecorded = False

    def __call__(self, url: str) -> sources.Response:
        self._last = url
        self._unrecorded = True
        return self._get(url)

    def take(self) -> str:
        """The URL, its ``api_key`` dropped, of the last request sent since the previous
        ``take``; ``""`` when none was sent since."""
        url = _without_key(self._last) if self._unrecorded else ""
        self._unrecorded = False
        return url


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomically(path, payload)


def _abstract_text(title: str, year: int, types: Sequence[str], abstract: str) -> str:
    head = f"{title}\nYear: {year}\nPublication types: {'; '.join(types) or '(none in PubMed)'}\n"
    return head + (f"\n{abstract}\n" if abstract.strip() else "")


def _file_name(name: str, fallback: str) -> str:
    """``name`` made a file name: each control character (NUL among them), ``/`` and backslash
    becomes ``_``; an empty, ``.`` or ``..`` name becomes ``fallback``; a name over
    ``NAME_LIMIT`` UTF-8 bytes is cut there, its extension (at most 10 characters) kept. With
    ``.txt`` and ``write_bytes_atomically``'s temporary suffix, a name stays under 255 bytes."""
    name = _UNSAFE.sub("_", name).strip()
    if name in ("", ".", ".."):
        name = fallback
    if len(name.encode()) <= NAME_LIMIT:
        return name
    extension = PurePosixPath(name).suffix[:10]
    budget = NAME_LIMIT - len(extension.encode())
    return name.encode()[:budget].decode("utf-8", errors="ignore") + extension


def _is_supplement_step(attempt: Attempt) -> bool:
    return attempt.via == "publisher" or (
        attempt.via == "europepmc" and "/supplementaryFiles" in attempt.url
    )


def failed_attempt(attempt: Attempt) -> bool:
    """True when a source could not answer: an outcome ``failed:`` (a request that raised
    ``SourceError``: a host not reached, an error status (a 404 included, except the 404 by
    which Europe PMC's full text and archive, PMC efetch and OpenAlex's work say "none here"),
    an answer that is not what was asked; an id efetch refuses before sending a request (not a
    PMC id); an OpenAlex copy whose URL cannot be read; or any non-200 answer to a request for
    an OpenAlex copy or a publisher supplement file, a 404 included) or ``bot check``. Not for
    "no record", "not available (404)", "free copies: 0" or a rejected copy."""
    return attempt.outcome.startswith(("failed:", "bot check"))


NOT_CARRIED_OVER: Final = (
    "not carried over: the new fetch replaced the earlier one and did not read this file"
)
_LOSS_PREFIXES: Final = ("rejected:", "failed:", "bot check", "not carried over:")


def supplement_losses(attempts: Sequence[Attempt]) -> list[Attempt]:
    """The supplement attempts that lost a file or a list of files: a Europe PMC archive or
    publisher attempt whose outcome is ``rejected:``, ``failed:``, a bot check or
    ``NOT_CARRIED_OVER`` (an earlier file a replacing fetch did not read again)."""
    return [a for a in attempts if _is_supplement_step(a) and a.outcome.startswith(_LOSS_PREFIXES)]


class _Paper:
    """The gathering of one paper: the attempts so far, and the files written."""

    def __init__(self, root: Path, digits: str, get: Get, api_key: str | None) -> None:
        self.root = root
        self.digits = digits
        self.dir = paper_dir(root, digits)  # where the files end up
        self.stage = stage_dir(root, digits)  # where they are written until the paper is done
        self.rec = _Recorder(get)
        self.api_key = api_key
        self.attempts: list[Attempt] = []
        self.full: tuple[str, convert.Converted] | None = None  # (via, text)
        self.supplements: list[str] = []
        self.doi: str | None = None
        self.licence: str | None = None
        self.supplied_sha256: str | None = None

    def note(self, via: str, outcome: str, url: str | None = None) -> None:
        """Record an attempt; without ``url``, the URL of the request sent since the last
        attempt, or ``""`` when none was."""
        sent = self.rec.take()
        self.attempts.append(Attempt(via, sent if url is None else url, outcome))

    def failed(self, via: str, exc: Exception, url: str | None = None) -> None:
        self.note(via, f"failed: {exc}", url)

    def keep(self, via: str, converted: convert.Converted, data: bytes, url: str | None) -> None:
        self.note(via, f"accepted, {converted.words} words", url)
        self.full = (via, converted)
        extension = _RAW_EXTENSIONS.get(converted.kind, "bin")
        _write(self.stage / "raw" / f"fulltext.{extension}", data)
        _write(self.stage / "fulltext.txt", converted.text.encode("utf-8"))

    def judge(self, via: str, name: str, data: bytes, url: str | None = None) -> bool:
        """Convert `data` and keep it as the full text when the verdict accepts it."""
        try:
            converted = convert.convert(name, data)
        except convert.NotText as exc:
            self.note(via, f"rejected: {exc}", url)
            return False
        reason = convert.full_text_verdict(converted)
        if reason is not None:
            self.note(via, f"rejected: {reason}", url)
            return False
        self.keep(via, converted, data, url)
        return True

    def supplied(self, path: Path) -> None:
        """The curator's PDF: kept whenever it converts to text as a PDF, at any length."""
        data = _read_supplied(path)
        self.supplied_sha256 = hashlib.sha256(data).hexdigest()
        url = _rel(self.root, path)
        try:
            converted = convert.convert(path.name, data)
        except convert.NotText as exc:
            self.note("supplied", f"rejected: {exc}", url)
            return
        if converted.kind != "pdf":
            self.note("supplied", f"rejected: a {converted.kind} file, not a PDF", url)
            return
        if converted.words == 0:
            self.note("supplied", "rejected: a PDF with no text (0 words), scanned pages", url)
            return
        self.keep("supplied", converted, data, url)

    def _unused(self, name: str) -> str:
        candidate, number = name, len(self.supplements) + 1
        while (self.stage / "raw" / "supplements" / candidate).exists():
            candidate = _file_name(f"{number}__{name}", name)
            number += 1
        return candidate

    def supplement(self, via: str, name: str, data: bytes, url: str, in_zip: bool = False) -> None:
        """Convert one supplement file and keep it, or record it as not read.

        A zip that is not an xlsx or docx is opened (``archive``) unless it was itself a zip's
        member (``in_zip``). A file converting to HTML is rejected unless its name ends
        ``.htm`` or ``.html``: a login or bot-check page served for a workbook's URL is not the
        workbook.
        """
        name = _file_name(name, "supplement")
        if convert.is_archive(data):
            if in_zip:
                self.note(via, "rejected: a zip inside a zip, not opened", url)
            else:
                self.archive(via, data, url, prefix=f"{name}__", in_zip=True)
            return
        try:
            converted = convert.convert(name, data)
        except convert.NotText as exc:
            self.note(via, f"rejected: {exc}", url)
            return
        if converted.kind == "html" and not name.lower().endswith(_HTML_NAMES):
            self.note(via, f"rejected: an HTML page served for {name}", url)
            return
        name = self._unused(name)
        _write(self.stage / "raw" / "supplements" / name, data)
        _write(self.stage / "supplements" / f"{name}.txt", converted.text.encode("utf-8"))
        self.supplements.append(_rel(self.root, self.dir / "supplements" / f"{name}.txt"))
        self.note(via, f"converted, {converted.words} words", url)

    def archive(self, via: str, data: bytes, url: str, prefix: str, in_zip: bool) -> bool:
        """Open a zip and take each member as a supplement named ``<prefix><member>``, recording
        each member ``convert.unpack`` skipped as rejected; False when the zip does not open
        (recorded as rejected)."""
        try:
            unpacked = convert.unpack(data)
        except convert.NotText as exc:
            self.note(via, f"rejected: {exc}", url)
            return False
        self.note(via, f"zip files: {len(unpacked.files)}", url)
        for member, payload in unpacked.files:
            self.supplement(via, f"{prefix}{member}", payload, f"{url}#{member}", in_zip)
        for member, reason in unpacked.skipped:
            self.note(via, f"rejected: {reason}", f"{url}#{member}")
        return True

    def europepmc_supplements(self, pmcid: str) -> bool:
        """Europe PMC's archive of the paper's supplements; False when it gave nothing (a
        failed request, a 404 or an archive that does not open)."""
        try:
            zipped = sources.europepmc_supplements(pmcid, self.rec)
        except SourceError as exc:
            self.failed("europepmc", exc)
            return False
        zip_url = self.rec.take()
        if zipped is None:
            self.note("europepmc", "not available (404)", zip_url)
            return False
        return self.archive("europepmc", zipped, zip_url, prefix="", in_zip=False)

    def publisher_supplements(self, doi: str) -> None:
        try:
            found = sources.publisher_supplements(doi, self.rec)
        except SourceError as exc:
            self.failed("publisher", exc)
            return
        if isinstance(found, sources.BotCheck):
            self.note("publisher", f"bot check at {found.host}: {found.why}")
            return
        self.note("publisher", f"supplement links: {len(found)}")
        for position, url in enumerate(found, 1):
            if not sources.readable_url(url):
                self.note("publisher", "rejected: a link whose URL cannot be read", url)
                continue
            try:
                response = self.rec(url)
            except SourceError as exc:
                self.failed("publisher", exc)
                continue
            if response.status != 200:
                self.note("publisher", f"failed: HTTP {response.status}")
                continue
            name = sources.supplement_name(url) or f"supplement-{position}"
            self.supplement("publisher", name, response.body, url)

    def openalex(self, pmid: str) -> str | None:
        """Try OpenAlex's free copies in order until one is accepted; its licence, if any."""
        try:
            copies = sources.openalex_free_copies(pmid, self.rec)
        except SourceError as exc:
            self.failed("openalex", exc)
            return None
        self.note("openalex", f"free copies: {len(copies)}")
        for copy in copies:
            if not sources.readable_url(copy.url):
                self.note("openalex", "failed: a URL that cannot be read", copy.url)
                continue
            try:
                response = self.rec(copy.url)
            except SourceError as exc:
                self.failed("openalex", exc, copy.url)
                continue
            if response.status != 200:
                self.note("openalex", f"failed: HTTP {response.status}", copy.url)
                continue
            name = _file_name(PurePosixPath(copy.url.split("?")[0]).name, "copy")
            if self.judge("openalex", name, response.body, copy.url):
                return copy.licence
        return None

    def gather(self, pmid: str, doi: str | None, pubmed_pmcid: str | None) -> None:
        """Fetch what the sources hold.

        Sets ``doi`` (PubMed's, else Europe PMC's) and ``licence``: that stated by the source
        whose copy became the full text (Europe PMC's record for a Europe PMC or PMC copy, the
        copy's own for OpenAlex, none for a supplied PDF), and Europe PMC's when there is no
        full text.
        """
        self.doi = doi
        supplied = supplied_pdf(self.root, self.digits)
        if supplied.is_file():
            self.supplied(supplied)
        pmcid = pubmed_pmcid
        epmc_licence: str | None = None
        try:
            record = sources.europepmc_record(pmid, self.rec)
        except SourceError as exc:
            record = None
            self.failed("europepmc", exc)
        else:
            if record is None:
                self.note("europepmc", "no record")
            else:
                epmc_licence = record.licence
                pmcid = record.pmcid or pmcid
                self.doi = doi = doi or record.doi
                access = "open access" if record.open_access else "not open access"
                self.note("europepmc", f"found, {record.pmcid or 'no PMC id'}, {access}")
        if record is not None and record.pmcid and record.open_access and self.full is None:
            try:
                data = sources.europepmc_fulltext(record.pmcid, self.rec)
            except SourceError as exc:
                self.failed("europepmc", exc)
            else:
                if data is None:
                    self.note("europepmc", "not available (404)")
                else:
                    self.judge("europepmc", "fulltext.xml", data)
        if pmcid and self.full is None:
            try:
                data = sources.pmc_efetch(pmcid, self.rec, self.api_key)
            except SourceError as exc:
                self.failed("pmc", exc)
            else:
                if data is None:
                    self.note("pmc", "not available (404)")
                else:
                    self.judge("pmc", "fulltext.xml", data)
        openalex_licence: str | None = None
        if self.full is None:
            openalex_licence = self.openalex(pmid)
        archive = self.europepmc_supplements(pmcid) if pmcid else False
        if not archive and doi:
            self.publisher_supplements(doi)
        via = self.full[0] if self.full is not None else None
        if via == "openalex":
            self.licence = openalex_licence
        elif via == "supplied":
            self.licence = None
        else:
            self.licence = epmc_licence


def _gains(earlier: Fetched, new: Fetched) -> list[str]:
    """What the new fetch gained: a full text where the earlier had none, or a supplied PDF
    that appeared or changed (whether or not it converted)."""
    gains: list[str] = []
    if earlier.needed and not new.needed:
        gains.append("a full text where there was none")
    if new.supplied_sha256 is not None and new.supplied_sha256 != earlier.supplied_sha256:
        gains.append("a new or changed supplied PDF")
    return gains


def _missing(earlier: Fetched, new: Fetched) -> list[str]:
    """The earlier fetch's supplement paths whose file name the new fetch does not have."""
    have = {PurePosixPath(s).name for s in new.supplements}
    return [s for s in earlier.supplements if PurePosixPath(s).name not in have]


def _shown_names(paths: Sequence[str]) -> str:
    return ", ".join(PurePosixPath(s).name.removesuffix(".txt") for s in paths)


def _keep_reasons(earlier: Fetched, new: Fetched) -> tuple[list[str], list[Attempt]]:
    """Why the earlier directory is kept, step by step, and the failed attempts behind it.

    The full-text step: a full-text attempt failed (``failed_attempt``, not a supplement
    step) and the new fetch has no full text where the earlier had one, unless that text came
    from a supplied PDF since changed or gone. The supplement step: a supplement-step attempt
    failed (Europe PMC's archive request, the publisher page, a publisher download), a
    supplement file of the earlier fetch, by name, is missing from the new one, and the new
    fetch gained nothing (``_gains``). Empty lists when neither holds.
    """
    reasons: list[str] = []
    behind: list[Attempt] = []
    failed = [a for a in new.attempts if failed_attempt(a)]
    full_failed = [a for a in failed if not _is_supplement_step(a)]
    supplied_gone = earlier.via == "supplied" and earlier.supplied_sha256 != new.supplied_sha256
    if full_failed and not earlier.needed and new.needed and not supplied_gone:
        reasons.append("no full text")
        behind += full_failed
    supplement_failed = [a for a in failed if _is_supplement_step(a)]
    missing = _missing(earlier, new)
    if supplement_failed and missing and not _gains(earlier, new):
        reasons.append("no " + _shown_names(missing))
        behind += supplement_failed
    return reasons, [a for a in new.attempts if a in behind]


def _not_carried_over(earlier: Fetched, new: Fetched) -> list[Attempt]:
    """One ``NOT_CARRIED_OVER`` attempt for each earlier supplement file the new fetch lacks
    and made no attempt on, with the via and URL of the earlier attempt that converted it (the
    earlier record's ``converted`` attempts follow its supplements in order; the file's path
    stands in for the URL when they do not pair up)."""
    converted = [a for a in earlier.attempts if a.outcome.startswith("converted")]
    if len(converted) == len(earlier.supplements):
        source = dict(zip(earlier.supplements, converted, strict=True))
    else:
        source = {s: Attempt("publisher", s, "") for s in earlier.supplements}
    tried = {a.url for a in new.attempts}
    return [
        Attempt(source[s].via, source[s].url, NOT_CARRIED_OVER)
        for s in _missing(earlier, new)
        if source[s].url not in tried
    ]


def fetch_paper(
    root: Path,
    digits: str,
    meta: pubmed.PaperMeta,
    found: tuple[str, str, tuple[str, ...]],
    get: Get,
    api_key: str | None,
    earlier: Fetched | None = None,
) -> tuple[Fetched, str | None]:
    """Gather one paper into ``<digits>.tmp`` beside its directory, ``fetch.json`` last, then
    swap that in for the directory. A fetch that raises leaves the old directory as it was.

    ``earlier`` is the paper's previous record, when there is one. The old directory is kept,
    and the result is ``(earlier, <notice>)``, when ``_keep_reasons`` gives a reason. When the
    new fetch replaces the earlier one though a supplement step failed and an earlier
    supplement file is missing (it gained something, ``_gains``), the missing files it made no
    attempt on are recorded (``_not_carried_over``) and the result is ``(new, <notice naming
    every missing file>)``; otherwise ``(new, None)``.
    """
    directory, stage = paper_dir(root, digits), stage_dir(root, digits)
    shutil.rmtree(stage, ignore_errors=True)
    try:
        fetched = _fetch_into_stage(root, digits, meta, found, get, api_key)
        reasons, behind = _keep_reasons(earlier, fetched) if earlier is not None else ([], [])
        if earlier is not None and reasons:
            shutil.rmtree(stage, ignore_errors=True)
            kept = (
                f"PMID:{digits}: kept the earlier texts, the new fetch has "
                f"{' and '.join(reasons)} ({_attempts_text(behind)})"
            )
            return earlier, kept
        notice: str | None = None
        failed = [a for a in fetched.attempts if failed_attempt(a) and _is_supplement_step(a)]
        if earlier is not None and failed and _missing(earlier, fetched):
            attempts = (*fetched.attempts, *_not_carried_over(earlier, fetched))
            fetched = replace(fetched, attempts=attempts, rejected=len(supplement_losses(attempts)))
            notice = (
                f"PMID:{digits}: replaced the earlier texts, the new fetch has "
                f"{' and '.join(_gains(earlier, fetched))}; not carried over: "
                f"{_shown_names(_missing(earlier, fetched))} ({_attempts_text(failed)})"
            )
        _write(stage / "fetch.json", _fetch_json(fetched))
        shutil.rmtree(directory, ignore_errors=True)
        stage.replace(directory)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return fetched, notice


def _fetch_into_stage(
    root: Path,
    digits: str,
    meta: pubmed.PaperMeta,
    found: tuple[str, str, tuple[str, ...]],
    get: Get,
    api_key: str | None,
) -> Fetched:
    _, abstract, types = found
    _write(
        stage_dir(root, digits) / "abstract.txt",
        _abstract_text(meta.title, meta.year, types, abstract).encode("utf-8"),
    )
    paper = _Paper(root, digits, get, api_key)
    paper.gather(digits, meta.doi, meta.pmcid)
    has_abstract = bool(abstract.strip())
    via: str | None = None
    if paper.full is not None:
        via = paper.full[0]
        source = "supplied_pdf" if via == "supplied" else "full_text"
    else:
        source = "abstract" if has_abstract else "none"
    return Fetched(
        pmid=f"PMID:{digits}",
        title=meta.title,
        year=str(meta.year),
        doi=paper.doi,
        types=types,
        licence=paper.licence,
        has_abstract=has_abstract,
        source=source,
        via=via,
        supplements=tuple(paper.supplements),
        rejected=len(supplement_losses(paper.attempts)),
        supplied_sha256=paper.supplied_sha256,
        attempts=tuple(paper.attempts),
    )


# --- the run --------------------------------------------------------------------------------


def gather_papers(
    root: Path,
    digits_list: Sequence[str],
    fetch: Fetch,
    get: Get,
    api_key: str | None,
    refresh: bool,
) -> tuple[list[Fetched], list[str]]:
    """Every paper's record, from its ``fetch.json`` or fetched anew, and one notice line for
    each paper whose earlier directory was kept.

    A paper is fetched anew with ``refresh``, when it has no ``fetch.json``, or when its
    supplied PDF's sha256 is not the one its ``fetch.json`` holds (one appeared, changed or
    disappeared). PubMed is asked about those papers first, before any other request: efetch
    for the abstracts, then esummary, each in batches of ``pubmed.BATCH_SIZE``, so a PubMed
    failure writes nothing. A paper fetched anew whose earlier record exists keeps its earlier
    directory as ``fetch_paper`` rules; the notice names the paper, what the new fetch lacked
    and the failed attempts.
    """
    cached: dict[str, Fetched] = {}
    earlier: dict[str, Fetched | None] = {}
    for digits in digits_list:
        if refresh:
            try:
                earlier[digits] = load_cached(root, digits)
            except TextsError:
                earlier[digits] = None  # an unusable old record is what a refresh replaces
            continue
        hit = load_cached(root, digits)
        if hit is not None and hit.supplied_sha256 == supplied_sha256(root, digits):
            cached[digits] = hit
        else:
            earlier[digits] = hit
    need = [d for d in digits_list if d not in cached]
    try:
        abstracts = pubmed.efetch_abstracts(need, fetch, api_key=api_key)
        metas = pubmed.esummary(need, fetch, api_key=api_key)
    except (pubmed.PubMedError, OSError) as exc:
        message = str(exc)
        raise TextsError(message.replace(api_key, "***") if api_key else message, 1) from None
    for digits in need:
        if digits not in abstracts or digits not in metas:
            raise TextsError(f"PubMed returned no record for PMID:{digits}; nothing was fetched", 1)
    done: list[Fetched] = []
    notices: list[str] = []
    for digits in digits_list:
        if digits in cached:
            done.append(cached[digits])
            continue
        try:
            fetched, notice = fetch_paper(
                root, digits, metas[digits], abstracts[digits], get, api_key, earlier[digits]
            )
        except OSError as exc:
            raise TextsError(f"{paper_dir(root, digits)}: cannot be written: {exc}", 1) from None
        done.append(fetched)
        if notice:
            notices.append(notice)
    return done, notices


def order(papers: Sequence[Fetched]) -> list[Fetched]:
    """Needed papers first: those not a case report, then newest year, then PMID; the others
    by PMID."""
    needed = sorted(
        (p for p in papers if p.needed),
        key=lambda p: (CASE_REPORT in p.types, -int(p.year), int(p.digits)),
    )
    rest = sorted((p for p in papers if not p.needed), key=lambda p: int(p.digits))
    return needed + rest


def _attempts_text(attempts: Sequence[Attempt]) -> str:
    return "; ".join(f"{a.via} {a.outcome}" for a in attempts)


def _loss_name(attempt: Attempt, page: bool) -> str:
    """What a supplement loss lost: ``the publisher page`` when ``page`` (the attempt is the
    paper's request for its DOI page); an archive member by its name (``S1.zip/T.xlsx`` for a
    member of a zip inside); a publisher link by its file name (``S1.zip/T.xlsx`` for a member
    of a linked zip); else ``Europe PMC's archive``."""
    base, _, inner = attempt.url.partition("#")
    if attempt.via == "europepmc":
        return inner.replace("#", "/") if inner else "Europe PMC's archive"
    if page:
        return "the publisher page"
    link = sources.supplement_name(base) or base
    return f"{link}/{inner.replace('#', '/')}" if inner else link


def _page_attempt(attempts: Sequence[Attempt]) -> int | None:
    """The index of the paper's DOI-page attempt: its first publisher attempt (the page is
    asked before any link it lists) that is not a ``NOT_CARRIED_OVER`` record."""
    return next(
        (
            i
            for i, a in enumerate(attempts)
            if a.via == "publisher" and a.outcome != NOT_CARRIED_OVER
        ),
        None,
    )


def _why(paper: Fetched) -> str:
    """For a needed paper, what the full-text sources answered and its PubMed types; for every
    paper with a supplement not read, each one named (``_loss_name``) with its outcome
    (``supplement_losses``); else ``""``."""
    parts: list[str] = []
    if paper.needed:
        facts = _attempts_text([a for a in paper.attempts if not _is_supplement_step(a)])
        types = "; ".join(paper.types) or "(none in PubMed)"
        parts.append(
            f"no free full text found ({facts or 'no source asked'}); PubMed types: {types}"
        )
    page = _page_attempt(paper.attempts)
    lost = [
        f"{_loss_name(a, i == page)}: {a.via} {a.outcome}"
        for i, a in enumerate(paper.attempts)
        if supplement_losses([a])
    ]
    if lost:
        parts.append(f"supplements not read ({'; '.join(lost)})")
    return "; ".join(parts)


def texts_document(root: Path, papers: Sequence[Fetched]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for p in order(papers):
        directory = paper_dir(root, p.digits)
        entries.append(
            {
                "pmid": p.pmid,
                "source": p.source,
                "text": None if p.needed else _rel(root, directory / "fulltext.txt"),
                "abstract": _rel(root, directory / "abstract.txt"),
                "supplements": list(p.supplements),
                "needed": p.needed,
                "why": _why(p),
                "title": p.title,
                "year": p.year,
                "doi": p.doi or "",
                "licence": p.licence,
                "attempts": [
                    {"via": a.via, "url": a.url, "outcome": a.outcome} for a in p.attempts
                ],
            }
        )
    return {"format": RUN_FORMAT, "papers": entries}


def write_texts(run: RunPaths, papers: Sequence[Fetched]) -> None:
    run.dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        texts_document(run.root, papers), sort_keys=True, ensure_ascii=False, indent=1
    )
    write_bytes_atomically(run.texts, (text + "\n").encode("utf-8"))


def summary(papers: Sequence[Fetched]) -> str:
    full = [p for p in papers if not p.needed]
    by_via = {
        v: sum(1 for p in full if p.via == v) for v in ("europepmc", "pmc", "openalex", "supplied")
    }
    return (
        f"{len(papers)} papers: {len(full)} full text "
        f"({by_via['europepmc']} Europe PMC, {by_via['pmc']} PMC, {by_via['openalex']} OpenAlex, "
        f"{by_via['supplied']} supplied), "
        f"{sum(1 for p in papers if p.source == 'abstract')} abstract only, "
        f"{sum(1 for p in papers if p.source == 'none')} neither; "
        f"{sum(len(p.supplements) for p in papers)} supplement files converted, "
        f"{sum(p.rejected for p in papers)} rejected; "
        f"{sum(1 for p in papers if p.needed)} needed"
    )
