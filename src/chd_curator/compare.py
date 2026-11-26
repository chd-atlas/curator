"""``chd-curator compare``: a run's second reading set against the first, and the judges' manifests.

The first pass is what ``merge`` wrote into the gene's literature file (or, for a backfill, the
declared fields of the findings as merged); the second pass is the blind reader's ``verify/``
files. Both go through the notation rules (``notation.py``) before they are compared, so a
difference those rules rewrite never reaches a judge: spacing around ``>`` and ``+`` and after
``c.`` or ``p.``, the ``∗`` character, "greater than", an allele slash, the legacy ``c.C835T``
order, and a lowercase ``ter`` after a digit or ``fs``. Other differences are listed as written,
a base's case (``c.835c>t``) and an uppercase ``TER`` included.

Three things are settled here by rule and are not listed as a difference (spec section 7.5,
R-F5, R-P2-62): a variant's ``as_reported`` span (the first pass's span stays), the order of a
variant's phenotypes, and notation. A pair is ``span_only`` when the span is its only
difference. Review marks are compared as a pseudo-field ``review marks``: ``diff`` drops them
(they are bookkeeping), but a blind reader that marks a finding ``pending_review`` has said
something a judge must see. A variant's mark is keyed by the variant's compared form without
``as_reported``, so a span-only pair whose variant carries one mark on both sides stays
``span_only``. ``review_note`` is free text and is not compared.

``compare_run`` and ``analyse`` are pure; :func:`prepare` reads the run, :func:`write_outputs`
writes it. A refusal is a :class:`CompareError` raised before the first write.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator.atlas import AtlasError, plain, schema_validator
from chd_curator.batch import _dump_yaml
from chd_curator.diff import (
    DiffError,
    FindingDiff,
    compared_fields,
    diff_findings,
    finding_schema,
    normalise_variant,
    parse_findings_file,
    variant_schema,
)
from chd_curator.fs import write_bytes_atomically
from chd_curator.notation import apply_rules
from chd_curator.records import is_empty, literature_path
from chd_curator.run import (
    Batch,
    BatchPlan,
    Comparison,
    MergeRecord,
    PaperText,
    RunPaths,
    load_backfill_pass,
    load_batches,
    load_merge,
    load_texts,
    write_comparison,
)
from chd_curator.write import APPROVAL_FIELDS, schema_error_lines

REVIEW_MARKS: Final = "review marks"
_MARK_FIELDS: Final = ("review_status", "review_settled_by")
_VERIFY_FILE: Final = re.compile(r"batch-([0-9]{2,})\.findings\.yaml")
Kind = Literal["differs", "only_first", "only_second"]


class CompareError(Exception):
    """A refusal; the command exits 2 and has written nothing."""


@dataclass(frozen=True)
class Judged:
    """One thing a judge decides: a pair that differs, or a finding only one pass reported."""

    batch: str
    kind: Kind
    first_id: str | None
    second_id: str | None
    publication: str
    design: str
    fields: tuple[tuple[str, Any, Any], ...]
    first: dict[str, Any] | None
    second: dict[str, Any] | None


@dataclass(frozen=True)
class Analysis:
    comparison: Comparison
    judged: tuple[Judged, ...]


@dataclass(frozen=True)
class Prepared:
    comparison: Comparison
    manifests: dict[str, str]  # batch id -> manifest text


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _mark(holder: Mapping[str, Any]) -> list[Any] | None:
    mark = [holder.get(name) for name in _MARK_FIELDS]
    return None if mark == [None, None] else mark


def _marks(
    finding: Mapping[str, Any], vschema: Mapping[str, Any], defs: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """A finding's review marks: a comparison key, and the view a judge reads.

    The key holds the finding's mark and, per marked variant, its compared form without
    ``as_reported`` with its mark. The view labels each marked variant by ``as_reported``."""
    keyed: list[str] = []
    shown: list[list[Any]] = []
    for variant in finding.get("variants") or []:
        mark = _mark(variant)
        if mark is None:
            continue
        form = normalise_variant(variant, vschema, defs)
        form.pop("as_reported", None)
        keyed.append(_j([form, mark]))
        shown.append([variant.get("as_reported"), *mark])
    fmark = _mark(finding)
    key = _j([fmark, sorted(keyed)])
    return key, {"finding": fmark, "variants": sorted(shown, key=_j)}


def _without_span(variants: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(_j({k: v for k, v in item.items() if k != "as_reported"}) for item in variants)


@dataclass(frozen=True)
class _Verdict:
    kind: Literal["agreed", "span_only", "differs"]
    fields: tuple[tuple[str, Any, Any], ...]


def _verdict(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    differences: Sequence[tuple[str, Any, Any]],
    vschema: Mapping[str, Any],
    defs: Mapping[str, Any],
) -> _Verdict:
    """What the pair is, from the fields ``diff`` reported and the review marks."""
    kept: list[tuple[str, Any, Any]] = []
    span = False
    for name, a, b in differences:
        if name == "variants" and _without_span(a) == _without_span(b):
            span = True
            continue
        kept.append((name, a, b))
    key_a, view_a = _marks(first, vschema, defs)
    key_b, view_b = _marks(second, vschema, defs)
    if key_a != key_b:
        kept.append((REVIEW_MARKS, view_a, view_b))
    if kept:
        return _Verdict("differs", tuple(sorted(kept, key=lambda item: item[0])))
    return _Verdict("span_only" if span else "agreed", ())


def _ruled(finding: Mapping[str, Any]) -> dict[str, Any]:
    return apply_rules(finding)[0]


def _without_approval(finding: Mapping[str, Any]) -> dict[str, Any]:
    """The finding as shown to a judge: a backfill's blind copy is the merged finding with the
    declared fields replaced, so it would otherwise carry an approval nobody gave it."""
    return {k: v for k, v in finding.items() if k not in APPROVAL_FIELDS}


def _pair_verdict(
    first: Mapping[str, Any], second: Mapping[str, Any], schema: Mapping[str, Any]
) -> _Verdict:
    """The verdict ``compare`` reaches on two copies of one finding paired by id."""
    ruled_a, ruled_b = _ruled(first), _ruled(second)
    diffed = diff_findings([ruled_a], [ruled_b], schema, by_id=True)
    if str(first["id"]) not in diffed.pairs:
        raise CompareError(f"{first['id']}: the two copies do not pair by id")
    defs = schema["$defs"]
    return _verdict(
        ruled_a,
        ruled_b,
        diffed.fields.get(str(first["id"]), ()),
        variant_schema(defs),
        defs,
    )


def differing_fields(
    first: Mapping[str, Any], second: Mapping[str, Any], schema: Mapping[str, Any]
) -> tuple[str, ...]:
    """The names, sorted, of the fields in which two copies of one finding differ as a judge
    would see them: after the notation rules, a variant's span ignored, ``review marks`` named
    as ``compare`` names them. Empty when the copies agree or differ only in span.

    Fields ``diff`` does not compare (``summary``, ``locators``, ``review_note``,
    ``shares_probands_with``, the bookkeeping) are not read."""
    return tuple(name for name, _, _ in _pair_verdict(first, second, schema).fields)


def stale_agreements(
    findings: Mapping[str, Mapping[str, Any]],
    plan: BatchPlan,
    comparison: Comparison,
    second: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    schema: Mapping[str, Any],
    ids: Sequence[str],
) -> list[tuple[str, str]]:
    """Each of `ids` (findings `comparison` pairs) that ``compare`` would no longer call agreed
    or span only, as ``(id, the blind pass's id)``: `findings` is the literature file's findings
    by id as they stand now, `second` the blind pass as ``prepare`` reads it. The blind finding
    is the one `comparison` paired the id with (a backfill's is the file's finding with its
    declared fields taken from the blind pass); one the blind pass no longer holds counts as no
    longer agreeing."""
    by_id: Mapping[str, Mapping[str, Any]] = (
        second if isinstance(second, Mapping) else {str(s["id"]): s for s in second}
    )
    stale: list[tuple[str, str]] = []
    for fid in ids:
        sid = comparison.pairs[fid]
        given = by_id.get(sid) if plan.mode != "backfill" else by_id.get(fid, {})
        if given is None:
            stale.append((fid, sid))
            continue
        blind = (
            _blind_copy(findings[fid], given, plan.fields)
            if plan.mode == "backfill"
            else {**given, "id": fid}  # ``_pair_verdict`` pairs copies by id
        )
        if _pair_verdict(findings[fid], blind, schema).kind not in ("agreed", "span_only"):
            stale.append((fid, sid))
    return stale


def _judged_pair(
    batch: str,
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    verdict: _Verdict,
    second_id: str,
) -> Judged:
    return Judged(
        batch,
        "differs",
        str(first["id"]),
        second_id,
        str(first["publication"]),
        str(first["design"]),
        verdict.fields,
        _without_approval(first),
        _without_approval(second),
    )


def _judged_alone(batch: str, kind: Kind, finding: Mapping[str, Any]) -> Judged:
    first = kind == "only_first"
    own = _without_approval(finding)
    return Judged(
        batch,
        kind,
        str(finding["id"]) if first else None,
        None if first else str(finding["id"]),
        str(finding["publication"]),
        str(finding["design"]),
        (),
        own if first else None,
        None if first else own,
    )


def _finding_validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    wrapper = {
        "type": "object",
        "properties": {"findings": {"type": "array", "items": {"$ref": "#/$defs/Finding"}}},
        "required": ["findings"],
        "$defs": schema["$defs"],
    }
    return Draft202012Validator(wrapper, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _pair_exact(
    diffed: FindingDiff,
    ruled_first: Mapping[str, Mapping[str, Any]],
    ruled_second: Mapping[str, Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> FindingDiff:
    """Pair findings ``diff_findings`` left over because several share a key, where exactly one
    finding on each side is identical to the other in every compared field and review mark.

    A key shared by several findings is never paired by ``diff``, so a blind reader that found
    one more finding of the same design in a paper would otherwise send every finding of that
    paper to a judge, agreed ones included. Only a pair identical under the notation rules, with
    the same span and marks, is paired here; if either side has two identical candidates they
    stay unpaired, because choosing between them would be arbitrary."""
    defs = schema["$defs"]
    vschema = variant_schema(defs)

    def identical(fid: str, sid: str) -> bool:
        one = diff_findings([ruled_first[fid]], [ruled_second[sid]], schema)
        if fid not in one.pairs:
            return False
        verdict = _verdict(
            ruled_first[fid], ruled_second[sid], one.fields.get(fid, ()), vschema, defs
        )
        return verdict.kind == "agreed"

    matches = {f: [s for s in diffed.only_second if identical(f, s)] for f in diffed.only_first}
    taken = Counter(s for found in matches.values() for s in found)
    new_pairs = {
        f: found[0] for f, found in matches.items() if len(found) == 1 and taken[found[0]] == 1
    }
    if not new_pairs:
        return diffed
    return replace(
        diffed,
        only_first=tuple(f for f in diffed.only_first if f not in new_pairs),
        only_second=tuple(s for s in diffed.only_second if s not in new_pairs.values()),
        agreed=tuple(sorted([*diffed.agreed, *new_pairs])),
        pairs={**diffed.pairs, **new_pairs},
    )


def _tally(
    batch_of: Mapping[str, str],
    pairs: dict[str, str],
    agreed: list[str],
    span_only: list[str],
    differs: dict[str, tuple[str, ...]],
    judged: list[Judged],
    only_first: list[str],
    only_second: list[str],
    *,
    first: Mapping[str, Mapping[str, Any]],
    second: Mapping[str, Mapping[str, Any]],
    ruled_first: Mapping[str, Mapping[str, Any]],
    ruled_second: Mapping[str, Mapping[str, Any]],
    diffed: FindingDiff,
    schema: Mapping[str, Any],
) -> None:
    """Fold one ``diff_findings`` result into the run's lists."""
    defs = schema["$defs"]
    vschema = variant_schema(defs)
    diffed = _pair_exact(diffed, ruled_first, ruled_second, schema)
    pairs.update(diffed.pairs)
    for fid, sid in diffed.pairs.items():
        verdict = _verdict(
            ruled_first[fid], ruled_second[sid], diffed.fields.get(fid, ()), vschema, defs
        )
        if verdict.kind == "agreed":
            agreed.append(fid)
        elif verdict.kind == "span_only":
            span_only.append(fid)
        else:
            differs[fid] = tuple(name for name, _, _ in verdict.fields)
            judged.append(_judged_pair(batch_of[fid], first[fid], second[sid], verdict, sid))
    for fid in diffed.only_first:
        only_first.append(fid)
        judged.append(_judged_alone(batch_of[fid], "only_first", first[fid]))
    for sid in diffed.only_second:
        only_second.append(sid)
        judged.append(_judged_alone(batch_of[sid], "only_second", second[sid]))


def _by_batch(
    plan: BatchPlan, findings: Sequence[Mapping[str, Any]], what: str
) -> dict[str, list[Mapping[str, Any]]]:
    batch_of = {pmid: b.id for b in plan.batches for pmid in b.papers}
    grouped: dict[str, list[Mapping[str, Any]]] = {b.id: [] for b in plan.batches}
    for finding in findings:
        pmid = str(finding.get("publication"))
        if pmid not in batch_of:
            raise CompareError(f"{what} {finding.get('id')} cites {pmid}, which no batch reads")
        grouped[batch_of[pmid]].append(finding)
    return grouped


def _blind_copy(
    current: Mapping[str, Any], given: Mapping[str, Any], declared: Sequence[str]
) -> dict[str, Any]:
    """A backfill's blind finding: the file's finding with each declared field taken from the
    blind pass (and removed when the pass does not give it)."""
    copied = copy.deepcopy(dict(current))
    for name in declared:
        if name in given:
            copied[name] = copy.deepcopy(given[name])
        else:
            copied.pop(name, None)
    return copied


def _analyse_new(
    literature: Mapping[str, Any],
    plan: BatchPlan,
    merged: MergeRecord,
    second: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> Analysis:
    in_file = {str(f["id"]): f for f in literature.get("findings") or [] if "id" in f}
    finals = list(merged.ids.values())
    if len(set(finals)) != len(finals):
        raise CompareError("merge.json maps two extract ids to one final id")
    for final in finals:
        if final not in in_file:
            raise CompareError(f"merge.json names {final}, which the literature file lacks")
    first = [in_file[f] for f in finals]
    second_ids = [str(s["id"]) for s in second]
    if len(set(second_ids)) != len(second_ids):
        raise CompareError("a blind-pass finding id appears twice")
    first_by = _by_batch(plan, first, "first-pass finding")
    second_by = _by_batch(plan, second, "blind-pass finding")

    pairs: dict[str, str] = {}
    agreed: list[str] = []
    span_only: list[str] = []
    differs: dict[str, tuple[str, ...]] = {}
    judged: list[Judged] = []
    only_first: list[str] = []
    only_second: list[str] = []
    for batch in plan.batches:
        a, b = first_by[batch.id], second_by[batch.id]
        ruled_a = {str(f["id"]): _ruled(f) for f in a}
        ruled_b = {str(f["id"]): _ruled(f) for f in b}
        diffed = diff_findings(list(ruled_a.values()), list(ruled_b.values()), schema)
        _tally(
            {str(f["id"]): batch.id for f in [*a, *b]},
            pairs,
            agreed,
            span_only,
            differs,
            judged,
            only_first,
            only_second,
            first={str(f["id"]): f for f in a},
            second={str(f["id"]): f for f in b},
            ruled_first=ruled_a,
            ruled_second=ruled_b,
            diffed=diffed,
            schema=schema,
        )
    return _finish(plan, pairs, agreed, span_only, differs, judged, only_first, only_second)


def _analyse_backfill(
    literature: Mapping[str, Any],
    plan: BatchPlan,
    second: Mapping[str, Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> Analysis:
    in_file = {str(f["id"]): f for f in literature.get("findings") or [] if "id" in f}
    declared = plan.fields
    if not declared:
        raise CompareError("batches.json declares no fields to backfill")
    compared = set(compared_fields(schema))
    for name in declared:
        if name not in compared:
            raise CompareError(
                f"batches.json declares {name!r}, which a comparison never reads: every "
                "difference in it would pass as agreement"
            )
    planned = {fid: b.id for b in plan.batches for fid in b.findings}
    for fid, given in second.items():
        if fid not in planned:
            raise CompareError(f"{fid} is in a blind pass but in no batch of batches.json")
        for name in given:
            if name not in declared:
                raise CompareError(
                    f"{fid}: the blind pass sets {name!r}, which the run does not declare"
                )
    validator = _finding_validator(schema)
    batch_of: dict[str, str] = {}
    first: dict[str, Mapping[str, Any]] = {}
    second_copy: dict[str, Mapping[str, Any]] = {}
    for fid, batch_id in planned.items():
        if fid not in in_file:
            raise CompareError(f"batches.json names {fid}, which the literature file lacks")
        current = in_file[fid]
        given = second.get(fid, {})
        if all(is_empty(current.get(n)) and is_empty(given.get(n)) for n in declared):
            continue  # neither reader filled it: nothing changed and nothing is verified
        copied = _blind_copy(current, given, declared)
        lines = schema_error_lines({"findings": [copied]}, validator)
        if lines:
            raise CompareError(
                f"{fid}: the blind pass's fields do not satisfy the Finding schema:\n"
                + "\n".join(lines)
            )
        batch_of[fid] = batch_id
        first[fid], second_copy[fid] = current, copied

    ruled_a = {fid: _ruled(f) for fid, f in first.items()}
    ruled_b = {fid: _ruled(f) for fid, f in second_copy.items()}
    diffed = diff_findings(list(ruled_a.values()), list(ruled_b.values()), schema, by_id=True)
    pairs: dict[str, str] = {}
    agreed: list[str] = []
    span_only: list[str] = []
    differs: dict[str, tuple[str, ...]] = {}
    judged: list[Judged] = []
    only_first: list[str] = []
    only_second: list[str] = []
    _tally(
        batch_of,
        pairs,
        agreed,
        span_only,
        differs,
        judged,
        only_first,
        only_second,
        first=first,
        second=second_copy,
        ruled_first=ruled_a,
        ruled_second=ruled_b,
        diffed=diffed,
        schema=schema,
    )
    return _finish(plan, pairs, agreed, span_only, differs, judged, only_first, only_second)


def _finish(
    plan: BatchPlan,
    pairs: dict[str, str],
    agreed: list[str],
    span_only: list[str],
    differs: dict[str, tuple[str, ...]],
    judged: list[Judged],
    only_first: list[str],
    only_second: list[str],
) -> Analysis:
    # Within a batch ``_tally`` appended pairs that differ (by first id), then first-only
    # findings, then second-only ones; a backfill tallies every batch at once, so order by batch.
    order = {b.id: n for n, b in enumerate(plan.batches)}
    ordered = sorted(judged, key=lambda j: order[j.batch])
    comparison = Comparison(
        pairs=dict(sorted(pairs.items())),
        agreed=tuple(sorted(agreed)),
        span_only=tuple(sorted(span_only)),
        differs=dict(sorted(differs.items())),
        only_first=tuple(sorted(only_first)),
        only_second=tuple(sorted(only_second)),
    )
    return Analysis(comparison, tuple(ordered))


def analyse(
    literature: Mapping[str, Any],
    plan: BatchPlan,
    merged: MergeRecord,
    second: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> Analysis:
    """The comparison and, for each thing a judge decides, what the manifest shows. Pure.

    ``second`` is the blind pass's findings (new or reread) or, for a backfill, its fields by
    finding id. Raises :class:`CompareError` for anything inconsistent."""
    finding_schema(schema)  # DiffError when the schema has no Finding
    if merged.mode != plan.mode:
        raise CompareError(
            f"merge.json is a {merged.mode} merge but batches.json plans {plan.mode}"
        )
    if plan.mode == "backfill":
        if not isinstance(second, Mapping):
            raise CompareError("a backfill's blind pass is a mapping of finding id to fields")
        return _analyse_backfill(literature, plan, second, schema)
    if isinstance(second, Mapping):
        raise CompareError(f"a {plan.mode} run's blind pass is a list of findings")
    return _analyse_new(literature, plan, merged, second, schema)


def compare_run(
    literature: Mapping[str, Any],
    plan: BatchPlan,
    merged: MergeRecord,
    second: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> Comparison:
    return analyse(literature, plan, merged, second, schema).comparison


def summary(c: Comparison) -> str:
    return (
        f"compared {len(c.pairs)} pairs: {len(c.agreed)} agreed, {len(c.span_only)} span only, "
        f"{len(c.differs)} differ; {len(c.only_first)} only in the first pass, "
        f"{len(c.only_second)} only in the blind pass"
    )


def _absolute(path: Path) -> str:
    return os.path.abspath(path)


def _manifest(
    run: RunPaths, batch: Batch, items: Sequence[Judged], papers: Mapping[str, PaperText]
) -> str:
    lines = [
        f"# Judging batch {batch.id} of run {run.run_id} (gene {run.symbol})",
        "",
        "Field values below are compared after the notation rules: spacing, the star character, "
        "'greater than', an allele slash, the legacy c.C835T order and a lowercase 'ter' after "
        "a digit or 'fs' are already settled; any other difference in a variant name, a base's "
        "case included, is listed. The YAML blocks show each finding as written.",
        "",
    ]
    for item in items:
        paper = papers.get(item.publication)
        if paper is None:
            raise CompareError(f"{item.publication}: not in texts.json")
        ids = {
            "differs": f"{item.first_id} (blind pass {item.second_id})",
            "only_first": f"{item.first_id} (first pass only)",
            "only_second": f"{item.second_id} (blind pass only)",
        }[item.kind]
        lines += [f"## {ids}", "", f"- publication: {item.publication}", f"- design: {item.design}"]
        if paper.text:
            lines.append(f"- text path: {_absolute(paper.text)}")
        if paper.abstract:
            lines.append(f"- abstract path: {_absolute(paper.abstract)}")
        lines += [f"- supplement: {_absolute(s)}" for s in paper.supplements]
        lines.append("")
        for name, a, b in item.fields:
            lines.append(f"- {name}: first {_j(a)} | second {_j(b)}")
        if item.fields:
            lines.append("")
        for label, finding in (
            ("First pass, as merged:", item.first),
            ("Blind pass:", item.second),
        ):
            if finding is not None:
                lines += [label, "", "```yaml", _dump_yaml([finding]).rstrip("\n"), "```", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def render_manifests(
    run: RunPaths, plan: BatchPlan, analysis: Analysis, texts: Sequence[PaperText]
) -> dict[str, str]:
    """``judge-NN.md`` text per batch that has anything to judge."""
    papers = {t.pmid: t for t in texts}
    out: dict[str, str] = {}
    for batch in plan.batches:
        items = [j for j in analysis.judged if j.batch == batch.id]
        if items:
            out[batch.id] = _manifest(run, batch, items, papers)
    return out


def read_second(
    run: RunPaths, plan: BatchPlan, schema: Mapping[str, Any]
) -> list[dict[str, Any]] | dict[str, dict[str, Any]]:
    """The blind pass of every batch: a list of findings (new or reread) or, for a backfill, its
    fields by finding id. Raises :class:`CompareError` for a file that is missing, misplaced or
    inconsistent."""
    known = {b.id for b in plan.batches}
    if run.verify.is_dir():
        for path in sorted(run.verify.iterdir()):
            if path.name.endswith(".findings.yaml"):
                found = _VERIFY_FILE.fullmatch(path.name)
                if found is None or found.group(1) not in known:
                    raise CompareError(f"{path}: is not the verify file of a batch in batches.json")
    new: list[dict[str, Any]] = []
    backfill: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for batch in plan.batches:
        path = run.verify / f"batch-{batch.id}.findings.yaml"
        if not path.is_file():
            raise CompareError(f"{path}: the blind pass has no findings file for batch {batch.id}")
        if plan.mode == "backfill":
            for fid, fields in load_backfill_pass(path).items():
                if fid not in batch.findings:
                    raise CompareError(f"{path}: {fid} is not a finding of batch {batch.id}")
                for name in fields:
                    if name not in plan.fields:
                        raise CompareError(
                            f"{path}: {fid} sets {name!r}, which batches.json does not declare"
                        )
                if fid in backfill:
                    raise CompareError(f"{path}: {fid} is in more than one batch's findings file")
                backfill[fid] = fields
            continue
        try:
            rows = parse_findings_file(path.read_bytes(), schema)
        except DiffError as exc:
            raise CompareError(f"{path}: {exc}") from None
        for row in rows:
            if row["publication"] not in batch.papers:
                raise CompareError(
                    f"{path}: {row['id']} cites {row['publication']}, which batch {batch.id} "
                    "does not read"
                )
            if row["id"] in seen:
                raise CompareError(f"{path}: {row['id']} is also in another batch's blind pass")
            seen.add(row["id"])
        new.extend(rows)
    return backfill if plan.mode == "backfill" else new


def prepare(run: RunPaths, approved: str) -> Prepared:
    """Read the run and the curated file, compare, render. Reads only."""
    stale = [
        *sorted(run.judge.glob("*.decisions.yaml")),
        *sorted(run.judging.glob("*.findings.yaml")),
    ]
    if stale:
        raise CompareError(
            f"{stale[0]}: the judges have already read a comparison of this run; nothing was "
            "written. Remove their files first if they are to read it again"
        )
    merged = load_merge(run)
    if merged is None:
        raise CompareError(f"{run.merge}: the run has no merge.json; run `chd-curator merge` first")
    plan = load_batches(run)
    texts = load_texts(run)
    path = literature_path(run.root, approved)
    try:
        literature = plain(YAML(typ="safe").load(path.read_bytes()))
    except (YAMLError, ValueError) as exc:
        raise CompareError(f"{path}: cannot be read as YAML: {exc}") from exc
    except FileNotFoundError:
        raise CompareError(f"{path}: no literature file for {approved}") from None
    if not isinstance(literature, dict):
        raise CompareError(f"{path}: is not a mapping")
    try:
        schema: Any = schema_validator(run.root, "gene_literature_file").schema
    except AtlasError as exc:
        raise CompareError(str(exc)) from None
    second = read_second(run, plan, schema)
    analysis = analyse(literature, plan, merged, second, schema)
    return Prepared(analysis.comparison, render_manifests(run, plan, analysis, texts))


def write_outputs(run: RunPaths, prepared: Prepared) -> None:
    """Drop the manifests an earlier compare wrote, write the new ones, ``comparison.json`` last."""
    if run.judging.is_dir():
        for old in sorted(run.judging.glob("judge-*.md")):
            old.unlink()
    if prepared.manifests:
        run.judging.mkdir(parents=True, exist_ok=True)
    for batch_id, text in prepared.manifests.items():
        write_bytes_atomically(run.judging / f"judge-{batch_id}.md", text.encode("utf-8"))
    write_comparison(run, prepared.comparison)
