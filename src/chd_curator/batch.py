"""``chd-curator batch``: split a run's papers into reading batches under a text budget.

Pure planning (:func:`plan_batches`) is kept apart from the selection of papers and the
manifests (:func:`prepare_batches`, :func:`write_plan`). Every refusal is a
:class:`BatchError`, raised before anything is written, so a refused run leaves every file as it
was. A manifest is what both blind readers open: it names the paper's texts and, for a backfill,
the findings as curated, and names no other file of the run.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator.atlas import plain
from chd_curator.diff import DiffError, finding_schema
from chd_curator.fs import write_bytes_atomically
from chd_curator.records import is_empty, literature_path, pmid_key
from chd_curator.run import (
    Batch,
    BatchPlan,
    Mode,
    PaperText,
    RunPaths,
    load_texts,
    write_batches,
)

# Finding properties a backfill may not re-read: identity, and who extracted or approved what.
FORBIDDEN_FIELDS: Final = (
    "id",
    "publication",
    "approved_by",
    "approved_on",
    "approved_digest",
    "extracted_by",
    "verified_by",
)
# Dropped from the findings a backfill manifest shows: a reader needs none of the approval.
_APPROVAL_FIELDS: Final = ("approved_by", "approved_on", "approved_digest")
_TEXT_LABEL: Final = {"full_text": "full text", "supplied_pdf": "supplied PDF"}


class BatchError(Exception):
    """A refusal; the command exits 2 and has written nothing."""


@dataclass(frozen=True)
class Prepared:
    plan: BatchPlan
    manifests: dict[str, str]  # batch id -> manifest text


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise BatchError(f"{path}: cannot be read: {exc}") from exc


def _counted(paper: PaperText) -> int:
    path = paper.text or paper.abstract
    if path is None:
        raise BatchError(f"{paper.pmid}: has neither a text nor an abstract in texts.json")
    return _size(path)


def plan_batches(
    papers: Sequence[PaperText], years: Mapping[str, int], budget_bytes: int
) -> tuple[Batch, ...]:
    """Pack papers, in (year, PMID number) order, into batches of at most ``budget_bytes``.

    A paper whose supplements together exceed the budget gets a batch alone; so does one whose own
    text exceeds it, because it can join no open batch and no later paper can join it.
    ``text_bytes`` counts main texts only. ``findings`` is left empty for the caller to fill.
    """
    ordered = sorted(papers, key=lambda p: (years[p.pmid], pmid_key(p.pmid)))
    groups: list[tuple[list[str], int]] = []
    joinable = False  # whether the last group is still open to the next paper
    for paper in ordered:
        size = _counted(paper)
        if sum(_size(s) for s in paper.supplements) > budget_bytes:
            groups.append(([paper.pmid], size))
            joinable = False
        elif joinable and groups[-1][1] + size <= budget_bytes:
            groups[-1][0].append(paper.pmid)
            groups[-1] = (groups[-1][0], groups[-1][1] + size)
        else:
            groups.append(([paper.pmid], size))
            joinable = True
    return tuple(
        Batch(f"{n:02d}", tuple(pmids), (), total) for n, (pmids, total) in enumerate(groups, 1)
    )


def _load_yaml(path: Path) -> Any:
    try:
        return YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise BatchError(f"{path}: cannot be read as YAML: {exc}") from exc


def _dump_yaml(data: Any) -> str:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.sort_base_mapping_type_on_output = False  # type: ignore[assignment]
    yaml.width = 1_000_000
    buffer = io.StringIO()
    yaml.dump(data, buffer)
    return buffer.getvalue()


def _publications(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "curation" / "publications.yaml"
    if not path.is_file():
        return {}
    data = _load_yaml(path)
    rows = data.get("publications") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise BatchError(f"{path}: has no 'publications' list")
    return {str(r["id"]): r for r in rows if isinstance(r, dict) and "id" in r}


def _check_fields(root: Path, fields: Sequence[str]) -> None:
    path = root / "schemas" / "gene_literature_file.schema.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        known = set(finding_schema(schema)["properties"])
    except (OSError, ValueError, DiffError) as exc:
        raise BatchError(f"cannot read the atlas's exported schema {path}: {exc}") from exc
    for name in fields:
        if name in FORBIDDEN_FIELDS:
            raise BatchError(f"--fields {name!r}: a backfill may not re-read it")
        if name not in known:
            raise BatchError(f"--fields {name!r}: not a Finding property of the exported schema")


def _manifest(
    p: RunPaths,
    batch: Batch,
    papers: Mapping[str, PaperText],
    info: Mapping[str, tuple[int, str]],
    mode: Mode,
    fields: Sequence[str],
    findings: Mapping[str, Any],
) -> str:
    lines = [f"# Batch {batch.id} of run {p.run_id} (gene {p.symbol}, mode {mode})", ""]
    if mode == "backfill":
        lines += [f"Fields to re-read: {', '.join(fields)}", ""]
    for pmid in batch.papers:
        paper = papers[pmid]
        year, title = info[pmid]
        which = _TEXT_LABEL.get(paper.source, "abstract only") if paper.text else "abstract only"
        lines += [f"## {pmid}", "", f"- year: {year}", f"- title: {title}", f"- text: {which}"]
        if paper.text:
            lines.append(f"- text path: {paper.text}")
        if paper.abstract:
            lines.append(f"- abstract path: {paper.abstract}")
        lines += [f"- supplement: {s} ({_size(s)} bytes)" for s in paper.supplements]
        lines.append("")
        if mode == "backfill":
            shown = [f for f in batch.findings if findings[f]["publication"] == pmid]
            lines += ["Findings as curated:", "", "```yaml"]
            for fid in shown:
                lines.append(_dump_yaml([findings[fid]]).rstrip("\n"))
            lines += ["```", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def prepare_batches(
    p: RunPaths,
    mode: Mode,
    fields: Sequence[str],
    finding_ids: Sequence[str],
    budget_bytes: int,
) -> Prepared:
    """Select the papers, plan the batches and render the manifests. Reads only."""
    if mode not in ("new", "backfill"):
        raise BatchError(f"--mode {mode!r} is not planned by this command; use new or backfill")
    if mode == "new" and (fields or finding_ids):
        raise BatchError("--fields and --findings belong to --mode backfill")
    if mode == "backfill" and not fields:
        raise BatchError("--mode backfill needs --fields")
    if p.batches_json.exists():
        raise BatchError(f"{p.batches_json}: the run is already batched; nothing was written")
    if p.batches_dir.is_dir() and any(p.batches_dir.iterdir()):
        left = sorted(f.name for f in p.batches_dir.iterdir())[0]
        raise BatchError(f"{p.batches_dir / left}: a leftover of an earlier run; remove it first")
    if mode == "backfill":
        _check_fields(p.root, fields)
    texts = {t.pmid: t for t in load_texts(p)}
    path = literature_path(p.root, p.symbol)
    if not path.is_file():
        raise BatchError(f"{path}: no literature file for {p.symbol}")
    literature = plain(_load_yaml(path))
    if not isinstance(literature, dict):
        raise BatchError(f"{path}: is not a mapping")
    findings = {str(f["id"]): f for f in literature.get("findings") or [] if "id" in f}
    ordered_ids = list(findings)
    if mode == "new":
        cited = {f["publication"] for f in findings.values()}
        selected = [
            str(t["pmid"])
            for t in literature.get("triage") or []
            if t.get("decision") == "included" and t["pmid"] not in cited
        ]
        chosen: list[str] = []
    else:
        unknown = [f for f in finding_ids if f not in findings]
        if unknown:
            raise BatchError(f"{path}: no finding {unknown[0]}")
        # A backfill fills declared fields a finding lacks and never rewrites a filled one
        # (merge refuses it), so a reader is not spent on a finding merge would refuse.
        for fid in finding_ids:
            for name in fields:
                if not is_empty(findings[fid].get(name)):
                    raise BatchError(
                        f"{path}: {fid} already holds {name}; "
                        "a backfill never rewrites a filled field"
                    )
        chosen = list(finding_ids) or [
            f for f in ordered_ids if all(is_empty(findings[f].get(n)) for n in fields)
        ]
        wanted = set(chosen)
        chosen = [f for f in ordered_ids if f in wanted]
        selected = sorted({str(findings[f]["publication"]) for f in chosen})
    if not selected:
        raise BatchError(f"{p.symbol}: no paper to read")
    missing = [pmid for pmid in selected if pmid not in texts]
    if missing:
        raise BatchError(f"{missing[0]}: not in texts.json; run `chd-curator texts` first")
    publications = _publications(p.root)
    info: dict[str, tuple[int, str]] = {}
    for pmid in selected:
        record = publications.get(pmid, {})
        year = record.get("year") or texts[pmid].year
        try:
            info[pmid] = (int(year), str(record.get("title") or texts[pmid].title))
        except (TypeError, ValueError):
            raise BatchError(f"{pmid}: has no year in publications.yaml or texts.json") from None
    batches = plan_batches(
        [texts[m] for m in selected], {m: y for m, (y, _) in info.items()}, budget_bytes
    )
    if mode == "backfill":
        batches = tuple(
            replace(b, findings=tuple(f for f in chosen if findings[f]["publication"] in b.papers))
            for b in batches
        )
    shown = {
        fid: {k: v for k, v in f.items() if k not in _APPROVAL_FIELDS}
        for fid, f in findings.items()
    }
    plan = BatchPlan(mode, tuple(fields), budget_bytes, batches)
    manifests = {b.id: _manifest(p, b, texts, info, mode, fields, shown) for b in batches}
    return Prepared(plan, manifests)


def write_plan(p: RunPaths, prepared: Prepared) -> None:
    """Manifests first, ``batches.json`` last: it is what marks the run as batched."""
    p.batches_dir.mkdir(parents=True, exist_ok=True)
    for batch_id, text in prepared.manifests.items():
        write_bytes_atomically(p.batches_dir / f"batch-{batch_id}.md", text.encode("utf-8"))
    write_batches(p, prepared.plan)


def summary(plan: BatchPlan) -> str:
    count = len(plan.batches)
    papers = sum(len(b.papers) for b in plan.batches)
    parts = "; ".join(
        f"{b.id} {len(b.papers)} paper{'' if len(b.papers) == 1 else 's'} {b.text_bytes} bytes"
        for b in plan.batches
    )
    return (
        f"planned {count} batch{'' if count == 1 else 'es'} for {papers} "
        f"paper{'' if papers == 1 else 's'} (budget {plan.budget_bytes:,} bytes): {parts}"
    )
