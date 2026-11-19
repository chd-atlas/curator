"""The review page's data, built from a curation run and nothing else.

The owner approves findings from the page, and the atlas then records the approval against a
digest of the finding as it stands. So a card must show everything that approval binds
(R-P1-17): every key of the finding's record except ``approved_by``, ``approved_on`` and
``approved_digest``, and the same for each variant. The pilot's page showed a hand-picked
subset and silently omitted ``p_value``, ``cases``, ``locators``, ``verified_by`` and others.
Here each row declares the record keys it renders, every remaining key gets an "other fields"
row (a variant: an ``others`` entry), and each card and variant carries ``keys_shown``, the
sorted keys it renders. ``keys_shown`` is assembled from the rows actually emitted, so a key
no row renders is absent from it and a test comparing it with the record fails.

What this module does not do: it computes no statistic, reads no clock and never recomputes a
quote check. ``checked`` and ``found`` are those of :func:`chd_curator.quotes.check_quotes`
(R-P1-26), and a quote over 30 words reaches the data only as its first 30 words (R-P1-5).
Nothing here iterates a ``set`` into the output; every list is built in a stated order.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator.atlas import Digests
from chd_curator.quotes import LONG_QUOTE_WORDS, QuoteCheck, check_quotes
from chd_curator.run import JudgeDecision, RunPaths, load_judge, load_texts

APPROVAL_KEYS: Final = frozenset({"approved_by", "approved_on", "approved_digest"})
GROUPS: Final = (
    {"key": "judged", "title": "The readers disagreed; a judge chose"},
    {"key": "abstract", "title": "Read from the abstract only"},
    {"key": "agreed", "title": "The readers agreed"},
)
_GROUP_ORDER: Final = {g["key"]: i for i, g in enumerate(GROUPS)}


class ReviewDataError(Exception):
    """The page's inputs are missing or inconsistent (a missing obo, an unknown finding)."""


@dataclass(frozen=True)
class Reviewer:
    name: str
    role: Literal["owner", "second_opinion"]


def _plain(value: Any) -> str:
    """A readable form of any YAML value: strings verbatim, the rest as sorted-key JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _carriers(c: Any, unit: Any = "") -> str:
    if not isinstance(c, Mapping) or not c:
        return _plain(c) if c else ""
    text = ", ".join(f"{v} {str(k).replace('_', ' ')}" for k, v in c.items())
    return text + (f" ({unit})" if unit else "")


@dataclass(frozen=True)
class _Row:
    name: str
    keys: tuple[str, ...]
    get: Callable[[Mapping[str, Any], Mapping[str, str]], str]


def _design(f: Mapping[str, Any], _h: Mapping[str, str]) -> str:
    text = str(f.get("design", "")).replace("_", " ")
    return text + (f" · {f['comparator']}" if f.get("comparator") else "")


def _terms(terms: Any, hpo: Mapping[str, str]) -> str:
    return "; ".join(f"{hpo[t]} ({t})" if t in hpo else str(t) for t in terms or [])


def _review_mark(f: Mapping[str, Any], _h: Mapping[str, str]) -> str:
    text = ": ".join(str(f[k]) for k in ("review_status", "review_note") if f.get(k))
    if f.get("review_settled_by"):
        text += f" (settled by {f['review_settled_by']})"
    return text


ROWS: Final = (
    _Row("design", ("design", "comparator"), _design),
    _Row("methods", ("methods",), lambda f, h: ", ".join(map(str, f.get("methods") or []))),
    _Row(
        "screened",
        ("screened", "count_unit"),
        lambda f, h: f"{f.get('screened', '')} {f.get('count_unit', '')}".strip(),
    ),
    _Row("carriers", ("carriers",), lambda f, h: _carriers(f.get("carriers"), f.get("count_unit"))),
    _Row("phenotypes", ("phenotypes",), lambda f, h: _terms(f.get("phenotypes"), h)),
    _Row(
        "lesion groups",
        ("lesion_groups",),
        lambda f, h: ", ".join(map(str, f.get("lesion_groups") or [])),
    ),
    _Row("syndromic", ("syndromic",), lambda f, h: str(f.get("syndromic") or "")),
    _Row("cohorts", ("cohorts",), lambda f, h: ", ".join(map(str, f.get("cohorts") or []))),
    _Row("summary", ("summary",), lambda f, h: str(f.get("summary") or "")),
    _Row(
        "review mark",
        ("review_status", "review_note", "review_settled_by"),
        _review_mark,
    ),
)
# Keys the card renders outside the rows: its id, its paper, and the variant block.
CARD_KEYS: Final = frozenset({"id", "publication", "variants"})
_FIELD_ALIASES: Final = {
    "lesion_groups": "lesion groups",
    "review": "review mark",
    "review_mark": "review mark",
}
_ROW_OF_FIELD: Final = {
    **{row.name: row.name for row in ROWS},
    **{key: row.name for row in ROWS for key in row.keys},
    **_FIELD_ALIASES,
}


def _resolve_field(field: str, other_keys: Sequence[str]) -> str | None:
    """The row a judge or evidence ``field`` names, or None."""
    name = field.strip().lower()
    for candidate in (name, name.replace(" ", "_")):
        if candidate in _ROW_OF_FIELD:
            return _ROW_OF_FIELD[candidate]
        if candidate in other_keys:
            return candidate
    return None


_VARIANT_DIRECT: Final = (
    "id",
    "as_reported",
    "placement",
    "kind",
    "zygosity",
    "with",
    "phase",
    "carriers",
    "phenotypes",
    "hgvs_c",
    "hgvs_p",
    "grch38",
    "review_status",
    "review_note",
    "review_settled_by",
)


def _variant_target(field: str, variants: Sequence[Mapping[str, Any]]) -> int | None:
    """The id of the variant a field like ``variant c.1A>G phase`` names, or None."""
    if not field.lower().startswith("variant"):
        return None
    named = [
        v for v in variants if str(v.get("as_reported") or "") and str(v["as_reported"]) in field
    ]
    if named:
        return int(max(named, key=lambda v: len(str(v["as_reported"])))["id"])
    m = re.match(r"variant\s+(\d+)\b", field, re.IGNORECASE)
    if m and any(v["id"] == int(m.group(1)) for v in variants):
        return int(m.group(1))
    return None


def _chose(d: JudgeDecision) -> str:
    return {"first": "chose reader 1", "second": "chose reader 2"}.get(
        d.decision, "chose neither reader"
    )


def _settled_by_rule(d: JudgeDecision) -> bool:
    """A span-only (``... as_reported``) or whitespace decision is settled by rule, not shown."""
    return d.field.strip().lower().endswith("as_reported") or "whitespace" in d.rule.lower()


def _shown_quote(check: QuoteCheck) -> dict[str, Any]:
    over = check.words > LONG_QUOTE_WORDS
    text = " ".join(check.quote.split()[:LONG_QUOTE_WORDS]) + " …" if over else check.quote
    return {
        "quote": text,
        "where": check.where,
        "checked": check.checked,
        "found": check.found,
        "over_limit": over,
        "words": check.words,
    }


def _unique(quotes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for q in quotes:
        if q["quote"] not in seen:
            seen.add(q["quote"])
            out.append(q)
    return out


def _hpo_labels(root: Path) -> dict[str, str]:
    directory = root / "ontologies"
    files = sorted(directory.glob("hp-*.obo")) if directory.is_dir() else []
    if len(files) != 1:
        raise ReviewDataError(
            f"{directory}: expected exactly one hp-*.obo to label phenotypes, found {len(files)}"
        )
    labels: dict[str, str] = {}
    current: str | None = None
    in_term = False
    for line in files[0].read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            in_term, current = line.strip() == "[Term]", None
        elif in_term and line.startswith("id: "):
            current = line[4:].strip()
        elif in_term and current and line.startswith("name: "):
            labels[current] = line[6:].strip()
            current = None
    return labels


def _safe_load(path: Path) -> Any:
    try:
        return YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise ReviewDataError(f"{path}: cannot be read as YAML: {exc}") from exc


def _publications(root: Path) -> dict[str, Mapping[str, Any]]:
    path = root / "curation/publications.yaml"
    if not path.is_file():
        return {}
    data = _safe_load(path)
    rows = data.get("publications") if isinstance(data, dict) else None
    return {str(p["id"]): p for p in rows or [] if isinstance(p, dict) and "id" in p}


class _Paper:
    """Title, year and DOI for a PMID: publications.yaml, else its triage entry, else empty."""

    def __init__(
        self, pubs: Mapping[str, Mapping[str, Any]], triage: Mapping[str, Mapping[str, Any]]
    ) -> None:
        self._pubs, self._triage = pubs, triage

    def get(self, pmid: str, key: str) -> str:
        for source in (self._pubs.get(pmid, {}), self._triage.get(pmid, {})):
            if source.get(key):
                return str(source[key])
        return ""

    def info(self, pmid: str) -> dict[str, Any]:
        digits = re.sub(r"\D", "", pmid)
        doi = self.get(pmid, "doi")
        return {
            "pmid": pmid,
            "title": self.get(pmid, "title"),
            "year": self.get(pmid, "year"),
            "doi_url": f"https://doi.org/{doi}" if doi else None,
            "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{digits}/",
        }


def _variant_card(
    v: Mapping[str, Any],
    variants: Sequence[Mapping[str, Any]],
    decisions: Sequence[tuple[JudgeDecision, QuoteCheck | None]],
    evidence: Sequence[QuoteCheck],
    hpo: Mapping[str, str],
) -> dict[str, Any]:
    with_text = ""
    if v.get("with") or v.get("phase"):
        ids = list(v.get("with") or [])
        named = [str(o.get("as_reported")) for o in variants if o["id"] in ids]
        unknown = [str(i) for i in ids if all(o["id"] != i for o in variants)]
        together = named + [f"variant {i} (not in this finding)" for i in unknown]
        with_text = " ".join(
            x
            for x in (str(v.get("phase") or ""), "with " + ", ".join(together) if together else "")
            if x
        )
    placed_parts = [str(v[k]) for k in ("hgvs_c", "hgvs_p") if v.get(k)]
    placed = " → ".join(placed_parts)
    if v.get("grch38"):
        g = v["grch38"]
        gtxt = (
            f"GRCh38 {g.get('chrom')}-{g.get('pos')}-{g.get('ref')}-{g.get('alt')}"
            if isinstance(g, Mapping) and set(g) == {"chrom", "pos", "ref", "alt"}
            else f"GRCh38 {_plain(g)}"
        )
        placed = f"{placed} · {gtxt}" if placed else gtxt
    note = _review_mark(v, hpo)
    direct = [k for k in _VARIANT_DIRECT if k in v]
    others_rows = [
        {"name": k, "value": _plain(v[k])} for k in sorted(set(v) - set(_VARIANT_DIRECT))
    ]
    quotes = _unique(
        [_shown_quote(c) for _d, c in decisions if c is not None and _d.quote]
        + [_shown_quote(c) for c in evidence]
    )
    return {
        "id": v["id"],
        "as_reported": v.get("as_reported"),
        "placement": v.get("placement"),
        "kind": v.get("kind"),
        "zygosity": v.get("zygosity"),
        "phase": with_text,
        "carriers": _carriers(v.get("carriers")),
        "phenotypes": _terms(v.get("phenotypes"), hpo),
        "placed": placed,
        "vv_note": note,
        "others": others_rows,
        "keys_shown": sorted(direct + [r["name"] for r in others_rows]),
        "state": "judged" if decisions else "agreed",
        "diffs": [
            {
                "field": d.field,
                "first": d.first,
                "verify": d.second,
                "judge": f"{_chose(d)}: {d.value}. {d.rule}",
            }
            for d, _c in decisions
        ],
        "quotes": quotes,
    }


def _finding_card(
    f: Mapping[str, Any],
    *,
    abstract_only: bool,
    shown_digest: str,
    approval: str,
    verified: bool,
    decisions: Sequence[JudgeDecision],
    checks: Sequence[QuoteCheck],
    paper: _Paper,
    pubs: Mapping[str, Mapping[str, Any]],
    hpo: Mapping[str, str],
) -> dict[str, Any]:
    pmid = str(f["publication"])
    variants: list[Mapping[str, Any]] = list(f.get("variants") or [])
    pub = pubs.get(pmid, {})
    authors = pub.get("authors") or []
    label = (
        " ".join(x for x in (f"{str(authors[0]).split()[0]} et al.", str(pub.get("year", ""))) if x)
        if authors
        else pmid
    )
    judge_checks = {(c.field, c.quote, c.where): c for c in checks if c.origin == "judge"}
    live = [d for d in decisions if not _settled_by_rule(d)]
    other_keys = sorted(set(f) - APPROVAL_KEYS - CARD_KEYS - set(_ROW_OF_FIELD))

    def check_of(d: JudgeDecision) -> QuoteCheck | None:
        return judge_checks.get((d.field, d.quote, d.where))

    on_variant: dict[int, list[tuple[JudgeDecision, QuoteCheck | None]]] = {}
    on_row: dict[str, list[JudgeDecision]] = {}
    on_finding: list[JudgeDecision] = []
    for d in live:
        vid = _variant_target(d.field, variants)
        if vid is not None:
            on_variant.setdefault(vid, []).append((d, check_of(d)))
        elif (row := _resolve_field(d.field, other_keys)) is not None and not (
            d.field.lower().startswith("variant")
        ):
            on_row.setdefault(row, []).append(d)
        else:
            on_finding.append(d)
    ev_row: dict[str, list[QuoteCheck]] = {}
    ev_variant: dict[int, list[QuoteCheck]] = {}
    unplaced: list[QuoteCheck] = []
    for c in (c for c in checks if c.origin == "evidence"):
        vid = _variant_target(c.field, variants)
        row = None if vid is not None else _resolve_field(c.field, other_keys)
        if vid is not None:
            ev_variant.setdefault(vid, []).append(c)
        elif row is not None and not c.field.lower().startswith("variant"):
            ev_row.setdefault(row, []).append(c)
        else:
            unplaced.append(c)

    rows: list[dict[str, Any]] = []
    keys_shown: set[str] = {k for k in CARD_KEYS if k in f}

    def add_row(name: str, keys: Sequence[str], value: str) -> None:
        mine = on_row.get(name, [])
        quotes = [_shown_quote(c) for c in ev_row.get(name, [])]
        present = [k for k in keys if k in f]
        if not present and not mine and not quotes:
            return
        keys_shown.update(present)
        row: dict[str, Any] = {
            "name": name,
            "value": value or ("(empty)" if present else ""),
            "keys": sorted(present),
            "quotes": quotes,
            "state": "agreed",
        }
        if mine:
            row["state"] = "judged"
            row["first"] = "; ".join(d.first for d in mine)
            row["verify"] = "; ".join(d.second for d in mine)
            row["judge"] = " | ".join(f"{_chose(d)}. {d.rule}" for d in mine)
            row["quotes"] = [
                _shown_quote(c) for d in mine if d.quote and (c := check_of(d)) is not None
            ] + quotes
        row["quotes"] = _unique(row["quotes"])
        rows.append(row)

    for spec in ROWS:
        add_row(spec.name, spec.keys, spec.get(f, hpo))
    for key in other_keys:
        add_row(key, (key,), _plain(f[key]))
    for d in on_finding:
        qc = check_of(d)
        rows.append(
            {
                "name": d.field,
                "value": d.value,
                "keys": [],
                "state": "judged",
                "first": d.first,
                "verify": d.second,
                "judge": f"{_chose(d)}. {d.rule}",
                "quotes": [_shown_quote(qc)] if d.quote and qc is not None else [],
            }
        )

    vouts = [
        _variant_card(
            v,
            variants,
            on_variant.get(int(v["id"]), []),
            ev_variant.get(int(v["id"]), []),
            hpo,
        )
        for v in variants
    ]
    loose = _unique([_shown_quote(c) for c in unplaced])
    every = [q for r in rows for q in r["quotes"]] + [q for v in vouts for q in v["quotes"]] + loose
    n_judged = sum(r["state"] == "judged" for r in rows) + sum(
        v["state"] == "judged" for v in vouts
    )
    carriers = f.get("carriers") or {}
    unit = f.get("count_unit", "")
    n_carriers = (
        sum(v for v in carriers.values() if isinstance(v, int))
        if isinstance(carriers, Mapping)
        else 0
    )
    headline = (
        f"{str(f.get('design', '')).replace('_', ' ')} · {f.get('screened')} {unit} screened"
        f" · {n_carriers}"
        f" carrier {unit}"
    ).strip()
    return {
        "id": f["id"],
        "pmid": pmid,
        "label": label,
        "title": str(pub.get("title", "")) or paper.get(pmid, "title"),
        "pmc": pub.get("pmcid"),
        "source": "abstract" if abstract_only else "full",
        "headline": headline,
        "summary": f.get("summary", ""),
        "fields": rows,
        "variants": vouts,
        "n_variants": len(vouts),
        "other_quotes": loose,
        "keys_shown": sorted(keys_shown),
        "n_judged": n_judged,
        "n_unchecked": sum(not q["checked"] for q in every),
        "n_not_found": sum(q["checked"] and not q["found"] for q in every),
        "approval": approval,
        "verified": verified,
        "shown_digest": shown_digest,
        "group": "judged" if n_judged else ("abstract" if abstract_only else "agreed"),
    }


_TRIAGE_RANK: Final = {"excluded": 1, "included": 2}


def _triage(entries: Sequence[Mapping[str, Any]], paper: _Paper) -> list[dict[str, Any]]:
    def key(e: Mapping[str, Any]) -> tuple[int, str, str]:
        decision = str(e["decision"])
        rank = _TRIAGE_RANK.get(decision, 0)
        return (rank, str(e.get("reason") or "") if rank == 1 else "", str(e["pmid"]))

    return [
        {
            "pmid": str(e["pmid"]),
            "year": paper.get(str(e["pmid"]), "year"),
            "title": paper.get(str(e["pmid"]), "title"),
            "decision": str(e["decision"]),
            "reason": str(e.get("reason") or "").replace("_", " "),
            "note": str(e.get("note") or ""),
        }
        for e in sorted(entries, key=key)
    ]


def _second_opinions(
    sources: Sequence[Mapping[str, Any]], gene: str, known: set[str]
) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for doc in sources:
        reviewer = doc.get("reviewer") or {}
        if doc.get("format") != 1 or reviewer.get("role") != "second_opinion":
            raise ValueError(
                "a second opinion must be a format 1 file by a second_opinion reviewer"
            )
        if doc.get("gene") != gene:
            raise ValueError(f"a second opinion is for {doc.get('gene')!r}, not {gene!r}")
        for item in doc.get("findings") or []:
            if item["id"] not in known:
                raise ValueError(f"a second opinion names {item['id']}, which the page lacks")
            out.setdefault(item["id"], []).append(
                {
                    "name": str(reviewer["name"]),
                    "decision": str(item["decision"]),
                    "note": str(item.get("note") or ""),
                }
            )
    return {
        k: sorted(v, key=lambda o: (o["name"], o["decision"], o["note"]))
        for k, v in sorted(out.items())
    }


def build_review_data(
    root: Path,
    run: RunPaths,
    digests: Digests,
    reviewer: Reviewer,
    built_on: datetime,
    atlas_commit: str | None,
    validate_line: str,
    second_opinions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The page's data for ``run``; see the module docstring for what a card carries."""
    if reviewer.role == "second_opinion" and second_opinions:
        raise ValueError("a second-opinion page shows no other reviewer's verdicts")
    lit_path = root / "curation/literature" / f"{run.symbol}.yaml"
    lit = _safe_load(lit_path)
    if not isinstance(lit, dict) or not isinstance(lit.get("findings"), list):
        raise ReviewDataError(f"{lit_path}: has no list of findings")
    hpo = _hpo_labels(root)
    pubs = _publications(root)
    triage_entries = [e for e in lit.get("triage") or [] if isinstance(e, dict)]
    paper = _Paper(pubs, {str(e["pmid"]): e for e in triage_entries})
    texts = {t.pmid: t for t in load_texts(run)}
    judge = load_judge(run)
    checks = check_quotes(run)
    by_digest = {d.id: d for d in digests.findings}

    cards: list[dict[str, Any]] = []
    for f in lit["findings"]:
        fid = str(f["id"])
        if fid not in by_digest:
            raise ReviewDataError(f"the atlas gave no digests for {fid}; a card needs its own")
        dg = by_digest[fid]
        pmid = str(f["publication"])
        text = texts.get(pmid)
        abstract_only = (text is not None and text.source == "abstract") or (
            f.get("review_status") == "not_accessible"
        )
        cards.append(
            _finding_card(
                f,
                abstract_only=abstract_only,
                shown_digest=dg.shown_digest,
                approval="void"
                if dg.approval_void
                else ("approved" if dg.approved else "unapproved"),
                verified=dg.verified,
                decisions=judge.get(fid, ()),
                checks=[c for c in checks if c.finding == fid],
                paper=paper,
                pubs=pubs,
                hpo=hpo,
            )
        )
    cards.sort(key=lambda c: (_GROUP_ORDER[c["group"]], -c["n_judged"], c["id"]))

    data: dict[str, Any] = {
        "page": {
            "gene": digests.gene,
            "symbol": digests.symbol,
            "built_on": built_on.isoformat(),
            "atlas_commit": atlas_commit,
            "literature_sha256": digests.literature_sha256,
            "validate": validate_line,
        },
        "reviewer": {"name": reviewer.name, "role": reviewer.role},
        "groups": [dict(g) for g in GROUPS],
        "papers_needed": [
            {**paper.info(t.pmid), "why": t.why}
            for t in sorted(texts.values(), key=lambda t: t.pmid)
            if t.needed
        ],
        "triage": _triage(triage_entries, paper),
        "findings": cards,
    }
    if reviewer.role == "owner":
        data["second_opinions"] = _second_opinions(
            second_opinions, digests.gene, {c["id"] for c in cards}
        )
    return data


__all__ = [
    "APPROVAL_KEYS",
    "ROWS",
    "ReviewDataError",
    "Reviewer",
    "build_review_data",
]
