"""How often each clinician agrees with the owner, per version of a card.

Nothing here is printed on a page or published: the report is for the owner to read.

A verdict is about one version of a card, named by its shown digest, so it is paired only
with the other side's verdict on the same version (``latest(per_version=True)``): a clinician's
``fix`` on version one is not set against the owner's ``approve`` of version two, which is the
owner's answer to the clinician's fix. Triage verdicts have no digest and are never paired
or counted as unpaired. A clinician is any reviewer whose role is ``second_opinion``, keyed
by ``Verdict.key`` so "Dr X" and " dr x" are one clinician.

Every pair is exactly one of: ``agreed`` (both approve or both fix), ``disagreed`` (one
approves and one fixes) or ``with_later`` (either side said later), so ``pairs`` is the sum of
the three. ``unpaired`` counts a clinician's verdicts on a version no owner decisions file
covers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from chd_curator.archive_read import ArchiveView, Verdict, latest

AGREEMENT_FORMAT = 1
DIGEST_SHOWN = 15

_UNPAIRED_LINE = (
    "Unpaired: a clinician's verdict on a version of a card that no owner decisions file "
    "covers; approvals made in the terminal have no file."
)


@dataclass(frozen=True)
class Pair:
    finding: str
    digest: str
    owner: Verdict
    clinician: Verdict


@dataclass(frozen=True)
class Counts:
    pairs: int = 0
    agreed: int = 0
    disagreed: tuple[Pair, ...] = ()
    with_later: int = 0
    unpaired: int = 0


@dataclass(frozen=True)
class ClinicianTally:
    key: str
    name: str  # as written in the clinician's latest file
    genes: dict[str, Counts] = field(default_factory=dict)


def pairs(view: ArchiveView) -> tuple[list[Pair], list[Verdict]]:
    """The owner/clinician pairs per (finding, digest), and the clinician verdicts left over."""
    best = latest((v for v in view.verdicts if v.shown_digest is not None), per_version=True)
    owner = {(v.finding, v.shown_digest): v for v in best.values() if v.role == "owner"}
    paired: list[Pair] = []
    unpaired: list[Verdict] = []
    for v in best.values():
        if v.role != "second_opinion":
            continue
        mine = owner.get((v.finding, v.shown_digest))
        if mine is None:
            unpaired.append(v)
        else:
            paired.append(Pair(v.finding, v.shown_digest or "", mine, v))
    paired.sort(key=lambda p: (p.finding, p.digest, p.clinician.key))
    unpaired.sort(key=lambda v: (v.finding, v.shown_digest or "", v.key))
    return paired, unpaired


def _kind(p: Pair) -> str:
    sides = {p.owner.decision, p.clinician.decision}
    if "later" in sides:
        return "later"
    return "agreed" if len(sides) == 1 else "disagreed"


def tally(views: Sequence[ArchiveView]) -> list[ClinicianTally]:
    """One tally per clinician, sorted by key, with counts per gene symbol."""
    names: dict[str, tuple[tuple[datetime, str], str]] = {}
    counts: dict[str, dict[str, Counts]] = {}
    for view in views:
        paired, unpaired = pairs(view)
        for v in view.verdicts:
            if v.role != "second_opinion":
                continue
            stamp = (v.finished_at, v.file)
            if v.key not in names or stamp > names[v.key][0]:
                names[v.key] = (stamp, v.name)
            counts.setdefault(v.key, {}).setdefault(view.symbol, Counts())
        for key, by_gene in counts.items():
            mine = [p for p in paired if p.clinician.key == key]
            lone = [v for v in unpaired if v.key == key]
            if not mine and not lone:
                continue
            kinds = [_kind(p) for p in mine]
            by_gene[view.symbol] = Counts(
                pairs=len(mine),
                agreed=kinds.count("agreed"),
                disagreed=tuple(p for p, k in zip(mine, kinds, strict=True) if k == "disagreed"),
                with_later=kinds.count("later"),
                unpaired=len(lone),
            )
    return [ClinicianTally(k, names[k][1], dict(sorted(counts[k].items()))) for k in sorted(counts)]


def _summary(c: Counts) -> str:
    return (
        f"{c.pairs} pair{'' if c.pairs == 1 else 's'}, {c.agreed} agreed, "
        f"{len(c.disagreed)} disagreed, "
        f"{c.with_later} with ask later; {c.unpaired} unpaired"
    )


def report(tallies: Sequence[ClinicianTally], symbols: Sequence[str]) -> list[str]:
    """The report's lines (see the module docstring for what each count means)."""
    if not tallies:
        where = f" for {', '.join(symbols)}" if symbols else ""
        return [f"No second opinion in the archive{where}."]
    lines: list[str] = []
    total = 0
    for t in tallies:
        who = t.name.strip()  # a leading space would read as a gene line's indent
        whole = Counts(
            pairs=sum(c.pairs for c in t.genes.values()),
            agreed=sum(c.agreed for c in t.genes.values()),
            disagreed=tuple(p for c in t.genes.values() for p in c.disagreed),
            with_later=sum(c.with_later for c in t.genes.values()),
            unpaired=sum(c.unpaired for c in t.genes.values()),
        )
        total += whole.unpaired
        lines.append(f"{who}: {_summary(whole)}")
        for symbol, c in t.genes.items():
            lines.append(f"  {symbol}: {_summary(c)}")
            for p in c.disagreed:
                lines.append(
                    f"    disagreed {p.finding} at {p.digest[:DIGEST_SHOWN]}: "
                    f"owner {p.owner.decision} {json.dumps(p.owner.note, ensure_ascii=False)}; "
                    f"{who} {p.clinician.decision} "
                    f"{json.dumps(p.clinician.note, ensure_ascii=False)}"
                )
    if total > 0:
        lines.append(_UNPAIRED_LINE)
    return lines


def _verdict_json(v: Verdict) -> dict[str, Any]:
    return {
        "name": v.name,
        "decision": v.decision,
        "note": v.note,
        "finished_at": v.finished_at.isoformat(),
        "file": v.file,
    }


def as_json(tallies: Sequence[ClinicianTally], commit: str) -> dict[str, Any]:
    return {
        "format": AGREEMENT_FORMAT,
        "archive_commit": commit,
        "clinicians": [
            {
                "key": t.key,
                "name": t.name,
                "genes": {
                    symbol: {
                        "pairs": c.pairs,
                        "agreed": c.agreed,
                        "disagreed": [
                            {
                                "finding": p.finding,
                                "shown_digest": p.digest,
                                "owner": _verdict_json(p.owner),
                                "clinician": _verdict_json(p.clinician),
                            }
                            for p in c.disagreed
                        ],
                        "with_later": c.with_later,
                        "unpaired": c.unpaired,
                    }
                    for symbol, c in t.genes.items()
                },
            }
            for t in tallies
        ],
    }
