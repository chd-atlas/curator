"""The ``chd-curator`` command line.

``search``, ``abstracts``, ``add-publication`` and ``diff`` are the atlas's ``chd-atlas
literature`` commands of the same names, copied from its ``cli.py`` at b4c8191. What
changed: the gene is resolved and the publication gate asked by running
``chd-atlas literature target`` (never by importing the atlas), and every file is
written through ``write.py``, which checks the bytes against the atlas's exported JSON
Schema first.

Exit codes: 2 for bad arguments, an atlas that refused or could not be asked, a gene
outside the publication gate, or a write the write path refused (a schema, an
anchor, an approval, or a file that changed since it was read); 1 for PubMed
failing, the network, or a file that cannot be read.
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

from chd_curator import pubmed
from chd_curator.atlas import AtlasError, default_atlas_cli, plain, schema_validator, target
from chd_curator.diff import DiffError, diff_findings, parse_findings_file
from chd_curator.pubmed import Fetch, PubMedError, urllib_fetch
from chd_curator.records import (
    RecordError,
    literature_path,
    load_round_trip,
    with_publication,
    with_search,
    with_verified,
)
from chd_curator.write import (
    WriteRefused,
    schema_error_lines,
    write_literature,
    write_publications,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="CHD atlas curator tools.")

# The one network hook: tests replace it, nothing else reaches PubMed.
_fetch: Fetch = urllib_fetch

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
