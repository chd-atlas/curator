"""``chd-curator search | abstracts | add-publication``, with a fake PubMed and a stub atlas.

Ported from the atlas's ``tests/unit/test_cli_literature.py`` at b4c8191. The repository
under test is a tmp directory holding the atlas's real exported ``schemas/`` (symlinked
from ``CHD_ATLAS_ROOT``, never copied) and a small ``publications.yaml``; the atlas
itself is the stub from ``conftest.py``. Nothing here reaches the network:
``chd_curator.cli._fetch`` is the one transport hook and every test replaces it.
"""

from __future__ import annotations

import json
import os
import urllib.error
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner, Result

from chd_curator import cli
from chd_curator.atlas import plain
from chd_curator.cli import app
from chd_curator.pubmed import PubMedError
from fake_pubmed import FakePubMed, papers

runner = CliRunner()

TBX5_QUERY = '("TBX5"[tiab]) AND stub-query'

PUBLICATIONS = """\
# Head comment: every record is transcribed from PubMed.
publications:
  - id: PMID:111
    title: First paper.
    journal: Journal One
    year: 1999
    authors:
      - Aa A
    # A comment sitting above a neighbouring field.
    study_type: case_report
  # A comment sitting between two records.
  - id: PMID:222
    title: Second paper.
    journal: Journal Two
    year: 2001
    authors:
      - Bb B
    study_type: gwas
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "curation").mkdir()
    (tmp_path / "schemas").symlink_to(Path(os.environ["CHD_ATLAS_ROOT"]) / "schemas")
    (tmp_path / "curation" / "publications.yaml").write_text(PUBLICATIONS)
    return tmp_path


def _install(monkeypatch: pytest.MonkeyPatch, fetch: Callable[[str], bytes]) -> None:
    monkeypatch.setattr(cli, "_fetch", fetch)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)


def _run(stub: Path, root: Path, command: str, *args: str) -> Result:
    extra = ["--atlas-cli", str(stub)] if command != "add-publication" else []
    return runner.invoke(app, [command, *args, "--root", str(root), *extra])


def _search(stub: Path, root: Path, *args: str) -> Result:
    return _run(stub, root, "search", *args)


def _load(path: Path) -> dict[str, object]:
    loaded: dict[str, object] = plain(YAML(typ="safe").load(path.read_text()))
    return loaded


def test_search_writes_the_approved_symbols_file_for_a_previous_symbol(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePubMed(papers("100", "9", "25"))
    _install(monkeypatch, fake)

    # "HOS" is TBX5's retired name: accepted, but the file is the approved symbol's.
    result = _search(stub_atlas, root, "HOS", "--run-on", "2026-11-02")

    assert result.exit_code == 0, result.stdout
    assert not (root / "curation" / "literature" / "HOS.yaml").exists()
    data = _load(root / "curation" / "literature" / "TBX5.yaml")
    assert data["gene"] == "HGNC:11604"
    searches = data["searches"]
    assert isinstance(searches, list)
    assert searches[0]["query"] == '("TBX5"[tiab]) AND stub-query'  # the stub's, verbatim
    # The query PubMed was asked is the atlas's too, not anything curator built.
    assert parse_qs(urlparse(fake.urls[0]).query)["term"] == [searches[0]["query"]]


def test_a_first_search_creates_the_file_with_pending_entries(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Numeric order 9 < 25 < 100; string order would put "100" first.
    _install(monkeypatch, FakePubMed(papers("100", "9", "25")))

    result = _search(stub_atlas, root, "TBX5", "--run-on", "2026-11-02")

    assert result.exit_code == 0, result.stdout
    assert result.stdout.splitlines() == [
        f"TBX5: search 1 found 3 hit(s), 3 new; wrote {root}/curation/literature/TBX5.yaml"
    ]
    data = _load(root / "curation" / "literature" / "TBX5.yaml")
    assert data["searches"] == [
        {
            "id": 1,
            "source": "pubmed",
            "query": TBX5_QUERY,
            "run_on": date(2026, 11, 2).isoformat(),
            "hits": 3,
            "new_hits": 3,
        }
    ]
    assert data["triage"] == [
        {"pmid": "PMID:9", "search": 1, "decision": "pending"},
        {"pmid": "PMID:25", "search": 1, "decision": "pending"},
        {"pmid": "PMID:100", "search": 1, "decision": "pending"},
    ]


def test_a_rerun_appends_a_search_and_triages_only_the_new_papers(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakePubMed(papers("9", "25")))
    assert _search(stub_atlas, root, "TBX5", "--run-on", "2026-11-02").exit_code == 0
    path = root / "curation" / "literature" / "TBX5.yaml"
    # A curator decides one paper and leaves comments.
    text = path.read_text()
    text = text.replace("triage:\n", "# curator: triage in progress\ntriage:\n", 1)
    text = text.replace("decision: pending", "decision: included # the TBX5 paper", 1)
    path.write_text(text)

    _install(monkeypatch, FakePubMed(papers("9", "25", "7", "300")))
    result = _search(stub_atlas, root, "TBX5", "--run-on", "2026-12-01")

    assert result.exit_code == 0, result.stdout
    after = path.read_text()
    assert "# curator: triage in progress" in after
    assert "decision: included # the TBX5 paper" in after
    data = _load(path)
    assert [(s["id"], s["hits"], s["new_hits"]) for s in data["searches"]] == [  # type: ignore[attr-defined]
        (1, 2, 2),
        (2, 4, 2),
    ]
    assert [(t["pmid"], t["search"], t["decision"]) for t in data["triage"]] == [  # type: ignore[attr-defined]
        ("PMID:9", 1, "included"),
        ("PMID:25", 1, "pending"),
        ("PMID:7", 2, "pending"),
        ("PMID:300", 2, "pending"),
    ]


class _Boom:
    def __init__(self, exc: Exception | None, body: bytes | None = None) -> None:
        self.exc, self.body = exc, body

    def __call__(self, url: str) -> bytes:
        if self.exc is not None:
            raise self.exc
        assert self.body is not None
        return self.body


def _count_changes(url: str) -> bytes:
    start = int(parse_qs(urlparse(url).query)["retstart"][0])
    count = 1200 if start == 0 else 1201
    ids = [str(n) for n in range(start, min(start + 500, count))]
    return json.dumps({"esearchresult": {"count": str(count), "idlist": ids}}).encode()


FAILURES = {
    "pubmed-error": _Boom(PubMedError("PubMed answered HTTP 500")),
    "url-error": _Boom(urllib.error.URLError("no route")),
    "http-429": _Boom(urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)),  # type: ignore[arg-type]
    "html-body": _Boom(None, b"<html>Service unavailable</html>"),
    "count-changes": _count_changes,
}


@pytest.mark.parametrize("name", sorted(FAILURES))
@pytest.mark.parametrize("existing", [False, True])
def test_a_pubmed_failure_exits_1_and_leaves_the_file_exactly_as_it_was(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch, name: str, existing: bool
) -> None:
    path = root / "curation" / "literature" / "TBX5.yaml"
    if existing:
        _install(monkeypatch, FakePubMed(papers("9")))
        assert _search(stub_atlas, root, "TBX5").exit_code == 0
        before = path.read_bytes()
    _install(monkeypatch, FAILURES[name])

    result = _search(stub_atlas, root, "TBX5")

    assert result.exit_code == 1, result.stdout
    if existing:
        assert path.read_bytes() == before
    else:
        assert not path.exists()
        assert not path.parent.exists()  # not even the directory is created early
    # No stray temporary from the atomic write, and no directory litter.
    assert [p.name for p in (root / "curation").rglob("*.tmp")] == []


@pytest.mark.parametrize(
    "args",
    [
        ["NOPE"],
        ["MADH7"],
        ["BRCA1"],
        ["TBX5", "--run-on", "yesterday"],
    ],
    ids=["unknown", "ambiguous", "outside-the-gate", "bad-date"],
)
def test_bad_arguments_exit_2_without_touching_pubmed_or_the_disk(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    fake = FakePubMed(papers("9"))
    _install(monkeypatch, fake)

    result = _search(stub_atlas, root, *args)

    assert result.exit_code == 2, result.stdout
    assert fake.urls == []
    assert not (root / "curation" / "literature").exists()


def test_search_refuses_a_gene_outside_the_gate_before_pubmed(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePubMed(papers("9"))
    _install(monkeypatch, fake)

    result = _search(stub_atlas, root, "BRCA1")

    assert result.exit_code == 2
    assert result.stdout == (
        "error: BRCA1 (HGNC:1100) is outside the publication gate; "
        "its literature would reach no page\n"
    )
    assert fake.urls == []
    assert not (root / "curation" / "literature").exists()


def test_search_relays_the_atlas_refusal(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePubMed(papers("9"))
    _install(monkeypatch, fake)

    result = _search(stub_atlas, root, "MADH7")

    assert result.exit_code == 2
    assert "'MADH7' is claimed by HGNC:6772, HGNC:6773" in result.stdout
    assert fake.urls == []


def test_the_api_key_reaches_the_request_and_never_the_output(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def leaky(url: str) -> bytes:
        seen.append(url)
        raise PubMedError(f"could not fetch {url}")

    monkeypatch.setattr(cli, "_fetch", leaky)
    monkeypatch.setenv("NCBI_API_KEY", "SEKRIT-KEY-123")

    result = _search(stub_atlas, root, "TBX5")

    assert result.exit_code == 1
    assert "api_key=SEKRIT-KEY-123" in seen[0]
    assert "SEKRIT-KEY-123" not in result.stdout

    # A blank or whitespace-only variable is no key at all.
    seen.clear()
    monkeypatch.setenv("NCBI_API_KEY", "   ")
    _search(stub_atlas, root, "TBX5")
    assert "api_key" not in seen[0]


def test_a_file_that_belongs_to_another_gene_is_refused_untouched(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = root / "curation" / "literature" / "TBX5.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "gene: HGNC:4173\nsearches:\n"
        "  - {id: 1, source: pubmed, query: q, run_on: 2026-11-02, hits: 0, new_hits: 0}\n"
    )
    before = path.read_bytes()
    _install(monkeypatch, FakePubMed(papers("9")))

    result = _search(stub_atlas, root, "TBX5")

    assert result.exit_code == 1
    assert "HGNC:4173" in result.stdout
    assert path.read_bytes() == before


def test_a_write_the_schema_refuses_exits_2_and_leaves_the_file(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file already holding a value the exported schema rejects is never rewritten."""
    path = root / "curation" / "literature" / "TBX5.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "gene: HGNC:11604\nsearches:\n"
        "  - {id: 1, source: pubmed, query: q, run_on: 2026-11-02, hits: 0, new_hits: 0}\n"
        "triage:\n  - {pmid: 'PMID:9', search: 1, decision: maybe}\n"
    )
    before = path.read_bytes()
    _install(monkeypatch, FakePubMed(papers("9", "25")))

    result = _search(stub_atlas, root, "TBX5")

    assert result.exit_code == 2, result.stdout
    assert path.read_bytes() == before


def _seed_triage(root: Path) -> Path:
    path = root / "curation" / "literature" / "TBX5.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "gene: HGNC:11604\n"
        "searches:\n"
        "  - {id: 1, source: pubmed, query: q, run_on: 2026-11-02, hits: 4, new_hits: 4}\n"
        "triage:\n"
        "  - {pmid: 'PMID:5', search: 1, decision: pending}\n"
        "  - {pmid: 'PMID:6', search: 1, decision: included}\n"
        "  - {pmid: 'PMID:7', search: 1, decision: pending}\n"
        "  - {pmid: 'PMID:8', search: 1, decision: excluded, reason: not_chd}\n"
    )
    return path


def test_abstracts_prints_pending_papers_and_marks_a_missing_abstract(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_triage(root)
    many = papers("5", "6", "7", "8")
    many["7"]["abstract"] = ""
    _install(monkeypatch, FakePubMed(many))

    # Typed as the previous symbol: the approved symbol's file is the one read.
    result = _run(stub_atlas, root, "abstracts", "HOS")

    assert result.exit_code == 0, result.stdout
    assert result.stdout.splitlines() == [
        "PMID:5 · 2005 · Title of 5",
        "Abstract of 5.",
        "",
        "PMID:7 · 2007 · Title of 7",
        "(no abstract in PubMed)",
        "",
    ]


def test_abstracts_filters_by_decision(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_triage(root)
    _install(monkeypatch, FakePubMed(papers("5", "6", "7", "8")))

    included = _run(stub_atlas, root, "abstracts", "TBX5", "--decision", "included")
    bad = _run(stub_atlas, root, "abstracts", "TBX5", "--decision", "maybe")
    missing = _run(stub_atlas, root, "abstracts", "GATA4")
    refused = _run(stub_atlas, root, "abstracts", "NOPE")

    assert included.stdout.splitlines()[0] == "PMID:6 · 2006 · Title of 6"
    assert "PMID:5" not in included.stdout
    assert bad.exit_code == 2
    assert missing.exit_code == 1  # a known gene with no literature file yet
    assert "chd-curator search GATA4" in missing.stdout
    assert refused.exit_code == 2


@pytest.mark.parametrize(
    ("args", "code", "fetch"),
    [
        (["111", "--study-type", "gwas"], 1, FakePubMed(papers("111"))),
        (["333", "--study-type", "not_a_type"], 2, FakePubMed(papers("333"))),
        (["abc", "--study-type", "gwas"], 2, FakePubMed(papers("333"))),
        (["333", "--study-type", "gwas"], 1, _Boom(urllib.error.URLError("down"))),
        (["999", "--study-type", "gwas"], 1, FakePubMed(papers("333"))),
        # The reading is required, and a blank one is no reading.
        (["333", "--study-type", "gwas", "--reading", "  "], 2, FakePubMed(papers("333"))),
        (["333", "--study-type", "gwas", "--reading", ""], 2, FakePubMed(papers("333"))),
    ],
    ids=[
        "already-present",
        "bad-study-type",
        "bad-pmid",
        "network-down",
        "pmid-unknown-to-pubmed",
        "blank-reading",
        "empty-reading",
    ],
)
def test_add_publication_refusals_leave_the_file_untouched(
    root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    code: int,
    fetch: Callable[[str], bytes],
) -> None:
    path = root / "curation" / "publications.yaml"
    before = path.read_bytes()
    _install(monkeypatch, fetch)

    reading = [] if "--reading" in args else ["--reading", "r"]
    result = _run(stub_atlas, root, "add-publication", *args, *reading)

    assert result.exit_code == code, result.stdout
    assert path.read_bytes() == before
    if args[0] == "111":
        assert "already in publications.yaml" in result.stdout


def test_a_bad_study_type_lists_the_atlas_values_and_makes_no_request(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePubMed(papers("333"))
    _install(monkeypatch, fake)

    result = _run(
        stub_atlas, root, "add-publication", "333", "--study-type", "nope", "--reading", "r"
    )

    assert result.exit_code == 2
    schema = json.loads((root / "schemas" / "publication_file.schema.json").read_text())
    allowed = schema["$defs"]["StudyType"]["enum"]
    assert f"is not one of: {', '.join(allowed)}" in result.stdout
    assert fake.urls == []


def test_add_publication_without_a_reading_is_refused_by_the_command_line(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = root / "curation" / "publications.yaml"
    before = path.read_bytes()
    _install(monkeypatch, FakePubMed(papers("333")))
    result = runner.invoke(
        app, ["add-publication", "333", "--study-type", "gwas", "--root", str(root)]
    )
    assert result.exit_code == 2
    assert path.read_bytes() == before


def test_a_malformed_esummary_exits_1_and_leaves_publications_untouched(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = root / "curation" / "publications.yaml"
    before = path.read_bytes()
    _install(monkeypatch, _Boom(None, b'{"result": {"uids": ["333"]}}'))

    result = _run(
        stub_atlas, root, "add-publication", "333", "--study-type", "gwas", "--reading", "r"
    )

    assert result.exit_code == 1, result.stdout
    assert result.stdout.startswith("error: esummary")
    assert path.read_bytes() == before


def test_the_atlas_cli_comes_from_the_environment(
    root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_triage(root)
    _install(monkeypatch, FakePubMed(papers("5", "6", "7", "8")))
    monkeypatch.setenv("CHD_ATLAS_CLI", str(stub_atlas))
    result = runner.invoke(app, ["abstracts", "HOS", "--root", str(root)])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("PMID:5")
