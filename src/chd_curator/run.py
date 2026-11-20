"""The run directory: the files a curation run leaves behind, read through one contract.

A run lives at ``<root>/local/literature/runs/<SYMBOL>/<run id>/``. The review page is built
from it, and the phase 2 commands write it. Every loader checks its file's keys by hand and
raises :class:`RunFormatError` naming the file and the key, because a misspelt key that was
ignored would drop a quote or a decision from the page without a sound.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

RUN_FORMAT: Final = 1
_SOURCES: Final = ("full_text", "abstract", "supplied_pdf", "none")
_PAPER_KEYS: Final = frozenset(
    {"pmid", "source", "text", "abstract", "supplements", "needed", "why"}
)
# Optional bibliographic keys of a paper entry, for a paper `curation/publications.yaml` lacks.
_PAPER_OPTIONAL: Final = frozenset({"title", "year", "doi"})
_QUOTE_KEYS: Final = frozenset({"field", "quote", "where"})
_COMPARISON_KEYS: Final = frozenset({"format", "pairs", "agreed", "span_only", "differs"})
_JUDGE_KEYS: Final = frozenset(
    {"field", "first", "second", "decision", "value", "quote", "where", "rule"}
)
_DECISIONS: Final = ("first", "second", "neither")

Source = Literal["full_text", "abstract", "supplied_pdf", "none"]
Decision = Literal["first", "second", "neither"]


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
        return self.dir / "verify.yaml"

    @property
    def comparison(self) -> Path:
        return self.dir / "comparison.json"

    @property
    def judge(self) -> Path:
        return self.dir / "judge"

    @property
    def quotes(self) -> Path:
        return self.dir / "quotes.json"

    @property
    def review_html(self) -> Path:
        return self.dir / "review.html"

    @property
    def decisions(self) -> Path:
        return self.dir / "decisions"


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
        if pmid in seen:
            raise RunFormatError(f"{path}: paper {pmid} appears twice")
        seen.add(pmid)
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


def load_evidence(p: RunPaths) -> dict[str, tuple[Quote, ...]]:
    found: dict[str, tuple[Quote, ...]] = {}
    for path in _yaml_files(p.evidence, ".yaml"):
        data = _read_yaml(path)
        if not isinstance(data, dict):
            raise RunFormatError(f"{path}: the file is not a mapping of finding id to quotes")
        for finding_id, rows in data.items():
            if finding_id in found:
                raise RunFormatError(f"{path}: finding {finding_id} appears in two evidence files")
            if not isinstance(rows, list):
                raise RunFormatError(f"{path}: {finding_id} is not a list of quotes")
            found[str(finding_id)] = tuple(
                Quote(
                    _str(r["field"], path, "field"),
                    _str(r["quote"], path, "quote"),
                    _str(r["where"], path, "where"),
                )
                for r in (_keys(row, _QUOTE_KEYS, path, "a quote") for row in rows)
            )
    return found


def load_comparison(p: RunPaths) -> Comparison:
    path = p.comparison
    if not path.is_file():
        return Comparison({}, (), (), {})
    data = _keys(_read_json(path), _COMPARISON_KEYS, path, "the file")
    _check_format(data, path)
    for key in ("pairs", "differs"):
        if not isinstance(data[key], dict):
            raise RunFormatError(f"{path}: {key!r} is not an object")
    return Comparison(
        pairs={str(k): _str(v, path, "pairs") for k, v in data["pairs"].items()},
        agreed=_str_list(data["agreed"], path, "agreed"),
        span_only=_str_list(data["span_only"], path, "span_only"),
        differs={str(k): _str_list(v, path, "differs") for k, v in data["differs"].items()},
    )


def load_judge(p: RunPaths) -> dict[str, tuple[JudgeDecision, ...]]:
    found: dict[str, tuple[JudgeDecision, ...]] = {}
    for path in _yaml_files(p.judge, ".decisions.yaml"):
        data = _read_yaml(path)
        if not isinstance(data, dict):
            raise RunFormatError(f"{path}: the file is not a mapping of finding id to decisions")
        for finding_id, rows in data.items():
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
    return found
