"""The ``chd-curator`` command line.

``synthesis`` is new here: it writes a gene's drafted, unapproved synthesis. ``search``,
``abstracts``, ``add-publication`` and ``diff`` are the atlas's ``chd-atlas
literature`` commands of the same names, copied from its ``cli.py`` at b4c8191. What
changed: the gene is resolved and the publication gate asked by running
``chd-atlas literature target`` (never by importing the atlas), and every file is
written through ``write.py``, which checks the bytes against the atlas's exported JSON
Schema first.

Exit codes: 2 for bad arguments, an atlas that refused or could not be asked, a gene
outside the publication gate, or a write the write path refused (a schema, an
anchor, an approval, or a file that changed since it was read); 1 for PubMed
failing, the network, or a file that cannot be read. For ``synthesis`` an unreadable or
refused ``--draft`` exits 2, and a literature file that is not valid YAML exits 1, as for
``search`` and ``diff --mark-verified``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import typer
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator import pubmed, variantvalidator
from chd_curator.atlas import (
    AtlasError,
    Frame,
    default_atlas_cli,
    frame,
    plain,
    schema_validator,
    target,
)
from chd_curator.diff import DiffError, diff_findings, parse_findings_file, stripped
from chd_curator.hgvs import names_a_residue, protein_key, same_protein_change, three_letter
from chd_curator.pubmed import Fetch, PubMedError, urllib_fetch
from chd_curator.records import (
    ApprovedSynthesis,
    RecordError,
    VariantChange,
    literature_path,
    load_round_trip,
    with_normalised,
    with_publication,
    with_search,
    with_synthesis,
    with_verified,
)
from chd_curator.write import (
    WriteRefused,
    refuse_anchors,
    schema_error_lines,
    write_literature,
    write_publications,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="CHD atlas curator tools.")

# The network hooks: tests replace them, nothing else reaches PubMed or VariantValidator.
_fetch: Fetch = urllib_fetch
_vv_fetch: variantvalidator.Fetch = variantvalidator.urllib_fetch

_DECISIONS = ("pending", "included", "excluded")


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"error: {message}")
    return typer.Exit(code=code)


def _api_key() -> str | None:
    """The NCBI key from the environment, or None when unset or blank."""
    return (os.environ.get("NCBI_API_KEY") or "").strip() or None


def _redact(message: str, key: str | None) -> str:
    return message.replace(key, "***") if key else message


def _require_root(root: Path) -> None:
    if not root.is_dir():
        raise _fail(f"--root {root} is not a directory", 2)


def _atlas_cli(root: Path, given: Path | None) -> Path:
    return given if given is not None else default_atlas_cli(root)


_ATLAS_CLI_OPTION = typer.Option(
    None,
    "--atlas-cli",
    envvar="CHD_ATLAS_CLI",
    help="The chd-atlas executable (default: <root>/.venv/bin/chd-atlas).",
)


@app.command("search")
def search(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    run_on: str = typer.Option(
        "", "--run-on", help="Date of the search, YYYY-MM-DD (default today)."
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Run the recorded PubMed search for a published gene into its literature file."""
    _require_root(root)
    try:
        when = date.fromisoformat(run_on) if run_on else date.today()
    except ValueError:
        raise _fail(f"--run-on {run_on!r} is not a YYYY-MM-DD date", 2) from None

    try:
        answer = target(root, symbol, _atlas_cli(root, atlas_cli))
        if not answer.published:
            raise _fail(
                f"{answer.symbol} ({answer.gene}) is outside the publication gate; "
                f"its literature would reach no page",
                2,
            )
        validator = schema_validator(root, "gene_literature_file")
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None

    path = literature_path(root, answer.symbol)
    key = _api_key()
    # Every network call finishes, and the new file is serialised, before the write
    # path checks and writes it: a failure anywhere leaves the file as it was.
    try:
        existing = path.read_bytes() if path.is_file() else None
        hits, pmids = pubmed.esearch_all(answer.query, _fetch, api_key=key)
        content, search_id, new_hits = with_search(
            existing, gene=answer.gene, query=answer.query, run_on=when, hits=hits, pmids=pmids
        )
    except (PubMedError, urllib.error.URLError, RecordError, OSError) as exc:
        raise _fail(_redact(str(exc), key), 1) from None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_literature(path, content, validator, expected=existing)
    except WriteRefused as exc:
        raise _fail(str(exc), 2) from None
    typer.echo(
        f"{answer.symbol}: search {search_id} found {hits} hit(s), {new_hits} new; wrote {path}"
    )


@app.command("abstracts")
def abstracts(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    decision: str = typer.Option("pending", help="pending, included or excluded."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Print PMID, year, title, publication types and abstract per triage entry with a decision."""
    _require_root(root)
    if decision not in _DECISIONS:
        raise _fail(f"--decision {decision!r} is not one of pending, included, excluded", 2)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    path = literature_path(root, approved)
    if not path.is_file():
        raise _fail(f"{path} does not exist; run `chd-curator search {approved}` first", 1)
    key = _api_key()
    try:
        record = load_round_trip(path.read_bytes())
        entries: list[Any] = list(record.get("triage") or [])
        pmids = [
            str(e["pmid"]).removeprefix("PMID:") for e in entries if e.get("decision") == decision
        ]
        titles = pubmed.efetch_abstracts(pmids, _fetch, api_key=key)
        meta = pubmed.esummary(pmids, _fetch, api_key=key)
    except (PubMedError, urllib.error.URLError, RecordError, OSError, KeyError) as exc:
        raise _fail(_redact(str(exc), key), 1) from None
    for pmid in pmids:
        found = titles.get(pmid)
        paper = meta.get(pmid)
        year = str(paper.year) if paper else "year unknown"
        if found is None:
            typer.echo(f"PMID:{pmid} · {year} · (PubMed returned no record for this PMID)")
        else:
            title, abstract, types = found
            typer.echo(f"PMID:{pmid} · {year} · {title}")
            typer.echo(f"Publication types: {'; '.join(types) or '(none in PubMed)'}")
            typer.echo(abstract or "(no abstract in PubMed)")
        typer.echo("")


def _study_types(root: Path) -> list[str]:
    """The allowed study types: ``$defs.StudyType.enum`` of the atlas's exported schema."""
    schema: Any = schema_validator(root, "publication_file").schema
    try:
        values = schema["$defs"]["StudyType"]["enum"]
    except (KeyError, TypeError) as exc:
        raise AtlasError("the atlas's publication_file schema has no StudyType enum") from exc
    return [str(v) for v in values]


@app.command("add-publication")
def add_publication(
    pmid: str = typer.Argument(..., help="PubMed id, with or without the PMID: prefix."),
    study_type: str = typer.Option(
        ..., "--study-type", help="The curator's reading of the design."
    ),
    reading: str = typer.Option(
        ...,
        "--reading",
        help="Why that design: what the paper says. Written as a comment beside the record.",
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
) -> None:
    """Append a publication record, built from PubMed, to curation/publications.yaml."""
    _require_root(root)
    digits = pmid.removeprefix("PMID:")
    if not re.fullmatch(r"[1-9]\d*", digits):
        raise _fail(f"{pmid!r} is not a PubMed id", 2)
    try:
        allowed = _study_types(root)
        validator = schema_validator(root, "publication_file")
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    if study_type not in allowed:
        raise _fail(f"--study-type {study_type!r} is not one of: {', '.join(allowed)}", 2)
    if not reading.strip():
        raise _fail("--reading (what the paper says about its design) must not be blank", 2)
    path = root / "curation" / "publications.yaml"
    if not path.is_file():
        raise _fail(f"{path} does not exist", 1)
    key = _api_key()
    try:
        existing = path.read_bytes()
        found = pubmed.esummary([digits], _fetch, api_key=key).get(digits)
        if found is None:
            raise PubMedError(f"PubMed returned no record for PMID:{digits}")
        content = with_publication(existing, pubmed.publication_record(found, study_type), reading)
    except (PubMedError, urllib.error.URLError, RecordError, OSError) as exc:
        raise _fail(_redact(str(exc), key), 1) from None
    try:
        write_publications(path, content, validator, expected=existing)
    except WriteRefused as exc:
        raise _fail(str(exc), 2) from None
    typer.echo(f"added PMID:{digits} to {path}")


def _ids(values: Sequence[str]) -> str:
    return ", ".join(values) or "none"


@app.command("diff")
def diff(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    against: Path = typer.Option(
        ..., "--against", help="The blind second pass: a YAML file with a top-level `findings:`."
    ),
    mark_verified: str | None = typer.Option(
        None,
        "--mark-verified",
        help="agent:<skill>/<model>; set verified_by on every agreeing, unverified finding.",
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Compare a blind second extraction with the curated findings; exits 0 on differences."""
    _require_root(root)
    if mark_verified is not None and not (
        mark_verified.startswith("agent:") and mark_verified.removeprefix("agent:").strip()
    ):
        raise _fail("--mark-verified names the verifier agent as 'agent:<skill>/<model>'", 2)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
        validator = schema_validator(root, "gene_literature_file")
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    path = literature_path(root, approved)
    if not path.is_file():
        raise _fail(f"{path} does not exist", 2)
    if not against.is_file():
        raise _fail(f"--against {against} is not a file", 2)
    schema: Any = validator.schema
    try:
        raw = path.read_bytes()
        curated = plain(YAML(typ="safe").load(raw))
        broken = schema_error_lines(curated, validator)
        if broken:
            raise DiffError(f"{path} does not satisfy its schema:\n" + "\n".join(broken))
        first: list[dict[str, Any]] = list(curated.get("findings") or [])
        second = parse_findings_file(against.read_bytes(), schema)
        result = diff_findings(first, second, schema)
    # ruamel raises a bare ValueError, not a YAMLError, for a date such as 2026-02-30.
    except (DiffError, YAMLError, ValueError, OSError) as exc:
        raise _fail(str(exc), 1) from None

    typer.echo(f"agreed ({len(result.agreed)}): {_ids(result.agreed)}")
    typer.echo(f"only in the first pass ({len(result.only_first)}): {_ids(result.only_first)}")
    typer.echo(f"only in the second pass ({len(result.only_second)}): {_ids(result.only_second)}")
    typer.echo(f"differ ({len(result.fields)}):")
    for finding_id, changes in result.fields.items():
        typer.echo(f"  {finding_id}")
        for name, mine, theirs in changes:
            typer.echo(
                f"    {name}: first {json.dumps(mine, sort_keys=True)}"
                f" | second {json.dumps(theirs, sort_keys=True)}"
            )
    if mark_verified is None:
        return
    unverified = {str(f["id"]) for f in first if f.get("verified_by") is None}
    marked = [i for i in result.agreed if i in unverified]
    if marked:
        try:
            write_literature(
                path, with_verified(raw, marked, mark_verified), validator, expected=raw
            )
        except RecordError as exc:
            raise _fail(str(exc), 1) from None
        except WriteRefused as exc:
            raise _fail(str(exc), 2) from None
    typer.echo(f"marked verified ({len(marked)}): {_ids(marked)}")


def _unversioned(accession: str) -> str:
    return accession.split(".", 1)[0]


def _on_mane(reported_transcript: str | None, mane: Frame) -> bool:
    """Whether the paper's own `p.` is numbered on MANE: no transcript named, or MANE's."""
    return reported_transcript is None or _unversioned(reported_transcript) in {
        _unversioned(mane.transcript),
        _unversioned(mane.protein),
    }


def _agrees(reported: dict[str, Any], hgvs_p: str | None, mane: Frame) -> bool:
    """Whether `hgvs_p` may be written beside the paper's `p.` (the atlas's LIT025 with D6').

    The comparison is the atlas's, and so is an absent `hgvs_p` beside a paper residue (a
    disagreement). curator is stricter only where the atlas skips LIT025 for a `hgvs_p` it
    cannot use and reports that itself: `_decide` unplaces one on another protein accession
    (the atlas: LIT016), and this unplaces an on-MANE one the atlas cannot read, such as
    `p.(Met1ext-5)` or `p.(Gln70[3])` (the atlas: LIT021).
    """
    paper, transcript = reported.get("p"), reported.get("transcript")
    if paper is None or not _on_mane(transcript.strip() if transcript else None, mane):
        return True
    paper = paper.strip()  # the atlas's models strip every string
    hgvs_p = hgvs_p.strip() if hgvs_p else None
    if hgvs_p is None:
        return not names_a_residue(paper)
    # D6': with a residue named on only one side there is nothing to compare.
    if not names_a_residue(paper) or not names_a_residue(hgvs_p):
        return True
    return same_protein_change(paper, hgvs_p)


def _note(variant: dict[str, Any], new: str) -> str:
    old = variant.get("review_note")
    return f"{old}; {new}" if old else new


def _decide(
    variant: dict[str, Any],
    result: variantvalidator.Placed | variantvalidator.Refused,
    description: str,
    mane: Frame,
) -> tuple[dict[str, Any], str]:
    """The fields to write for one variant, and the line to print about it."""
    reported: dict[str, Any] = variant.get("reported") or {}
    if isinstance(result, variantvalidator.Placed):
        accession = (
            result.hgvs_p.split(":", 1)[0] if result.hgvs_p and ":" in result.hgvs_p else None
        )
        if accession is not None and accession != mane.protein:
            note = (
                f"VariantValidator places {description} on protein {accession}, not the pinned "
                f"{mane.protein}; the variant is unplaced"
            )
            message = f"unplaced: VariantValidator's protein is {accession}, not {mane.protein}"
        elif _agrees(reported, result.hgvs_p, mane):
            fields = {"hgvs_c": result.hgvs_c, "hgvs_p": result.hgvs_p, "grch38": result.grch38}
            return fields, f"placed {result.hgvs_c} at {result.grch38} ({result.hgvs_p or 'no p.'})"
        else:
            shown = result.hgvs_p or "no protein change"
            note = (
                f"the paper names {reported['p']} but VariantValidator places {description} as "
                f"{shown}; the two disagree, so the variant is unplaced"
            )
            message = f"unplaced: the paper names {reported['p']}, VariantValidator gives {shown}"
    else:
        note = f"VariantValidator refused {description}: {result.warnings[0]}"
        message = f"unplaced: {result.warnings[0]}"
    kept = None
    paper = reported.get("p")
    if (
        isinstance(result, variantvalidator.Refused)
        and paper is not None
        and _on_mane(reported.get("transcript"), mane)
        and names_a_residue(paper)
    ):
        kept = f"{mane.protein}:p.({three_letter(paper)})"
        message += f"; kept the paper's {kept}, run chd-atlas validate to check its residue"
    fields = {
        "placement": "unplaced",
        "hgvs_c": None,
        "grch38": None,
        "hgvs_p": kept,
        "review_status": "pending_review",
        "review_note": _note(variant, note),
        # R-U6: the tool cannot tell a paper error from a tool or frame limit, so it names the one
        # settler that is always true; a settler a curator already wrote is kept.
        "review_settled_by": variant.get("review_settled_by") or "expert",
    }
    return fields, message


def _row_key(variant: dict[str, Any]) -> str | None:
    """The key the atlas groups a variant's page row under, or None (`_row_key` there)."""
    if variant.get("placement") == "mane" and variant.get("grch38") is not None:
        return "g:" + str(variant["grch38"])
    if variant.get("placement") == "protein" and variant.get("hgvs_p") is not None:
        return "p:" + protein_key(str(variant["hgvs_p"]))
    return None


def _groups(variants: list[dict[str, Any]]) -> dict[int, int]:
    """Variant id -> its `with` group number: connected components of the links that resolve,
    read undirected, of size two or more. Empty when two variants share an id."""
    ids = [v["id"] for v in variants]
    if len(set(ids)) != len(ids):
        return {}
    parent = {i: i for i in ids}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for v in variants:
        for other in v.get("with") or []:
            if other in parent and other != v["id"]:
                parent[find(v["id"])] = find(other)
    members: dict[int, list[int]] = {}
    for i in ids:
        members.setdefault(find(i), []).append(i)
    sized = [g for g in members.values() if len(g) > 1]
    return {i: n for n, group in enumerate(sized) for i in group}


def _context(variant: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> tuple[Any, Any]:
    """The genotype context (ruling D16'): the partners' names and the phase.

    A partner is named by its row key, or by `as_reported` when it has none, so the same
    pair written under different ids is one context.
    """
    names = set()
    for other in variant.get("with") or []:
        partner = by_id.get(other)
        names.add(f"#{other}" if partner is None else _row_key(partner) or partner["as_reported"])
    return frozenset(names), variant.get("phase")


def _duplicate_keys(
    findings: list[dict[str, Any]], changes: dict[tuple[str, int], dict[str, Any]]
) -> list[str]:
    """Pairs of variants the atlas would refuse as one name repeated (LIT022, ruling D16').

    Computed on the state after normalisation. Two variants with one row key clash when their
    genotype contexts are equal, or when both sit in one `with` group (one change twice in a
    group is in effect homozygous).
    """
    problems: list[str] = []
    for finding in findings:
        merged = [
            {**stripped(v), **changes.get((str(finding["id"]), v["id"]), {})}
            for v in finding.get("variants") or []
        ]
        by_id = {v["id"]: v for v in merged}
        group_of = _groups(merged)
        seen: dict[str, list[dict[str, Any]]] = {}
        for variant in merged:
            key = _row_key(variant)
            if key is None:
                continue
            twin = next(
                (
                    w
                    for w in seen.get(key, [])
                    if w["id"] != variant["id"]
                    and (
                        _context(w, by_id) == _context(variant, by_id)
                        or (
                            variant["id"] in group_of
                            and group_of.get(w["id"]) == group_of[variant["id"]]
                        )
                    )
                ),
                None,
            )
            if twin is not None:
                problems.append(
                    f"{finding['id']}: variants {twin['id']} and {variant['id']} both name "
                    f"{key[2:]} in one genotype context (or one `with` group); the atlas would "
                    "refuse the file (LIT022), so fix their names or links first"
                )
            seen.setdefault(key, []).append(variant)
    return problems


@app.command("normalise")
def normalise(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Place each `mane` variant the paper named by its c. on the MANE frame, with VariantValidator.

    Fills hgvs_c, hgvs_p and grch38, or marks the variant unplaced and pending review, and
    records the run. Never touches an approval or verified_by.
    """
    _require_root(root)
    try:
        mane = frame(root, symbol, _atlas_cli(root, atlas_cli))
        validator = schema_validator(root, "gene_literature_file")
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    path = literature_path(root, mane.symbol)
    if not path.is_file():
        raise _fail(f"{path} does not exist", 2)
    try:
        raw = path.read_bytes()
        curated = plain(YAML(typ="safe").load(raw))
        broken = schema_error_lines(curated, validator)
        if broken:
            raise DiffError(f"{path} does not satisfy its schema:\n" + "\n".join(broken))
    except (DiffError, YAMLError, ValueError, OSError) as exc:
        raise _fail(str(exc), 1) from None
    findings: list[dict[str, Any]] = list(curated.get("findings") or [])

    changes: dict[tuple[str, int], dict[str, Any]] = {}
    lines: list[str] = []
    skipped: list[str] = []
    metadata: variantvalidator.Metadata | None = None
    for finding in findings:
        for raw_variant in finding.get("variants") or []:
            # The atlas's models strip every string; the decisions read the stripped view.
            variant = stripped(raw_variant)
            reported = variant.get("reported") or {}
            if (
                variant.get("placement") != "mane"
                or reported.get("c") is None
                or variant.get("normalisation") is not None
            ):
                continue
            if variant.get("kind") != "snv_indel":
                skipped.append(
                    f"{finding['id']} variant {variant['id']} ({variant['as_reported']}): skipped, "
                    f"kind {variant.get('kind')} is not snv_indel (the atlas refuses a mane "
                    "variant of that kind, LIT022)"
                )
                continue
            description = f"{reported.get('transcript') or mane.transcript}:{reported['c']}"
            try:
                result, seen = variantvalidator.place(description, mane.transcript, _vv_fetch)
            except variantvalidator.VariantValidatorError as exc:
                raise _fail(f"{exc}; nothing was written", 1) from None
            if metadata is not None and seen != metadata:
                raise _fail(
                    "VariantValidator's versions changed during the run; nothing was written", 1
                )
            metadata = seen
            fields, message = _decide(variant, result, description, mane)
            changes[(str(finding["id"]), variant["id"])] = fields
            lines.append(
                f"{finding['id']} variant {variant['id']} ({variant['as_reported']}): {message}"
            )
    if metadata is None:
        for line in skipped:
            typer.echo(line)
        typer.echo(
            "no variant was asked about: none is a snv_indel placed `mane` with a reported c. "
            "and no normalisation"
        )
        return
    problems = _duplicate_keys(findings, changes)
    if problems:
        raise _fail("\n".join(problems) + "\nnothing was written", 2)
    run = {
        "version": metadata.version,
        "hgvs_version": metadata.hgvs_version,
        "vvta": metadata.vvta,
        "vvdb": metadata.vvdb,
        "seqrepo": metadata.seqrepo,
        "run_on": date.today(),
    }
    try:
        content, run_id = with_normalised(
            raw, [VariantChange(f, v, fields) for (f, v), fields in changes.items()], run
        )
        write_literature(path, content, validator, expected=raw)
    except RecordError as exc:
        raise _fail(str(exc), 1) from None
    except WriteRefused as exc:
        raise _fail(str(exc), 2) from None
    for line in [*lines, *skipped]:
        typer.echo(line)
    approved = {str(f["id"]) for f in findings if f.get("approved_by") is not None}
    for finding_id in sorted({f for f, _ in changes} & approved):
        typer.echo(
            f"{finding_id}: its approval is now void, because its variants changed; re-approve "
            f"it with: chd-atlas literature review {mane.symbol} --approve {finding_id} "
            "--by <name>"
        )
    typer.echo(
        f"asked about {len(lines)} variant(s) in {path} as run {run_id}; run chd-atlas validate"
    )


_DRAFT_KEYS = ("sentences", "drafted_by")
_MAX_DRAFT_BYTES = 64 * 1024
_MAX_SENTENCES = 5
_SENTENCE_KEYS = ("text", "cites")


def _check_draft_shape(loaded: Any) -> None:
    """Refuse (exit 2) a draft that is not `sentences` + `drafted_by`, naming the fault.

    Only the shape is checked; what the values may be is the atlas schema's, in the write.
    """
    if not isinstance(loaded, dict):
        raise _fail("the draft must be a mapping with sentences and drafted_by", 2)
    extra = sorted(str(key) for key in loaded if key not in _DRAFT_KEYS)
    if extra:
        raise _fail(
            f"the draft may hold only sentences and drafted_by; remove {', '.join(extra)}", 2
        )
    missing = [key for key in _DRAFT_KEYS if key not in loaded]
    if missing:
        raise _fail(f"the draft has no {', '.join(missing)}", 2)
    sentences = loaded["sentences"]
    if not isinstance(sentences, list) or not all(isinstance(s, dict) for s in sentences):
        raise _fail("the draft's sentences must be a list of mappings with text and cites", 2)
    if len(sentences) > _MAX_SENTENCES:
        raise _fail("the draft holds more than five sentences", 2)
    for sentence in sentences:
        stray = sorted(str(key) for key in sentence if key not in _SENTENCE_KEYS)
        if stray:
            raise _fail(f"a sentence may hold only text and cites; remove {', '.join(stray)}", 2)
        absent = [key for key in _SENTENCE_KEYS if key not in sentence]
        if absent:
            raise _fail(f"a sentence has no {', '.join(absent)}", 2)
        if not isinstance(sentence["cites"], list):
            raise _fail("a sentence's cites must be a list", 2)
        if not all(isinstance(cite, str) for cite in sentence["cites"]):
            raise _fail("a sentence's cites must all be strings", 2)


def _check_written(content: bytes, loaded: dict[str, Any]) -> None:
    """Refuse (exit 2) unless the content, read back, holds the draft's synthesis as drafted.

    YAML turns some characters (U+0085, for one) into other ones on the way through, so a
    draft can be written as different text while every other check passes.
    """
    try:
        block = plain(YAML(typ="safe").load(content)).get("synthesis")
    except (YAMLError, AttributeError):
        block = None
    drafted = {
        "sentences": [{"text": s["text"], "cites": list(s["cites"])} for s in loaded["sentences"]],
        "drafted_by": loaded["drafted_by"],
    }
    if not isinstance(block, dict) or {k: block.get(k) for k in drafted} != drafted:
        raise _fail("the written synthesis would differ from the draft; nothing was written", 2)


@app.command("synthesis")
def synthesis(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    draft: Path = typer.Option(
        ..., "--draft", help="A YAML file holding exactly `sentences` and `drafted_by`."
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Write a drafted, unapproved synthesis into a gene's literature file.

    Checks the file against the exported schema only; the atlas's SYN rules (citations,
    numbers, length) are run by `chd-atlas validate`. A person approves, in the atlas.
    """
    _require_root(root)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
        validator = schema_validator(root, "gene_literature_file")
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    path = literature_path(root, approved)
    if not path.is_file():
        raise _fail(f"{path} does not exist; run `chd-curator search {approved}` first", 2)
    if not draft.is_file():
        raise _fail(f"--draft {draft} is not a file", 2)
    try:
        if draft.stat().st_size > _MAX_DRAFT_BYTES:
            raise _fail("the draft is larger than 64 KiB", 2)
        raw_draft = draft.read_bytes()
        refuse_anchors(raw_draft)
        loaded = plain(YAML(typ="safe").load(raw_draft))
    except WriteRefused as exc:
        raise _fail(f"the draft: {exc}", 2) from None
    except (YAMLError, ValueError, OSError) as exc:
        raise _fail(f"the draft cannot be read: {exc}", 2) from None
    _check_draft_shape(loaded)
    try:
        existing = path.read_bytes()
        content = with_synthesis(existing, loaded)
        _check_written(content, loaded)
    except ApprovedSynthesis as exc:
        raise _fail(str(exc), 2) from None
    except (RecordError, OSError) as exc:
        raise _fail(str(exc), 1) from None
    try:
        write_literature(path, content, validator, expected=existing)
    except WriteRefused as exc:
        raise _fail(str(exc), 2) from None
    count = len(loaded["sentences"])
    typer.echo(f"{approved}: wrote a {count}-sentence synthesis, unapproved, to {path}")
