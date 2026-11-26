"""The one reader of the review archive: each reviewer's latest verdicts, and what is owed.

:func:`read_archive` reads every ``<SYMBOL>/<run>/decisions/*.json`` that ``HEAD`` lists, in
sorted order, and refuses the archive (:class:`~chd_curator.archive.ArchiveError`, exit 2)
when it cannot be trusted: uncommitted changes anywhere in the checkout (untracked files
included; the message says to commit or discard them, and names no file), a path in ``HEAD``
under a decisions directory that is not a ``.json`` file directly in it (a skipped file would
silently drop a reviewer's verdicts), a working-tree file under a decisions directory that
``HEAD`` does not hold (git-ignored, say), a page an owner's approve or fix of the triage was
made on that cannot be found or read, or a file that fails
:func:`~chd_curator.archive.check_decisions` (the atlas's schema, a finished session, the right
symbol, a second opinion not keyed as the owner, no finding named twice), names another gene
than the rest, or sits under a name other than
``<finished_at, compact UTC>--<reviewer slug>.json``. Paths and bytes both come from ``HEAD``,
never from the working tree, which git can be told to hide a file from (``assume-unchanged``,
``skip-worktree``).

A reviewer is keyed by role: every owner file is the key ``owner``, and a clinician is
``name.strip().casefold()``. "Latest" compares parsed instants, not strings; two verdicts at
the same instants fall back to their file paths as strings.

Triage flips. A flip is a verdict on one paper: each reviewer's latest flip of a paper (by
``finished_at``, then file path) is their word on it, and a later triage verdict that does not
mention the paper says nothing about it. A flip is *pending* while its paper is in the
literature file's triage with a decision other than the flip's ``to`` (a paper absent from the
triage is not pending), unless an owner has *closed* it. The owner closes a flip by a triage
verdict that is ``approve`` or ``fix`` (never ``later``), made on a filed page that showed that
flip, in a file that finished after the flip did, whose own flips do not include the paper. A
page showed a flip when its data has ``archive.read`` true and lists the flip's reviewer
(``name`` and ``role``; the key is derived from them), ``pmid`` and ``finished_at`` in
``triage_earlier.flips``; a page without ``archive.read`` true showed nothing. A clinician's
verdict never closes a flip.
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
    head_bytes,
)
from chd_curator.atlas import AtlasError, schema_validator
from chd_curator.decisions import ExportError, page_data_from_bytes

STATE_FORMAT = 2


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
    # An owner's approve or fix of the triage only: the (reviewer key, pmid, finished_at) of
    # each flip the page it was made on showed. Empty for every other verdict.
    shown: frozenset[tuple[str, str, datetime]] = frozenset()


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


def _verdicts_of(
    doc: Mapping[str, Any],
    rel: str,
    run: str,
    finished: datetime,
    shown: frozenset[tuple[str, str, datetime]],
) -> list[Verdict]:
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
                shown=shown,
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


def _head_paths(top: Path, symbol: str) -> frozenset[str]:
    """Every path ``HEAD`` holds under ``symbol``, as archive-relative POSIX strings."""
    done = _git(top, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", symbol)
    if done.returncode != 0:
        raise GitError(f"git ls-tree failed: {done.stderr.strip()}")
    return frozenset(p for p in done.stdout.split("\0") if p)


def _decisions_paths(held: frozenset[str], symbol: str) -> list[Path]:
    """The decisions files among ``held``, sorted; others in a decisions directory are refused."""
    paths: list[Path] = []
    for rel in sorted(held):
        parts = rel.split("/")
        if len(parts) < 4 or parts[0] != symbol or parts[2] != "decisions":
            continue
        if len(parts) != 4 or not parts[3].endswith(".json"):
            raise ArchiveError(
                f"{rel} is not a .json file directly in a decisions directory: nothing but "
                "decisions files belongs under a decisions directory"
            )
        paths.append(Path(rel))
    return paths


def _page_flips(page: Mapping[str, Any], label: str) -> frozenset[tuple[str, str, datetime]]:
    """The flips a page's data says it showed; none unless it read the archive."""
    archive = page.get("archive")
    if not isinstance(archive, dict) or archive.get("read") is not True:
        return frozenset()
    earlier = page.get("triage_earlier")
    if not isinstance(earlier, dict) or not isinstance(earlier.get("flips"), list):
        raise ArchiveError(
            f"{label}: the page read the archive but its data has no triage_earlier.flips list"
        )
    shown: set[tuple[str, str, datetime]] = set()
    for i, entry in enumerate(earlier["flips"]):
        where = f"{label}: triage_earlier.flips[{i}]"
        if not isinstance(entry, dict):
            raise ArchiveError(f"{where} is not an object")
        for field in ("name", "pmid"):
            if not isinstance(entry.get(field), str):
                raise ArchiveError(f"{where} has no string {field}")
        role = entry.get("role")
        if role not in ("owner", "second_opinion"):
            raise ArchiveError(f"{where} has a role that is not owner or second_opinion: {role!r}")
        when = entry.get("finished_at")
        try:
            if not isinstance(when, str):
                raise ValueError("not a string")
            parsed = datetime.fromisoformat(when)
            if parsed.tzinfo is None:
                raise ValueError("no UTC offset")
        except ValueError as exc:
            raise ArchiveError(
                f"{where} has a finished_at that is not a date-time with an offset: {when!r}"
            ) from exc
        shown.add((reviewer_key(entry["name"], role), entry["pmid"], parsed))
    return frozenset(shown)


def _shown_by(
    top: Path,
    held: frozenset[str],
    symbol: str,
    run: str,
    doc: Mapping[str, Any],
    rel: str,
) -> frozenset[tuple[str, str, datetime]]:
    """The flips shown by the page an owner's approve or fix of the triage was made on.

    Empty for any other file. The page is the one filed under ``run`` whose ``built_on``,
    ``literature_sha256``, reviewer name and role equal the decisions file's, read from ``HEAD``
    (the first by name when several match); none matching is refused.
    """
    triage = doc.get("triage")
    who = doc["reviewer"]
    if who["role"] != "owner" or triage is None or triage["decision"] not in ("approve", "fix"):
        return frozenset()
    info = doc["page"]
    wanted = (str(info["built_on"]), str(info["literature_sha256"]), str(who["name"]), "owner")
    prefix = f"{symbol}/{run}/pages/"
    for page_rel in sorted(h for h in held if h.startswith(prefix) and h.endswith(".html")):
        if "/" in page_rel[len(prefix) :]:
            continue
        raw = head_bytes(top, Path(page_rel))
        if raw is None:
            raise ArchiveError(f"{page_rel}: is listed by HEAD but cannot be read from it")
        try:
            data = page_data_from_bytes(raw, page_rel)
        except ExportError as exc:
            raise ArchiveError(str(exc)) from exc
        try:
            found = (
                str(data["page"]["built_on"]),
                str(data["page"]["literature_sha256"]),
                str(data["reviewer"]["name"]),
                str(data["reviewer"]["role"]),
            )
        except (KeyError, TypeError) as exc:
            raise ArchiveError(f"{page_rel}: the page's data lacks {exc}") from exc
        if found == wanted:
            return _page_flips(data, page_rel)
    raise ArchiveError(
        f"{rel}: no filed page under {symbol}/{run}/pages matches this owner decisions file's "
        "built_on, literature_sha256 and reviewer"
    )


def read_archive(archive: Path, symbol: str, schema_root: Path) -> ArchiveView:
    """Every verdict filed for ``symbol``, once the archive is a clean checkout of valid files."""
    check_id("symbol", symbol)
    top, head = open_archive(archive)
    try:
        validator = schema_validator(schema_root, "review_decisions")
    except AtlasError as exc:
        raise ArchiveError(str(exc)) from exc

    held_paths = _head_paths(top, symbol)
    paths = _decisions_paths(held_paths, symbol)
    base = top / symbol
    if base.is_dir():
        for found in sorted(p for p in base.glob("*/decisions/**/*") if not p.is_dir()):
            rel = found.relative_to(top).as_posix()
            if rel not in held_paths:
                raise ArchiveError(
                    f"{rel}: is not in HEAD (git-ignored?): commit it before reading"
                )

    verdicts: list[Verdict] = []
    genes: set[str] = set()
    keys: set[str] = set()  # a file holding no verdict still names a reviewer
    for path in paths:
        rel = path.as_posix()
        held = head_bytes(top, path)
        if held is None:
            raise ArchiveError(f"{rel}: is not in HEAD (git-ignored?): commit it before reading")
        try:
            doc = json.loads(held)
        except ValueError as exc:
            raise ArchiveError(f"{rel}: cannot be read as JSON: {exc}") from exc
        expected = check_decisions(doc, rel, symbol, validator)
        genes.add(doc["gene"])
        if len(genes) > 1:
            raise ArchiveError(f"{rel}: the archive holds two genes for {symbol}: {sorted(genes)}")
        if path.name != expected:
            raise ArchiveError(f"{rel}: the file is named {path.name}, not {expected}")
        name = str(doc["reviewer"]["name"])
        keys.add(reviewer_key(name, doc["reviewer"]["role"]))
        run = path.parent.parent.name
        shown = _shown_by(top, held_paths, symbol, run, doc, rel)
        verdicts.extend(
            _verdicts_of(
                doc, rel, run, datetime.fromisoformat(doc["session"]["finished_at"]), shown
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


def _flips(verdicts: Iterable[Verdict], triage: Mapping[str, str]) -> list[dict[str, Any]]:
    """Each reviewer's latest flip of each paper, with the owner file that closed it, if any."""

    def order(v: Verdict) -> tuple[datetime, str]:
        return (v.finished_at, v.file)

    triage_verdicts = [v for v in verdicts if v.finding == "triage"]
    latest_flip: dict[tuple[str, str], tuple[Verdict, Mapping[str, str]]] = {}
    for v in triage_verdicts:
        for f in v.flips:
            k = (v.key, f["pmid"])
            if k not in latest_flip or order(v) > order(latest_flip[k][0]):
                latest_flip[k] = (v, f)
    closers = sorted(
        (v for v in triage_verdicts if v.role == "owner" and v.decision in ("approve", "fix")),
        key=order,
    )
    rows = []
    for (key, pmid), (v, f) in sorted(
        latest_flip.items(), key=lambda kv: (kv[0][0] != "owner", kv[0][0], kv[0][1])
    ):
        closed_by = next(
            (
                c.file
                for c in closers
                if (key, pmid, v.finished_at) in c.shown
                and c.finished_at > v.finished_at
                and pmid not in {x["pmid"] for x in c.flips}
            ),
            None,
        )
        rows.append(
            {
                "pmid": pmid,
                "to": f["to"],
                "note": f["note"],
                "name": v.name,
                "role": v.role,
                "finished_at": v.finished_at.isoformat(),
                "file": v.file,
                "closed_by": closed_by,
                "pending": closed_by is None and pmid in triage and triage[pmid] != f["to"],
            }
        )
    return rows


def state(
    view: ArchiveView, current: Mapping[str, str], triage: Mapping[str, str]
) -> dict[str, Any]:
    """What the archive holds, set against the atlas's current digests and triage.

    ``current`` maps finding id to its current shown digest; ``triage`` maps each pmid in the
    literature file's triage to its decision (``pending``, ``included`` or ``excluded``). A
    finding verdict is pending when it is a reviewer's latest verdict on the finding and is a
    ``fix`` or ``later`` made on its current digest.

    ``triage.verdicts`` holds each reviewer's latest triage verdict (owner first, then by key).
    ``triage.flips`` holds each reviewer's latest flip of each paper (by ``finished_at``, then
    file), whatever their later triage verdicts say, sorted owner first, then by key and pmid.
    A flip's ``closed_by`` is the earliest (by ``finished_at``, then file) owner triage verdict
    that closed it, else None: an ``approve`` or ``fix`` (never ``later``) made on a filed page
    that showed this flip (same reviewer key, pmid and instant), in a file whose ``finished_at``
    is after the flip's, whose own flips do not include the paper. A flip is ``pending`` when it
    is not closed, its pmid is in ``triage`` (a flip of a pmid absent from it is not pending) and
    that decision is not the flip's ``to``.
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
    triage_rows = [
        {
            "name": v.name,
            "role": v.role,
            "decision": v.decision,
            "note": v.note,
            "finished_at": v.finished_at.isoformat(),
            "file": v.file,
        }
        for v in _by_reviewer(x for x in mine.values() if x.finding == "triage")
    ]
    return {
        "format": STATE_FORMAT,
        "symbol": view.symbol,
        "gene": view.gene,
        "archive_commit": view.commit,
        "findings": findings,
        "triage": {"verdicts": triage_rows, "flips": _flips(view.verdicts, triage)},
    }
