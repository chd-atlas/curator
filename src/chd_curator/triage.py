"""Triage: one request per pending hit, the screen and check answers combined, the result applied.

The hybrid: a cheap local model (``triage-screen``) triages every PubMed hit, and a capable one
(``triage-check``) re-reads the exclusions whose errors would be silent. Losing a paper that
carries findings is the failure this exists to prevent, so:

- no exclusion in a risky class (`RISKY`) is final without the check;
- a sample of every trusted class's exclusions is audited by the check, and one audited
  exclusion the check includes makes that class risky for the run (it escalates);
- an answer that is missing, an error row or unparseable is never read as an exclusion: the hit
  goes to the check, and a hit the check has not answered either is left ``awaiting_check``,
  which `triage-apply` refuses to apply.

Nothing here approves anything: ``triage`` entries carry no approval.
"""

from __future__ import annotations

import json
import math
import random
import urllib.error
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator

from chd_curator import pubmed
from chd_curator.atlas import schema_validator
from chd_curator.fs import write_bytes_atomically
from chd_curator.pubmed import PubMedError
from chd_curator.records import RecordError, literature_path, load_round_trip, with_triage
from chd_curator.run import TriageChange
from chd_curator.write import WriteRefused, write_literature

RISKY: Final = frozenset({"not_chd", "no_finding_in_gene"})
REASONS: Final = frozenset(
    {
        "not_peer_reviewed",
        "review_article",
        "not_human_genetics",
        "no_finding_in_gene",
        "not_chd",
        "other",
    }
)
DECISIONS_FORMAT: Final = 1
NO_RECORD: Final = "(PubMed returned no record for this PMID)"
NO_ABSTRACT: Final = "(no abstract in PubMed)"


class TriageError(Exception):
    """A content failure (exit 1)."""


class TriageUsageError(TriageError):
    """A bad argument or input file (exit 2)."""


def hit_block(pmid: str, year: str, found: tuple[str, str, Sequence[str]] | None) -> str:
    """The block `abstracts` prints for one PMID (digits), without its trailing blank line."""
    if found is None:
        return f"PMID:{pmid} · {year} · {NO_RECORD}"
    title, abstract, types = found
    return "\n".join(
        [
            f"PMID:{pmid} · {year} · {title}",
            f"Publication types: {'; '.join(types) or '(none in PubMed)'}",
            abstract or NO_ABSTRACT,
        ]
    )


def triage_requests(
    root: Path,
    symbol: str,
    out: Path,
    *,
    fetch: Callable[[str], bytes],
    api_key: str | None = None,
) -> int:
    """Write one `ask` request per pending triage entry of `symbol` (approved), in file order."""
    path = literature_path(root, symbol)
    if not path.is_file():
        raise TriageError(f"{path} does not exist; run `chd-curator search {symbol}` first")
    try:
        record = load_round_trip(path.read_bytes())
        pending = [
            str(e["pmid"]) for e in record.get("triage") or [] if e.get("decision") == "pending"
        ]
        digits = [p.removeprefix("PMID:") for p in pending]
        titles = pubmed.efetch_abstracts(digits, fetch, api_key=api_key)
        meta = pubmed.esummary(digits, fetch, api_key=api_key)
    except (PubMedError, urllib.error.URLError, RecordError, OSError, KeyError) as exc:
        raise TriageError(str(exc)) from None
    rows = []
    for pmid, d in zip(pending, digits, strict=True):
        paper = meta.get(d)
        year = str(paper.year) if paper else "year unknown"
        block = hit_block(d, year, titles.get(d))
        rows.append({"key": pmid, "gene": symbol, "inputs": {"gene": symbol, "hit": block}})
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    write_bytes_atomically(out, payload.encode("utf-8"))
    return len(rows)


def audit_sample(
    trusted: Mapping[str, list[str]], seed: str, rate: float = 0.10, minimum: int = 5
) -> set[str]:
    """The keys the capable check audits: per trusted class, `max(minimum, ceil(rate * n))`, all
    of a class smaller than that. Each class is sampled from its sorted keys with
    ``random.Random(f"{seed}:{class}")``; a string seed does not depend on PYTHONHASHSEED."""
    chosen: set[str] = set()
    for name in sorted(trusted):
        keys = sorted(trusted[name])
        want = max(minimum, math.ceil(rate * len(keys)))
        chosen.update(random.Random(f"{seed}:{name}").sample(keys, min(len(keys), want)))
    return chosen


def is_excluded(answer: Mapping[str, Any]) -> bool:
    return answer.get("decision") == "excluded"


def trusted_exclusions(
    screen: Mapping[str, dict[str, Any]], risky: frozenset[str]
) -> dict[str, list[str]]:
    """The screen's exclusions outside `risky`, by reason."""
    found: dict[str, list[str]] = {}
    for key in sorted(screen):
        answer = screen[key]
        if answer and is_excluded(answer) and answer["reason"] not in risky:
            found.setdefault(str(answer["reason"]), []).append(key)
    return found


def to_check(
    screen: Mapping[str, dict[str, Any]],
    risky: frozenset[str],
    audit: set[str],
    escalated: set[str] | frozenset[str] = frozenset(),
    thin: set[str] | frozenset[str] = frozenset(),
) -> set[str]:
    """Every hit whose screen answer is not final: an unanswered key (an empty answer), an
    exclusion in a risky or escalated class, an audited exclusion, and an exclusion of a `thin`
    hit (PubMed has no record or no abstract: the house rules do not exclude a paper for that, so
    the screen's reason, whatever it is, is never final). A screen inclusion is never sent: the
    check cannot lose it. This is the one rule `combine` and `awaiting` read."""
    wanted = set(audit)
    for key, answer in screen.items():
        if (
            not answer
            or is_excluded(answer)
            and (answer["reason"] in risky or answer["reason"] in escalated or key in thin)
        ):
            wanted.add(key)
    return wanted


def thin_keys(rows: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """The requested keys whose `hit` block says PubMed had no record or no abstract."""
    found = set()
    for key, row in rows.items():
        hit = (row.get("inputs") or {}).get("hit")
        lines = hit.splitlines() if isinstance(hit, str) else []
        if lines and (lines[0].endswith(NO_RECORD) or lines[-1] == NO_ABSTRACT):
            found.add(key)
    return found


def escalated_classes(
    screen: Mapping[str, dict[str, Any]], check: Mapping[str, dict[str, Any]], audit: set[str]
) -> set[str]:
    """Trusted classes with an audit miss: an audited exclusion the check includes."""
    return {
        str(screen[key]["reason"])
        for key in audit
        if check.get(key) and not is_excluded(check[key])
    }


def _decision(answer: Mapping[str, Any], tier: str) -> dict[str, str]:
    included = not is_excluded(answer)
    return {
        "decision": "included" if included else "excluded",
        "reason": "none" if included else str(answer["reason"]),
        "why": str(answer["why"]),
        "tier": tier,
    }


def _owed(
    screen: Mapping[str, dict[str, Any]],
    check: Mapping[str, dict[str, Any]],
    risky: frozenset[str],
    audit: set[str] | frozenset[str],
    thin: set[str] | frozenset[str],
) -> set[str]:
    escalated = escalated_classes(screen, check, set(audit))
    return to_check(screen, risky, set(audit), escalated, thin)


def sent_answers(
    screen: Mapping[str, dict[str, Any]],
    check: Mapping[str, dict[str, Any]],
    risky: frozenset[str],
    audit: set[str] | frozenset[str] = frozenset(),
    thin: set[str] | frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """The check answers the hybrid may read: those for hits `to_check` names. A check that also
    answered other hits (in shadow it answers every hit) must not rescue or decide them, or the
    recorded hybrid uses answers the real one would not have had (R-SWA-51). The one rule
    `combine_run` and `qualify.score_triage` read."""
    sent = _owed(screen, check, risky, audit, thin)
    return {key: answer for key, answer in check.items() if key in sent}


def combine(
    screen: Mapping[str, dict[str, Any]],
    check: Mapping[str, dict[str, Any]],
    risky: frozenset[str],
    audit: set[str] | frozenset[str] = frozenset(),
    thin: set[str] | frozenset[str] = frozenset(),
) -> dict[str, dict[str, str]]:
    """One decision per hit that has one; a hit still needing the check has none (see
    `awaiting`). A hit is included if either tier includes it; when both exclude, the check's
    reason stands; a screen exclusion `to_check` does not name stays the screen's. Every check
    answer given here decides its hit, so the hybrid passes only `sent_answers`."""
    needs = _owed(screen, check, risky, audit, thin)
    decided: dict[str, dict[str, str]] = {}
    for key in sorted(screen):
        s, c = screen[key], check.get(key) or {}
        if s and not is_excluded(s):
            decided[key] = _decision(s, "screen")  # either tier including a hit includes it
        elif c:
            decided[key] = _decision(c, "check")
        elif s and key not in needs:
            decided[key] = _decision(s, "screen")
    return decided


def awaiting(
    screen: Mapping[str, dict[str, Any]],
    check: Mapping[str, dict[str, Any]],
    risky: frozenset[str],
    audit: set[str] | frozenset[str] = frozenset(),
    thin: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """The hits the check must still answer: `to_check`'s, less those it has answered."""
    return sorted(key for key in _owed(screen, check, risky, audit, thin) if not check.get(key))


def valid_answer(parsed: Any, key: str) -> bool:
    """Whether a row's `parsed` is a triage answer for `key` the combiner may rely on."""
    if not isinstance(parsed, dict) or parsed.get("pmid") != key:
        return False
    if not isinstance(parsed.get("why"), str) or not parsed["why"].strip():
        return False
    if parsed.get("decision") == "included":
        return True
    reason = parsed.get("reason")
    return parsed.get("decision") == "excluded" and isinstance(reason, str) and reason in REASONS


def read_request_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TriageUsageError(f"cannot read the requests {path}: {exc}") from None
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            raise TriageUsageError(f"{path}:{number}: not a request row")
        if row["key"] in rows:
            raise TriageUsageError(f"{path}:{number}: the key {row['key']!r} is requested twice")
        rows[row["key"]] = row
    return rows


def read_answers(path: Path, requested: set[str]) -> dict[str, dict[str, Any]]:
    """The valid answers of an `ask` answers file by key. A key the requests lack, or one that
    appears twice, is a usage error naming it; an error row or an invalid answer is absent."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TriageUsageError(f"cannot read the answers {path}: {exc}") from None
    seen: set[str] = set()
    answers: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            raise TriageUsageError(f"{path}:{number}: not an answer row")
        key = row["key"]
        if key not in requested:
            raise TriageUsageError(f"{path}:{number}: the key {key!r} was not requested")
        if key in seen:
            raise TriageUsageError(f"{path}:{number}: the key {key!r} is answered twice")
        seen.add(key)
        if valid_answer(row.get("parsed"), key):
            answers[key] = row["parsed"]
    return answers


def merge_check_answers(
    rounds: Sequence[tuple[Path, dict[str, dict[str, Any]]]],
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path, answers in rounds:
        for key, answer in answers.items():
            if key in merged and merged[key] != answer:
                raise TriageUsageError(
                    f"{path}: the key {key!r} has a different answer in an earlier check file"
                )
            merged[key] = answer
    return merged


def combine_run(
    requests: Path,
    screen_path: Path,
    check_paths: Sequence[Path],
    run: str,
    gene: str,
    *,
    shadow: bool = False,
    risky: frozenset[str] = RISKY,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The decisions document and the check requests still owed (rows copied from `requests`).
    `gene` (the approved symbol) is written whatever the rows hold, so a run with no pending hit
    still names its gene; a request row naming another gene is refused."""
    rows = read_request_rows(requests)
    keys = set(rows)
    answered = read_answers(screen_path, keys)
    screen = {key: answered.get(key, {}) for key in sorted(keys)}
    check = merge_check_answers([(p, read_answers(p, keys)) for p in check_paths])
    audit = audit_sample(trusted_exclusions(screen, risky), run)
    thin = thin_keys(rows)
    others = sorted(
        {str(row["gene"]) for row in rows.values() if row.get("gene") not in (None, gene)}
    )
    if others:
        raise TriageUsageError(f"{requests}: the requests name {', '.join(others)}, not {gene}")
    asked = sent_answers(screen, check, risky, audit, thin)
    hybrid = combine(screen, asked, risky, audit, thin)
    escalated = escalated_classes(screen, asked, audit)
    owed_hybrid = awaiting(screen, asked, risky, audit, thin)
    decisions: dict[str, Any]
    if shadow:
        decisions = {}
        owed = sorted(key for key in keys if key not in check)
        for key in sorted(check):
            decisions[key] = {**_decision(check[key], "check"), "hybrid": hybrid.get(key)}
    else:
        decisions, owed = dict(hybrid), owed_hybrid
    document = {
        "format": DECISIONS_FORMAT,
        "gene": gene,
        "run": run,
        "shadow": shadow,
        "escalated": sorted(escalated),
        "awaiting_check": owed,
        "decisions": decisions,
    }
    return document, [rows[key] for key in owed]


def write_json(path: Path, document: Mapping[str, Any]) -> None:
    text = json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=1)
    write_bytes_atomically(path, (text + "\n").encode("utf-8"))


def write_requests(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    write_bytes_atomically(path, payload.encode("utf-8"))


def load_decisions(path: Path, symbol: str) -> dict[str, dict[str, Any]]:
    """The decisions of a `triage-combine` file for `symbol` (the approved symbol); refuses one
    still awaiting the check (exit 1) and one written for another gene or none (exit 1)."""
    return load_decisions_file(path, symbol)[0]


def load_decisions_file(
    path: Path, symbol: str
) -> tuple[dict[str, dict[str, Any]], frozenset[str]]:
    """`load_decisions`, plus the file's `escalated` classes (refused unless a list of
    strings), which `apply_decisions` re-checks."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TriageUsageError(f"cannot read the decisions {path}: {exc}") from None
    if not isinstance(document, dict) or document.get("format") != DECISIONS_FORMAT:
        raise TriageUsageError(f"{path}: not a format {DECISIONS_FORMAT} decisions file")
    owed = document.get("awaiting_check")
    if not isinstance(owed, list) or not isinstance(document.get("decisions"), dict):
        raise TriageUsageError(f"{path}: `awaiting_check` or `decisions` is missing")
    if document.get("gene") != symbol:
        raise TriageError(
            f"{path}: the decisions are for gene {document.get('gene')!r}, not {symbol!r}"
        )
    if owed:
        raise TriageError(
            f"{path}: {len(owed)} hit(s) still await the check ({', '.join(map(str, owed))}); "
            "run the check on them and combine again"
        )
    escalated = document.get("escalated", [])
    if not isinstance(escalated, list) or not all(isinstance(c, str) for c in escalated):
        raise TriageUsageError(f"{path}: `escalated` is not a list of classes")
    return dict(document["decisions"]), frozenset(escalated)


def apply_decisions(
    root: Path,
    symbol: str,
    decisions: Mapping[str, Mapping[str, Any]],
    validator: Draft202012Validator | None = None,
    escalated: frozenset[str] = frozenset(),
) -> int:
    """Write each decision into the `pending` triage entry of `symbol`'s file; return the count.

    Only ``decision`` and, for an exclusion, ``reason`` (plus ``note`` = the answer's `why` when
    the reason is ``other``) are written; ``pmid`` and ``search`` are never touched. A key that
    is not a pending entry is refused, and so is a file changed since it was read.

    What this re-checks itself, whatever the decisions file says: every key is a pending entry
    of this file; an exclusion in a risky class, or in a class the file's ``escalated`` names,
    carries tier ``check`` (a screen-tier one is refused, naming the key); an exclusion has a
    known reason and an ``other`` has a ``why`` to become its note; nothing is written unless
    every key passes; the file is unchanged since it was read; and the schema accepts the result
    (`load_decisions` also checks the gene and that nothing awaits the check). What rests on
    `combine` alone: that the audit was drawn, that the check answered what `to_check` named, and
    that a thin hit was checked. A shadow file applies like any other: its decisions are the
    check's."""
    path = literature_path(root, symbol)
    if not path.is_file():
        raise TriageError(f"{path} does not exist")
    try:
        validator = validator or schema_validator(root, "gene_literature_file")
        existing = path.read_bytes()
        pending = {
            str(e["pmid"])
            for e in load_round_trip(existing).get("triage") or []
            if e.get("decision") == "pending"
        }
        changes = []
        for key in sorted(decisions):
            if key not in pending:
                raise TriageError(f"{key}: is not a pending triage entry of {path}")
            d = decisions[key]
            if (
                d.get("decision") == "excluded"
                and d.get("tier") == "screen"
                and (d.get("reason") in RISKY or d.get("reason") in escalated)
            ):
                raise TriageError(f"{key}: a risky exclusion ({d['reason']}) needs the check")
            if d.get("decision") == "included":
                changes.append(TriageChange(key, "pending", "included", None, None))
            elif d.get("decision") == "excluded" and d.get("reason") in REASONS:
                why = d.get("why")
                if d["reason"] == "other" and not (isinstance(why, str) and why.strip()):
                    raise TriageError(f"{key}: an `other` exclusion needs a why for its note")
                note = why if d["reason"] == "other" else None
                changes.append(TriageChange(key, "pending", "excluded", str(d["reason"]), note))
            else:
                raise TriageError(f"{key}: not a decision that can be applied: {dict(d)!r}")
        content = with_triage(existing, changes) if changes else existing
        if changes:
            write_literature(path, content, validator, expected=existing)
    except WriteRefused as exc:
        raise TriageUsageError(str(exc)) from None
    except (RecordError, OSError) as exc:
        raise TriageError(str(exc)) from None
    return len(changes)
