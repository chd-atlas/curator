"""The run directory: the files a curation run leaves behind, read through one contract.

A run lives at ``<root>/local/literature/runs/<SYMBOL>/<run id>/``. The review page is built
from it, and the phase 2 commands write it. Every loader checks its file's keys by hand and
raises :class:`RunFormatError` naming the file and the key, because a misspelt key that was
ignored would drop a quote or a decision from the page without a sound.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator.atlas import plain
from chd_curator.notation import NotationChange

RUN_FORMAT: Final = 1
_PMID: Final = re.compile(r"PMID:[0-9]+")
_FINDING_ID: Final = re.compile(r"CHDA:LIT:[0-9]{7}")
_BATCH_ID: Final = re.compile(r"[0-9]{2,}")
_SOURCES: Final = ("full_text", "abstract", "supplied_pdf", "none")
_PAPER_KEYS: Final = frozenset(
    {"pmid", "source", "text", "abstract", "supplements", "needed", "why"}
)
# Optional bibliographic keys of a paper entry, for a paper `curation/publications.yaml` lacks.
_PAPER_OPTIONAL: Final = frozenset({"title", "year", "doi", "licence", "attempts"})
_ATTEMPT_KEYS: Final = frozenset({"via", "url", "outcome"})
_QUOTE_KEYS: Final = frozenset({"field", "quote", "where"})
_COMPARISON_KEYS: Final = frozenset({"format", "pairs", "agreed", "span_only", "differs"})
_COMPARISON_OPTIONAL: Final = frozenset({"only_first", "only_second"})
_JUDGE_KEYS: Final = frozenset(
    {"field", "first", "second", "decision", "value", "quote", "where", "rule"}
)
_DECISIONS: Final = ("first", "second", "neither")

Source = Literal["full_text", "abstract", "supplied_pdf", "none"]
Decision = Literal["first", "second", "neither"]
Mode = Literal["new", "backfill", "reread"]
_MODES: Final = ("new", "backfill", "reread")


class RunFormatError(Exception):
    """A run file is missing, unreadable or not in the contract's shape."""


@dataclass(frozen=True)
class RunPaths:
    root: Path
    symbol: str
    run_id: str

    @property
    def dir(self) -> Path:
        return self.root / "local/literature/runs" / self.symbol / self.run_id

    @property
    def texts(self) -> Path:
        return self.dir / "texts.json"

    @property
    def evidence(self) -> Path:
        return self.dir / "evidence"

    @property
    def verify(self) -> Path:
        return self.dir / "verify"

    @property
    def comparison(self) -> Path:
        return self.dir / "comparison.json"

    @property
    def judge(self) -> Path:
        return self.dir / "judge"

    @property
    def quotes(self) -> Path:
        """Written by ``chd-curator quotes``; nothing reads it back."""
        return self.dir / "quotes.json"

    @property
    def review_html(self) -> Path:
        return self.dir / "review.html"

    @property
    def normalise(self) -> Path:
        return self.dir / "normalise.json"

    @property
    def decisions(self) -> Path:
        return self.dir / "decisions"

    @property
    def batches_json(self) -> Path:
        return self.dir / "batches.json"

    @property
    def batches_dir(self) -> Path:
        return self.dir / "batches"

    @property
    def extract(self) -> Path:
        return self.dir / "extract"

    @property
    def merge(self) -> Path:
        return self.dir / "merge.json"

    @property
    def judging(self) -> Path:
        return self.dir / "judging"

    @property
    def assemble(self) -> Path:
        return self.dir / "assemble.json"


@dataclass(frozen=True)
class Attempt:
    """One try for a paper (a request, a supplied file, a member of a supplement zip), and how
    it ended."""

    via: str
    url: str
    outcome: str


@dataclass(frozen=True)
class PaperText:
    pmid: str
    source: Source
    text: Path | None
    abstract: Path | None
    supplements: tuple[Path, ...]
    needed: bool
    why: str
    title: str = ""
    year: str = ""
    doi: str = ""
    licence: str | None = None
    attempts: tuple[Attempt, ...] = ()


@dataclass(frozen=True)
class Quote:
    field: str
    quote: str
    where: str


@dataclass(frozen=True)
class Comparison:
    pairs: dict[str, str]
    agreed: tuple[str, ...]
    span_only: tuple[str, ...]
    differs: dict[str, tuple[str, ...]]
    only_first: tuple[str, ...] = ()
    only_second: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeDecision:
    field: str
    first: str
    second: str
    decision: Decision
    value: str
    quote: str
    where: str
    rule: str


def _keys(
    data: Any,
    expected: frozenset[str],
    path: Path,
    what: str,
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RunFormatError(f"{path}: {what} is not an object")
    extra, missing = sorted(set(data) - expected - optional), sorted(expected - set(data))
    if extra or missing:
        raise RunFormatError(f"{path}: {what} has unexpected key(s) {extra}, missing {missing}")
    return data


def _str(value: Any, path: Path, key: str) -> str:
    if not isinstance(value, str):
        raise RunFormatError(f"{path}: {key!r} is not a string: {value!r}")
    return value


def _str_list(value: Any, path: Path, key: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RunFormatError(f"{path}: {key!r} is not a list: {value!r}")
    return tuple(_str(v, path, key) for v in value)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunFormatError(f"{path}: cannot be read as JSON: {exc}") from exc


def _read_yaml(path: Path) -> Any:
    try:
        return YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise RunFormatError(f"{path}: cannot be read as YAML: {exc}") from exc


def _check_format(data: dict[str, Any], path: Path) -> None:
    if type(data["format"]) is not int or data["format"] != RUN_FORMAT:
        raise RunFormatError(f"{path}: format is {data['format']!r}; curator reads {RUN_FORMAT}")


def _relative(root: Path, value: Any, path: Path, key: str) -> Path | None:
    if value is None:
        return None
    return root / _str(value, path, key)


def load_texts(p: RunPaths) -> tuple[PaperText, ...]:
    path = p.texts
    if not path.is_file():
        raise RunFormatError(f"{path}: the run has no texts.json")
    top = _keys(_read_json(path), frozenset({"format", "papers"}), path, "the file")
    _check_format(top, path)
    if not isinstance(top["papers"], list):
        raise RunFormatError(f"{path}: 'papers' is not a list")
    papers: list[PaperText] = []
    seen: set[str] = set()
    for entry in top["papers"]:
        row = _keys(entry, _PAPER_KEYS, path, "a paper", _PAPER_OPTIONAL)
        source = _str(row["source"], path, "source")
        if source not in _SOURCES:
            raise RunFormatError(f"{path}: 'source' is {source!r}; expected one of {_SOURCES}")
        if type(row["needed"]) is not bool:
            raise RunFormatError(f"{path}: 'needed' is not a boolean: {row['needed']!r}")
        year = row.get("year", "")
        if type(year) is int:
            year = str(year)
        pmid = _str(row["pmid"], path, "pmid")
        if not _PMID.fullmatch(pmid):
            raise RunFormatError(f"{path}: 'pmid' is {pmid!r}; expected PMID:<digits>")
        if pmid in seen:
            raise RunFormatError(f"{path}: paper {pmid} appears twice")
        seen.add(pmid)
        licence = row.get("licence")
        if licence is not None:
            licence = _str(licence, path, "licence")
        attempts = row.get("attempts", [])
        if not isinstance(attempts, list):
            raise RunFormatError(f"{path}: 'attempts' is not a list: {attempts!r}")
        papers.append(
            PaperText(
                pmid=pmid,
                source=cast(Source, source),
                text=_relative(p.root, row["text"], path, "text"),
                abstract=_relative(p.root, row["abstract"], path, "abstract"),
                supplements=tuple(
                    p.root / s for s in _str_list(row["supplements"], path, "supplements")
                ),
                needed=row["needed"],
                why=_str(row["why"], path, "why"),
                title=_str(row.get("title", ""), path, "title"),
                year=_str(year, path, "year"),
                doi=_str(row.get("doi", ""), path, "doi"),
                licence=licence,
                attempts=tuple(
                    Attempt(
                        _str(a["via"], path, "via"),
                        _str(a["url"], path, "url"),
                        _str(a["outcome"], path, "outcome"),
                    )
                    for a in (_keys(item, _ATTEMPT_KEYS, path, "an attempt") for item in attempts)
                ),
            )
        )
    return tuple(papers)


def _yaml_files(directory: Path, suffix: str) -> list[Path]:
    if not directory.is_dir():
        return []
    files = sorted(f for f in directory.iterdir() if f.is_file())
    for f in files:
        if not f.name.endswith(suffix):
            raise RunFormatError(
                f"{f}: not a *{suffix} file; a misnamed file would drop its contents "
                "from the page without a sound"
            )
    return files


def _finding_id(value: Any, path: Path, what: str) -> str:
    if not isinstance(value, str) or not _FINDING_ID.fullmatch(value):
        raise RunFormatError(f"{path}: {what} is {value!r}; expected CHDA:LIT:<7 digits>")
    return value


def _pmid(value: Any, path: Path, what: str) -> str:
    if not isinstance(value, str) or not _PMID.fullmatch(value):
        raise RunFormatError(f"{path}: {what} is {value!r}; expected PMID:<digits>")
    return value


def _int(value: Any, path: Path, key: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise RunFormatError(f"{path}: {key!r} is not an integer >= {minimum}: {value!r}")
    return value


def _list(value: Any, path: Path, key: str) -> list[Any]:
    if not isinstance(value, list):
        raise RunFormatError(f"{path}: {key!r} is not a list: {value!r}")
    return value


def _mode(value: Any, path: Path) -> Mode:
    if value not in _MODES:
        raise RunFormatError(f"{path}: 'mode' is {value!r}; expected one of {_MODES}")
    return cast(Mode, value)


def _ids(value: Any, path: Path, key: str) -> tuple[str, ...]:
    return tuple(_finding_id(v, path, key) for v in _list(value, path, key))


def _pmids(value: Any, path: Path, key: str) -> tuple[str, ...]:
    return tuple(_pmid(v, path, key) for v in _list(value, path, key))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def _write_yaml(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.width = 1_000_000
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(obj, handle)


def load_evidence_file(path: Path) -> dict[str, tuple[Quote, ...]]:
    """One evidence file: finding id -> its quotes. Every key must be a final-shaped id."""
    data = _read_yaml(path)
    if not isinstance(data, dict):
        raise RunFormatError(f"{path}: the file is not a mapping of finding id to quotes")
    found: dict[str, tuple[Quote, ...]] = {}
    for finding_id, rows in data.items():
        _finding_id(finding_id, path, "an evidence key")
        if not isinstance(rows, list):
            raise RunFormatError(f"{path}: {finding_id} is not a list of quotes")
        found[finding_id] = tuple(
            Quote(
                _str(r["field"], path, "field"),
                _str(r["quote"], path, "quote"),
                _str(r["where"], path, "where"),
            )
            for r in (_keys(row, _QUOTE_KEYS, path, "a quote") for row in rows)
        )
    return found


def load_evidence(p: RunPaths) -> dict[str, tuple[Quote, ...]]:
    found: dict[str, tuple[Quote, ...]] = {}
    for path in _yaml_files(p.evidence, ".yaml"):
        for finding_id, quotes in load_evidence_file(path).items():
            if finding_id in found:
                raise RunFormatError(f"{path}: finding {finding_id} appears in two evidence files")
            found[finding_id] = quotes
    return found


def write_evidence(p: RunPaths, batch_id: str, quotes: Mapping[str, Sequence[Quote]]) -> None:
    """Write ``evidence/batch-<id>.yaml`` (finding id -> quotes)."""
    _write_yaml(
        p.evidence / f"batch-{batch_id}.yaml",
        {
            finding_id: [{"field": q.field, "quote": q.quote, "where": q.where} for q in rows]
            for finding_id, rows in quotes.items()
        },
    )


def load_comparison(p: RunPaths) -> Comparison:
    path = p.comparison
    if not path.is_file():
        return Comparison({}, (), (), {})
    data = _keys(_read_json(path), _COMPARISON_KEYS, path, "the file", _COMPARISON_OPTIONAL)
    _check_format(data, path)
    for key in ("pairs", "differs"):
        if not isinstance(data[key], dict):
            raise RunFormatError(f"{path}: {key!r} is not an object")
    return Comparison(
        pairs={str(k): _str(v, path, "pairs") for k, v in data["pairs"].items()},
        agreed=_str_list(data["agreed"], path, "agreed"),
        span_only=_str_list(data["span_only"], path, "span_only"),
        differs={str(k): _str_list(v, path, "differs") for k, v in data["differs"].items()},
        only_first=_str_list(data.get("only_first", []), path, "only_first"),
        only_second=_str_list(data.get("only_second", []), path, "only_second"),
    )


def write_comparison(p: RunPaths, c: Comparison) -> None:
    _write_json(
        p.comparison,
        {
            "format": RUN_FORMAT,
            "pairs": c.pairs,
            "agreed": list(c.agreed),
            "span_only": list(c.span_only),
            "differs": {k: list(v) for k, v in c.differs.items()},
            "only_first": list(c.only_first),
            "only_second": list(c.only_second),
        },
    )


_NORMALISE_KEYS: Final = frozenset({"format", "normalisation", "variants"})
_NORMALISE_VARIANT_KEYS: Final = frozenset({"finding", "variant", "outcome", "line", "reading"})
_OUTCOMES: Final = ("placed", "unplaced", "skipped")


def load_normalise(p: RunPaths) -> dict[tuple[str, int], str | None]:
    """The run's ``normalise.json`` as ``(finding id, variant id) -> reading`` (None when the
    variant carries none); an empty mapping when the run has no such file."""
    path = p.normalise
    if not path.is_file():
        return {}
    data = _keys(_read_json(path), _NORMALISE_KEYS, path, "the file")
    _check_format(data, path)
    normalisation = data["normalisation"]
    if normalisation is not None and (
        not isinstance(normalisation, int) or isinstance(normalisation, bool)
    ):
        raise RunFormatError(
            f"{path}: 'normalisation' is not an integer or null: {normalisation!r}"
        )
    entries = data["variants"]
    if not isinstance(entries, list):
        raise RunFormatError(f"{path}: 'variants' is not a list")
    readings: dict[tuple[str, int], str | None] = {}
    for entry in entries:
        entry = _keys(entry, _NORMALISE_VARIANT_KEYS, path, "a 'variants' entry")
        finding = _str(entry["finding"], path, "finding")
        variant = entry["variant"]
        if not isinstance(variant, int) or isinstance(variant, bool):
            raise RunFormatError(f"{path}: 'variant' is not an integer: {variant!r}")
        if entry["outcome"] not in _OUTCOMES:
            raise RunFormatError(
                f"{path}: 'outcome' is not one of {_OUTCOMES}: {entry['outcome']!r}"
            )
        _str(entry["line"], path, "line")
        if (finding, variant) in readings:
            raise RunFormatError(f"{path}: lists {finding} variant {variant} twice")
        reading = entry["reading"]
        readings[(finding, variant)] = None if reading is None else _str(reading, path, "reading")
    return readings


def load_judge(p: RunPaths) -> dict[str, tuple[JudgeDecision, ...]]:
    """The judges' decisions by finding id.

    A judge keys the decisions for a finding it added by its own local id (``CHDA:LIT:07...``).
    When the run has an ``assemble.json``, such a key is read under the id ``assemble`` gave the
    finding in the literature file, because the review page and the quote check look findings up
    there. A key ``assemble.json`` does not map stays as it is, so a run not yet assembled is
    refused by naming that id; a mapped key that is already another key is refused."""
    found: dict[str, tuple[JudgeDecision, ...]] = {}
    for path in _yaml_files(p.judge, ".decisions.yaml"):
        data = _read_yaml(path)
        if not isinstance(data, dict):
            raise RunFormatError(f"{path}: the file is not a mapping of finding id to decisions")
        for finding_id, rows in data.items():
            _finding_id(finding_id, path, "a judge key")
            if finding_id in found:
                raise RunFormatError(f"{path}: finding {finding_id} appears in two judge files")
            if not isinstance(rows, list):
                raise RunFormatError(f"{path}: {finding_id} is not a list of decisions")
            decisions: list[JudgeDecision] = []
            for row in rows:
                r = _keys(row, _JUDGE_KEYS, path, "a decision")
                decision = _str(r["decision"], path, "decision")
                if decision not in _DECISIONS:
                    raise RunFormatError(
                        f"{path}: 'decision' is {decision!r}; expected one of {_DECISIONS}"
                    )
                decisions.append(
                    JudgeDecision(
                        field=_str(r["field"], path, "field"),
                        first=_str(r["first"], path, "first"),
                        second=_str(r["second"], path, "second"),
                        decision=cast(Decision, decision),
                        value=_str(r["value"], path, "value"),
                        quote=_str(r["quote"], path, "quote"),
                        where=_str(r["where"], path, "where"),
                        rule=_str(r["rule"], path, "rule"),
                    )
                )
            found[str(finding_id)] = tuple(decisions)
    assembled = load_assemble(p)
    if assembled is None or not assembled.added:
        return found
    mapped: dict[str, tuple[JudgeDecision, ...]] = {}
    for key, decisions_ in found.items():
        final = assembled.added.get(key, key)
        if final in mapped:
            raise RunFormatError(
                f"{p.assemble}: maps {key} to {final}, but the judge files already decide {final}"
            )
        mapped[final] = decisions_
    return mapped


@dataclass(frozen=True)
class Batch:
    id: str
    papers: tuple[str, ...]
    findings: tuple[str, ...]
    text_bytes: int


@dataclass(frozen=True)
class BatchPlan:
    mode: Mode
    fields: tuple[str, ...]
    budget_bytes: int
    batches: tuple[Batch, ...]


_BATCHES_KEYS: Final = frozenset({"format", "mode", "fields", "budget_bytes", "batches"})
_BATCH_KEYS: Final = frozenset({"id", "papers", "findings", "text_bytes"})


def load_batches(p: RunPaths) -> BatchPlan:
    path = p.batches_json
    if not path.is_file():
        raise RunFormatError(f"{path}: the run has no batches.json")
    data = _keys(_read_json(path), _BATCHES_KEYS, path, "the file")
    _check_format(data, path)
    batches: list[Batch] = []
    seen: set[str] = set()
    for entry in _list(data["batches"], path, "batches"):
        row = _keys(entry, _BATCH_KEYS, path, "a batch")
        batch_id = _str(row["id"], path, "id")
        if not _BATCH_ID.fullmatch(batch_id):
            raise RunFormatError(f"{path}: batch 'id' is {batch_id!r}; expected two or more digits")
        if batch_id in seen:
            raise RunFormatError(f"{path}: batch {batch_id} appears twice")
        seen.add(batch_id)
        batches.append(
            Batch(
                batch_id,
                _pmids(row["papers"], path, "papers"),
                _ids(row["findings"], path, "findings"),
                _int(row["text_bytes"], path, "text_bytes", 0),
            )
        )
    return BatchPlan(
        _mode(data["mode"], path),
        _str_list(data["fields"], path, "fields"),
        _int(data["budget_bytes"], path, "budget_bytes", 1),
        tuple(batches),
    )


def write_batches(p: RunPaths, plan: BatchPlan) -> None:
    _write_json(
        p.batches_json,
        {
            "format": RUN_FORMAT,
            "mode": plan.mode,
            "fields": list(plan.fields),
            "budget_bytes": plan.budget_bytes,
            "batches": [
                {
                    "id": b.id,
                    "papers": list(b.papers),
                    "findings": list(b.findings),
                    "text_bytes": b.text_bytes,
                }
                for b in plan.batches
            ],
        },
    )


@dataclass(frozen=True)
class PaperOutcome:
    """What a reader concluded about one paper: its study type and how it was read, or why it
    was excluded as ``(reason, note)``."""

    pmid: str
    study_type: str | None
    reading: str
    exclude: tuple[str, str] | None


_OUTCOME_KEYS: Final = frozenset({"pmid", "study_type", "reading"})
_EXCLUDE_ENTRY_KEYS: Final = frozenset({"pmid", "exclude"})
_EXCLUDE_KEYS: Final = frozenset({"reason", "note"})


def _non_empty(value: Any, path: Path, key: str) -> str:
    text = _str(value, path, key)
    if not text:
        raise RunFormatError(f"{path}: {key!r} is empty")
    return text


def load_papers(path: Path) -> tuple[PaperOutcome, ...]:
    """An agent's ``papers.json``: a list; each entry ``{pmid, study_type, reading}`` or
    ``{pmid, exclude: {reason, note}}``. Whether the reason is a valid one is not checked here."""
    data = _read_json(path)
    outcomes: list[PaperOutcome] = []
    seen: set[str] = set()
    for entry in _list(data, path, "the file"):
        if isinstance(entry, dict) and "exclude" in entry:
            row = _keys(entry, _EXCLUDE_ENTRY_KEYS, path, "an excluded paper")
            why = _keys(row["exclude"], _EXCLUDE_KEYS, path, "'exclude'")
            outcome = PaperOutcome(
                _pmid(row["pmid"], path, "pmid"),
                None,
                "",
                (_non_empty(why["reason"], path, "reason"), _non_empty(why["note"], path, "note")),
            )
        else:
            row = _keys(entry, _OUTCOME_KEYS, path, "a paper")
            study_type = row["study_type"]
            outcome = PaperOutcome(
                _pmid(row["pmid"], path, "pmid"),
                None if study_type is None else _non_empty(study_type, path, "study_type"),
                _non_empty(row["reading"], path, "reading"),
                None,
            )
        if outcome.pmid in seen:
            raise RunFormatError(f"{path}: paper {outcome.pmid} appears twice")
        seen.add(outcome.pmid)
        outcomes.append(outcome)
    return tuple(outcomes)


def load_pass(path: Path) -> list[dict[str, Any]]:
    """An agent's findings file for a new or reread pass: ``findings:`` is a list of mappings,
    each with a distinct ``id``. The rest of a finding is checked later, against the schema."""
    data = _keys(_read_yaml(path), frozenset({"findings"}), path, "the file")
    rows = _list(data["findings"], path, "findings")
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RunFormatError(f"{path}: a finding is not a mapping: {row!r}")
        finding_id = _finding_id(row.get("id"), path, "a finding's 'id'")
        if finding_id in seen:
            raise RunFormatError(f"{path}: finding {finding_id} appears twice")
        seen.add(finding_id)
        found.append(cast(dict[str, Any], plain(row)))
    return found


def load_backfill_pass(path: Path) -> dict[str, dict[str, Any]]:
    """An agent's backfill file: ``findings:`` maps a final id to a non-empty mapping of the
    fields it fills."""
    data = _keys(_read_yaml(path), frozenset({"findings"}), path, "the file")
    rows = data["findings"]
    if not isinstance(rows, dict):
        raise RunFormatError(f"{path}: 'findings' is not a mapping of finding id to fields")
    found: dict[str, dict[str, Any]] = {}
    for finding_id, fields in rows.items():
        _finding_id(finding_id, path, "a finding key")
        if not isinstance(fields, dict) or not fields:
            raise RunFormatError(f"{path}: {finding_id} is not a non-empty mapping of fields")
        found[finding_id] = cast(dict[str, Any], plain(fields))
    return found


@dataclass(frozen=True)
class TriageChange:
    pmid: str
    before: str
    after: str
    reason: str | None
    note: str | None


@dataclass(frozen=True)
class Replacement:
    pmid: str
    old: tuple[str, ...]
    new: tuple[str, ...]


@dataclass(frozen=True)
class MergeRecord:
    mode: Mode
    ids: dict[str, str]
    publications_added: tuple[str, ...]
    triage: tuple[TriageChange, ...]
    replaces: tuple[Replacement, ...]


_MERGE_KEYS: Final = frozenset(
    {"format", "mode", "ids", "publications_added", "triage", "replaces"}
)
_TRIAGE_KEYS: Final = frozenset({"pmid", "before", "after", "reason", "note"})
_REPLACE_KEYS: Final = frozenset({"pmid", "old", "new"})


def _str_or_null(value: Any, path: Path, key: str) -> str | None:
    return None if value is None else _str(value, path, key)


def load_merge(p: RunPaths) -> MergeRecord | None:
    path = p.merge
    if not path.is_file():
        return None
    data = _keys(_read_json(path), _MERGE_KEYS, path, "the file")
    _check_format(data, path)
    if not isinstance(data["ids"], dict):
        raise RunFormatError(f"{path}: 'ids' is not an object")
    ids = {
        _finding_id(k, path, "an 'ids' key"): _finding_id(v, path, "an 'ids' value")
        for k, v in data["ids"].items()
    }
    triage = tuple(
        TriageChange(
            _pmid(row["pmid"], path, "pmid"),
            _str(row["before"], path, "before"),
            _str(row["after"], path, "after"),
            _str_or_null(row["reason"], path, "reason"),
            _str_or_null(row["note"], path, "note"),
        )
        for row in (
            _keys(e, _TRIAGE_KEYS, path, "a triage change")
            for e in _list(data["triage"], path, "triage")
        )
    )
    replaces = tuple(
        Replacement(
            _pmid(row["pmid"], path, "pmid"),
            _ids(row["old"], path, "old"),
            _ids(row["new"], path, "new"),
        )
        for row in (
            _keys(e, _REPLACE_KEYS, path, "a replacement")
            for e in _list(data["replaces"], path, "replaces")
        )
    )
    return MergeRecord(
        _mode(data["mode"], path),
        ids,
        _pmids(data["publications_added"], path, "publications_added"),
        triage,
        replaces,
    )


def write_merge(p: RunPaths, record: MergeRecord) -> None:
    _write_json(
        p.merge,
        {
            "format": RUN_FORMAT,
            "mode": record.mode,
            "ids": record.ids,
            "publications_added": list(record.publications_added),
            "triage": [
                {
                    "pmid": t.pmid,
                    "before": t.before,
                    "after": t.after,
                    "reason": t.reason,
                    "note": t.note,
                }
                for t in record.triage
            ],
            "replaces": [
                {"pmid": r.pmid, "old": list(r.old), "new": list(r.new)} for r in record.replaces
            ],
        },
    )


@dataclass(frozen=True)
class AssembleRecord:
    """What an assemble did. ``judged`` maps each final id a judge's finding decides (a
    replaced or an added finding) to the sha256 hex of that finding as the judge wrote it:
    its parsed mapping as sorted-key compact JSON, UTF-8. ``approvals`` maps the same ids to the
    ``approved_digest`` the file's finding carried just after that assemble wrote it (None when
    it carried none)."""

    replaced: tuple[str, ...]
    added: dict[str, str]
    notation: tuple[NotationChange, ...]
    verified: tuple[str, ...]
    verifier: str | None
    judged: dict[str, str]
    approvals: dict[str, str | None]


_ASSEMBLE_KEYS: Final = frozenset(
    {"format", "replaced", "added", "notation", "verified", "verifier", "judged", "approvals"}
)
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_NOTATION_KEYS: Final = frozenset({"finding", "variant", "field", "before", "after", "rules"})


def load_assemble(p: RunPaths) -> AssembleRecord | None:
    path = p.assemble
    if not path.is_file():
        return None
    data = _keys(_read_json(path), _ASSEMBLE_KEYS, path, "the file")
    _check_format(data, path)
    if not isinstance(data["added"], dict):
        raise RunFormatError(f"{path}: 'added' is not an object")
    added = {
        _finding_id(k, path, "an 'added' key"): _finding_id(v, path, "an 'added' value")
        for k, v in data["added"].items()
    }
    if not isinstance(data["judged"], dict):
        raise RunFormatError(f"{path}: 'judged' is not an object")
    judged: dict[str, str] = {}
    for key, digest in data["judged"].items():
        fid = _finding_id(key, path, "a 'judged' key")
        if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
            raise RunFormatError(
                f"{path}: 'judged' {fid} is {digest!r}; expected 64 lowercase hex digits"
            )
        judged[fid] = digest
    if not isinstance(data["approvals"], dict):
        raise RunFormatError(f"{path}: 'approvals' is not an object")
    approvals: dict[str, str | None] = {}
    for key, digest in data["approvals"].items():
        fid = _finding_id(key, path, "an 'approvals' key")
        approvals[fid] = None if digest is None else _str(digest, path, f"approvals {fid}")
    changes: list[NotationChange] = []
    for entry in _list(data["notation"], path, "notation"):
        row = _keys(entry, _NOTATION_KEYS, path, "a notation change")
        variant = row["variant"]
        changes.append(
            NotationChange(
                _finding_id(row["finding"], path, "finding"),
                None if variant is None else _int(variant, path, "variant", 0),
                _str(row["field"], path, "field"),
                _str(row["before"], path, "before"),
                _str(row["after"], path, "after"),
                _str_list(row["rules"], path, "rules"),
            )
        )
    return AssembleRecord(
        _ids(data["replaced"], path, "replaced"),
        added,
        tuple(changes),
        _ids(data["verified"], path, "verified"),
        _str_or_null(data["verifier"], path, "verifier"),
        judged,
        approvals,
    )


def write_assemble(p: RunPaths, record: AssembleRecord) -> None:
    _write_json(
        p.assemble,
        {
            "format": RUN_FORMAT,
            "replaced": list(record.replaced),
            "added": record.added,
            "notation": [
                {
                    "finding": n.finding,
                    "variant": n.variant,
                    "field": n.field,
                    "before": n.before,
                    "after": n.after,
                    "rules": list(n.rules),
                }
                for n in record.notation
            ],
            "verified": list(record.verified),
            "verifier": record.verifier,
            "judged": record.judged,
            "approvals": record.approvals,
        },
    )
