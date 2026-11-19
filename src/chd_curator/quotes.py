"""Is each quote a review page shows in the paper it came from?

A quote is compared with the text of its own finding's paper (full text, abstract and every
supplement), after both sides are normalised over typography: case, whitespace, curly
quotes, dashes (U+2010 included), ligatures, and the ``|`` and ``---row`` marks a flattened
table leaves behind. A quote that does not match is reported ``found: False`` and the page
says "not found in text". A quote whose finding has no source text (no paper in texts.json, or
a paper with no text, abstract or supplement) was not checked: it carries ``checked: False`` and
``found: False``, and the page must not present it as a miss.

Adapted from the pilot's ``quotecheck.py`` (19 lines). Differences: U+2010 is mapped, and the
word count is kept for the page's long-quote flag. Whether the normalisation is complete is
not measured beyond the fixture and the parametrised test: a typographic variant outside the
list shows as "not found", which fails safe.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator.fs import write_bytes_atomically
from chd_curator.run import (
    RunFormatError,
    RunPaths,
    load_evidence,
    load_judge,
    load_texts,
)

QUOTES_FORMAT: Final = 1
LONG_QUOTE_WORDS: Final = 30

_TRANSLATION: Final = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "‐": "-",
        " ": " ",
        " ": " ",
        " ": " ",
        "∗": "*",
        "×": "x",
    }
)
_ELLIPSIS_MARK: Final = "\x00"


def normalise_text(text: str) -> str:
    """The comparison form of ``text``; both quote and source go through it."""
    s = unicodedata.normalize("NFKC", text).translate(_TRANSLATION)
    s = re.sub(r"\s*\|\s*", " ", s)
    s = s.replace("---row", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_found(quote: str, normalised_sources: Sequence[str]) -> bool:
    """True when ``quote`` occurs in one of the (already normalised) sources.

    ``...`` or ``…`` split the quote into parts; every part must occur in the same source.
    A quote with no text left after stripping is never found.
    """
    q = normalise_text(quote).strip(" .\"'…")
    q = q.replace("...", _ELLIPSIS_MARK).replace("…", _ELLIPSIS_MARK)
    parts = [p.strip() for p in q.split(_ELLIPSIS_MARK) if p.strip()]
    return bool(parts) and any(all(p in s for p in parts) for s in normalised_sources)


@dataclass(frozen=True)
class QuoteCheck:
    finding: str
    origin: Literal["evidence", "judge"]
    field: str
    quote: str
    where: str
    found: bool
    checked: bool
    paper: str
    words: int


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunFormatError(
            f"{path}: a source text texts.json lists cannot be read: {exc}"
        ) from exc


def _papers_by_finding(run: RunPaths) -> dict[str, str]:
    path = run.root / "curation/literature" / f"{run.symbol}.yaml"
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise RunFormatError(f"{path}: cannot be read as YAML: {exc}") from exc
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        raise RunFormatError(f"{path}: has no list of findings")
    return {
        str(f["id"]): str(f["publication"])
        for f in findings
        if isinstance(f, dict) and "id" in f and "publication" in f
    }


def check_quotes(run: RunPaths) -> tuple[QuoteCheck, ...]:
    """Check every evidence and judge quote; order: finding id, origin, file order."""
    evidence, judge = load_evidence(run), load_judge(run)
    paper_of = _papers_by_finding(run)
    papers = {paper.pmid: paper for paper in load_texts(run)}
    cache: dict[str, tuple[str, ...]] = {}

    def sources(finding: str) -> tuple[str, ...]:
        pmid = paper_of.get(finding)
        paper = papers.get(pmid) if pmid is not None else None
        if paper is None or pmid is None:
            return ()
        if pmid not in cache:
            files = [paper.text, paper.abstract, *paper.supplements]
            cache[pmid] = tuple(normalise_text(_read(f)) for f in files if f is not None)
        return cache[pmid]

    checks: list[QuoteCheck] = []
    for finding in sorted(set(evidence) | set(judge)):
        known = sources(finding)
        rows: list[tuple[Literal["evidence", "judge"], str, str, str]] = []
        for q in evidence.get(finding, ()):
            rows.append(("evidence", q.field, q.quote, q.where))
        for d in judge.get(finding, ()):
            rows.append(("judge", d.field, d.quote, d.where))
        for origin, field, quote, where in rows:
            checks.append(
                QuoteCheck(
                    finding,
                    origin,
                    field,
                    quote,
                    where,
                    quote_found(quote, known),
                    bool(known),
                    paper_of.get(finding, finding),
                    len(quote.split()),
                )
            )
    return tuple(checks)


def write_quotes(run: RunPaths, checks: Sequence[QuoteCheck]) -> None:
    payload = {"format": QUOTES_FORMAT, "checks": [asdict(c) for c in checks]}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    write_bytes_atomically(run.quotes, text.encode("utf-8"))
