"""The ``chd-curator`` command line.

``search``, ``abstracts`` and ``add-publication`` are the atlas's ``chd-atlas
literature`` commands of the same names, copied from its ``cli.py`` at b4c8191. What
changed: the gene is resolved and the publication gate asked by running
``chd-atlas literature target`` (never by importing the atlas), and every file is
written through ``write.py``, which checks the bytes against the atlas's exported JSON
Schema first.

Exit codes: 2 for bad arguments, an atlas that refused or could not be asked, a gene
outside the publication gate, or a write the write path refused; 1 for PubMed
failing, the network, or a file that cannot be read.
"""

from __future__ import annotations

import os
import re
import urllib.error
from datetime import date
from pathlib import Path
from typing import Any

import typer

from chd_curator import pubmed
from chd_curator.atlas import AtlasError, default_atlas_cli, schema_validator, target
from chd_curator.pubmed import Fetch, PubMedError, urllib_fetch
from chd_curator.records import (
    RecordError,
    literature_path,
    load_round_trip,
    with_publication,
    with_search,
)
from chd_curator.write import WriteRefused, write_literature, write_publications

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
    root: Path = typer.Option(Path("."), help="Repository root."),
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
        write_literature(path, content, validator)
    except WriteRefused as exc:
        raise _fail(str(exc), 2) from None
    typer.echo(
        f"{answer.symbol}: search {search_id} found {hits} hit(s), {new_hits} new; wrote {path}"
    )


@app.command("abstracts")
def abstracts(
    symbol: str = typer.Argument(..., help="Gene symbol (approved, alias or previous)."),
    decision: str = typer.Option("pending", help="pending, included or excluded."),
    root: Path = typer.Option(Path("."), help="Repository root."),
    atlas_cli: Path | None = _ATLAS_CLI_OPTION,
) -> None:
    """Print PMID, year, title and abstract for each triage entry with a decision."""
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
            title, abstract = found
            typer.echo(f"PMID:{pmid} · {year} · {title}")
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
    root: Path = typer.Option(Path("."), help="Repository root."),
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
        found = pubmed.esummary([digits], _fetch, api_key=key).get(digits)
        if found is None:
            raise PubMedError(f"PubMed returned no record for PMID:{digits}")
        content = with_publication(
            path.read_bytes(), pubmed.publication_record(found, study_type), reading
        )
    except (PubMedError, urllib.error.URLError, RecordError, OSError) as exc:
        raise _fail(_redact(str(exc), key), 1) from None
    try:
        write_publications(path, content, validator)
    except WriteRefused as exc:
        raise _fail(str(exc), 2) from None
    typer.echo(f"added PMID:{digits} to {path}")
