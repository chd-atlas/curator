"""A review page's store export, turned into a format 1 decisions file.

When the page runs as a claude.ai artifact it saves each card to the page's store, and
Claude exports that store (ArtifactData ``out_dir``) to ``<dir>/review/<doc id>.json`` and
``<dir>/session/main.json``, each file holding only the document's fields. This module
reads exactly what the page writes (``templates/review.html``: ``docBody``, ``setFlips``
and ``finish``) and builds the file the atlas's ``literature apply-review`` applies, so no
decision is ever retyped.

Provenance (``page``) and the reviewer come from the page's own inline data, not from the
export. The export must say it belongs to that page (R-P1-36): the page writes its
``literature_sha256``, ``built_on`` and the reviewer into ``session/main``, and an export whose
identity is missing or differs, or whose SYMBOL is not the page's, is refused. A flip must name a
paper of the page's triage and move it to the opposite decision, and a ``papers_seen`` entry must
be a paper the page asked to be read. A decided card's ``shown_digest`` must equal the page's
for that card: the page stores the card's digest with each decision and shows an older one as
"decided on an earlier version". Such a card is LEFT OUT of the decisions file and named in
:attr:`Converted.stale`, as the page's own download leaves it out (R-P1-44). A decision stamped
after ``finished_at`` is refused: it was changed after "I'm done". Notes are copied untouched.

Exit codes at the CLI: :class:`ExportError` carries ``exit_code`` 1 when the review is not
finished (or changed after it was) and 2 for everything else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from chd_curator.atlas import schema_validator

_DATA_OPEN: Final = '<script type="application/json" id="data">'
_DECISIONS: Final = ("approve", "fix", "later")


class ExportError(Exception):
    """The export or the page cannot become a decisions file; ``exit_code`` is the CLI's."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def page_data_from_text(text: str, label: str) -> dict[str, Any]:
    """The inline data of page text (``label`` names the page in each refusal)."""
    if text.count(_DATA_OPEN) != 1:
        raise ExportError(
            f"{label}: expected exactly one data element, found {text.count(_DATA_OPEN)}"
        )
    start = text.index(_DATA_OPEN) + len(_DATA_OPEN)
    end = text.find("</script>", start)
    if end < 0:
        raise ExportError(f"{label}: the data element is not closed")
    try:
        data = json.loads(text[start:end].replace("<\\/", "</"))
    except ValueError as exc:
        raise ExportError(f"{label}: the data element is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExportError(f"{label}: the data element is not an object")
    return data


def page_data_from_bytes(raw: bytes, label: str) -> dict[str, Any]:
    """The inline data of a page held as bytes (UTF-8), as :func:`page_data` reads a file."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExportError(f"{label}: cannot be read as UTF-8: {exc}") from exc
    return page_data_from_text(text, label)


def page_data(page_html: Path) -> dict[str, Any]:
    """The page's inline data, from its one ``<script ... id="data">`` element."""
    try:
        text = page_html.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"{page_html}: cannot be read: {exc}") from exc
    return page_data_from_text(text, str(page_html))


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"{path}: cannot be read as JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ExportError(f"{path}: is not an object")
    return doc


def _decision(doc: dict[str, Any], what: str) -> str | None:
    value = doc.get("decision")
    if value is None:
        return None
    if value not in _DECISIONS:
        raise ExportError(f"{what}: decision {value!r} is not one of {'|'.join(_DECISIONS)}")
    return str(value)


@dataclass(frozen=True)
class Converted:
    """The decisions file, the cards left out as stale, and how many cards the page holds."""

    decisions: dict[str, Any]
    stale: tuple[str, ...]
    cards: int


def _when(value: Any, what: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ExportError(f"{what} is not a date-time: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ExportError(f"{what} carries no UTC offset: {value!r}")
    return parsed


def decisions_from_export(
    export_dir: Path, page_html: Path, schema_root: Path, symbol: str
) -> Converted:
    """The decisions file for ``export_dir``; ``schema_root`` is the atlas checkout.

    ``symbol`` is the gene the command was asked about; it must be the page's.
    """
    data = page_data(page_html)
    try:
        cards = {c["id"]: c["shown_digest"] for c in data["findings"]}
        provenance = {k: data["page"][k] for k in ("built_on", "atlas_commit", "literature_sha256")}
        gene, page_symbol = data["page"]["gene"], data["page"]["symbol"]
        reviewer = {"name": data["reviewer"]["name"], "role": data["reviewer"]["role"]}
        triage_now = {str(t["pmid"]): str(t["decision"]) for t in data["triage"]}
        needed = {str(p["pmid"]) for p in data["papers_needed"]}
    except (KeyError, TypeError) as exc:
        raise ExportError(f"{page_html}: the page's data lacks {exc}") from exc
    if symbol != page_symbol:
        raise ExportError(f"{page_html} is the page for {page_symbol}, not {symbol}")
    if not export_dir.is_dir():
        raise ExportError(f"--export {export_dir} is not a directory")

    session_path = export_dir / "session" / "main.json"
    if not session_path.is_file():
        raise ExportError(f"no session/main.json in {export_dir}", 1)
    session = _read(session_path)
    if not session.get("finished_at"):
        raise ExportError("the reviewer has not pressed I'm done", 1)
    expected = {
        "page": {
            "literature_sha256": provenance["literature_sha256"],
            "built_on": provenance["built_on"],
        },
        "reviewer": reviewer,
    }
    for key, want in expected.items():
        if session.get(key) != want:
            raise ExportError(
                f"the export is not from this page: session/main's {key} is {session.get(key)!r}, "
                f"the page's is {want!r}"
            )
    finished = _when(session["finished_at"], "session/main's finished_at")

    docs: dict[str, dict[str, Any]] = {}
    review_dir = export_dir / "review"
    for path in sorted(review_dir.iterdir()) if review_dir.is_dir() else []:
        if path.suffix != ".json":
            raise ExportError(f"{path}: not a .json document of the page's store")
        docs[path.stem] = _read(path)
    foreign = sorted(i for i in docs if i != "triage" and i not in cards)
    if foreign:
        raise ExportError(f"the export holds {', '.join(foreign)}, which are not on the page")

    findings: list[dict[str, Any]] = []
    stale: list[str] = []

    def after_done(doc: dict[str, Any], what: str) -> None:
        if _when(doc.get("decided_at"), f"{what}'s decided_at") > finished:
            raise ExportError(f"{what}: a choice changed after I'm done: press I'm done again", 1)

    for card_id, shown in cards.items():
        doc = docs.get(card_id)
        if doc is None or _decision(doc, card_id) is None:
            continue
        if doc.get("shown_digest") != shown:
            stale.append(card_id)
            continue
        after_done(doc, card_id)
        findings.append(
            {
                "id": card_id,
                "shown_digest": doc["shown_digest"],
                "decision": doc["decision"],
                "note": doc.get("note", ""),
                "active_ms": doc.get("active_ms"),
                "first_opened_at": doc.get("first_opened_at"),
                "decided_at": doc.get("decided_at"),
            }
        )
    triage_doc = docs.get("triage", {})
    for flip in triage_doc.get("flips", []):
        pmid = flip.get("pmid") if isinstance(flip, dict) else None
        if pmid not in triage_now:
            raise ExportError(
                f"the export flips {pmid}, which is not a paper of this page's triage"
            )
        opposite = "excluded" if triage_now[pmid] == "included" else "included"
        if flip.get("to") != opposite:
            raise ExportError(
                f"the export flips {pmid} to {flip.get('to')!r}, but the page has it "
                f"{triage_now[pmid]} and a flip moves it to {opposite}"
            )
    unknown_seen = sorted(set(triage_doc.get("papers_seen", [])) - needed)
    if unknown_seen:
        raise ExportError(
            f"the export lists {', '.join(map(str, unknown_seen))} as seen, which this page "
            "does not ask to be read"
        )
    triage: dict[str, Any] | None = None
    if _decision(triage_doc, "triage") is not None:
        after_done(triage_doc, "triage")
        triage = {
            "decision": triage_doc["decision"],
            "note": triage_doc.get("note", ""),
            "active_ms": triage_doc.get("active_ms"),
            "flips": [
                {"pmid": f.get("pmid"), "to": f.get("to"), "note": f.get("note")}
                for f in triage_doc.get("flips", [])
            ],
        }
    result: dict[str, Any] = {
        "format": 1,
        "gene": gene,
        "symbol": symbol,
        "page": provenance,
        "reviewer": reviewer,
        "session": {
            "started_at": session.get("started_at"),
            "finished_at": session["finished_at"],
            "active_ms": session.get("total_active_ms"),
        },
        "findings": findings,
        "papers_seen": sorted(set(triage_doc.get("papers_seen", []))),
        "triage": triage,
    }
    errors = sorted(
        f"{'/'.join(map(str, e.path)) or '(file)'}: {e.message}"
        for e in schema_validator(schema_root, "review_decisions").iter_errors(result)
    )
    if errors:
        raise ExportError(f"the decisions file fails its schema: {errors[0]}")
    return Converted(result, tuple(stale), len(cards))
