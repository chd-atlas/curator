"""The one reader of the review archive: each reviewer's latest verdicts, and what is owed.

:func:`read_archive` reads every ``<SYMBOL>/<run>/decisions/*.json`` in sorted order and
refuses the archive (:class:`~chd_curator.archive.ArchiveError`, exit 2) when it cannot be
trusted: uncommitted changes anywhere in the checkout (untracked files included, every one
listed), a file that is not ``.json`` under a decisions directory (a skipped file would
silently drop a reviewer's verdicts), a decisions file that ``HEAD`` does not hold (git-ignored,
say), or a file that fails :func:`~chd_curator.archive.check_decisions` (the atlas's schema, a
finished session, the right symbol, a second opinion not keyed as the owner, no finding named
twice), names another gene than the rest, or sits under a name other than
``<finished_at, compact UTC>--<reviewer slug>.json``.

A reviewer is keyed by role: every owner file is the key ``owner``, and a clinician is
``name.strip().casefold()``. "Latest" compares parsed instants, not strings; two verdicts at
the same instants fall back to their file paths as strings.
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
    check_decisions,
    check_id,
)
from chd_curator.atlas import AtlasError, schema_validator

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
    triage = doc.get("triage")
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


def open_archive(archive: Path) -> tuple[Path, str]:
    """The archive's top and short HEAD, once it is a marked checkout with nothing uncommitted."""
    top = check_archive(archive)
    status = _git(top, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        raise GitError(f"git status failed: {status.stderr.strip()}")
    if status.stdout.strip():
        raise ArchiveError(
            f"{archive} has uncommitted changes: commit or discard them before reading it"
        )
    head = _git(top, "rev-parse", "--short", "HEAD")
    if head.returncode != 0:
        raise GitError(f"git rev-parse failed: {head.stderr.strip()}")
    return top, head.stdout.strip()


def read_archive(archive: Path, symbol: str, schema_root: Path) -> ArchiveView:
    """Every verdict filed for ``symbol``, once the archive is a clean checkout of valid files."""
    check_id("symbol", symbol)
    top, head = open_archive(archive)
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

    tracked = _git(top, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", symbol)
    if tracked.returncode != 0:
        raise GitError(f"git ls-tree failed: {tracked.stderr.strip()}")
    in_head = set(tracked.stdout.split("\0"))

    verdicts: list[Verdict] = []
    genes: set[str] = set()
    keys: set[str] = set()  # a file holding no verdict still names a reviewer
    for path in paths:
        rel = str(path.relative_to(top))
        if path.relative_to(top).as_posix() not in in_head:
            raise ArchiveError(f"{rel}: is not in HEAD (git-ignored?): commit it before reading")
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ArchiveError(f"{rel}: cannot be read as JSON: {exc}") from exc
        expected = check_decisions(doc, rel, symbol, validator)
        genes.add(doc["gene"])
        if len(genes) > 1:
            raise ArchiveError(f"{rel}: the archive holds two genes for {symbol}: {sorted(genes)}")
        if path.name != expected:
            raise ArchiveError(f"{rel}: the file is named {path.name}, not {expected}")
        name = str(doc["reviewer"]["name"])
        keys.add(reviewer_key(name, doc["reviewer"]["role"]))
        verdicts.extend(
            _verdicts_of(
                doc,
                rel,
                path.parent.parent.name,
                datetime.fromisoformat(doc["session"]["finished_at"]),
            )
        )

    return ArchiveView(
        symbol=symbol,
        gene=next(iter(genes)) if genes else None,
        commit=head,
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

    ``current`` maps finding id to its current shown digest; ``triage`` maps each pmid in the
    literature file's triage to its decision (``pending``, ``included`` or ``excluded``); a
    flip of a pmid absent from it is not pending. A verdict is pending when it is a
    reviewer's latest verdict on the finding and is a ``fix`` or ``later`` made on its
    current digest.
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
