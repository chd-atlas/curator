"""``chd-curator merge``: a run's extraction batches into the gene's curated literature file.

A merge reads the four curated inputs once (the literature file, ``publications.yaml``,
``.id_registry.yaml`` and the run's own files), checks every batch against them, builds every
output in memory, fetches PubMed once, runs every write's own checks, and only then writes. A
refusal is a :class:`MergeError` raised before the first write, so a refused run leaves every
file as it was. The writes go in this order: ``evidence/batch-NN.yaml`` for each batch and
``merge.json``; the id registry; ``publications.yaml`` (when a Publication was added); the
literature file. A crash part-way therefore leaves skipped ids or a Publication no finding cites,
never a finding whose id lies past the counter, and it leaves ``merge.json`` in place, which
makes a re-run refuse until the curator removes it.

Exit codes: 2 is every refusal, including a curated file that is missing, malformed or not UTF-8;
1 is a PubMed failure or an ``OSError`` while reading or writing a file. A backfill fills the
declared fields a finding lacks and never rewrites a filled one.

``append_numbered`` is pure (bytes and mappings in, bytes and mappings out) so a command that
only checks a batch can number it the way a merge will.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator import pubmed
from chd_curator.atlas import AtlasError, plain, schema_validator
from chd_curator.batch import FORBIDDEN_FIELDS
from chd_curator.diff import DiffError, finding_schema, parse_findings_file
from chd_curator.records import (
    RecordError,
    is_empty,
    pmid_key,
    read_counter,
    with_counter,
    with_fields_set,
    with_findings_added,
    with_publication,
    with_triage,
)
from chd_curator.run import (
    Batch,
    BatchPlan,
    MergeRecord,
    PaperOutcome,
    Quote,
    RunPaths,
    TriageChange,
    load_backfill_pass,
    load_evidence_file,
    load_papers,
    load_texts,
    write_evidence,
    write_merge,
)
from chd_curator.write import (
    APPROVAL_FIELDS,
    check_literature,
    check_publications,
    check_registry,
    write_literature,
    write_publications,
    write_registry,
)

PREFIX: Final = "LIT"
# The common-variant design and the class a first variant gives it (R-P2-20), checked against
# the exported schema's enums before use.
COMMON_VARIANT_DESIGN: Final = "common_variant_association"
COMMON_VARIANT_CLASS: Final = "snv_indel"


class MergeError(Exception):
    """A refusal; the command exits 2 and has written nothing."""


def _final_id(number: int) -> str:
    return f"CHDA:LIT:{number:07d}"


def append_numbered(
    raw: bytes, findings: Sequence[Mapping[str, Any]], years: Mapping[str, int], counter: int
) -> tuple[bytes, dict[str, str], int]:
    """`raw` with `findings` appended under final ids: `(new bytes, local -> final ids, counter)`.

    Findings are numbered from ``counter + 1`` in (year of their paper, PMID number, position in
    `findings`) order, `years` mapping each paper to its year. A ``shares_probands_with`` entry
    that is a local id of `findings` becomes its final id, one already in `raw` is kept, and any
    other is refused; ``unknown`` and ``[]`` are kept as they are. A local id repeated, or a final
    id already in the file, is refused. Pure: no file is read or written.
    """
    existing = {
        str(f.get("id")) for f in (plain(YAML(typ="safe").load(raw)) or {}).get("findings") or []
    }
    try:
        order = sorted(
            range(len(findings)),
            key=lambda i: (
                years[findings[i]["publication"]],
                pmid_key(findings[i]["publication"]),
                i,
            ),
        )
    except KeyError as exc:
        raise RecordError(f"no year for {exc.args[0]}") from None
    mapping: dict[str, str] = {}
    for i in order:
        local = str(findings[i]["id"])
        if local in mapping:
            raise RecordError(f"{local}: more than one finding has this id")
        counter += 1
        final = _final_id(counter)
        if final in existing:
            raise RecordError(f"{final} is already in the file: the {PREFIX} counter is behind it")
        mapping[local] = final
    numbered: list[dict[str, Any]] = []
    for i in order:
        finding = dict(findings[i])
        finding["id"] = mapping[str(finding["id"])]
        shared = finding.get("shares_probands_with")
        if isinstance(shared, list):
            rewritten = []
            for ref in shared:
                if ref in mapping:
                    rewritten.append(mapping[ref])
                elif ref in existing:
                    rewritten.append(ref)
                else:
                    raise RecordError(
                        f"{finding['id']}: shares_probands_with names {ref}, which is neither a "
                        "finding of this run nor one in the file"
                    )
            finding["shares_probands_with"] = rewritten
        numbered.append(finding)
    return with_findings_added(raw, numbered), mapping, counter


@dataclass(frozen=True)
class Curated:
    lit_path: Path
    pub_path: Path
    reg_path: Path
    lit: bytes
    pubs: bytes
    reg: bytes
    lit_validator: Draft202012Validator
    pub_validator: Draft202012Validator
    findings: dict[str, dict[str, Any]]
    triage: dict[str, dict[str, Any]]


def safe_load(raw: bytes, path: Path) -> Any:
    try:
        return plain(YAML(typ="safe").load(raw))
    except (YAMLError, ValueError) as exc:
        raise MergeError(f"{path}: cannot be read as YAML: {exc}") from exc


def read_curated(root: Path, run: RunPaths, symbol: str, *, refuse_merged: bool = True) -> Curated:
    if refuse_merged and run.merge.exists():
        raise MergeError(
            f"{run.merge}: the run is already merged; nothing was written. Remove the file only "
            "if the earlier merge stopped part-way"
        )
    lit_path = root / "curation" / "literature" / f"{symbol}.yaml"
    pub_path = root / "curation" / "publications.yaml"
    reg_path = root / "curation" / ".id_registry.yaml"
    for path in (lit_path, pub_path, reg_path):
        if not path.is_file():
            raise MergeError(f"{path} does not exist")
    try:
        lit_validator = schema_validator(root, "gene_literature_file")
        pub_validator = schema_validator(root, "publication_file")
    except AtlasError as exc:
        raise MergeError(str(exc)) from None
    lit = lit_path.read_bytes()
    data = safe_load(lit, lit_path)
    if not isinstance(data, dict):
        raise MergeError(f"{lit_path}: is not a mapping")
    findings = {str(f["id"]): f for f in data.get("findings") or [] if "id" in f}
    triage = {str(t["pmid"]): t for t in data.get("triage") or [] if "pmid" in t}
    return Curated(
        lit_path,
        pub_path,
        reg_path,
        lit,
        pub_path.read_bytes(),
        reg_path.read_bytes(),
        lit_validator,
        pub_validator,
        findings,
        triage,
    )


def _enum(schema: Mapping[str, Any], name: str) -> list[str]:
    try:
        return [str(v) for v in schema["$defs"][name]["enum"]]
    except (KeyError, TypeError) as exc:
        raise MergeError(f"the atlas's gene_literature_file schema has no {name} enum") from exc


def _study_types(root: Path) -> list[str]:
    schema: Any = schema_validator(root, "publication_file").schema
    try:
        return [str(v) for v in schema["$defs"]["StudyType"]["enum"]]
    except (KeyError, TypeError) as exc:
        raise MergeError("the atlas's publication_file schema has no StudyType enum") from exc


def paper_years(c: Curated, run: RunPaths, wanted: Sequence[str]) -> dict[str, int]:
    """Each wanted paper's year: ``publications.yaml``'s, else ``texts.json``'s (as ``batch``)."""
    rows = safe_load(c.pubs, c.pub_path)
    known = {str(r["id"]): r.get("year") for r in (rows or {}).get("publications") or []}
    texts = {t.pmid: t.year for t in load_texts(run)}
    years: dict[str, int] = {}
    for pmid in wanted:
        try:
            years[pmid] = int(known.get(pmid) or texts.get(pmid) or "")
        except ValueError:
            raise MergeError(f"{pmid}: has no year in publications.yaml or texts.json") from None
    return years


def _pass_paths(run: RunPaths, batch: Batch) -> tuple[Path, Path, Path]:
    base = run.extract / f"batch-{batch.id}"
    return (
        base.with_name(base.name + ".findings.yaml"),
        base.with_name(base.name + ".papers.json"),
        base.with_name(base.name + ".evidence.yaml"),
    )


def _require(path: Path) -> Path:
    if not path.is_file():
        raise MergeError(f"{path}: the batch has no such file")
    return path


@dataclass(frozen=True)
class _Writes:
    """Everything a merge writes, built and checked before the first write."""

    evidence: dict[str, dict[str, tuple[Quote, ...]]]
    record: MergeRecord
    literature: bytes
    registry: bytes | None
    publications: bytes | None
    added: frozenset[str]


def _commit(root: Path, run: RunPaths, c: Curated, w: _Writes) -> None:
    check_literature(c.lit_path, w.literature, c.lit_validator, expected=c.lit, added=w.added)
    if w.publications is not None:
        check_publications(c.pub_path, w.publications, c.pub_validator, expected=c.pubs)
    if w.registry is not None:
        check_registry(c.reg_path, w.registry, expected=c.reg)
    for batch_id, quotes in w.evidence.items():
        write_evidence(run, batch_id, quotes)
    write_merge(run, w.record)
    if w.registry is not None:
        write_registry(c.reg_path, w.registry, expected=c.reg)
    if w.publications is not None:
        write_publications(c.pub_path, w.publications, c.pub_validator, expected=c.pubs)
    write_literature(c.lit_path, w.literature, c.lit_validator, expected=c.lit, added=w.added)


def merge_run(
    root: Path,
    run: RunPaths,
    plan: BatchPlan,
    study_types: Mapping[str, str],
    fetch: pubmed.Fetch,
    api_key: str | None,
    *,
    approved: str | None = None,
) -> MergeRecord:
    """Merge a run's batches into the gene's literature file; see the module docstring.

    `approved` is the gene's approved symbol, which names its literature file (default: the
    run's own symbol). `study_types` maps ``PMID:n`` to a study type for a paper the reader left
    without one. Raises :class:`MergeError` (or ``RunFormatError``, ``DiffError``, ``RecordError``,
    ``WriteRefused`` from the files it reads and writes) before any write, and
    ``pubmed.PubMedError`` or ``OSError`` for the failures that exit 1.
    """
    curated = read_curated(root, run, approved or run.symbol)
    schema: Any = curated.lit_validator.schema
    if plan.mode == "new":
        writes = _plan_new(root, run, plan, study_types, fetch, api_key, curated, schema)
    elif plan.mode == "backfill":
        if study_types:
            raise MergeError("--study-type belongs to a new merge, not a backfill")
        writes = _plan_backfill(run, plan, curated, schema)
    else:
        raise MergeError(f"batches.json mode {plan.mode!r} is not merged by this command")
    _commit(root, run, curated, writes)
    return writes.record


def _plan_new(
    root: Path,
    run: RunPaths,
    plan: BatchPlan,
    study_types: Mapping[str, str],
    fetch: pubmed.Fetch,
    api_key: str | None,
    c: Curated,
    schema: Mapping[str, Any],
) -> _Writes:
    allowed_types = _study_types(root)
    reasons = exclusion_reasons(schema)
    cited_in_file = {str(f["publication"]) for f in c.findings.values()}
    flat: list[dict[str, Any]] = []
    local_batch: dict[str, str] = {}
    evidence: dict[str, dict[str, tuple[Quote, ...]]] = {}
    outcomes: dict[str, PaperOutcome] = {}
    paper_batch: dict[str, str] = {}
    for batch in plan.batches:
        findings_path, papers_path, evidence_path = (_require(p) for p in _pass_paths(run, batch))
        try:
            found = parse_findings_file(findings_path.read_bytes(), schema)
        except DiffError as exc:
            raise MergeError(f"{findings_path}: {exc}") from None
        papers = load_papers(papers_path)
        quotes = load_evidence_file(evidence_path)
        for pmid in batch.papers:
            if pmid in paper_batch:
                raise MergeError(f"{pmid} is in batch {paper_batch[pmid]} and batch {batch.id}")
            paper_batch[pmid] = batch.id
            if pmid in cited_in_file:
                raise MergeError(f"{pmid}: already has a finding in {c.lit_path}")
        named = [o.pmid for o in papers]
        for pmid in batch.papers:
            if pmid not in named:
                raise MergeError(f"{papers_path}: does not account for {pmid} (batch {batch.id})")
        for pmid in named:
            if pmid not in batch.papers:
                raise MergeError(f"{papers_path}: names {pmid}, which is not in batch {batch.id}")
        outcomes.update({o.pmid: o for o in papers})
        local_ids = {str(f["id"]) for f in found}
        check_new_findings(found, batch, c, local_batch)
        for pmid in sorted(unaccounted_papers(batch, papers, found), key=pmid_key):
            raise MergeError(
                f"{pmid}: {papers_path.name} gives it a study type and reading, but no finding "
                f"of batch {batch.id} cites it; add a finding that cites it, or exclude it in "
                f"{papers_path.name} with a reason"
            )
        for key in quotes:
            if key not in local_ids:
                raise MergeError(f"{evidence_path}: {key} is not a finding of batch {batch.id}")
        evidence[batch.id] = quotes
        flat.extend(found)

    changes = exclusion_changes(c, outcomes, flat, reasons)
    for pmid, outcome in outcomes.items():
        if outcome.study_type is not None and outcome.study_type not in allowed_types:
            raise MergeError(
                f"{pmid}: study type {outcome.study_type!r} is not one of: "
                f"{', '.join(allowed_types)}"
            )
    cited = sorted({str(f["publication"]) for f in flat}, key=pmid_key)
    years = paper_years(c, run, cited)
    registered = {str(r["id"]) for r in safe_load(c.pubs, c.pub_path).get("publications") or []}
    needed = sorted(
        (p for p in cited if p not in registered), key=lambda p: (years[p], pmid_key(p))
    )
    types = _resolve_types(outcomes, needed, study_types, allowed_types)

    counter = read_counter(c.reg, PREFIX)
    literature, mapping, new_counter = append_numbered(c.lit, flat, years, counter)
    if changes:
        literature = with_triage(literature, changes)
    registry = with_counter(c.reg, PREFIX, new_counter) if new_counter != counter else None

    publications: bytes | None = None
    if needed:
        digits = [p.removeprefix("PMID:") for p in needed]
        found_meta = pubmed.esummary(digits, fetch, api_key=api_key)
        publications = c.pubs
        for pmid, digit in zip(needed, digits, strict=True):
            if digit not in found_meta:
                raise pubmed.PubMedError(f"PubMed returned no record for {pmid}")
            record = pubmed.publication_record(found_meta[digit], types[pmid])
            publications = with_publication(publications, record, outcomes[pmid].reading)

    by_batch = {
        batch.id: {mapping[local]: quotes for local, quotes in evidence[batch.id].items()}
        for batch in plan.batches
    }
    return _Writes(
        by_batch,
        MergeRecord("new", mapping, tuple(needed), tuple(changes), ()),
        literature,
        registry,
        publications,
        frozenset(mapping.values()),
    )


def unaccounted_papers(
    batch: Batch, papers: Sequence[PaperOutcome], found: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The papers of `batch` that its ``papers.json`` read (a study type and reading, no
    exclusion) and that none of its findings cites. The atlas refuses such a paper (``LIT003``:
    included, cited by no finding), and neither merge's other checks nor a later command would
    say so before the validator does."""
    cited = {str(f["publication"]) for f in found}
    return [
        o.pmid
        for o in papers
        if o.exclude is None and o.pmid in batch.papers and o.pmid not in cited
    ]


def check_new_findings(
    found: Sequence[Mapping[str, Any]], batch: Batch, c: Curated, local_batch: dict[str, str]
) -> None:
    """Refuse an agent's findings for `batch` unless each has a local id that no other batch
    (`local_batch`, which this adds to) and no finding of the file holds, cites a paper of the
    batch, and carries no approval field or ``verified_by``."""
    for finding in found:
        local = str(finding["id"])
        if local in local_batch:
            raise MergeError(f"{local} is in batch {local_batch[local]} and batch {batch.id}")
        if local in c.findings:
            raise MergeError(f"{local}: is already a finding in {c.lit_path}")
        local_batch[local] = batch.id
        if finding["publication"] not in batch.papers:
            raise MergeError(
                f"{local}: cites {finding['publication']}, which is not in batch {batch.id}"
            )
        for name in (*APPROVAL_FIELDS, "verified_by"):
            if name in finding:
                raise MergeError(f"{local}: carries {name}; an agent's finding may not")


def exclusion_reasons(schema: Mapping[str, Any]) -> list[str]:
    """The exclusion reasons the atlas's schema allows."""
    return _enum(schema, "ExclusionReason")


def exclusion_changes(
    c: Curated,
    outcomes: Mapping[str, PaperOutcome],
    findings: Sequence[Mapping[str, Any]],
    reasons: Sequence[str],
) -> list[TriageChange]:
    """The triage changes the papers' exclusions make, shared by ``merge`` and ``check-batch``:
    one ``included`` -> ``excluded`` per excluded paper, refused for a reason outside `reasons`,
    a paper some finding of `findings` cites, or a paper whose triage decision is not
    ``included``."""
    cited = {str(f["publication"]) for f in findings}
    changes: list[TriageChange] = []
    for pmid in sorted((p for p, o in outcomes.items() if o.exclude), key=pmid_key):
        exclude = outcomes[pmid].exclude
        if exclude is None:
            continue
        reason, note = exclude
        if reason not in reasons:
            raise MergeError(
                f"{pmid}: exclusion reason {reason!r} is not one of: {', '.join(reasons)}"
            )
        if pmid in cited:
            raise MergeError(f"{pmid}: is excluded in papers.json and cited by a finding")
        decision = c.triage.get(pmid, {}).get("decision")
        if decision != "included":
            raise MergeError(f"{pmid}: cannot be excluded: its triage decision is {decision!r}")
        changes.append(TriageChange(pmid, "included", "excluded", reason, note))
    return changes


def _resolve_types(
    outcomes: Mapping[str, PaperOutcome],
    needed: Sequence[str],
    given: Mapping[str, str],
    allowed: Sequence[str],
) -> dict[str, str]:
    """The study type of each Publication to add: the reader's, else ``--study-type``."""
    null_papers = {p for p, o in outcomes.items() if o.exclude is None and o.study_type is None}
    for pmid, value in given.items():
        if pmid not in null_papers:
            raise MergeError(
                f"--study-type {pmid}: not a paper of this run left without a study type"
            )
        if pmid not in needed:
            raise MergeError(
                f"--study-type {pmid}: no Publication is added for it, so it would not be used"
            )
        if value not in allowed:
            raise MergeError(f"--study-type {pmid}={value}: not one of: {', '.join(allowed)}")
    types: dict[str, str] = {}
    for pmid in needed:
        outcome = outcomes[pmid]
        study_type = outcome.study_type if outcome.study_type is not None else given.get(pmid)
        if study_type is None:
            raise MergeError(
                f"{pmid}: the reader left its study type null; "
                f"name it with --study-type {pmid}=TYPE"
            )
        types[pmid] = study_type
    return types


@dataclass(frozen=True)
class BackfillEdit:
    """What a backfill does to one finding: `fields` to set (the changed values plus any
    allowance), `drop` keys to remove, and `allowed`, every key that may move."""

    fields: dict[str, Any]
    drop: tuple[str, ...]
    allowed: frozenset[str]


def backfill_edit(current: Mapping[str, Any], values: Mapping[str, Any]) -> BackfillEdit:
    """The one rule a backfill applies to a finding, shared by ``merge`` and ``assemble``.

    `values` are the declared fields a pass gives. A value equal to the finding's is no change,
    and neither is an empty value for a key the finding lacks (re-reading a paper and finding
    nothing must not void a finding's verification). When anything changed, ``verified_by`` is
    dropped (the blind agreement no longer covers it), and a common-variant finding that gains its
    first variant, has no ``variant_classes`` and whose pass does not give them gets
    ``variant_classes: [snv_indel]`` (R-P2-20). With no change, `fields` and `drop` are empty."""
    changed = {
        k: v
        for k, v in values.items()
        if current.get(k) != v and not (k not in current and v in (None, [], {}))
    }
    allowed = set(values)
    if not changed:
        return BackfillEdit({}, (), frozenset(allowed))
    extra: dict[str, Any] = {}
    if (
        "variants" in changed
        and changed["variants"]
        and not current.get("variants")
        and current.get("design") == COMMON_VARIANT_DESIGN
        and not current.get("variant_classes")
        and "variant_classes" not in values
    ):
        extra["variant_classes"] = [COMMON_VARIANT_CLASS]
        allowed.add("variant_classes")
    drop = ("verified_by",) if "verified_by" in current else ()
    if drop:
        allowed.add("verified_by")
    return BackfillEdit({**changed, **extra}, drop, frozenset(allowed))


def check_backfill_declaration(plan: BatchPlan, schema: Mapping[str, Any]) -> frozenset[str]:
    """The fields `plan` declares, refused unless a backfill may set them all."""
    declared = set(plan.fields)
    if not declared:
        raise MergeError("batches.json declares no fields to backfill")
    for name in sorted(declared & set(FORBIDDEN_FIELDS)):
        raise MergeError(f"batches.json declares {name!r}, which a backfill may not set")
    known = set(finding_schema(schema)["properties"])
    for name in sorted(declared - known):
        raise MergeError(f"batches.json declares {name!r}, which is not a Finding field")
    if COMMON_VARIANT_DESIGN not in _enum(
        schema, "FindingDesign"
    ) or COMMON_VARIANT_CLASS not in _enum(schema, "FindingVariantClass"):
        raise MergeError("the schema has no common-variant design or snv_indel class to rely on")
    return frozenset(declared)


def backfill_fields(
    path: Path,
    batch: Batch,
    c: Curated,
    declared: frozenset[str],
    taken: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """A backfill pass file's fields by finding id, refused unless each id is a finding of the
    file and of `batch` that no other pass in `taken` fills, and each field is declared and
    still empty in the finding (R-P2-63)."""
    sets: dict[str, dict[str, Any]] = {}
    for fid, fields in load_backfill_pass(path).items():
        if fid not in c.findings:
            raise MergeError(f"{path}: {fid} is not a finding in {c.lit_path}")
        if fid not in batch.findings:
            raise MergeError(f"{path}: {fid} is not a finding of batch {batch.id}")
        if fid in taken:
            raise MergeError(f"{fid} is in more than one batch's findings file")
        for name in fields:
            if name not in declared:
                raise MergeError(
                    f"{path}: {fid} sets {name!r}, which batches.json does not declare"
                )
            if not is_empty(c.findings[fid].get(name)):
                raise MergeError(
                    f"{path}: {fid} already holds {name}; a backfill fills a field "
                    "a finding lacks and never rewrites a filled one"
                )
        sets[fid] = fields
    return sets


def apply_backfill(c: Curated, sets: Mapping[str, Mapping[str, Any]]) -> bytes:
    """The literature file with each finding of `sets` given its fields, by ``backfill_edit``,
    refused unless nothing but the allowed fields moved."""
    literature = c.lit
    allowed: dict[str, set[str]] = {}
    for fid, fields in sets.items():
        edit = backfill_edit(c.findings[fid], fields)
        allowed[fid] = set(edit.allowed)
        if not edit.fields:
            continue
        try:
            literature = with_fields_set(literature, fid, edit.fields, edit.drop)
        except RecordError as exc:
            raise MergeError(str(exc)) from None
    _only_declared_changes(c, literature, allowed)
    return literature


def _plan_backfill(
    run: RunPaths, plan: BatchPlan, c: Curated, schema: Mapping[str, Any]
) -> _Writes:
    declared = check_backfill_declaration(plan, schema)
    sets: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, tuple[Quote, ...]]] = {}
    for batch in plan.batches:
        findings_path = _require(_pass_paths(run, batch)[0])
        evidence_path = _require(_pass_paths(run, batch)[2])
        sets.update(backfill_fields(findings_path, batch, c, declared, sets))
        quotes = load_evidence_file(evidence_path)
        for key in quotes:
            if key not in batch.findings:
                raise MergeError(f"{evidence_path}: {key} is not a finding of batch {batch.id}")
        evidence[batch.id] = quotes

    literature = apply_backfill(c, sets)
    return _Writes(
        evidence,
        MergeRecord("backfill", {}, (), (), ()),
        literature,
        None,
        None,
        frozenset(),
    )


def _without_findings(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "findings"}


def _only_declared_changes(c: Curated, after: bytes, allowed: Mapping[str, set[str]]) -> None:
    """Refuse a backfilled file where anything but the allowed fields of a finding moved."""
    before = safe_load(c.lit, c.lit_path)
    now = safe_load(after, c.lit_path)
    if _without_findings(before) != _without_findings(now):
        raise MergeError(f"{c.lit_path}: a top-level key other than findings changed")
    old, new = before.get("findings") or [], now.get("findings") or []
    if [f["id"] for f in old] != [f["id"] for f in new]:
        raise MergeError(f"{c.lit_path}: the findings' ids or order changed")
    for was, is_ in zip(old, new, strict=True):
        moved = {k for k in set(was) | set(is_) if was.get(k) != is_.get(k)}
        stray = moved - allowed.get(str(was["id"]), set())
        if stray:
            raise MergeError(
                f"{was['id']}: fields other than the declared ones changed: {sorted(stray)}"
            )
        if "verified_by" in moved and is_.get("verified_by") is not None:
            raise MergeError(f"{was['id']}: verified_by may only be removed")


def summary(root: Path, plan: BatchPlan, record: MergeRecord, run: RunPaths) -> str:
    """The line ``merge`` prints."""
    counter = read_counter((root / "curation" / ".id_registry.yaml").read_bytes(), PREFIX)
    batches = f"{len(plan.batches)} batch{'' if len(plan.batches) == 1 else 'es'}"
    if record.mode == "backfill":
        count = sum(len(load_backfill_pass(_pass_paths(run, b)[0])) for b in plan.batches)
        return (
            f"backfilled {count} findings ({', '.join(plan.fields)}) from {batches}; "
            f"{PREFIX} counter {counter}"
        )
    ids = list(record.ids.values())
    span = f"{min(ids)}..{max(ids)}" if ids else "none"
    excluded = sum(1 for t in record.triage if t.after == "excluded")
    return (
        f"merged {len(ids)} findings ({span}) from {batches}; "
        f"{len(record.publications_added)} publications added; {excluded} papers excluded; "
        f"{PREFIX} counter {counter}"
    )
