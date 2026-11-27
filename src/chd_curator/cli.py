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

import io
import json
import os
import re
import shlex
import subprocess
import urllib.error
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Final, cast

import typer
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator import agreement as agreement_mod
from chd_curator import archive as archive_mod
from chd_curator import archive_read, pubmed, review_page, sources, variantvalidator
from chd_curator import ask as ask_module
from chd_curator import assemble as assemble_module
from chd_curator import batch as batch_module
from chd_curator import check as check_module
from chd_curator import compare as compare_module
from chd_curator import jobs as jobs_module
from chd_curator import merge as merge_module
from chd_curator import package as package_module
from chd_curator import qualify as qualify_module
from chd_curator import redact as redact_mod
from chd_curator import register as register_mod
from chd_curator import report as report_module
from chd_curator import requests as requests_module
from chd_curator import scratch as scratch_module
from chd_curator import texts as texts_module
from chd_curator import triage as triage_module
from chd_curator.atlas import (
    AtlasError,
    Frame,
    default_atlas_cli,
    digests,
    frame,
    plain,
    run_validate,
    schema_validator,
    target,
)
from chd_curator.decisions import ExportError, decisions_from_export
from chd_curator.diff import DiffError, diff_findings, parse_findings_file, stripped
from chd_curator.fs import write_bytes_atomically
from chd_curator.hgvs import names_a_residue, protein_key, same_protein_change, three_letter
from chd_curator.pubmed import Fetch, PubMedError, urllib_fetch
from chd_curator.quotes import LONG_QUOTE_WORDS, check_quotes, write_quotes
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
from chd_curator.review_data import ReviewDataError, Reviewer, build_review_data
from chd_curator.roles import ROLES
from chd_curator.run import RUN_FORMAT, RunFormatError, RunPaths, load_batches
from chd_curator.site import SiteError, load_site, model_key
from chd_curator.write import (
    WriteRefused,
    refuse_anchors,
    schema_error_lines,
    write_literature,
    write_publications,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="CHD atlas curator tools.")

qualify_app = typer.Typer(no_args_is_help=True, help="Score a model against a gold for a role.")
app.add_typer(qualify_app, name="qualify")

# The network hooks: tests replace them, nothing else reaches PubMed or VariantValidator.
_fetch: Fetch = urllib_fetch
_vv_fetch: variantvalidator.Fetch = variantvalidator.urllib_fetch
_get: sources.Get = sources.urllib_get

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
        typer.echo(triage_module.hit_block(pmid, year, found))
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


# R-P2-3 (CP-D8): "before the MANE release" is before the pinned release's files. The MANE v1.5
# release files on NCBI's FTP (https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/release_1.5/)
# are dated 2025-12-04, read on 2026-11-24. A publication holds only a year, so a paper predates
# them when its year is below this one. A transcript that is `.1` in a later release was `.1` at
# every earlier date, so repinning the atlas forward leaves this true;
# `test_the_pinned_mane_release_is_not_older_than_the_constant` fails if the atlas pins below
# MANE_PINNED_RELEASE.
MANE_PINNED_RELEASE: Final = "1.5"
MANE_PINNED_RELEASE_YEAR: Final = 2025


def _read_unversioned(
    reported_transcript: str | None, mane: Frame, paper_year: int | None
) -> str | None:
    """MANE's transcript when a paper's unversioned accession is to be read as it (CP-D8).

    That is when the paper's transcript is MANE's accession without a version (a versioned
    accession never equals it), MANE's version is ``.1``, the paper's year is known and is
    before the pinned MANE release's year. Otherwise None, and the description keeps the
    accession the paper wrote. Only the description sent changes; `reported` is never rewritten.
    """
    if (
        reported_transcript is None
        or reported_transcript != _unversioned(mane.transcript)
        or not mane.transcript.endswith(".1")
        or paper_year is None
        or paper_year >= MANE_PINNED_RELEASE_YEAR
    ):
        return None
    return mane.transcript


def _paper_years(root: Path) -> dict[str, int]:
    """PMID -> year from ``curation/publications.yaml``; a record with no integer year has none.

    A missing, unreadable or malformed file exits 1, because without it CP-D8 would silently
    never apply.
    """
    path = root / "curation" / "publications.yaml"
    if not path.is_file():
        raise _fail(f"{path} does not exist; nothing was written", 1)
    try:
        data = plain(YAML(typ="safe").load(path.read_bytes()))
    except (YAMLError, ValueError, OSError) as exc:
        raise _fail(f"{path} cannot be read: {exc}; nothing was written", 1) from None
    rows = data.get("publications") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _fail(f"{path} has no 'publications' list; nothing was written", 1)
    return {
        str(row["id"]): row["year"]
        for row in rows
        if isinstance(row, dict)
        and "id" in row
        and isinstance(row.get("year"), int)
        and not isinstance(row["year"], bool)
    }


def _on_mane(reported_transcript: str | None, mane: Frame) -> bool:
    """Whether the paper's own `p.` is numbered on MANE: no transcript named, or MANE's."""
    return reported_transcript is None or _unversioned(reported_transcript) in {
        _unversioned(mane.transcript),
        _unversioned(mane.protein),
    }


def _agrees(reported: dict[str, Any], hgvs_p: str | None, mane: Frame) -> bool:
    """Whether `hgvs_p` may be written beside the paper's `p.` (the atlas's LIT025 with D6').

    The comparison is the atlas's, and so is an absent `hgvs_p` beside a paper residue (a
    disagreement). Three differences are curator's: `_decide` unplaces a `hgvs_p` on another
    protein accession (the atlas: LIT016), and this unplaces an on-MANE one the atlas cannot
    read, such as `p.(Met1ext-5)` (LIT021). And it compares the paper's `p.` even where the paper
    named another transcript, which the atlas skips: `place` puts that variant on MANE through
    its genomic position (R-F9). That catches a paper `c.` naming the wrong residue only if the
    paper names a residue, MANE's `p.` names one (an intronic or UTR position gives `p.?` or
    `p.(=)` and passes unchecked), and the paper numbers residues as MANE does.
    """
    paper = reported.get("p")
    if paper is None:
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
    reading: str | None = None,
) -> tuple[dict[str, Any], str]:
    """The fields to write for one variant, and the line to print about it.

    `reading` is the CP-D8 sentence when the description asked was read from an unversioned
    accession. An unplaced variant's note gains it (R-P2-28), because the description its note
    names is one the paper never wrote; a placed variant has no field to hold it (R-P2-4).
    """
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
        what = (
            description
            if result.asked in (None, description)
            else f"{result.asked}, the genomic form of {description}, on {mane.transcript}"
        )
        note = f"VariantValidator refused {what}: {result.warnings[0]}"
        message = f"unplaced: {result.warnings[0]}"
    if reading is not None:
        note += f"; {reading}"
    kept = None
    paper = reported.get("p")
    if (
        isinstance(result, variantvalidator.Refused)
        and paper is not None
        and names_a_residue(paper)
    ):
        kept = f"{mane.protein}:p.({three_letter(paper)})"
        message += f"; kept the paper's {kept}, run chd-atlas validate to check its residue"
        # R-P2-5: the paper names no protein accession and the atlas holds none but MANE's, so a
        # `p.` the paper numbered on another transcript is kept on MANE's protein, said so.
        named = reported.get("transcript")
        if not _on_mane(named, mane):
            note += f"; the paper's p. is numbered on {named}; kept on {mane.protein}"
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


def _entry(
    finding: Any, variant: Any, outcome: str, line: str, reading: str | None
) -> dict[str, Any]:
    return {
        "finding": finding,
        "variant": variant,
        "outcome": outcome,
        "line": line,
        "reading": reading,
    }


def _write_run_record(
    paths: RunPaths, normalisation: int | None, entries: list[dict[str, Any]], failure: str
) -> None:
    """Write ``<run>/normalise.json``: the normalisation id this run wrote (None when it asked
    nothing) and one entry per variant asked or skipped, in file order. `failure` says what the
    run did to the curated file; it opens the message when the write fails (exit 1)."""
    record = {"format": RUN_FORMAT, "normalisation": normalisation, "variants": entries}
    text = json.dumps(record, sort_keys=True, ensure_ascii=False, indent=1) + "\n"
    try:
        write_bytes_atomically(paths.normalise, text.encode("utf-8"))
    except OSError as exc:
        raise _fail(f"{failure}, but {paths.normalise} was not written: {exc}", 1) from None


@app.command("normalise")
def normalise(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    run_name: str | None = typer.Option(
        None, "--run", help="A run id: also write <run>/normalise.json for the review page."
    ),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Place each `mane` variant the paper named by its c. on the MANE frame, with VariantValidator.

    Fills hgvs_c, hgvs_p and grch38, or marks the variant unplaced and pending review, and
    records the run. Never touches an approval or verified_by. A variant VariantValidator
    answers 404 for is refused alone. With --run, writes <run>/normalise.json, one entry per
    variant asked or skipped, after the curated file when one is written (and refuses, exit 2,
    a run that already holds one).
    """
    _require_root(root)
    try:
        mane = frame(root, symbol, _atlas_cli(root, atlas_cli))
        validator = schema_validator(root, "gene_literature_file")
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    paths: RunPaths | None = None
    if run_name is not None:
        paths = RunPaths(root, mane.symbol, run_name)
        if Path(run_name).name != run_name or run_name in ("", ".", "..") or not paths.dir.is_dir():
            raise _fail(f"{paths.dir} is not a run directory; nothing was written", 2)
        if paths.normalise.exists():
            raise _fail(
                f"{paths.normalise} exists: this run was already normalised; use a new run "
                "id; nothing was written",
                2,
            )
    years = _paper_years(root)
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
    entries: list[dict[str, Any]] = []
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
                skip = (
                    f"{finding['id']} variant {variant['id']} ({variant['as_reported']}): skipped, "
                    f"kind {variant.get('kind')} is not snv_indel (the atlas refuses a mane "
                    "variant of that kind, LIT022)"
                )
                skipped.append(skip)
                entries.append(_entry(finding["id"], variant["id"], "skipped", skip, None))
                continue
            named = reported.get("transcript")
            year = years.get(str(finding.get("publication")))
            as_mane = _read_unversioned(named, mane, year)
            reading = (
                f"read {named} as {as_mane} (CP-D8: MANE's version is .1; the paper is from "
                f"{year}, before MANE v{MANE_PINNED_RELEASE})"
                if as_mane is not None
                else None
            )
            description = f"{as_mane or named or mane.transcript}:{reported['c']}"
            try:
                result, seen = variantvalidator.place(
                    description, mane.transcript, _vv_fetch, mane.strand
                )
            except variantvalidator.VariantValidatorError as exc:
                raise _fail(f"{exc}; nothing was written", 1) from None
            if seen is not None:  # a 404 carries no versions
                if metadata is not None and seen != metadata:
                    raise _fail(
                        "VariantValidator's versions changed during the run; nothing was written",
                        1,
                    )
                metadata = seen
            fields, message = _decide(variant, result, description, mane, reading)
            if reading is not None:
                message += f"; {reading}"
            changes[(str(finding["id"]), variant["id"])] = fields
            line = f"{finding['id']} variant {variant['id']} ({variant['as_reported']}): {message}"
            lines.append(line)
            outcome = "unplaced" if fields.get("placement") == "unplaced" else "placed"
            entries.append(_entry(finding["id"], variant["id"], outcome, line, reading))
    if not changes:
        for line in skipped:
            typer.echo(line)
        typer.echo(
            "no variant was asked about: none is a snv_indel placed `mane` with a reported c. "
            "and no normalisation"
        )
        if paths is not None:
            _write_run_record(
                paths, None, entries, "no variant was asked, so the curated file is unchanged"
            )
        return
    if metadata is None:
        raise _fail(
            "VariantValidator answered 404 for every variant asked, so no request carried its "
            "versions to record; nothing was written",
            1,
        )
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
    if paths is not None:
        _write_run_record(paths, run_id, entries, "the curated file was written")
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


@app.command("texts")
def texts(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    run: str = typer.Option(..., "--run", help="The run id; its directory is created."),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Fetch every paper anew instead of reusing its fetch.json, which is read only to "
        "decide what is kept: a paper keeps its earlier texts when a full-text source failed "
        "and the new fetch has no full text where it had one (not when that text came from a "
        "supplied PDF since changed or gone), or when a supplement source failed, a "
        "supplement file of the earlier fetch is missing and the new fetch gained neither a "
        "full text where there was none nor a new or changed supplied PDF.",
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Gather the free full texts and supplements of a gene's triaged papers; write texts.json.

    Papers are the triage's included entries and those excluded as full_text_not_accessible.
    Texts go under local/literature/texts/<PMID digits>/, and <run>/texts.json lists them for
    the other run commands. A paper is fetched again with --refresh, or when its supplied PDF
    appeared, changed or went since its fetch.json; it keeps its earlier texts, and a line says
    so, when the step that has less failed: a full-text source, with no full text where there
    was one (not when that text came from a supplied PDF since changed or gone), or a
    supplement source, with an earlier supplement file missing and nothing gained (a full text
    where there was none, or a new or changed supplied PDF); a fetch that gained replaces the
    earlier one, and a line names the earlier files not carried over. Exit 1 when PubMed fails
    or has no record for a PMID, a paper's fetch.json is unusable, a supplied PDF cannot be read
    or a file cannot be written; exit 2 for an unknown gene, a missing or malformed literature
    file, a bad run name or a run that already holds a texts.json (nothing is requested then).
    """
    _require_root(root)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    paths = RunPaths(root, approved, run)
    if Path(run).name != run or run in ("", ".", ".."):
        raise _fail(f"{run!r} is not a run id; nothing was written", 2)
    if paths.texts.exists():
        raise _fail(
            f"{paths.texts} exists: this run already has its texts; use a new run id; "
            "nothing was written",
            2,
        )
    path = literature_path(root, approved)
    if not path.is_file():
        raise _fail(f"{path} does not exist; run `chd-curator search {approved}` first", 2)
    key = _api_key()
    try:
        try:
            literature = plain(YAML(typ="safe").load(path.read_bytes()))
        except (YAMLError, OSError) as exc:
            raise texts_module.TextsError(f"{path}: cannot be read as YAML: {exc}", 2) from None
        if not isinstance(literature, dict):
            raise texts_module.TextsError(f"{path}: the top level is not a mapping", 2)
        digits = texts_module.triage_pmids(literature, path)
        papers, notices = texts_module.gather_papers(root, digits, _fetch, _get, key, refresh)
        texts_module.write_texts(paths, papers)
    except texts_module.TextsError as exc:
        raise _fail(str(exc), exc.code) from None
    except OSError as exc:
        raise _fail(f"{paths.texts}: cannot be written: {exc}", 1) from None
    for notice in notices:
        typer.echo(notice)
    typer.echo(texts_module.summary(papers))


@app.command("quotes")
def quotes(
    symbol: str = typer.Argument(..., help="Gene symbol, as named in the run's directory."),
    run: str = typer.Option(..., "--run", help="The run id."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
) -> None:
    """Check every quote a run holds against the text of its paper; write quotes.json.

    A quote not found is shown on the review page, not fatal: exit 0. Exit 2 when a run
    file is missing or not in the contract's shape.
    """
    _require_root(root)
    paths = RunPaths(root, symbol, run)
    try:
        checks = check_quotes(paths)
        write_quotes(paths, checks)
    except RunFormatError as exc:
        raise _fail(str(exc), 2) from None
    except OSError as exc:
        raise _fail(f"{paths.quotes}: cannot be written: {exc}", 1) from None
    done = [c for c in checks if c.checked]
    missing = [c for c in done if not c.found]
    unchecked = [c for c in checks if not c.checked]
    long = [c for c in checks if c.words > LONG_QUOTE_WORDS]
    typer.echo(
        f"{len(done)} quotes checked, {len(missing)} not found, "
        f"{len(long)} over {LONG_QUOTE_WORDS} words"
    )
    if unchecked:
        papers = ", ".join(sorted({c.paper for c in unchecked}))
        typer.echo(f"{len(unchecked)} quotes not checked: no text ({papers})")
    for c in missing:
        typer.echo(f"not found: {c.finding} {c.origin} {c.field}: {c.quote}")
    for c in long:
        typer.echo(f"over {LONG_QUOTE_WORDS} words ({c.words}): {c.finding} {c.origin} {c.field}")


@app.command("batch")
def batch(
    symbol: str = typer.Argument(..., help="Gene symbol, as named in the run's directory."),
    run: str = typer.Option(..., "--run", help="The run id."),
    mode: str = typer.Option(..., "--mode", help="new or backfill."),
    fields: str = typer.Option("", "--fields", help="Backfill: comma-separated Finding fields."),
    findings: list[str] = typer.Option(
        [],
        "--findings",
        help="Backfill: finding ids to re-read (default every finding whose declared fields "
        "are all empty).",
    ),
    budget_kb: int = typer.Option(170, "--budget-kb", min=1, help="Text budget per batch."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Plan a run's reading batches under a text budget; write batches.json and the manifests.

    SYMBOL may be an alias or a previous symbol: the run directory is named as typed and the
    literature file by the approved symbol. The budget is budget-kb x 1,000 bytes of main text per
    batch. A backfill's summary line says how many findings its default selection left out for
    holding a declared field. Exit 2, nothing written, when the run is already batched, has no
    texts.json, a selected paper has no text or year, or a field is unknown, forbidden or one
    that compare does not compare as a field of its own.
    """
    _require_root(root)
    if mode not in ("new", "backfill"):
        raise _fail(f"--mode {mode!r} is not new or backfill", 2)
    paths = RunPaths(root, symbol, run)
    names = tuple(n.strip() for n in fields.split(",") if n.strip())
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
        prepared = batch_module.prepare_batches(
            paths,
            "new" if mode == "new" else "backfill",
            names,
            tuple(findings),
            budget_kb * 1000,
            approved,
        )
    except (AtlasError, batch_module.BatchError, RunFormatError) as exc:
        raise _fail(str(exc), 2) from None
    try:
        batch_module.write_plan(paths, prepared)
    except OSError as exc:
        raise _fail(f"{paths.dir}: cannot be written: {exc}", 1) from None
    typer.echo(batch_module.summary(prepared.plan, prepared.skipped))


@app.command("merge")
def merge(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    run: str = typer.Option(..., "--run", help="The run id."),
    study_type: list[str] = typer.Option(
        [],
        "--study-type",
        help="PMID:<n>=<study type> for a paper whose reader left the study type null.",
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Merge a run's extraction batches into the gene's literature file.

    New findings get final ids from the LIT counter; a paper newly cited gets a Publication; an
    excluded paper's triage entry is set to excluded. A backfill run fills the declared fields of
    existing findings that lack them, and never rewrites a filled field. Exit 2, nothing
    written, for every refusal found before the first write: an inconsistent batch, an already
    merged run, a curated file that is missing, malformed or not UTF-8. Exit 1 when PubMed fails
    or an OS error occurs while reading or writing a file. A curated file that changes between
    the merge reading and writing it is refused at the write (exit 2), naming the files already
    written, which stay.
    """
    _require_root(root)
    given: dict[str, str] = {}
    for item in study_type:
        pmid, _, value = item.partition("=")
        if not re.fullmatch(r"PMID:[0-9]+", pmid) or not value or pmid in given:
            raise _fail(f"--study-type {item!r} is not PMID:<n>=<type>, or repeats a PMID", 2)
        given[pmid] = value
    key = _api_key()
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
        paths = RunPaths(root, symbol, run)
        plan = load_batches(paths)
        record = merge_module.merge_run(root, paths, plan, given, _fetch, key, approved=approved)
        line = merge_module.summary(root, plan, record, paths)
    except (
        AtlasError,
        merge_module.MergeError,
        RunFormatError,
        RecordError,
        DiffError,
        WriteRefused,
    ) as exc:
        raise _fail(_redact(str(exc), key), 2) from None
    except (PubMedError, urllib.error.URLError, OSError) as exc:
        raise _fail(_redact(str(exc), key), 1) from None
    typer.echo(line)


@app.command("compare")
def compare(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    run: str = typer.Option(..., "--run", help="The run id."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Compare a merged run's blind pass with its first pass and write the judges' manifests.

    Writes comparison.json and judging/judge-NN.md for each batch with something to judge. A
    re-run replaces the manifests, so a corrected verify file can be compared again. Exit 2,
    nothing written, when the judges have already written decisions or findings, the run has no
    merge.json, or a batch's blind pass is missing, misplaced or inconsistent. Exit 1 on an OS
    error.
    """
    _require_root(root)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
        paths = RunPaths(root, symbol, run)
        prepared = compare_module.prepare(paths, approved)
    except (AtlasError, compare_module.CompareError, RunFormatError, DiffError) as exc:
        raise _fail(str(exc), 2) from None
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    try:
        compare_module.write_outputs(paths, prepared)
    except OSError as exc:
        raise _fail(f"{paths.dir}: cannot be written: {exc}", 1) from None
    typer.echo(compare_module.summary(prepared.comparison))


@app.command("assemble")
def assemble(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    run: str = typer.Option(..., "--run", help="The run id."),
    verifier: str | None = typer.Option(
        None,
        "--verifier",
        help="The blind reader's name for verified_by; a backfill needs it, other runs read it.",
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Apply a run's judge findings, the notation rules and the blind agreement to the gene's file.

    A judge's finding replaces the finding of its id, or is added under the next LIT id; the
    notation rules then fix every finding of the run; and each finding the readers agreed on
    gains `verified_by`. Writes assemble.prev.json (a copy of the earlier assemble.json),
    assemble.json, the id registry (when findings were added), the literature file and
    assemble.json again, in that order. A re-run replaces the findings it already numbered, skips
    each judge finding whose digest assemble.json records unchanged, and changes no byte when
    nothing was edited. Exit 2, nothing written, for every refusal found before the first write
    (a write refused after earlier ones exits 2 naming the files already written): no merge.json or
    comparison.json, a judge finding no decision covers, a changed judge finding for a finding
    approved since the assemble that applied it (or a first one for an approved finding in a new
    run), a finding edited after compare that no longer
    agrees with its blind finding, a field changed that no decision names, a judged paper with no
    Publication, an assemble.json naming ids the file lacks, or a file changed since it was read.
    Exit 1 on an OS error.
    """
    _require_root(root)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
        paths = RunPaths(root, symbol, run)
        built = assemble_module.assemble_run(root, paths, approved, verifier)
        assemble_module.commit(paths, built)
    except (
        AtlasError,
        assemble_module.AssembleError,
        merge_module.MergeError,
        RunFormatError,
        RecordError,
        DiffError,
        WriteRefused,
    ) as exc:
        raise _fail(str(exc), 2) from None
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(assemble_module.summary(built.record))


@app.command("check-batch")
def check_batch(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    run: str = typer.Option(..., "--run", help="The run id."),
    batch: str = typer.Option(..., "--batch", help="The batch id, as in batches.json (NN)."),
    pass_: str = typer.Option(..., "--pass", help="extract, verify or judge."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Check one batch's pass against the atlas's validator, on a scratch copy of the root.

    The pass is applied to a temporary copy of `curation/` (every other top-level entry but
    `local`, `.git` and `.venv` is linked), `chd-atlas validate` runs on the copy, and the copy
    is removed. Nothing under `--root` is written. Prints the validator's summary line, then
    `OK: only the errors expected before merge and normalise` (exit 0), or `UNEXPECTED ERRORS:`
    and each error line (exit 1). The expected errors are LIT003 for a paper outside the batch,
    LIT011, LIT022 for a mane variant lacking hgvs_c, grch38 or normalisation, and LIT025 for a
    p. with no hgvs_p. An extract or verify pass is applied with the exclusions of its own
    papers.json, so a paper of the batch that is neither excluded nor cited is reported. A
    validator that crashes, exits other than 0 or 1, or prints a summary that does not match its
    error lines is never OK: its raw output is printed and the exit code is 1. Exit 2 for a batch
    file that does not load.
    """
    _require_root(root)
    cli = _atlas_cli(root, atlas_cli)
    try:
        approved = target(root, symbol, cli).symbol
        checked = check_module.check_batch(root, symbol, approved, run, batch, pass_, cli)
    except (
        AtlasError,
        check_module.CheckError,
        merge_module.MergeError,
        assemble_module.AssembleError,
        RunFormatError,
        RecordError,
        DiffError,
    ) as exc:
        raise _fail(str(exc), 2) from None
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    typer.echo("\n".join(checked.lines))
    raise typer.Exit(code=checked.code)


def _atlas_commit(root: Path) -> str | None:
    """The checkout's short HEAD commit, or None when it is not a git checkout."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


@app.command("review-page")
def review_page_command(
    symbol: str = typer.Argument(..., help="Gene symbol, as named in the run's directory."),
    run: str = typer.Option(..., "--run", help="The run id."),
    reviewer: str = typer.Option(..., "--reviewer", help="The reviewer's name, shown on the page."),
    role: str = typer.Option("owner", "--role", help="owner or second_opinion."),
    archive: Path | None = typer.Option(
        None,
        "--archive",
        help="The review archive checkout: the owner page shows the earlier verdicts filed there.",
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Write the run's review page: one HTML file the owner or a second reviewer opens.

    The owner page is `<run>/review.html`; a second opinion's is `<run>/review-<name>.html`
    and shows no approval state and no other reviewer's verdict. With `--archive <dir>` the
    owner page also shows what the review archive holds for the gene: the owner's own earlier
    notes, each clinician's latest verdict on each card, the triage card's earlier verdicts and
    open flips (the owner's own open flips are ticked again on their rows; Approve or Needs a
    fix on the card closes every flip listed whose paper is not flipped on that card, Ask later
    or no verdict closes none), and a fold of verdicts on findings with no card on this page.
    Opened as a claude.ai artifact the page saves to its store; opened as a file it saves in the
    browser and offers "Download my review" (a decisions file for `chd-atlas literature
    apply-review`).
    Refuses (exit 1) to build when `chd-atlas validate` reports an error, except an approval
    changed since it was given (LIT015) on a finding the page shows as a card, which reads
    "approval void: re-approve"; a LIT015 on a finding the page does not show is refused,
    naming it. Exit 2 for a bad argument, an unreadable run, `--archive` with `--role
    second_opinion`, or an archive the reader refuses (not a marked checkout, uncommitted
    changes, an invalid decisions file, or the symbol filed as another gene than the atlas's);
    nothing is written then, and a failing git command exits 1. Validate is asked first, so a
    gene the atlas cannot digest still reports its validate errors.
    """
    _require_root(root)
    if role not in ("owner", "second_opinion"):
        raise _fail(f"--role {role!r} is not owner or second_opinion", 2)
    if role == "second_opinion" and archive is not None:
        raise _fail("a second-opinion page reads no archive", 2)
    name = reviewer.strip()
    name_slug = review_page.slug(name)
    if not name_slug:
        raise _fail("--reviewer must contain a letter or digit", 2)
    cli = _atlas_cli(root, atlas_cli)
    paths = RunPaths(root, symbol, run)
    try:
        code, stdout, stderr = run_validate(root, cli)
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    answer = check_module.read_validate(code, stdout, stderr)
    if answer.summary is None:
        raise _fail(f"chd-atlas validate printed nothing (exit {code})", 2)
    line = answer.summary
    if answer.problem is not None:
        raise _fail(
            f"chd-atlas validate's answer cannot be trusted ({answer.problem}); fix it before "
            "building a page",
            1,
        )
    # An approval changed since the owner gave it (LIT015) is an error the page exists to show:
    # a card reads "approval void: re-approve". It is the only error a page is built over, and
    # only for a finding the page shows (checked once the cards are known).
    voided = {fid for fid in map(check_module.void_approval, answer.errors) if fid is not None}
    if any(check_module.void_approval(e) is None for e in answer.errors):
        raise _fail(
            f"chd-atlas validate reports errors ({line}); fix them before building a page", 1
        )
    try:
        found = digests(root, symbol, cli)
        view = archive_read.read_archive(archive, symbol, root) if archive is not None else None
    except (AtlasError, archive_mod.ArchiveError) as exc:
        raise _fail(str(exc), 2) from None
    except archive_mod.GitError as exc:
        raise _fail(str(exc), 1) from None
    who = Reviewer(name, "owner" if role == "owner" else "second_opinion")
    try:
        data = build_review_data(
            root,
            paths,
            found,
            who,
            datetime.now(UTC),
            _atlas_commit(root),
            line,
            archive=view,
        )
    except (ReviewDataError, RunFormatError, ValueError, archive_mod.ArchiveError) as exc:
        raise _fail(str(exc), 2) from None
    unshown = sorted(voided - {str(c["id"]) for c in data["findings"]})
    if unshown:
        raise _fail(
            f"chd-atlas validate reports errors ({line}), among them an approval changed since "
            f"it was given on {', '.join(unshown)}, which this page does not show; fix them "
            "before building a page",
            1,
        )
    out = paths.dir / ("review.html" if role == "owner" else f"review-{name_slug}.html")
    try:
        write_bytes_atomically(out, review_page.render(data).encode("utf-8"))
    except OSError as exc:
        raise _fail(f"{out}: cannot be written: {exc}", 1) from None
    typer.echo(str(out))


@app.command("review-decisions")
def review_decisions_command(
    symbol: str = typer.Argument(..., help="Gene symbol, as named in the run's directory."),
    run: str = typer.Option(..., "--run", help="The run id."),
    export: Path = typer.Option(
        ..., "--export", help="The page store's export directory (ArtifactData out_dir)."
    ),
    out: Path = typer.Option(..., "--out", help="Where to write the decisions file."),
    page: Path | None = typer.Option(None, "--page", help="The page (default: <run>/review.html)."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
) -> None:
    """Turn a review page's saved decisions into a decisions file for `apply-review`.

    Reads the export of the page's store (`review/<id>.json`, `session/main.json`) and the
    page's own inline data, and writes the format 1 file `chd-atlas literature apply-review`
    applies. Exit 1 when the reviewer has not pressed "I'm done", or changed a choice after it
    (press it again); exit 2 for an export that is not this page's (another page, reviewer or
    gene, a flip or a seen paper the page does not hold), a document for a card not on the page,
    a decision outside approve|fix|later, or a result failing the schema. Nothing is written on
    exit 1 or 2. A card decided on an earlier version is left out of the file, as the page's own
    download leaves it out, and named on stdout (exit 0). A triage document not stamped with this
    page's build time (made on another page, or by a page that stamped nothing) is read for
    nothing but the papers this page asks to be read; a verdict in it is left out and one line
    says so (exit 0). The last-but-one line counts what was written.
    """
    _require_root(root)
    source = page if page is not None else RunPaths(root, symbol, run).review_html
    try:
        converted = decisions_from_export(export, source, root, symbol)
    except (ExportError, AtlasError) as exc:
        raise _fail(str(exc), exc.exit_code if isinstance(exc, ExportError) else 2) from None
    decisions = converted.decisions
    payload = json.dumps(decisions, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomically(out, payload.encode("utf-8"))
    except OSError as exc:
        raise _fail(f"{out}: cannot be written: {exc}", 1) from None
    if converted.stale:
        typer.echo(f"left out, decided on an earlier version: {', '.join(converted.stale)}")
    if converted.triage_left_out:
        typer.echo("left out a triage verdict that was not made on this page")
    made = [f["decision"] for f in decisions["findings"]]
    triage = decisions["triage"]["decision"] if decisions["triage"] else "none"
    typer.echo(
        f"{len(made)} decisions: {made.count('approve')} approve, {made.count('fix')} fix, "
        f"{made.count('later')} later; triage {triage}; {len(decisions['papers_seen'])} papers "
        f"seen; {converted.cards} cards on the page"
    )
    typer.echo(str(out))


@app.command("archive-page")
def archive_page_command(
    symbol: str = typer.Argument(..., help="Gene symbol the page is for."),
    run: str = typer.Option(..., "--run", help="The run id."),
    page: Path = typer.Option(..., "--page", help="The review page, as it was sent."),
    archive: Path = typer.Option(..., "--archive", help="The review archive checkout."),
    artifact: str | None = typer.Option(
        None, "--artifact", help="The page's claude.ai artifact URL, filed beside it."
    ),
) -> None:
    """File a review page in the review archive, byte for byte, and commit it.

    Append-only against what is committed: a different file at the same place is refused (even
    if the working copy was deleted or edited since), an identical one prints `already filed`
    (restored from the commit if the working copy differs, and committed if it was never
    committed). Prints one line per path, then `committed <sha>` when it committed. Exit 2 for
    a refusal (an archive that is not the top of a checkout with its marker committed, no
    `user.name`/`user.email` in the archive's own config, a page for another symbol, a bad id,
    reviewer name or `--artifact`, a second opinion whose name reads as `owner`, a different file
    already filed, a path git ignores); exit 1
    when git fails, a file cannot be written, or a commit leaves a path out of HEAD. Never
    pushes.
    """
    try:
        filed, sha = archive_mod.file_page(archive, symbol, run, page, artifact)
    except archive_mod.ArchiveError as exc:
        raise _fail(str(exc), 2) from None
    except archive_mod.GitError as exc:
        raise _fail(str(exc), 1) from None
    for item in filed:
        typer.echo(f"{item.state} {item.path}")
    if sha is not None:
        typer.echo(f"committed {sha}")


@app.command("archive-decisions")
def archive_decisions_command(
    symbol: str = typer.Argument(..., help="Gene symbol the decisions are for."),
    run: str = typer.Option(..., "--run", help="The run id."),
    decisions: Path = typer.Option(..., "--decisions", help="The finished decisions file."),
    archive: Path = typer.Option(..., "--archive", help="The review archive checkout."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
) -> None:
    """File a finished decisions file beside the page it was made on, and commit it.

    The file is filed byte for byte as
    `<SYMBOL>/<run>/decisions/<finished_at in UTC>--<reviewer slug>.json`.
    Append-only against what is committed: a different file at that name is refused (the
    message names it), an identical one prints `already filed` (restored from the commit if the
    working copy differs, and committed if it was never committed). Prints `filed <path>` or
    `already filed <path>`, then `committed <sha>` when it committed; for an owner's file the
    last line is the `chd-atlas literature apply-review` command to run next. Exit 2 for a
    refusal (an unusable archive, no `user.name`/`user.email` in the archive's own config,
    a bad symbol or run id, invalid JSON or a schema error, an unfinished session, another
    symbol, a second opinion keyed as the owner, a finding decided twice, a reviewer name with
    no ASCII letter or digit, another gene than the page's or than one already filed for the
    symbol, a session that finished before its page was built, no page filed for the file, a
    different file already filed, a path git ignores);
    exit 1 when git fails, a file cannot be written, or a commit leaves a path out of HEAD.
    Never pushes.
    """
    _require_root(root)
    try:
        filed, sha = archive_mod.file_decisions(archive, symbol, run, decisions, root)
        top = archive_mod.check_archive(archive)
        doc = json.loads((top / filed.path).read_bytes())
    except archive_mod.ArchiveError as exc:
        raise _fail(str(exc), 2) from None
    except (archive_mod.GitError, OSError) as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(f"{filed.state} {filed.path}")
    if sha is not None:
        typer.echo(f"committed {sha}")
    reviewer = doc["reviewer"]
    if reviewer["role"] == "owner":
        typer.echo(
            f"next: chd-atlas literature apply-review {symbol} "
            f"--decisions {shlex.quote(str(top / filed.path))} "
            f"--by {shlex.quote(reviewer['name'])} --root {shlex.quote(str(root.resolve()))}"
        )


@app.command("archive-state")
def archive_state_command(
    symbol: str = typer.Argument(..., help="Gene symbol to read."),
    archive: Path = typer.Option(..., "--archive", help="The review archive checkout."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Print the full state as JSON."),
) -> None:
    """Read what the archive holds for a gene, against the atlas's current digests.

    Prints one line, `<SYMBOL>: <n> findings with verdicts in <k decisions files from m
    reviewers>; <p> pending fix or ask later, <f> pending flips (archive <commit>)`: `<p>`
    counts every reviewer whose latest verdict on the finding is a fix or later made on its
    current digest (a finding with two such reviewers counts 2) and `<f>` the triage flips
    that are pending. A flip is a verdict on one paper: a reviewer's latest flip of it counts,
    whatever their later triage verdicts say. It is pending while the paper's triage decision is
    not the flip's `to` (a paper absent from the triage is not pending), unless the owner closed
    it: an owner triage verdict that approved or fixed (never "ask later") on a filed page that
    showed that flip, in a file that finished after the flip did, without flipping the paper
    itself. With `--json` it prints the whole
    state instead. Exit 2 for a refusal (an unusable archive, one with uncommitted changes, a
    decisions file HEAD does not hold, an owner's approve or fix of the triage whose filed page
    cannot be found or says nothing usable about the flips it showed, a file that fails the
    schema, is not finished, finished before its page was built, is for another symbol, names a
    finding twice, is a second opinion keyed as the owner, or is misnamed, two genes in the
    archive for the symbol, anything in a decisions directory other than a .json file directly
    in it, an unreadable literature file, or a gene other than the atlas's); exit 1 when git
    fails. Writes nothing.
    """
    _require_root(root)
    cli = _atlas_cli(root, atlas_cli)
    try:
        view = archive_read.read_archive(archive, symbol, root)
        found = digests(root, symbol, cli)
    except archive_mod.ArchiveError as exc:
        raise _fail(str(exc), 2) from None
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    except archive_mod.GitError as exc:
        raise _fail(str(exc), 1) from None
    if view.gene is not None and view.gene != found.gene:
        raise _fail(
            f"the archive holds {symbol} as {view.gene}, but the atlas has it as {found.gene}", 2
        )
    lit_path = root / "curation/literature" / f"{symbol}.yaml"
    try:
        lit = YAML(typ="safe").load(lit_path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise _fail(f"{lit_path}: cannot be read as YAML: {exc}", 2) from None
    entries = lit.get("triage") if isinstance(lit, dict) else None
    if not isinstance(entries, list):
        raise _fail(f"{lit_path}: has no triage list", 2)
    triage = {str(e["pmid"]): str(e["decision"]) for e in entries if isinstance(e, dict)}
    current = {f.id: f.shown_digest for f in found.findings}
    out = archive_read.state(view, current, triage)
    if as_json:
        typer.echo(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False))
        return
    pending = sum(v["pending"] for f in out["findings"] for v in f["verdicts"])
    flips = sum(fl["pending"] for fl in out["triage"]["flips"])
    n = len(out["findings"])
    typer.echo(
        f"{symbol}: {n} finding{'' if n == 1 else 's'} with verdicts in "
        f"{archive_read.files_line(view)}; {pending} pending fix or ask later, "
        f"{flips} pending flip{'' if flips == 1 else 's'} (archive {view.commit})"
    )


@app.command("archive-redact")
def archive_redact_command(
    name: str = typer.Option(
        ..., "--name", help="The reviewer to remove, as the archive names them."
    ),
    archive: Path = typer.Option(..., "--archive", help="The review archive checkout."),
    apply: bool = typer.Option(False, "--apply", help="Do it; without this, only print the plan."),
) -> None:
    """Remove a reviewer from the archive on their request (a dry run unless `--apply`).

    The dry run prints five lists and changes nothing: the files to delete (the reviewer's
    decisions files and their own pages with the pages' `.artifact` files), the owner pages to
    rewrite (the reviewer's name and notes redacted in the page's data, every other byte kept),
    the artifact URLs to republish and to unpublish, and the files that still name the reviewer
    after that (an owner's note, say), which stop `--apply` until edited. Both the dry run and
    `--apply` refuse (exit 2, nothing changed) an archive in which a path carries a
    skip-worktree or assume-unchanged bit, and a file the scan cannot read. `--apply` also
    refuses an archive with uncommitted changes, a name still found elsewhere (in a file's text,
    or as the name's file-name slug in a path), a removal that touches nothing, a missing README
    or one without exactly one `## Removals` line, or `.git/redact-*.txt` left by an earlier
    removal; it deletes, rewrites, adds a dated line under `## Removals`, scans again and commits
    `redact a removed reviewer` (a match in the scan, a failed write or a failed commit restores
    every touched path and exits 1), and prints the history-rewrite procedure, the copies outside
    the archive to delete by hand, and the pages to republish or unpublish. That procedure has
    not been run: it is unmeasured as of 2026-11-25. Never pushes.
    """
    try:
        plan = redact_mod.plan_redact(archive, name)
        if not apply:
            for title, items in (
                ("delete", plan.delete),
                ("rewrite", plan.rewrite),
                ("republish", plan.republish),
                ("unpublish", plan.unpublish),
                ("elsewhere", plan.elsewhere),
            ):
                typer.echo(f"{title} ({len(items)}):")
                for item in items:
                    typer.echo(f"  {item}")
            typer.echo("dry run: nothing changed; add --apply to carry it out")
            return
        sha = redact_mod.apply_redact(archive, name, plan, datetime.now(UTC).date())
    except archive_mod.ArchiveError as exc:
        raise _fail(str(exc), 2) from None
    except archive_mod.GitError as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(f"committed {sha}")
    for line in redact_mod.procedure(archive, name, plan):
        typer.echo(line)


_AGREEMENT_SYMBOLS = typer.Argument(
    None, help="Gene symbols to read; none means every gene directory in the archive."
)


@app.command("agreement")
def agreement_command(
    symbols: list[str] | None = _AGREEMENT_SYMBOLS,
    archive: Path = typer.Option(..., "--archive", help="The review archive checkout."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root (for the decisions schema)."),
    as_json: bool = typer.Option(False, "--json", help="Print the full report as JSON."),
) -> None:
    """Report how often each clinician agrees with the owner, per version of a card.

    A clinician's latest verdict on a version of a card (its shown digest) is paired with the
    owner's latest on the same version. Each pair is one of: agreed (both approve or both fix),
    disagreed (one approves, one fixes; the notes are quoted) or with ask later (either side
    said later). A clinician verdict on a version no owner decisions file covers is unpaired.
    With no symbol it reads every top-level directory of the archive that is not a
    dot-directory (one that is not a gene symbol is refused). `--json` prints the whole
    report instead. Exit 2 for a refusal (an unusable archive, one with uncommitted changes,
    anything in a decisions directory other than a .json file directly in it, a decisions file
    HEAD does not hold, an owner's approve or fix of the triage whose filed page cannot be found
    or read, a file that is not valid JSON, fails the schema, is unfinished, finished before its
    page was built, is for another symbol, names a finding twice, is a second opinion keyed as
    the owner, or is misnamed, or two genes under one symbol); exit 1 when git fails. Unlike
    `archive-state` it does not compare the archive's gene with the atlas's. Writes nothing, and
    the report is printed nowhere else.
    """
    _require_root(root)
    try:
        top, commit = archive_read.open_archive(archive)
        names = list(symbols or [])
        if not names:
            names = sorted(
                p.name for p in top.iterdir() if p.is_dir() and not p.name.startswith(".")
            )
            for n in names:
                try:
                    archive_mod.check_id("symbol", n)
                except archive_mod.ArchiveError:
                    raise archive_mod.ArchiveError(
                        f"top-level directory {n!r} is not a gene symbol"
                    ) from None
        views = [archive_read.read_archive(archive, s, root) for s in names]
    except archive_mod.ArchiveError as exc:
        raise _fail(str(exc), 2) from None
    except archive_mod.GitError as exc:
        raise _fail(str(exc), 1) from None
    tallies = agreement_mod.tally(views)
    if as_json:
        out = agreement_mod.as_json(tallies, commit)
        typer.echo(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False))
        return
    for line in agreement_mod.report(tallies, names):
        typer.echo(line)


_SHADOWABLE = (ask_module.SHADOWABLE,)


@app.command("check-site")
def check_site(
    site: Path = typer.Option(..., "--site", help="The site config YAML."),
    shadow_role: list[str] = typer.Option(
        [],
        "--shadow-role",
        help="triage-screen, when it runs in shadow, so its model needs no qualification.",
    ),
) -> None:
    """Check that every role maps to a backend the register allows.

    Exit 0 when each of the five roles is configured and allowed (or shadowed); exit 2, one line
    per failing role, when not. A site config that does not load exits 2 with the reason. Reads
    only: run it before `ask` or a pipeline to see which role a site would have refused.
    """
    for role in shadow_role:
        if role not in _SHADOWABLE:
            raise _fail(
                f"--shadow-role {role!r}: only {', '.join(_SHADOWABLE)} may run in shadow", 2
            )
    try:
        loaded = load_site(site)
        entries = register_mod.load_register()
    except (SiteError, register_mod.RegisterError) as exc:
        raise _fail(str(exc), 2) from None
    problems: list[str] = []
    for role in ROLES:
        backend = loaded.roles.get(role)
        if backend is None:
            if role not in shadow_role:
                problems.append(f"{role}: not configured in the site config")
        elif role not in shadow_role and register_mod.allowed(role, backend, entries) is None:
            problems.append(f"{role}: {model_key(backend)} is not qualified for this role")
    if problems:
        for line in problems:
            typer.echo(f"error: {line}")
        raise typer.Exit(code=2)
    typer.echo(f"site ok: {len(loaded.roles)} role(s) configured, {len(shadow_role)} in shadow")


@app.command("read-requests")
def read_requests_command(
    symbol: str = typer.Argument(..., help="Gene symbol, as named in the run's directory."),
    run: str = typer.Option(..., "--run", help="The run id."),
    role: str = typer.Option(..., "--role", help="extract, blind-read or judge."),
    out: Path = typer.Option(..., "--out", help="The requests JSONL to write."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    decisions: Path | None = typer.Option(
        None, "--decisions", help="The judge only: the owner's decisions file of a fix round."
    ),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Write a run's batch (or judge) manifests, with the texts they name, as `ask` requests.

    One row `{key, gene, batch, mode, inputs: {manifest, texts, registries}}` per batch of
    batches.json (extract and blind-read, identical rows) or per judging/judge-NN.md (judge, with
    `owner_notes` too: the `fix` notes of `--decisions` for the findings that manifest names, or
    `none`); each text and registry is under a `--- file: <path> ---` marker. Exit 1, nothing
    written, for a named file that is missing, outside the root, not a .txt or not UTF-8, a
    missing registry, or a `fix` decision on a finding no judge manifest names; exit 2 for a bad
    argument (an unusable `--decisions`, or one given for a reader), a symbol the atlas refuses
    or a run with no batches.json. Writes no approval.
    """
    _require_root(root)
    if role not in ("extract", "blind-read", "judge"):
        raise _fail(f"--role {role!r} is not extract, blind-read or judge", 2)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
        notes = requests_module.fix_notes(decisions, approved) if decisions is not None else None
        count = requests_module.read_requests(
            root, symbol, run, cast(requests_module.Role, role), out, notes
        )
    except (AtlasError, RunFormatError, requests_module.RequestsUsageError) as exc:
        raise _fail(str(exc), 2) from None
    except (requests_module.RequestsError, OSError) as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(f"wrote {count} request(s) for {role} to {out}")


@app.command("triage-requests")
def triage_requests_command(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    out: Path = typer.Option(..., "--out", help="The requests JSONL to write."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Write one `ask` request per pending triage entry: `{key: PMID:n, gene, inputs: {gene, hit}}`.

    `hit` is the block `abstracts` prints for that PMID, fetched from PubMed (network). Exit 1
    for PubMed failing or a missing literature file, 2 for a bad argument or a symbol the atlas
    refuses. Writes no approval.
    """
    _require_root(root)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    key = _api_key()
    try:
        count = triage_module.triage_requests(root, approved, out, fetch=_fetch, api_key=key)
    except (triage_module.TriageError, OSError) as exc:
        raise _fail(_redact(str(exc), key), 1) from None
    typer.echo(f"wrote {count} triage request(s) for {approved} to {out}")


@app.command("triage-combine")
def triage_combine_command(
    requests: Path = typer.Option(..., "--requests", help="The screen's requests JSONL."),
    screen: Path = typer.Option(..., "--screen", help="The screen's answers JSONL."),
    check: list[Path] = typer.Option([], "--check", help="A check answers JSONL; repeatable."),
    run: str = typer.Option(..., "--run", help="The run id (seeds the audit)."),
    gene: str = typer.Option(..., "--gene", help="The approved symbol the requests are for."),
    out: Path = typer.Option(..., "--out", help="The decisions JSON to write."),
    out_requests: Path | None = typer.Option(
        None, "--out-requests", help="Write the check requests still owed here."
    ),
    shadow: bool = typer.Option(
        False, "--shadow", help="The check decides every hit; the hybrid is recorded beside it."
    ),
) -> None:
    """Combine the screen's and the check's answers into one decision per hit.

    A hit goes to the check when the screen excluded it in a risky class (not_chd,
    no_finding_in_gene), when it is in the audit sample (10% of each other class, at least 5,
    seeded by `--run`), when the screen's answer is missing, an error or unparseable, or when it
    excluded a thin hit (PubMed has no record or no abstract for it). An audit
    miss escalates its class. Hits the check still owes are listed under `awaiting_check` and
    written to `--out-requests`. The hybrid reads only the check answers for hits it sent; with
    `--shadow` the check decides every hit it answered. `--gene` is written into the file even
    when there is no pending hit. Exit 2 for a key not in the requests or repeated in one answers
    file, or a request naming another gene. Writes no approval.
    """
    try:
        document, owed = triage_module.combine_run(
            requests, screen, check, run, gene, shadow=shadow
        )
        triage_module.write_json(out, document)
        if out_requests is not None:
            triage_module.write_requests(out_requests, owed)
    except triage_module.TriageUsageError as exc:
        raise _fail(str(exc), 2) from None
    except (triage_module.TriageError, OSError) as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(
        f"{len(document['decisions'])} decision(s), {len(owed)} awaiting the check, "
        f"{len(document['escalated'])} class(es) escalated; wrote {out}"
    )


@app.command("triage-apply")
def triage_apply_command(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    decisions: Path = typer.Option(..., "--decisions", help="The `triage-combine` output."),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Write the decisions into the gene's pending triage entries.

    Changes `decision`, and for an exclusion `reason` (and `note`, the answer's `why`, for
    `other`); never `pmid` or `search`; refuses a decisions file with any hit awaiting the check
    (exit 1), a screen-tier exclusion in a risky class or a class the file escalated (exit 1), a
    key that is not a pending entry (exit 1) and a file changed since it was read (exit 2). A
    file with no decisions applies nothing (exit 0). Writes no approval.
    """
    _require_root(root)
    try:
        approved = target(root, symbol, _atlas_cli(root, atlas_cli)).symbol
        loaded, escalated = triage_module.load_decisions_file(decisions, approved)
        count = triage_module.apply_decisions(root, approved, loaded, escalated=escalated)
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    except triage_module.TriageUsageError as exc:
        raise _fail(str(exc), 2) from None
    except (triage_module.TriageError, OSError) as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(f"applied {count} decision(s) to {literature_path(root, approved)}")


@app.command("ask")
def ask_command(
    role: str = typer.Option(..., "--role", help=f"One of {', '.join(ROLES)}."),
    requests: Path = typer.Option(..., "--requests", help="JSONL of {key, inputs} rows."),
    answers: Path = typer.Option(..., "--answers", help="The JSONL file to write."),
    site: Path = typer.Option(..., "--site", help="The site config YAML."),
    root: Path = typer.Option(..., "--root", help="Atlas checkout root."),
    cache: Path | None = typer.Option(None, "--cache", help="A directory of validated answers."),
    validate: str = typer.Option("schema", "--validate", help="schema or check-batch."),
    run: str | None = typer.Option(None, "--run", help="The run id (needed by check-batch)."),
    shadow: bool = typer.Option(
        False, "--shadow", help="triage-screen only: run a model the register does not allow."
    ),
    allow_errors: bool = typer.Option(
        False, "--allow-errors", help="Exit 0 although some request has an error row."
    ),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Ask a role's model once per request, validate, retry at most twice, cache by input.

    Writes one answer row per request, in the requests' order, to `--answers`: `{key, parsed,
    attempts, usage, seconds, model_key, input_digest, rules_digest, meta}` or `{key, error,
    attempts, usage, seconds}`. Prints one summary line: requests the model answered, requests
    answered from the cache, requests with an error row (their keys), and the tokens (as
    `report` counts them). Exit 0 when every request has an answer (or with `--allow-errors`); 1
    when a request has an error row; 2 for a bad argument, request, site, run, a model the
    register does not qualify for the role (unless `--shadow`, which only triage-screen takes)
    or a configuration that would fail every request (an unset `key_env` variable; for
    claude_code a cluster job variable or a failing `claude --version`; a missing replay
    `dir`), before any call; and 2 when an HTTP 401 or 403 stopped the run, after writing an
    error row for every request not asked. Writes no approval: it writes answers, and with
    `--validate check-batch` the batch's contract files under the run directory.
    """
    _require_root(root)
    if validate not in ("schema", "check-batch"):
        raise _fail(f"--validate {validate!r} is not schema or check-batch", 2)
    try:
        loaded = load_site(site)
        entries = register_mod.load_register()
        summary = ask_module.ask(
            role,
            requests,
            answers,
            loaded,
            entries,
            root,
            cache,
            "check-batch" if validate == "check-batch" else "schema",
            run,
            shadow,
            atlas_cli=atlas_cli,
        )
    except (SiteError, register_mod.RegisterError, ask_module.AskUsageError) as exc:
        raise _fail(str(exc), 2) from None
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    tokens = summary.tokens
    failed = f"{len(summary.failed)} ({', '.join(summary.failed)})" if summary.failed else "0"
    extra = (", shadow: true" if shadow else "") + (f", run: {run}" if run else "")
    typer.echo(
        f"answered {summary.asked}, from cache {summary.cached}, failed {failed}, "
        f"tokens {tokens}{extra}"
    )
    if summary.failed and not allow_errors:
        raise typer.Exit(code=1)


@app.command("report")
def report_command(
    run_dir: Path = typer.Option(
        ..., "--run-dir", help="The run folder: <root>/local/literature/runs/<SYMBOL>/<run id>."
    ),
    answers: list[str] = typer.Option(
        [], "--answers", help="<role>=<answers file>; repeat for each role and file."
    ),
    trace: Path | None = typer.Option(None, "--trace", help="A Nextflow trace file."),
    local_usage: list[Path] = typer.Option(
        [], "--local-usage", help="A sidecar serve-and-ask.sh wrote beside its answers; repeatable."
    ),
    out_dir: Path = typer.Option(..., "--out-dir", help="Where report.json and report.md go."),
) -> None:
    """Report a run's tiers, cost and how each finding was settled.

    Tokens and seconds come from the answers files (`ask`'s rows), wall time per process from
    the trace (never core-hours: a task's cpus x realtime is not a model's work), the local
    tier's core-hours only from `--local-usage` sidecars ("not measured" without one), and the
    findings' counts from the run folder: `agreed` and `judged` from
    assemble.json, `corrected by the owner` from decisions/*.json, `normalised` from
    normalise.json. Writes report.json and report.md, and prints the summary line. A role whose
    answers carry rules that differ from its register entry's gets a warning. Exit 2 for an
    unreadable or malformed input, an unknown role, a trace lacking process, cpus or realtime,
    a sidecar that is not `{cpus, seconds, job_id}`, or a folder that is not a run. Writes no
    approval.
    """
    pairs: list[tuple[str, Path]] = []
    for item in answers:
        role, sep, path = item.partition("=")
        if not sep or not role or not path:
            raise _fail(f"--answers {item!r} is not <role>=<path>", 2)
        pairs.append((role, Path(path)))
    try:
        json_path, _ = report_module.write_report(
            run_dir, pairs, trace, out_dir, local_usage=local_usage
        )
    except (report_module.ReportError, register_mod.RegisterError) as exc:
        raise _fail(str(exc), 2) from None
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(json.loads(json_path.read_text(encoding="utf-8"))["summary"])


@app.command("scratch-root")
def scratch_root_command(
    atlas: Path = typer.Option(..., "--atlas", help="The atlas checkout (never written)."),
    dest: Path = typer.Option(..., "--dest", help="The scratch root to create (must not exist)."),
    gene: str = typer.Option(..., "--gene", help="The approved symbol, as the run directory."),
    state: list[Path] = typer.Option(
        [], "--state", help="A folder `export-state` wrote; repeat to lay several over as one."
    ),
) -> None:
    """Create a scratch root: links to the atlas, the gene's three mutable files copied in.

    Every top-level entry of the atlas is a link, except `curation` and `local/literature`,
    which are real directories with a link per file (nothing else of `local` enters the root);
    the three mutable files (the gene's literature file, `publications.yaml`,
    `.id_registry.yaml`) are copied from the states when given, else from the atlas; then the
    union of everything under each `<state>/local/` is copied over. Exit 1, no root made, naming
    each file: a mutable file the atlas has and a state lacks, a mutable file the states do not
    hold byte for byte alike, or a `local/` file two states hold with different bytes. Exit 2
    for an existing `--dest` or an unusable argument. Writes nothing in the atlas, and no
    approval.
    """
    try:
        scratch_module.scratch_root(atlas, dest, gene, state)
    except scratch_module.ScratchError as exc:
        raise _fail(str(exc), exc.code) from None
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(f"scratch root {dest}")


@app.command("export-state")
def export_state_command(
    root: Path = typer.Option(..., "--root", help="The scratch root."),
    gene: str = typer.Option(..., "--gene", help="The approved symbol, as the run directory."),
    run: str = typer.Option(..., "--run", help="The run id."),
    dest: Path = typer.Option(..., "--dest", help="The state folder to create (must not exist)."),
) -> None:
    """Copy a scratch root's three files, the run folder and the texts the run lists to `--dest`.

    The three files each when present, the run folder when it exists, the texts only when the
    run has a `texts.json`. Exit 1, nothing exported, naming a text `texts.json` lists that is
    missing, or a real file a step wrote under `curation/` or `local/` that none of these
    covers; exit 2 for an existing `--dest`. Writes no approval.
    """
    try:
        scratch_module.export_state(root, gene, run, dest)
    except scratch_module.ScratchError as exc:
        raise _fail(str(exc), exc.code) from None
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(f"state {dest}")


@app.command("base")
def base_command(
    atlas: Path = typer.Option(..., "--atlas", help="The atlas checkout."),
    gene: str = typer.Option(..., "--gene", help="The approved symbol."),
) -> None:
    """Print, as JSON, `digests` (the sha256 of the gene's three mutable files, null when
    absent, and a tree digest of each text folder `local/literature/texts/<name>`, so
    `integrate` can replace a text the run re-fetched) and the checkout's HEAD commit (null when
    it is not a git checkout). Writes nothing."""
    if not atlas.is_dir():
        raise _fail(f"--atlas {atlas} is not a directory", 2)
    try:
        digests_ = {
            **scratch_module.base_digests(atlas, gene),
            **scratch_module.text_digests(atlas),
        }
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    body = {"atlas_commit": scratch_module.atlas_head(atlas), "digests": digests_}
    typer.echo(json.dumps(body, indent=2, sort_keys=True))


@app.command("package")
def package_command(
    root: Path = typer.Option(..., "--root", help="The work root (a scratch root)."),
    gene: str = typer.Option(..., "--gene", help="The approved symbol, as the run directory."),
    run: str = typer.Option(..., "--run", help="The run id."),
    base: Path = typer.Option(..., "--base", help="The JSON `base` printed at the run's start."),
    out: Path = typer.Option(..., "--out", help="The package directory to create."),
    answers: list[str] = typer.Option(
        [], "--answers", help="<role>=<answers file> the run used; repeat for each."
    ),
) -> None:
    """Package a run's proposal: the three files, the run folder, its texts and `package.json`.

    `models` in `package.json` is one `{role, model_key, verdict, rules_digest}` per distinct
    model and rules of the `--answers` rows; `verdict` is the register's (`pass`, `fail`,
    `reference`), `unqualified` with no entry, or `test-backend`. Exit 2 for an existing `--out`,
    an unreadable `--base` or answers file, or an unknown role; exit 1 for a text `texts.json`
    names that is missing, or a missing run folder. Writes no approval.
    """
    pairs: list[tuple[str, Path]] = []
    for item in answers:
        role, sep, path = item.partition("=")
        if not sep or not role or not path:
            raise _fail(f"--answers {item!r} is not <role>=<path>", 2)
        pairs.append((role, Path(path)))
    try:
        loaded = json.loads(base.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _fail(f"cannot read --base: {exc}", 2) from None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("digests"), dict):
        raise _fail(f"{base}: not the JSON `chd-curator base` prints", 2)
    try:
        listed = package_module.models_from_answers(pairs, register_mod.load_register())
        package_module.write_package(
            root, gene, run, loaded["digests"], out, listed, loaded.get("atlas_commit")
        )
    except register_mod.RegisterError as exc:
        raise _fail(str(exc), 2) from None
    except scratch_module.ScratchError as exc:
        raise _fail(str(exc), exc.code) from None
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    typer.echo(f"package {out}")


@app.command("integrate")
def integrate_command(
    package: Annotated[Path, typer.Argument(help="A package directory.")],
    atlas: Path = typer.Option(..., "--atlas", help="The atlas checkout to bring it into."),
) -> None:
    """Bring a package into the atlas, only onto the base the run started from.

    Refuses (exit 1, nothing copied, every file named) when a mutable file of the checkout no
    longer has the package's base digest, when the run folder already exists, when a text folder
    changed since the base (or appeared or went since), or when a supplied PDF is present with
    different bytes. A text folder still at its base digest is replaced by the package's. Exit 2
    for an unreadable package. Writes the proposal as it stands, and no approval of its own: an
    approval in the proposal is the proposal's.
    """
    try:
        code = package_module.integrate(package, atlas)
    except OSError as exc:
        raise _fail(str(exc), 1) from None
    if code:
        raise typer.Exit(code=code)


@app.command("job-script")
def job_script_command(
    name: str = typer.Argument(
        ..., help="serve-and-ask.sh, llama-build.sbatch or cluster-ask.sbatch."
    ),
) -> None:
    """Print the absolute path of a packaged job script.

    The scripts run a local model beside `ask` inside one task or one cluster job. They ship as
    package data; run one as `bash "$(chd-curator job-script serve-and-ask.sh)" ...` so nothing
    depends on its executable bit. An unknown name exits 2 and lists the three. Writes nothing,
    and no approval.
    """
    try:
        typer.echo(str(jobs_module.job_script_path(name)))
    except KeyError:
        raise _fail(
            f"no job script {name!r}; the scripts are {', '.join(jobs_module.JOB_SCRIPTS)}", 2
        ) from None


def _answers_values(path: Path, field: str) -> set[str]:
    """The distinct non-empty values of `field` over an answers file's rows."""
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line) if line.strip() else {}
        if isinstance(row, dict) and isinstance(row.get(field), str) and row[field]:
            found.add(row[field])
    return found


@qualify_app.command("triage")
def qualify_triage_command(
    symbol: str = typer.Argument(..., help="The approved symbol whose curation is the gold."),
    screen: Path = typer.Option(..., "--screen", help="The screen's answers JSONL."),
    check: Path = typer.Option(..., "--check", help="The capable check's answers JSONL."),
    run: str = typer.Option(..., "--run", help="The run id (seeds the audit)."),
    site: Path = typer.Option(..., "--site", help="The site config YAML (its triage-screen role)."),
    gold_root: Path = typer.Option(
        Path("."), "--gold-root", help="The atlas checkout holding the approved curation."
    ),
    requests: Path | None = typer.Option(
        None, "--requests", help="The triage requests JSONL: applies thin-hit routing."
    ),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Score a triage configuration against a gene's approved curation; propose a register entry.

    The gold is `triage_gold`: each decided hit's final decision and the count of approved
    findings citing it (an approval the atlas reports void is not counted). The `--screen`
    answers must cover exactly the gene's decided hits: a key outside them, or a decided hit with
    no row (an error row counts as one), exits 2, because the score is defined over the gold's
    hits. The hybrid is `triage-combine`'s (the check over the
    risky classes, the audit sample seeded by `--run`, every unanswered hit); with `--requests`,
    a hit PubMed had no record or abstract for is checked too, as `triage-combine` does, and
    without it a line says thin-hit routing was not applied. PASS means no paper with approved
    findings is lost. Prints the score and, for a PASS or FAIL, the register entry to propose
    (YAML), its `backend` and `model_key` taken from `--site`'s `triage-screen` role. Exit 0 for
    PASS and FAIL, 1 for INCOMPLETE (the check owes hits; no entry), 2 for an unreadable file,
    site or gold, or screen answers carrying no single `rules_digest`. Warns on stderr when the
    answers' `model_key` is not the site's. Writes nothing: no register entry, no approval.
    """
    if not gold_root.is_dir():
        raise _fail(f"--gold-root {gold_root} is not a directory", 2)
    try:
        spec = load_site(site).roles.get("triage-screen")
        if spec is None:
            raise _fail(f"{site} has no triage-screen role", 2)
        gold = qualify_module.triage_gold(gold_root, symbol, _atlas_cli(gold_root, atlas_cli))
        keys = set(gold)
        answered = triage_module.read_answers(screen, keys)
        checked = triage_module.read_answers(check, keys)
        thin = (
            triage_module.thin_keys(triage_module.read_request_rows(requests))
            if requests is not None
            else set()
        )
        rules = _answers_values(screen, "rules_digest")
        keys_seen = _answers_values(screen, "model_key")
        unasked = sorted(keys - _answers_values(screen, "key"))
    except (qualify_module.QualifyError, triage_module.TriageUsageError, SiteError) as exc:
        raise _fail(str(exc), 2) from None
    except (OSError, ValueError) as exc:
        raise _fail(str(exc), 2) from None
    if unasked:
        raise _fail(
            f"the screen answers have no row for {len(unasked)} of the gene's {len(keys)} decided "
            f"hit(s) ({', '.join(unasked)}): `ask` writes a row per request, so the gene was not "
            "fully asked",
            2,
        )
    if len(rules) != 1:
        found = ", ".join(sorted(rules)) or "none"
        raise _fail(f"the screen answers must carry one rules_digest; found: {found}", 2)
    computed = model_key(spec)
    if keys_seen - {computed}:
        typer.echo(
            f"warning: the screen answers carry model_key {', '.join(sorted(keys_seen))}, "
            f"not {computed}, the site's triage-screen: they were not produced by this "
            "site's configuration",
            err=True,
        )
    score = qualify_module.score_triage(
        gold, {key: answered.get(key, {}) for key in sorted(keys)}, checked, run, thin=thin
    )
    typer.echo(
        f"{score.verdict}: {score.with_findings} hit(s) with approved findings, "
        f"{score.screen_alone} included by the screen alone, {len(score.missed)} missed"
        + (f" ({', '.join(score.missed)})" if score.missed else "")
        + f"; {score.sent_to_check} of {len(gold)} sent to the check before the audit, "
        f"{score.audit} audited, {len(score.owed)} still owed"
        + (f" ({', '.join(score.owed)})" if score.owed else "")
    )
    if requests is None:
        typer.echo("thin-hit routing was not applied (no --requests given)")
    if score.verdict == "INCOMPLETE":
        raise typer.Exit(code=1)
    check_keys = _answers_values(check, "model_key")
    entry = qualify_module.proposed_entry(
        symbol,
        gold,
        score,
        seed=run,
        spec=spec,
        check_model_key=", ".join(sorted(check_keys)) or "unknown",
        rules_digest=next(iter(rules)),
    )
    out = io.StringIO()
    yaml_out = YAML()
    yaml_out.default_flow_style = False
    yaml_out.dump({"entries": [entry]}, out)
    typer.echo("Proposed register entry (not written):")
    typer.echo(out.getvalue().rstrip())
