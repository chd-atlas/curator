"""The one reader of the review archive: each reviewer's latest verdicts, and what is owed.

:func:`read_archive` reads every ``<SYMBOL>/<run>/decisions/*.json`` in sorted order and
refuses the archive (:class:`~chd_curator.archive.ArchiveError`, exit 2) when it cannot be
trusted: uncommitted changes anywhere in the checkout, a file that is not ``.json`` under a
decisions directory (a skipped file would silently drop a reviewer's verdicts), a file that
fails the atlas's schema, is not finished, is for another symbol, names another gene than
the rest, or sits under a name other than ``<finished_at, compact UTC>--<reviewer slug>.json``.

A reviewer is keyed by role: every owner file is the key ``owner``, and a clinician is
``name.strip().casefold()``. "Latest" compares parsed instants, never strings.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from chd_curator.archive import (
    ArchiveError,
    GitError,
    _git,
    check_archive,
    check_id,
    compact_utc,
)
from chd_curator.atlas import AtlasError, schema_validator
from chd_curator.review_page import slug

STATE_FORMAT = 1


@dataclass(frozen=True)
class Verdict:
    name: str  # as written in its file
    key: str  # "owner", or name.strip().casefold()
    role: Literal["owner", "second_opinion"]
    finding: str  # a finding id, or "triage"
    decision: Literal["approve", "fix", "later"]
    note: str
    shown_digest: str | None  # None for triage
    finished_at: datetime
    decided_at: datetime | None  # None for triage
    flips: tuple[Mapping[str, str], ...]  # triage only: {pmid, to, note}
    file: str  # relative to the archive
    run: str


@dataclass(frozen=True)
class ArchiveView:
    symbol: str
    gene: str | None  # None when the archive holds no file for symbol
    commit: str  # short HEAD
    files: int
    reviewers: int  # distinct keys
    verdicts: tuple[Verdict, ...]


def reviewer_key(name: str, role: str) -> str:
    return "owner" if role == "owner" else name.strip().casefold()


def _verdicts_of(doc: Mapping[str, Any], rel: str, run: str, finished: datetime) -> list[Verdict]:
    name = str(doc["reviewer"]["name"])
    role = doc["reviewer"]["role"]
    key = reviewer_key(name, role)
    out = [
        Verdict(
            name=name,
            key=key,
            role=role,
            finding=f["id"],
            decision=f["decision"],
            note=f.get("note", ""),
            shown_digest=f["shown_digest"],
            finished_at=finished,
            decided_at=datetime.fromisoformat(f["decided_at"]),
            flips=(),
            file=rel,
            run=run,
        )
        for f in doc["findings"]
    ]
    triage = doc["triage"]
    if triage is not None:
        out.append(
            Verdict(
                name=name,
                key=key,
                role=role,
                finding="triage",
                decision=triage["decision"],
                note=triage.get("note", ""),
                shown_digest=None,
                finished_at=finished,
                decided_at=None,
                flips=tuple(
                    {"pmid": f["pmid"], "to": f["to"], "note": f["note"]}
                    for f in triage.get("flips", [])
                ),
                file=rel,
                run=run,
            )
        )
    return out


def read_archive(archive: Path, symbol: str, schema_root: Path) -> ArchiveView:
    """Every verdict filed for ``symbol``, once the archive is a clean checkout of valid files."""
    check_id("symbol", symbol)
    top = check_archive(archive)
    status = _git(top, "status", "--porcelain")
    if status.returncode != 0:
        raise GitError(f"git status failed: {status.stderr.strip()}")
    if status.stdout.strip():
        raise ArchiveError(
            f"{archive} has uncommitted changes: commit or discard them before reading it"
        )
    head = _git(top, "rev-parse", "--short", "HEAD")
    if head.returncode != 0:
        raise GitError(f"git rev-parse failed: {head.stderr.strip()}")
    try:
        validator = schema_validator(schema_root, "review_decisions")
    except AtlasError as exc:
        raise ArchiveError(str(exc)) from exc

    paths: list[Path] = []
    base = top / symbol
    if base.is_dir():
        for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            decisions = run_dir / "decisions"
            if not decisions.is_dir():
                continue
            for path in sorted(decisions.iterdir()):
                if not path.is_file() or path.suffix != ".json":
                    raise ArchiveError(
                        f"{path.relative_to(top)} is not a .json file: nothing but decisions "
                        "files belongs under a decisions directory"
                    )
                paths.append(path)

    verdicts: list[Verdict] = []
    genes: set[str] = set()
    keys: set[str] = set()  # a file holding no verdict still names a reviewer
    for path in paths:
        rel = str(path.relative_to(top))
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ArchiveError(f"{rel}: cannot be read as JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise ArchiveError(f"{rel}: is not a JSON object")
        errors = sorted(
            f"{'/'.join(map(str, e.path)) or '(file)'}: {e.message}"
            for e in validator.iter_errors(doc)
        )
        if errors:
            raise ArchiveError(f"{rel} fails the schema: {errors[0]}")
        finished_text = doc["session"]["finished_at"]
        if not finished_text:
            raise ArchiveError(f"{rel}: the session is not finished (session.finished_at is empty)")
        if doc["symbol"] != symbol:
            raise ArchiveError(f"{rel} is for {doc['symbol']}, not {symbol}")
        genes.add(doc["gene"])
        if len(genes) > 1:
            raise ArchiveError(f"{rel}: the archive holds two genes for {symbol}: {sorted(genes)}")
        name = str(doc["reviewer"]["name"])
        keys.add(reviewer_key(name, doc["reviewer"]["role"]))
        expected = f"{compact_utc(finished_text)}--{slug(name)}.json"
        if path.name != expected:
            raise ArchiveError(f"{rel}: the file is named {path.name}, not {expected}")
        verdicts.extend(
            _verdicts_of(doc, rel, path.parent.parent.name, datetime.fromisoformat(finished_text))
        )

    return ArchiveView(
        symbol=symbol,
        gene=next(iter(genes)) if genes else None,
        commit=head.stdout.strip(),
        files=len(paths),
        reviewers=len(keys),
        verdicts=tuple(verdicts),
    )


def latest(
    verdicts: Iterable[Verdict], *, per_version: bool = False
) -> dict[tuple[str, ...], Verdict]:
    """Each reviewer's latest verdict per finding (or per finding and shown digest)."""
    best: dict[tuple[str, ...], Verdict] = {}
    for v in verdicts:
        k: tuple[str, ...] = (v.key, v.finding)
        if per_version:
            k = (*k, v.shown_digest or "")

        def order(x: Verdict) -> tuple[datetime, datetime, str]:
            return (x.finished_at, x.decided_at or x.finished_at, x.file)

        if k not in best or order(v) > order(best[k]):
            best[k] = v
    return best


def files_line(view: ArchiveView) -> str:
    files = f"{view.files} decisions file{'' if view.files == 1 else 's'}"
    return f"{files} from {view.reviewers} reviewer{'' if view.reviewers == 1 else 's'}"


def _by_reviewer(verdicts: Iterable[Verdict]) -> list[Verdict]:
    return sorted(verdicts, key=lambda v: (v.key != "owner", v.key))


def state(
    view: ArchiveView, current: Mapping[str, str], triage: Mapping[str, str]
) -> dict[str, Any]:
    """What the archive holds, set against the atlas's current digests and triage.

    ``current`` maps finding id to its current shown digest; ``triage`` maps pmid to
    ``included``/``excluded``.
    """
    mine = latest(view.verdicts)
    ids = sorted({v.finding for v in mine.values() if v.finding != "triage"})
    findings: list[dict[str, Any]] = []
    for fid in ids:
        rows = []
        for v in _by_reviewer(x for x in mine.values() if x.finding == fid):
            is_current = v.shown_digest == current.get(fid)
            rows.append(
                {
                    "name": v.name,
                    "role": v.role,
                    "decision": v.decision,
                    "note": v.note,
                    "shown_digest": v.shown_digest,
                    "finished_at": v.finished_at.isoformat(),
                    "decided_at": v.decided_at.isoformat() if v.decided_at else None,
                    "file": v.file,
                    "current": is_current,
                    "pending": v.decision in ("fix", "later") and is_current,
                }
            )
        findings.append(
            {
                "id": fid,
                "in_file": fid in current,
                "current_digest": current.get(fid),
                "verdicts": rows,
            }
        )
    triage_rows = []
    for v in _by_reviewer(x for x in mine.values() if x.finding == "triage"):
        triage_rows.append(
            {
                "name": v.name,
                "role": v.role,
                "decision": v.decision,
                "note": v.note,
                "finished_at": v.finished_at.isoformat(),
                "file": v.file,
                "flips": [
                    {
                        "pmid": f["pmid"],
                        "to": f["to"],
                        "note": f["note"],
                        "pending": f["pmid"] in triage and triage[f["pmid"]] != f["to"],
                    }
                    for f in v.flips
                ],
            }
        )
    return {
        "format": STATE_FORMAT,
        "symbol": view.symbol,
        "gene": view.gene,
        "archive_commit": view.commit,
        "findings": findings,
        "triage": {"verdicts": triage_rows},
    }
