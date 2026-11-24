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
import subprocess
import urllib.error
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import typer
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator import agreement as agreement_mod
from chd_curator import archive as archive_mod
from chd_curator import archive_read, pubmed, review_page, variantvalidator
from chd_curator.atlas import (
    AtlasError,
    Frame,
    default_atlas_cli,
    digests,
    frame,
    plain,
    schema_validator,
    target,
    validate_summary,
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
from chd_curator.run import RUN_FORMAT, RunFormatError, RunPaths
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


def _second_opinions(
    paths: Sequence[Path], root: Path, symbol: str, gene: str
) -> list[dict[str, Any]]:
    validator = schema_validator(root, "review_decisions")
    loaded: list[dict[str, Any]] = []
    names: dict[str, str] = {}  # reviewer name, case-folded, to the file that gave it
    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _fail(f"--with {path}: cannot be read as JSON: {exc}", 2) from None
        errors = sorted(
            f"{'/'.join(map(str, e.path)) or '(file)'}: {e.message}"
            for e in validator.iter_errors(doc)
        )
        if errors:
            raise _fail(f"--with {path} is not a format 1 decisions file: {errors[0]}", 2)
        if doc["reviewer"]["role"] != "second_opinion":
            raise _fail(f"--with {path}: its reviewer's role is not second_opinion", 2)
        if doc["gene"] != gene or doc["symbol"] != symbol:
            raise _fail(
                f"--with {path} is for {doc['symbol']} ({doc['gene']}), not {symbol} ({gene})", 2
            )
        if not doc["session"].get("finished_at"):
            raise _fail(
                f"--with {path}: the review is not finished (no I'm done), so it is no verdict", 2
            )
        who = doc["reviewer"]["name"].strip().casefold()
        if who in names:
            raise _fail(
                f"--with {path}: {names[who]} already gave that reviewer's second opinion; "
                "one file each",
                2,
            )
        names[who] = str(path)
        loaded.append(doc)
    return loaded


@app.command("review-page")
def review_page_command(
    symbol: str = typer.Argument(..., help="Gene symbol, as named in the run's directory."),
    run: str = typer.Option(..., "--run", help="The run id."),
    reviewer: str = typer.Option(..., "--reviewer", help="The reviewer's name, shown on the page."),
    role: str = typer.Option("owner", "--role", help="owner or second_opinion."),
    with_: list[Path] = typer.Option(
        [], "--with", help="A second opinion's decisions file to show on the owner page."
    ),
    root: Path = typer.Option(Path("."), help="Atlas checkout root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Write the run's review page: one HTML file the owner or a second reviewer opens.

    The owner page is `<run>/review.html`; a second opinion's is `<run>/review-<name>.html`
    and shows no approval state and no other reviewer's verdict. Opened as a claude.ai
    artifact the page saves to its store; opened as a file it saves in the browser and
    offers "Download my review" (a decisions file for `chd-atlas literature apply-review`).
    Refuses (exit 1) to build when `chd-atlas validate` reports an error; exit 2 for a bad
    argument, an unreadable run, or a `--with` file that is not a finished second opinion on this
    gene (two files from one reviewer are refused too). Validate is asked first, so a gene the atlas
    cannot digest still reports its validate errors.
    """
    _require_root(root)
    if role not in ("owner", "second_opinion"):
        raise _fail(f"--role {role!r} is not owner or second_opinion", 2)
    if role == "second_opinion" and with_:
        raise _fail("a second-opinion page shows no other reviewer's verdicts: drop --with", 2)
    name = reviewer.strip()
    name_slug = review_page.slug(name)
    if not name_slug:
        raise _fail("--reviewer must contain a letter or digit", 2)
    cli = _atlas_cli(root, atlas_cli)
    paths = RunPaths(root, symbol, run)
    try:
        code, line = validate_summary(root, cli)
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
    if code != 0 or not re.match(r"0 error\(s\)", line):
        raise _fail(
            f"chd-atlas validate reports errors ({line}); fix them before building a page", 1
        )
    try:
        found = digests(root, symbol, cli)
        opinions = _second_opinions(with_, root, symbol, found.gene)
    except AtlasError as exc:
        raise _fail(str(exc), 2) from None
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
            second_opinions=opinions,
        )
    except (ReviewDataError, RunFormatError, ValueError) as exc:
        raise _fail(str(exc), 2) from None
    out = paths.dir / ("review.html" if role == "owner" else f"review-{name_slug}.html")
    try:
        out.write_text(review_page.render(data), encoding="utf-8")
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
    download leaves it out, and named on stdout (exit 0). The last-but-one line counts what was
    written.
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

    Append-only: a different file at the same place is refused, an identical one prints
    `already filed` (and is committed if it was left uncommitted). Prints one line per path,
    then `committed <sha>` when it committed. Exit 2 for a refusal (an archive that is not the
    top of a checkout with its marker committed, no `user.name`/`user.email` in the archive's
    own config, a page for another gene, a bad id or `--artifact`, a different file already
    filed); exit 1 when git fails or a file cannot be written. Never pushes.
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
    `<SYMBOL>/<run>/decisions/<finished_at in UTC>--<reviewer>.json`.
    Append-only: a different file at that name is refused (the message names it), an identical
    one prints `already filed` (and is committed if it was left uncommitted). Prints `filed
    <path>` or `already filed <path>`, then `committed <sha>` when it committed; for an owner's
    file the last line is the `chd-atlas literature apply-review` command to run next. Exit 2
    for a refusal (an unusable archive, no `user.name`/`user.email` in the archive's own config,
    invalid JSON or a schema error, an unfinished session, another gene, no page filed for the
    file, a different file already filed); exit 1 when git fails or a file cannot be written.
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
            f"next: chd-atlas literature apply-review {symbol} --decisions {top / filed.path} "
            f"--by {reviewer['name']} --root {root.resolve()}"
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
    counts every reviewer's latest fix or later verdict on the finding's current digest (a
    finding with two such reviewers counts 2) and `<f>` every pending triage flip in every
    reviewer's latest triage verdict. With `--json` it prints the whole state instead. Exit 2
    for a refusal (an unusable or dirty archive, a file that fails the schema, is not
    finished, is for another gene or is misnamed, a non-JSON file under a decisions
    directory, an unreadable literature file); exit 1 when git fails. Writes nothing.
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
    flips = sum(fl["pending"] for t in out["triage"]["verdicts"] for fl in t["flips"])
    typer.echo(
        f"{symbol}: {len(out['findings'])} findings with verdicts in "
        f"{archive_read.files_line(view)}; {pending} pending fix or ask later, "
        f"{flips} pending flips (archive {view.commit})"
    )


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
    dot-directory. `--json` prints the whole report instead. Exit 2 for a refusal (an unusable
    or dirty archive, a file that fails the schema, is unfinished or is misnamed); exit 1 when
    git fails. Writes nothing, and the report is printed nowhere else.
    """
    _require_root(root)
    try:
        top = archive_mod.check_archive(archive)
        names = list(symbols or [])
        if not names:
            names = sorted(
                p.name for p in top.iterdir() if p.is_dir() and not p.name.startswith(".")
            )
        views = [archive_read.read_archive(archive, s, root) for s in names]
    except archive_mod.ArchiveError as exc:
        raise _fail(str(exc), 2) from None
    except archive_mod.GitError as exc:
        raise _fail(str(exc), 1) from None
    tallies = agreement_mod.tally(views)
    if as_json:
        commit = views[0].commit if views else ""
        out = agreement_mod.as_json(tallies, commit)
        typer.echo(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False))
        return
    for line in agreement_mod.report(tallies, names):
        typer.echo(line)
