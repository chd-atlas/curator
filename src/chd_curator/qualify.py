"""``qualify triage``: score a triage configuration against a gene's approved curation.

A model fills a role only after passing that role's test in its place in the system. For
triage the place is the hybrid (`triage.py`): the screen's answers, the capable check over the
risky classes, the audit sample and every unanswered hit. The pass rule is one sentence: **zero
papers with approved findings are missed by the hybrid**. This module never re-implements the
hybrid: it calls `triage.audit_sample`, `to_check`, `combine` and `awaiting` as `triage-combine`
does, so a scorer and the tool it qualifies cannot disagree.

Nothing here writes: `proposed_entry` returns the register entry a curator may paste into
``workflow/qualification.yaml``, and the loader (`register.py`) judges it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

from chd_curator.records import RecordError, literature_path, load_round_trip
from chd_curator.triage import (
    RISKY,
    audit_sample,
    awaiting,
    combine,
    escalated_classes,
    is_excluded,
    to_check,
    trusted_exclusions,
)

Verdict = Literal["PASS", "FAIL", "INCOMPLETE"]
SCREEN_ROLE: Final = "triage-screen"


class QualifyError(Exception):
    """A gold that cannot be read (exit 1)."""


@dataclass(frozen=True)
class TriageScore:
    """`with_findings`: gold hits with approved findings; `screen_alone`: of those, the ones the
    screen included itself; `missed`: sorted keys the hybrid lost; `sent_to_check`: hits the
    check owes before any audit (risky exclusions and unanswered hits); `audit`: the audit
    sample's size; `owed`: sorted keys the check has still to answer."""

    with_findings: int
    screen_alone: int
    missed: list[str]
    sent_to_check: int
    audit: int
    owed: list[str]
    verdict: Verdict


def triage_gold(root: Path, symbol: str) -> dict[str, dict[str, Any]]:
    """Per decided hit of `symbol`'s literature file: the final triage ``decision``, its
    ``reason`` (``none`` for an inclusion) and ``findings``, the number of APPROVED findings
    citing that PMID. A pending hit has no gold and is left out."""
    path = literature_path(root, symbol)
    try:
        record = load_round_trip(path.read_bytes())
    except (OSError, RecordError) as exc:
        raise QualifyError(f"cannot read the gold {path}: {exc}") from None
    approved: dict[str, int] = {}
    for finding in record.get("findings") or []:
        if finding.get("approved_by"):
            pmid = str(finding.get("publication"))
            approved[pmid] = approved.get(pmid, 0) + 1
    gold: dict[str, dict[str, Any]] = {}
    for entry in record.get("triage") or []:
        decision = entry.get("decision")
        if decision not in ("included", "excluded"):
            continue
        pmid = str(entry["pmid"])
        reason = "none" if decision == "included" else str(entry.get("reason"))
        gold[pmid] = {"decision": decision, "reason": reason, "findings": approved.get(pmid, 0)}
    return gold


def score_triage(
    gold: Mapping[str, Mapping[str, Any]],
    screen: Mapping[str, dict[str, Any]],
    check: Mapping[str, dict[str, Any]],
    seed: str,
    risky: frozenset[str] = RISKY,
) -> TriageScore:
    """Score the hybrid (`screen` + `check`, audit seeded by `seed`) against `gold`.

    `screen` has one entry per requested hit, ``{}`` for one with no valid answer (it goes to
    the check). A gold hit with findings the screen was never asked about is a miss. FAIL: any
    miss. INCOMPLETE: no miss, but the check has not answered a hit owed to it. PASS: neither.
    """
    with_findings = sorted(key for key, g in gold.items() if g["findings"] > 0)
    audit = audit_sample(trusted_exclusions(screen, risky), seed)
    # The hybrid consults the check only where `to_check` names a hit; a check file that also
    # answers other hits (the benchmark's does, for all of them) must not rescue those.
    sent = to_check(screen, risky, audit, escalated_classes(screen, check, audit))
    asked = {key: answer for key, answer in check.items() if key in sent}
    hybrid = combine(screen, asked, risky, audit)
    owed = awaiting(screen, asked, risky, audit)
    missed = sorted(
        key
        for key in with_findings
        if key not in screen or hybrid.get(key, {}).get("decision") == "excluded"
    )
    verdict: Verdict = "FAIL" if missed else "INCOMPLETE" if owed else "PASS"
    return TriageScore(
        with_findings=len(with_findings),
        screen_alone=sum(
            1 for key in with_findings if screen.get(key) and not is_excluded(screen[key])
        ),
        missed=missed,
        sent_to_check=len(to_check(screen, risky, set())),
        audit=len(audit),
        owed=owed,
        verdict=verdict,
    )


def proposed_entry(
    symbol: str,
    gold: Mapping[str, Mapping[str, Any]],
    score: TriageScore,
    *,
    seed: str,
    screen_model_key: str,
    check_model_key: str,
    rules_digest: str,
) -> dict[str, Any]:
    """The register entry for a PASS or FAIL score (INCOMPLETE proposes none). The gold digest
    is the sha256 of the gold's canonical JSON, so a changed gold is a different gold. ``backend``
    is left for the curator: the answers file records the model's key, not its settings."""
    if score.verdict == "INCOMPLETE":
        raise QualifyError("an incomplete run has no entry to propose")
    digest = hashlib.sha256(
        json.dumps(gold, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "role": SCREEN_ROLE,
        "model_key": screen_model_key,
        "verdict": "pass" if score.verdict == "PASS" else "fail",
        "gold": symbol,
        "gold_digest": f"sha256:{digest}",
        "rules_digest": rules_digest,
        "date": date.today().isoformat(),
        "scores": {
            "with_findings": score.with_findings,
            "screen_alone": score.screen_alone,
            "missed": len(score.missed),
            "sent_to_check": score.sent_to_check,
            "audit": score.audit,
            "hits": len(gold),
        },
        "note": (
            f"scored as the screen of the hybrid with check {check_model_key}, "
            f"audit seed {seed!r}; "
            f"missed: {', '.join(score.missed) or 'none'}. Bias: the gold is the owner-approved "
            "curation of one gene."
        ),
        "backend": {},
    }
