"""``chd-curator assemble``: the judges' findings, the notation rules and the blind agreement,
applied to the gene's curated literature file.

A merged and compared run is assembled in one pass. Everything is read once, every output is
built in memory and checked, and only then is anything written; a refusal is an
:class:`AssembleError` (or one of the readers' errors) raised before the first write, so a
refused run leaves every file as it was. The writes go in this order: ``assemble.json``; the id
registry (when findings were added); the literature file. ``assemble.json`` is first because the
ids it maps are what stops a re-run numbering an added finding twice: written last, a crash
between the two files would let a re-run number it again. Written first, a crash leaves a map
naming ids the file lacks, and a re-run refuses until the curator removes ``assemble.json``.

What is applied, in this order:

1. The judges' findings (``judging/judge-NN.findings.yaml``). A finding whose id is in the
   comparison's ``differs`` or ``only_first`` replaces the file's finding of that id. A finding
   with a local id ``CHDA:LIT:07...`` is added, numbered from the counter; one a previous
   ``assemble.json`` mapped (under its local id or its final id) replaces the finding it was
   numbered as. In a backfill only the declared fields of a judged finding are taken from the
   judge, and every other field must equal the file's.
2. The notation rules, on every finding of the run (a backfill: on the findings the comparison
   paired, when ``variants`` is a declared field).
3. ``verified_by`` on the findings the comparison lists as ``agreed`` or ``span_only``. A judged
   finding carries none.

``assemble.json`` records, for each final id a judge's finding decides, the sha256 of that
finding as read (``judge_digest``). On a re-run a finding whose digest is unchanged is not
applied again, so what the file holds for it (an approval, a placement ``normalise`` wrote) stays
as it is; a changed finding replaces an unapproved one, and for one carrying ``approved_by`` is
refused, naming its id. The refusal needs a recorded digest, so a first application, including a
backfill's change to an approved finding's declared fields, is not refused by it.

In a new or reread run (a reread plan is handled like a new one; no command writes one yet, so
that path is untested), whatever a judge changed in a finding that ``differs`` lists must be a
field ``differs`` names for it, so a field nobody recorded a decision for cannot reach the owner
looking like blind agreement. A last check re-reads the result and refuses it if anything but
these changes moved.

Exit codes (see the CLI): 2 is every refusal, 1 an ``OSError``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from chd_curator.compare import differing_fields
from chd_curator.diff import DiffError, parse_findings_file
from chd_curator.merge import (
    PREFIX,
    Curated,
    append_numbered,
    backfill_edit,
    paper_years,
    read_curated,
    safe_load,
)
from chd_curator.notation import NotationChange, apply_rules
from chd_curator.records import (
    is_empty,
    read_counter,
    with_counter,
    with_fields_set,
    with_findings_replaced,
    with_triage,
    with_verified,
)
from chd_curator.run import (
    AssembleRecord,
    BatchPlan,
    Comparison,
    MergeRecord,
    RunFormatError,
    RunPaths,
    TriageChange,
    load_assemble,
    load_batches,
    load_comparison,
    load_merge,
    load_pass,
    write_assemble,
)
from chd_curator.write import (
    APPROVAL_FIELDS,
    check_literature,
    check_registry,
    write_literature,
    write_registry,
)

_JUDGE_FILE: Final = re.compile(r"judge-([0-9]{2,})\.findings\.yaml")
_JUDGE_MANIFEST: Final = re.compile(r"judge-[0-9]{2,}\.md")
LOCAL_ID: Final = re.compile(r"CHDA:LIT:07[0-9]{5}")
_ANY: Final = frozenset({"*"})


class AssembleError(Exception):
    """A refusal; the command exits 2 and has written nothing."""


@dataclass(frozen=True)
class Assembled:
    """Everything an assemble writes, built and checked before the first write."""

    curated: Curated
    record: AssembleRecord
    literature: bytes
    registry: bytes | None
    added: frozenset[str]


def judge_findings(
    run: RunPaths, plan: BatchPlan, schema: Mapping[str, Any], only: str | None = None
) -> list[dict[str, Any]]:
    """Every finding of ``judging/judge-NN.findings.yaml``, each citing a paper of batch NN.

    With `only` (a batch id) the other batches' files are name-checked but not read, so one
    judge's file can be checked without another's being well formed."""
    batches = {b.id: b for b in plan.batches}
    found: list[dict[str, Any]] = []
    seen: dict[str, Path] = {}
    if not run.judging.is_dir():
        return found
    for path in sorted(run.judging.iterdir()):
        if _JUDGE_MANIFEST.fullmatch(path.name):
            continue
        match = _JUDGE_FILE.fullmatch(path.name)
        if match is None or match.group(1) not in batches:
            raise AssembleError(
                f"{path}: is not a judge file of a batch in batches.json (judge-NN.md or "
                "judge-NN.findings.yaml); a misnamed file would drop its findings without a sound"
            )
        if only is not None and match.group(1) != only:
            continue
        batch = batches[match.group(1)]
        try:
            rows = parse_findings_file(path.read_bytes(), schema)
        except DiffError as exc:
            raise AssembleError(f"{path}: {exc}") from None
        for row in rows:
            fid = str(row["id"])
            for name in (*APPROVAL_FIELDS, "verified_by"):
                if name in row:
                    raise AssembleError(f"{path}: {fid} carries {name}; a judge's finding may not")
            if row["publication"] not in batch.papers:
                raise AssembleError(
                    f"{path}: {fid} cites {row['publication']}, which batch {batch.id} "
                    "does not read"
                )
            if fid in seen:
                raise AssembleError(f"{path}: {fid} is also in {seen[fid]}")
            seen[fid] = path
            found.append(row)
    return found


def judge_digest(finding: Mapping[str, Any]) -> str:
    """The sha256 hex of a judge's finding as parsed: sorted-key compact JSON, UTF-8. It is what
    ``assemble.json`` records for each finding a judge decided, so a re-run can tell a finding
    it has already applied from one the judge has since changed."""
    text = json.dumps(finding, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rewrite_refs(
    finding: Mapping[str, Any],
    mapping: Mapping[str, str],
    held: frozenset[str],
    *,
    others_ok: bool = False,
) -> dict[str, Any]:
    """The finding with a ``shares_probands_with`` local id replaced by its final id.

    A ref in neither `mapping` nor `held` is refused, unless `others_ok` leaves it for
    ``append_numbered``, which maps the ids of the findings it numbers and refuses the rest."""
    out = dict(finding)
    shared = out.get("shares_probands_with")
    if isinstance(shared, list):
        rewritten = []
        for ref in shared:
            if ref in mapping:
                rewritten.append(mapping[ref])
            elif ref in held or others_ok:
                rewritten.append(ref)
            else:
                raise AssembleError(
                    f"{finding['id']}: shares_probands_with names {ref}, which is neither a "
                    "finding the judges added nor one in the file"
                )
        out["shares_probands_with"] = rewritten
    return out


def add_judged(
    c: Curated,
    run: RunPaths,
    previous: Mapping[str, str],
    additions: Sequence[Mapping[str, Any]],
    counter: int,
) -> tuple[bytes, dict[str, str], int]:
    """`c.lit` with the judges' added findings numbered from `counter`: `(bytes, local -> final
    ids, counter)`. A ``shares_probands_with`` entry naming a local id that `previous` (an earlier
    assemble's map) numbered becomes its final id; the rest is ``append_numbered``'s."""
    prepared = [rewrite_refs(f, previous, frozenset(c.findings), others_ok=True) for f in additions]
    years = paper_years(c, run, sorted({str(f["publication"]) for f in additions}))
    return append_numbered(c.lit, prepared, years, counter)


def replace_judged(
    literature: bytes,
    replacements: Mapping[str, Mapping[str, Any]],
    mapping: Mapping[str, str],
    held: frozenset[str],
) -> bytes:
    """`literature` with each finding of `replacements` (final id -> the judge's finding)
    replacing the finding of its id, in id order, its ``shares_probands_with`` local ids read
    through `mapping`; an id in neither `mapping` nor `held` is refused."""
    whole = [rewrite_refs(replacements[fid], mapping, held) for fid in sorted(replacements)]
    return with_findings_replaced(literature, whole) if whole else literature


def _verifier(run: RunPaths, plan: BatchPlan, given: str | None, to_mark: bool) -> str | None:
    """The name to write as ``verified_by``, or None when nothing is marked.

    A backfill takes ``--verifier`` (its blind pass records no ``extracted_by``) and refuses a run
    without it. Any other run refuses ``--verifier`` and takes the one ``extracted_by`` its verify
    findings share, refusing two or more."""
    if plan.mode == "backfill":
        if not given:
            raise AssembleError(
                "a backfill needs --verifier NAME: its blind pass records no extracted_by"
            )
        return given if to_mark else None
    if given is not None:
        raise AssembleError(
            f"--verifier belongs to a backfill; a {plan.mode} run takes it from the blind pass's "
            "extracted_by"
        )
    names: set[str] = set()
    if run.verify.is_dir():
        for path in sorted(run.verify.glob("batch-*.findings.yaml")):
            try:
                rows = load_pass(path)
            except RunFormatError as exc:
                raise AssembleError(str(exc)) from None
            for row in rows:
                by = row.get("extracted_by")
                if not isinstance(by, str) or not by:
                    raise AssembleError(f"{path}: {row['id']} has no extracted_by")
                names.add(by)
    if len(names) > 1:
        raise AssembleError(
            f"the blind pass's findings name more than one extracted_by: {', '.join(sorted(names))}"
        )
    if not to_mark:
        return None
    if not names:
        raise AssembleError(f"{run.verify}: holds no finding to read the verifier's name from")
    return next(iter(names))


def _declared_edit(
    file_finding: Mapping[str, Any], judge: Mapping[str, Any], declared: frozenset[str]
) -> tuple[dict[str, Any], list[str], frozenset[str]]:
    """What a backfill takes from a judge's whole finding: the fields to set, the keys to drop
    and the keys that may move. The rule for which values change a finding is merge's
    ``backfill_edit``; a declared field the judge left out is also dropped. Any other field of
    the judge's finding must equal the file's, except ``variant_classes`` where the rule's own
    allowance sets it (the judge may write that value or leave it out)."""
    given = {k: judge[k] for k in sorted(declared) if k in judge}
    edit = backfill_edit(file_finding, given)
    extra = {k: v for k, v in edit.fields.items() if k not in given}
    skip = {*declared, *APPROVAL_FIELDS, "verified_by"}
    stray = sorted(
        k
        for k in (set(file_finding) | set(judge)) - skip
        if file_finding.get(k) != judge.get(k)
        and not (k in extra and judge.get(k) in (None, extra[k]))
    )
    if stray:
        raise AssembleError(
            f"{judge['id']}: the judge changed fields the run did not declare: {stray}"
        )
    omitted = [k for k in sorted(declared) if k not in judge and not is_empty(file_finding.get(k))]
    drop = [*edit.drop, *omitted]
    if "verified_by" in file_finding and "verified_by" not in drop:
        drop.append("verified_by")
    return edit.fields, drop, edit.allowed | set(omitted) | {"verified_by"}


def _check_nothing_else_moved(
    c: Curated,
    after: bytes,
    *,
    new_ids: frozenset[str],
    allowed: Mapping[str, frozenset[str]],
    triage: Sequence[TriageChange],
    verifier: str | None,
) -> None:
    """Refuse a result where anything moved that no step above is entitled to move."""
    before, now = safe_load(c.lit, c.lit_path), safe_load(after, c.lit_path)
    other = {"findings", "triage"}
    if {k: v for k, v in before.items() if k not in other} != {
        k: v for k, v in now.items() if k not in other
    }:
        raise AssembleError(f"{c.lit_path}: a top-level key other than findings and triage moved")
    changed = {t.pmid for t in triage}
    old_t, new_t = before.get("triage") or [], now.get("triage") or []
    if [t["pmid"] for t in old_t] != [t["pmid"] for t in new_t]:
        raise AssembleError(f"{c.lit_path}: the triage entries or their order changed")
    for was, is_ in zip(old_t, new_t, strict=True):
        if was != is_ and was["pmid"] not in changed:
            raise AssembleError(f"{was['pmid']}: its triage entry changed without a reason")
    old_f, new_f = before.get("findings") or [], now.get("findings") or []
    old_ids = [str(f["id"]) for f in old_f]
    new_all = [str(f["id"]) for f in new_f]
    if new_all[: len(old_ids)] != old_ids or set(new_all[len(old_ids) :]) != new_ids:
        raise AssembleError(f"{c.lit_path}: findings were dropped, reordered or added unexpectedly")
    if len(new_all) != len(old_ids) + len(new_ids):
        raise AssembleError(f"{c.lit_path}: a finding id appears twice")
    for was, is_ in zip(old_f, new_f, strict=False):
        fid = str(was["id"])
        permitted = allowed.get(fid, frozenset())
        moved = {k for k in set(was) | set(is_) if was.get(k) != is_.get(k)}
        if permitted != _ANY and (stray := sorted(moved - permitted)):
            raise AssembleError(f"{fid}: fields nobody was entitled to change moved: {stray}")
        if "verified_by" in moved and is_.get("verified_by") not in (None, verifier):
            raise AssembleError(f"{fid}: verified_by is {is_.get('verified_by')!r}, not {verifier}")


@dataclass(frozen=True)
class RunState:
    """What ``assemble`` and ``check-batch`` read before they look at the judges' findings."""

    merged: MergeRecord
    plan: BatchPlan
    comparison: Comparison
    curated: Curated
    schema: Any
    previous: AssembleRecord


def read_state(root: Path, run: RunPaths, approved: str) -> RunState:
    """The run's merge record, plan, comparison, curated files and previous assemble record
    (empty when there is none), refused unless they agree. Reads only."""
    merged = load_merge(run)
    if merged is None:
        raise AssembleError(
            f"{run.merge}: the run has no merge.json; run `chd-curator merge` first"
        )
    if not run.comparison.is_file():
        raise AssembleError(
            f"{run.comparison}: the run has no comparison.json; run `chd-curator compare` first"
        )
    plan = load_batches(run)
    if plan.mode != merged.mode:
        raise AssembleError(
            f"merge.json is a {merged.mode} merge but batches.json plans {plan.mode}"
        )
    comparison = load_comparison(run)
    c = read_curated(root, run, approved, refuse_merged=False)
    previous = load_assemble(run) or AssembleRecord((), {}, (), (), None, {})
    for local, final in previous.added.items():
        if final not in c.findings:
            raise AssembleError(
                f"{run.assemble}: maps {local} to {final}, which {c.lit_path} lacks. A previous "
                "assemble stopped part-way: remove assemble.json and run it again"
            )
    return RunState(merged, plan, comparison, c, c.lit_validator.schema, previous)


@dataclass(frozen=True)
class Accepted:
    """The judges' findings sorted into the file's findings they replace (by final id) and the
    ones added, and the triage changes the added findings' papers need.

    A finding an earlier assemble applied, whose digest ``assemble.json`` still records, is not
    applied again: its id is in `unchanged` and the file's finding stays as it is. `digests` maps
    each decided final id to its judge finding's digest, `addition_digests` each added finding's
    local id."""

    replacements: dict[str, dict[str, Any]]
    additions: list[dict[str, Any]]
    triage: list[TriageChange]
    prior_final: frozenset[str]
    unchanged: frozenset[str]
    digests: dict[str, str]
    addition_digests: dict[str, str]


def accept_judged(
    state: RunState,
    judged: Sequence[dict[str, Any]],
    digests: Mapping[str, str],
    in_scope: Callable[[str], bool] = lambda _: True,
) -> Accepted:
    """Apply assemble's acceptance rules to the judges' findings, refusing with
    :class:`AssembleError` what assemble refuses: an id of no known kind, a finding the file
    lacks, one comparison.json also lists as agreed, a ``differs`` id left undecided or changed
    in a field its decisions do not name (new and reread runs), and a paper with no Publication
    or whose triage someone else set. `in_scope` limits which ``differs`` ids must be decided
    (the default is all, as assemble does; check-batch passes its batch's). `digests` maps each
    judge finding's id to ``judge_digest`` of the finding as read, before any caller edited it.

    A changed finding for an approved file finding is refused, but only where an earlier assemble
    recorded a digest for it: a first application may change an approved finding's declared
    fields in a backfill."""
    merged, plan, comparison = state.merged, state.plan, state.comparison
    c, schema, previous = state.curated, state.schema, state.previous
    backfill = plan.mode == "backfill"
    differs, only_first = set(comparison.differs), set(comparison.only_first)
    prior_final = set(previous.added.values())
    replacements: dict[str, dict[str, Any]] = {}  # file id -> the judge's finding
    additions: list[dict[str, Any]] = []
    unchanged: dict[str, str] = {}  # file id -> digest, applied by an earlier assemble
    target_digests: dict[str, str] = {}  # file id -> digest, of every decided finding
    addition_digests: dict[str, str] = {}
    for finding in judged:
        jid = str(finding["id"])
        digest = digests[jid]
        if jid in differs or jid in only_first:
            target = jid
        elif not backfill and jid in previous.added:
            target = previous.added[jid]
        elif not backfill and jid in prior_final:
            target = jid
        elif not backfill and LOCAL_ID.fullmatch(jid):
            additions.append(finding)
            addition_digests[jid] = digest
            continue
        else:
            raise AssembleError(
                f"judging: {jid} is neither a finding that differs or was read by one pass "
                "only, nor an added finding (CHDA:LIT:07...)"
                + ("; a backfill adds no finding" if backfill else "")
            )
        if target in replacements or target in unchanged:
            raise AssembleError(f"judging: more than one judge finding replaces {target}")
        target_digests[target] = digest
        recorded = previous.judged.get(target)
        if recorded == digest:
            unchanged[target] = digest
            continue
        if recorded is not None and target in c.findings and "approved_by" in c.findings[target]:
            raise AssembleError(f"{target}: approved; a judge may not change an approved finding")
        replacements[target] = {**finding, "id": target}
    decided = {*replacements, *unchanged}
    needed = {fid for fid in differs if in_scope(fid)}
    for fid in sorted(needed):
        if fid not in decided:
            raise AssembleError(f"{fid}: differs, but no judge finding decides it")
    for fid in sorted(decided):
        if fid not in c.findings:
            raise AssembleError(f"{fid}: a judge finding replaces it, but {c.lit_path} lacks it")
    for fid in sorted(decided & {*comparison.agreed, *comparison.span_only}):
        raise AssembleError(f"{fid}: comparison.json lists it as agreed and as judged")

    if not backfill:
        for fid in sorted(needed & set(replacements)):
            stray = sorted(
                set(differing_fields(c.findings[fid], replacements[fid], schema))
                - set(comparison.differs[fid])
            )
            if stray:
                raise AssembleError(
                    f"{fid}: the judge changed {stray}, which no decision covers "
                    f"(differs lists {list(comparison.differs[fid])})"
                )

    # Papers of judge-added findings: a Publication and an `included` triage entry each.
    registered = {str(r["id"]) for r in safe_load(c.pubs, c.pub_path).get("publications") or []}
    added_back = {fid for fid in replacements if fid in prior_final}
    cited = sorted(
        {str(f["publication"]) for f in [*additions, *(replacements[i] for i in added_back)]}
        | {
            str(replacements[fid]["publication"])
            for fid in replacements
            if fid not in prior_final
            and str(replacements[fid]["publication"]) != str(c.findings[fid]["publication"])
        }
    )
    for pmid in cited:
        if pmid not in registered:
            raise AssembleError(
                f"{pmid}: a judge's finding cites it but publications.yaml lacks it; run "
                f"`chd-curator add-publication {pmid}` first"
            )
    triage: list[TriageChange] = []
    for pmid in cited:
        entry = c.triage.get(pmid)
        if entry is None:
            raise AssembleError(f"{pmid}: is not in the triage of {c.lit_path}")
        decision = entry.get("decision")
        if decision == "included":
            continue
        if decision == "excluded" and _excluded_by_merge(merged, pmid):
            triage.append(TriageChange(pmid, "excluded", "included", None, None))
            continue
        raise AssembleError(
            f"{pmid}: a judge's finding cites it but its triage decision is {decision!r}, "
            "which merge did not set: someone else decided it"
        )

    return Accepted(
        replacements,
        additions,
        triage,
        frozenset(prior_final),
        frozenset(unchanged),
        target_digests,
        addition_digests,
    )


@dataclass(frozen=True)
class AppliedJudged:
    """The literature file with the judges' findings applied, and what that numbered."""

    literature: bytes
    new_map: dict[str, str]
    new_counter: int
    mapping: dict[str, str]
    held: frozenset[str]
    new_ids: frozenset[str]
    backfill_allowed: dict[str, frozenset[str]]


def apply_judged(state: RunState, run: RunPaths, accepted: Accepted) -> AppliedJudged:
    """`accepted` applied to the curated file: added findings numbered from the counter, then
    each replacement, whole in a new or reread run and by the declared fields only in a
    backfill (``_declared_edit``), then the triage changes. Pure."""
    c, plan, previous = state.curated, state.plan, state.previous
    backfill = plan.mode == "backfill"
    declared = frozenset(plan.fields)
    additions, replacements, triage = accepted.additions, accepted.replacements, accepted.triage
    literature = c.lit
    counter = read_counter(c.reg, PREFIX)
    new_map: dict[str, str] = {}
    new_counter = counter
    if additions:
        literature, new_map, new_counter = add_judged(c, run, previous.added, additions, counter)
    mapping = {**previous.added, **new_map}
    held = frozenset(c.findings) | frozenset(new_map.values())
    new_ids = frozenset(new_map.values())

    backfill_allowed: dict[str, frozenset[str]] = {}
    if backfill:
        for fid in sorted(replacements):
            fields, drop, permitted = _declared_edit(c.findings[fid], replacements[fid], declared)
            backfill_allowed[fid] = permitted
            literature = with_fields_set(literature, fid, fields, drop)
    else:
        literature = replace_judged(literature, replacements, mapping, held)
    if triage:
        literature = with_triage(literature, triage)
    return AppliedJudged(literature, new_map, new_counter, mapping, held, new_ids, backfill_allowed)


def assemble_run(root: Path, run: RunPaths, approved: str, verifier: str | None) -> Assembled:
    """Build the assembled literature file and ``assemble.json`` for a run; see the module
    docstring. Reads only. Raises :class:`AssembleError` (or ``MergeError``, ``RunFormatError``,
    ``DiffError``, ``RecordError``) for every refusal."""
    state = read_state(root, run, approved)
    merged, plan, comparison, c = state.merged, state.plan, state.comparison, state.curated
    schema, previous = state.schema, state.previous
    judged = judge_findings(run, plan, schema)
    backfill = plan.mode == "backfill"
    declared = frozenset(plan.fields)
    accepted = accept_judged(state, judged, {str(f["id"]): judge_digest(f) for f in judged})
    replacements, triage = accepted.replacements, accepted.triage
    prior_final = accepted.prior_final
    counter = read_counter(c.reg, PREFIX)
    applied = apply_judged(state, run, accepted)
    literature, new_counter, mapping = applied.literature, applied.new_counter, applied.mapping
    new_ids, backfill_allowed = applied.new_ids, applied.backfill_allowed

    # The notation rules.
    if backfill:
        scope = sorted(comparison.pairs) if "variants" in declared else []
    else:
        scope = sorted({*merged.ids.values(), *replacements, *accepted.unchanged, *new_ids})
    now = {str(f["id"]): f for f in safe_load(literature, c.lit_path).get("findings") or []}
    for fid in scope:
        if fid not in now:
            raise AssembleError(f"{fid}: is in the run but not in {c.lit_path}")
    changes: list[NotationChange] = []
    ruled: list[dict[str, Any]] = []
    for fid in scope:
        fixed, found = apply_rules(now[fid])
        if found:
            changes.extend(found)
            ruled.append(fixed)
    if ruled:
        if backfill:
            for fixed in ruled:
                literature = with_fields_set(
                    literature, str(fixed["id"]), {"variants": fixed["variants"]}
                )
        else:
            literature = with_findings_replaced(literature, ruled)

    # Blind agreement.
    to_mark = sorted({*comparison.agreed, *comparison.span_only})
    name = _verifier(run, plan, verifier, bool(to_mark))
    if to_mark and name is not None:
        literature = with_verified(literature, to_mark, name)

    allowed: dict[str, frozenset[str]] = {}
    for fid in replacements:
        allowed[fid] = _ANY if not backfill else backfill_allowed[fid]
    for fixed in ruled:
        allowed[str(fixed["id"])] = _allow(allowed, str(fixed["id"]), "variants")
    for fid in to_mark:
        allowed[fid] = _allow(allowed, fid, "verified_by")
    _check_nothing_else_moved(
        c, literature, new_ids=new_ids, allowed=allowed, triage=triage, verifier=name
    )

    touched = {*replacements, *new_ids}
    kept = [n for n in previous.notation if n.finding not in touched]
    notation = sorted([*kept, *changes], key=lambda n: n.finding)
    replaced = tuple(
        sorted(fid for fid in (*replacements, *accepted.unchanged) if fid not in prior_final)
    )
    judged_digests = {
        **accepted.digests,
        **{applied.new_map[local]: d for local, d in accepted.addition_digests.items()},
    }
    record = AssembleRecord(
        replaced, mapping, tuple(notation), tuple(to_mark), name, judged_digests
    )
    registry = with_counter(c.reg, PREFIX, new_counter) if new_counter != counter else None
    return Assembled(c, record, literature, registry, new_ids)


def _allow(allowed: Mapping[str, frozenset[str]], fid: str, key: str) -> frozenset[str]:
    held = allowed.get(fid, frozenset())
    return held if held == _ANY else held | {key}


def _excluded_by_merge(merged: MergeRecord, pmid: str) -> bool:
    return any(t.pmid == pmid and t.after == "excluded" for t in merged.triage)


def commit(run: RunPaths, built: Assembled) -> None:
    """Check every write, then write ``assemble.json``, the registry and the literature file."""
    c = built.curated
    check_literature(
        c.lit_path, built.literature, c.lit_validator, expected=c.lit, added=built.added
    )
    if built.registry is not None:
        check_registry(c.reg_path, built.registry, expected=c.reg)
    write_assemble(run, built.record)
    if built.registry is not None:
        write_registry(c.reg_path, built.registry, expected=c.reg)
    write_literature(
        c.lit_path, built.literature, c.lit_validator, expected=c.lit, added=built.added
    )


def summary(record: AssembleRecord) -> str:
    """The line ``assemble`` prints: the totals ``assemble.json`` records."""
    ids = sorted(record.added.values())
    if not ids:
        added = "added 0"
    elif len(ids) == 1:
        added = f"added 1 ({ids[0]})"
    else:
        added = f"added {len(ids)} ({ids[0]}..{ids[-1]})"
    return (
        f"replaced {len(record.replaced)} judged findings, {added}, "
        f"fixed {len(record.notation)} names by rule, marked {len(record.verified)} verified"
    )
