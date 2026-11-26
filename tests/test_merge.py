"""``chd-curator merge``: the literal outcome on the shared run fixture, every refusal, and the
backfill mode.

A refusal test compares the bytes of every file under ``curation/`` (the literature file,
``publications.yaml`` and ``.id_registry.yaml``) and of the whole run directory before and
after, because a merge that wrote evidence or ``merge.json`` and then refused would leave a run
that reads as merged. The expected values (ids, the counter, the triage entry, the Publications,
the re-keyed evidence) are written out, never rebuilt from the module's own constants.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator import cli
from chd_curator import merge as merge_module
from chd_curator.cli import app
from chd_curator.merge import append_numbered
from chd_curator.records import (
    RecordError,
    is_empty,
    read_counter,
    with_counter,
    with_fields_set,
    with_triage,
)
from chd_curator.run import RunPaths, TriageChange
from fake_pubmed import FakePubMed
from run_fixture import copy_run_fixture

runner = CliRunner()
YAML_SAFE = YAML(typ="safe")


def _fresh(tmp_path: Path, atlas_root: Path) -> RunPaths:
    p = copy_run_fixture(tmp_path)
    shutil.copytree(atlas_root / "schemas", p.root / "schemas")
    return p


def _pubmed(monkeypatch: pytest.MonkeyPatch, *pmids: str) -> FakePubMed:
    titles = {"91000002": ("Two merged variants", 2021), "91000003": ("A merged splice", 2022)}
    fake = FakePubMed(
        {
            p: {"title": titles.get(p, ("x", 2000))[0], "year": titles.get(p, ("x", 2000))[1]}
            for p in (pmids or ("91000002", "91000003", "91000004"))
        }
    )
    monkeypatch.setattr(cli, "_fetch", fake)
    return fake


def _merge(p: RunPaths, stub: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["merge", "TBX5", "--run", "r1", "--root", str(p.root), "--atlas-cli", str(stub), *args],
        catch_exceptions=False,
    )
    return result.exit_code, result.output


def _snapshot(p: RunPaths) -> dict[str, str]:
    files = [f for d in (p.root / "curation", p.dir) for f in sorted(d.rglob("*")) if f.is_file()]
    return {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new))


def _load(path: Path) -> Any:
    return YAML_SAFE.load(path.read_text())


def _literature(p: RunPaths) -> Path:
    return p.root / "curation" / "literature" / "TBX5.yaml"


def test_merge_new_numbers_findings_from_the_counter_in_year_pmid_batch_order(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _fresh(tmp_path, atlas_root)
    fake = _pubmed(monkeypatch)
    before = _literature(p).read_text()
    code, out = _merge(p, stub_atlas)
    assert code == 0
    assert out == (
        "merged 3 findings (CHDA:LIT:0009002..CHDA:LIT:0009004) from 2 batches; "
        "2 publications added; 1 papers excluded; LIT counter 9004\n"
    )
    # One esummary for the two cited papers, none for the excluded one, nothing else.
    assert fake.urls == [
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        "?db=pubmed&id=91000002%2C91000003&retmode=json&tool=chd-curator"
    ]
    assert (
        p.root / "curation/.id_registry.yaml"
    ).read_text() == "prefixes:\n  AST: 2\n  LIT: 9004\n"

    text = _literature(p).read_text()
    head = before.split("findings:\n", 1)[0]
    assert text.startswith(head.split("    decision: included\n  - pmid: PMID:91000004")[0])
    approved_block = before.split("findings:\n", 1)[1]
    assert approved_block in text  # the approved finding's lines are untouched
    data = YAML_SAFE.load(text)
    assert [(f["id"], f["publication"], f["design"]) for f in data["findings"]] == [
        ("CHDA:LIT:0009001", "PMID:91000001", "family_segregation"),
        ("CHDA:LIT:0009002", "PMID:91000002", "case_report"),
        ("CHDA:LIT:0009003", "PMID:91000002", "family_segregation"),
        ("CHDA:LIT:0009004", "PMID:91000003", "case_report"),
    ]
    assert [f["shares_probands_with"] for f in data["findings"]] == ["unknown"] * 4
    for new in data["findings"][1:]:
        assert not {"verified_by", "approved_by", "approved_on", "approved_digest"} & set(new)
    assert data["triage"] == [
        {"pmid": "PMID:91000001", "search": 1, "decision": "included"},
        {"pmid": "PMID:91000002", "search": 1, "decision": "included"},
        {"pmid": "PMID:91000003", "search": 1, "decision": "included"},
        {
            "pmid": "PMID:91000004",
            "search": 1,
            "decision": "excluded",
            "reason": "not_human_genetics",
            "note": "A mouse model; no human carrier is reported.",
        },
    ]
    # The canonical style: model key order, flow lists and maps, no key defaulted.
    assert (
        "  - id: CHDA:LIT:0009004\n"
        "    publication: PMID:91000003\n"
        "    design: case_report\n"
        "    methods: [exome]\n"
        "    count_unit: probands\n"
        "    screened: 1\n"
        "    carriers: {de_novo: 0, inherited: 0, origin_unknown: 1}\n"
        "    variant_classes: [snv_indel]\n"
        "    variants:\n"
        "      - id: 1\n"
        "        as_reported: c.700+1G>A\n"
        "        locator: {kind: in_text, element: Abstract}\n"
        "        kind: snv_indel\n"
        "        placement: unplaced\n"
        "        reported: {c: c.700+1G>A}\n"
        "        zygosity: heterozygous\n"
        "        carriers: {de_novo: 0, inherited: 0, origin_unknown: 1}\n"
        "        phenotypes: [HP:0001631]\n"
        "    phenotypes: [HP:0001631]\n"
        "    lesion_groups: [septal]\n"
        "    syndromic: isolated\n"
        "    shares_probands_with: unknown\n"
        "    locators:\n"
        "      - {kind: in_text, element: Abstract}\n"
    ) in text

    pubs = (p.root / "curation/publications.yaml").read_text()
    assert pubs.startswith(
        (Path(__file__).parent / "fixtures/run/curation/publications.yaml").read_text()
    )
    assert _load(p.root / "curation/publications.yaml")["publications"][1:] == [
        {
            "id": "PMID:91000002",
            "title": "Two merged variants",
            "journal": "Journal of Tests",
            "year": 2021,
            "authors": ["Zed Z"],
            "study_type": "case_report",
            "doi": "10.1000/t91000002",
        },
        {
            "id": "PMID:91000003",
            "title": "A merged splice",
            "journal": "Journal of Tests",
            "year": 2022,
            "authors": ["Zed Z"],
            "study_type": "case_report",
            "doi": "10.1000/t91000003",
        },
    ]
    assert "    # full text and supplement\n    study_type: case_report\n" in pubs
    assert "    # abstract only\n    study_type: case_report\n" in pubs

    quote = {"field": "design", "quote": "We report a proband with a ventricular septal defect"}
    assert _load(p.evidence / "batch-01.yaml") == {
        "CHDA:LIT:0009002": [
            {**quote, "where": "Abstract"},
            {
                "field": "variant 1 phase",
                "quote": "The proband carried both variants in trans",
                "where": "Results - Patients",
            },
        ],
        "CHDA:LIT:0009003": [
            {
                "field": "design",
                "quote": "a second family carried a heterozygous c.835C>T, p.(Arg279Ter) variant",
                "where": "Results - Family B",
            },
            {
                "field": "phenotypes",
                "quote": "three relatives had an atrial septal defect",
                "where": "Results - Family B",
            },
        ],
    }
    assert _load(p.evidence / "batch-02.yaml") == {
        "CHDA:LIT:0009004": [
            {
                "field": "design",
                "quote": "Exome sequencing of a fetus with an atrial septal defect",
                "where": "Abstract",
            },
            {
                "field": "variant 1 as_reported",
                "quote": "a heterozygous TBX5 splice-donor variant, c.700+1G>A",
                "where": "Abstract",
            },
        ]
    }
    assert json.loads(p.merge.read_text()) == {
        "format": 1,
        "mode": "new",
        "ids": {
            "CHDA:LIT:0901001": "CHDA:LIT:0009002",
            "CHDA:LIT:0901002": "CHDA:LIT:0009003",
            "CHDA:LIT:0902001": "CHDA:LIT:0009004",
        },
        "publications_added": ["PMID:91000002", "PMID:91000003"],
        "triage": [
            {
                "pmid": "PMID:91000004",
                "before": "included",
                "after": "excluded",
                "reason": "not_human_genetics",
                "note": "A mouse model; no human carrier is reported.",
            }
        ],
        "replaces": [],
    }


def test_shares_probands_with_is_rewritten_to_final_ids_and_keeps_the_empty_forms(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _pubmed(monkeypatch)
    batch_1 = p.extract / "batch-01.findings.yaml"
    batch_2 = p.extract / "batch-02.findings.yaml"
    # 0901001 names a sibling of its own batch and a finding already in the file; 0901002 an empty
    # list; 0902001 a finding of the other batch.
    _edit(
        batch_1,
        "  id: CHDA:LIT:0901001\n",
        "  id: CHDA:LIT:0901001\n  shares_probands_with: [CHDA:LIT:0901002, CHDA:LIT:0009001]\n",
    )
    _edit(batch_1, "shares_probands_with: unknown\n  summary: One proband", "summary: One proband")
    _edit(
        batch_1,
        "shares_probands_with: unknown\n  summary: One family",
        "shares_probands_with: []\n  summary: One family",
    )
    _edit(batch_2, "shares_probands_with: unknown", "shares_probands_with: [CHDA:LIT:0901001]")
    code, _ = _merge(p, stub_atlas)
    assert code == 0
    data = _load(_literature(p))
    assert {f["id"]: f["shares_probands_with"] for f in data["findings"]} == {
        "CHDA:LIT:0009001": "unknown",
        "CHDA:LIT:0009002": ["CHDA:LIT:0009003", "CHDA:LIT:0009001"],
        "CHDA:LIT:0009003": [],
        "CHDA:LIT:0009004": ["CHDA:LIT:0009002"],
    }


def test_append_numbered_orders_by_year_then_pmid_number_then_position() -> None:
    raw = b"gene: HGNC:1\nfindings: []\n"

    def finding(local: str, pmid: str) -> dict[str, Any]:
        return {"id": local, "publication": pmid, "shares_probands_with": "unknown"}

    findings = [
        finding("CHDA:LIT:0900001", "PMID:100"),  # 2022
        finding("CHDA:LIT:0900002", "PMID:9"),  # 2022, lower PMID number
        finding("CHDA:LIT:0900003", "PMID:100"),  # 2022, later position
        finding("CHDA:LIT:0900004", "PMID:5000"),  # 2019
    ]
    years = {"PMID:100": 2022, "PMID:9": 2022, "PMID:5000": 2019}
    _, mapping, counter = append_numbered(raw, findings, years, 40)
    assert list(mapping.items()) == [
        ("CHDA:LIT:0900004", "CHDA:LIT:0000041"),
        ("CHDA:LIT:0900002", "CHDA:LIT:0000042"),
        ("CHDA:LIT:0900001", "CHDA:LIT:0000043"),
        ("CHDA:LIT:0900003", "CHDA:LIT:0000044"),
    ]
    assert counter == 44


def _drop_papers_entry(p: RunPaths) -> None:
    path = p.extract / "batch-02.papers.json"
    entries = json.loads(path.read_text())
    path.write_text(json.dumps(entries[:1]))


def _citing_outside_its_batch(p: RunPaths) -> None:
    _edit(
        p.extract / "batch-01.findings.yaml",
        "publication: PMID:91000002",
        "publication: PMID:91000003",
    )


def _register(p: RunPaths, pmid: str, year: int) -> None:
    with (p.root / "curation/publications.yaml").open("a") as handle:
        handle.write(
            f"  - id: {pmid}\n    title: Registered earlier\n    journal: J\n    year: {year}\n"
            "    authors:\n      - Q Q\n    study_type: case_report\n"
        )


def _null_study_type(p: RunPaths) -> None:
    _edit(p.extract / "batch-01.papers.json", '"study_type": "case_report"', '"study_type": null')


def _exclusion_reason(p: RunPaths) -> None:
    _edit(p.extract / "batch-02.papers.json", "not_human_genetics", "bogus_reason")


def _triage_pending(p: RunPaths) -> None:
    _edit(
        _literature(p),
        "  - pmid: PMID:91000004\n    search: 1\n    decision: included",
        "  - pmid: PMID:91000004\n    search: 1\n    decision: pending",
    )


def _shares_unknown_id(p: RunPaths) -> None:
    _edit(
        p.extract / "batch-02.findings.yaml",
        "shares_probands_with: unknown",
        "shares_probands_with: [CHDA:LIT:0999999]",
    )


def _study_type_value(p: RunPaths) -> None:
    _edit(
        p.extract / "batch-01.papers.json",
        '"study_type": "case_report"',
        '"study_type": "bogus_type"',
    )


def _no_year(p: RunPaths) -> None:
    _edit(p.dir / "texts.json", '"year": "2021"', '"year": ""')


def _read_but_cited_nowhere(p: RunPaths) -> None:
    """Batch 02's reader gives PMID:91000003 a study type and a reading and reports no finding
    on it (and none on 91000004, which it excludes)."""
    (p.extract / "batch-02.findings.yaml").write_text("findings: []\n")
    (p.extract / "batch-02.evidence.yaml").write_text("{}\n")


def _anchor_in_the_curated_file(p: RunPaths) -> None:
    _edit(_literature(p), "    syndromic: syndromic\n", "    syndromic: &s syndromic\n")


REFUSALS: list[tuple[str, Callable[[RunPaths], None], tuple[str, ...], str]] = [
    (
        "a batch paper missing from papers.json",
        _drop_papers_entry,
        (),
        "does not account for PMID:91000004",
    ),
    (
        "an evidence key not in the batch",
        lambda p: (p.extract / "batch-01.evidence.yaml").write_text(
            (p.extract / "batch-01.evidence.yaml").read_text()
            + "CHDA:LIT:0902001:\n- field: design\n  quote: q\n  where: w\n"
        ),
        (),
        "CHDA:LIT:0902001 is not a finding of batch 01",
    ),
    (
        "a finding citing a paper outside its batch",
        _citing_outside_its_batch,
        (),
        "cites PMID:91000003, which is not in batch 01",
    ),
    (
        "verified_by on a finding",
        lambda p: _edit(
            p.extract / "batch-01.findings.yaml",
            "  id: CHDA:LIT:0901001\n",
            "  id: CHDA:LIT:0901001\n  verified_by: agent:v/w\n",
        ),
        (),
        "CHDA:LIT:0901001: carries verified_by",
    ),
    (
        "an approval on a finding",
        lambda p: _edit(
            p.extract / "batch-01.findings.yaml",
            "  id: CHDA:LIT:0901001\n",
            "  id: CHDA:LIT:0901001\n  approved_by: E.A.M\n",
        ),
        (),
        "CHDA:LIT:0901001: carries approved_by",
    ),
    (
        "a null study type without --study-type",
        _null_study_type,
        (),
        "--study-type PMID:91000002=TYPE",
    ),
    (
        "a study type the atlas does not allow",
        _study_type_value,
        (),
        "PMID:91000002: study type 'bogus_type' is not one of",
    ),
    (
        "a paper that already has findings",
        lambda p: _edit(_literature(p), "publication: PMID:91000001", "publication: PMID:91000002"),
        (),
        "PMID:91000002: already has a finding",
    ),
    (
        "a local id repeated across batches",
        lambda p: _edit(
            p.extract / "batch-02.findings.yaml", "id: CHDA:LIT:0902001", "id: CHDA:LIT:0901001"
        ),
        (),
        "CHDA:LIT:0901001 is in batch 01 and batch 02",
    ),
    (
        "an exclusion reason outside the schema's",
        _exclusion_reason,
        (),
        "'bogus_reason' is not one of",
    ),
    (
        "an excluded paper that is not included in triage",
        _triage_pending,
        (),
        "PMID:91000004: cannot be excluded: its triage decision is 'pending'",
    ),
    (
        "shares_probands_with naming an unknown finding",
        _shares_unknown_id,
        (),
        "shares_probands_with names CHDA:LIT:0999999",
    ),
    ("a paper with no year", _no_year, (), "PMID:91000002: has no year"),
    (
        "a --study-type for a paper that is not a null-type paper",
        lambda p: None,
        ("--study-type", "PMID:91000002=case_report"),
        "not a paper of this run left without a study type",
    ),
    (
        "a --study-type naming a PMID that is not in the run",
        lambda p: None,
        ("--study-type", "PMID:91000009=case_report"),
        "not a paper of this run",
    ),
    (
        "a paper in two batches",
        lambda p: _edit(
            p.dir / "batches.json", '"PMID:91000003",', '"PMID:91000002", "PMID:91000003",'
        ),
        (),
        "PMID:91000002 is in batch 01 and batch 02",
    ),
    (
        "a papers.json naming a paper outside its batch",
        lambda p: _edit(
            p.extract / "batch-01.papers.json",
            "[\n {",
            '[\n {"pmid": "PMID:91000003", "study_type": "case_report", "reading": "r"},\n {',
        ),
        (),
        "names PMID:91000003, which is not in batch 01",
    ),
    (
        "a local id already a finding in the file",
        lambda p: [
            _edit(p.extract / "batch-02.findings.yaml", "CHDA:LIT:0902001", "CHDA:LIT:0009001"),
            _edit(p.extract / "batch-02.evidence.yaml", "CHDA:LIT:0902001", "CHDA:LIT:0009001"),
        ],
        (),
        "CHDA:LIT:0009001: is already a finding in",
    ),
    (
        "a paper both excluded and cited",
        lambda p: (p.extract / "batch-02.papers.json").write_text(
            json.dumps(
                [
                    {"pmid": "PMID:91000003", "exclude": {"reason": "other", "note": "n"}},
                    {"pmid": "PMID:91000004", "exclude": {"reason": "other", "note": "n"}},
                ]
            )
        ),
        (),
        "PMID:91000003: is excluded in papers.json and cited by a finding",
    ),
    (
        "a --study-type for a paper that needs no Publication",
        lambda p: [
            _register(p, "PMID:91000003", 2022),
            _edit(
                p.extract / "batch-02.papers.json",
                '"study_type": "case_report"',
                '"study_type": null',
            ),
        ],
        ("--study-type", "PMID:91000003=case_report"),
        "no Publication is added for it, so it would not be used",
    ),
    (
        "a --study-type the atlas does not allow",
        lambda p: _null_study_type(p),
        ("--study-type", "PMID:91000002=bogus_type"),
        "PMID:91000002=bogus_type: not one of",
    ),
    (
        "a --study-type naming one PMID twice",
        lambda p: None,
        (
            "--study-type",
            "PMID:91000002=case_report",
            "--study-type",
            "PMID:91000002=functional",
        ),
        "repeats a PMID",
    ),
    (
        "a --study-type that is not PMID:n=TYPE",
        lambda p: None,
        ("--study-type", "91000002=case_report"),
        "is not PMID:<n>=<type>",
    ),
    (
        "a paper read but cited by no finding and not excluded",
        _read_but_cited_nowhere,
        (),
        "PMID:91000003: batch-02.papers.json gives it a study type and reading, but no finding "
        "of batch 02 cites it; add a finding that cites it, or exclude it in "
        "batch-02.papers.json with a reason",
    ),
    (
        "an anchor in the curated file, refused after everything is built",
        _anchor_in_the_curated_file,
        (),
        "a YAML anchor (&s) is not allowed",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "args", "message"),
    [pytest.param(m, a, msg, id=name) for name, m, a, msg in REFUSALS],
)
def test_every_refusal_exits_2_and_leaves_all_curated_files_and_the_run_as_they_were(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[RunPaths], None],
    args: tuple[str, ...],
    message: str,
) -> None:
    p = _fresh(tmp_path, atlas_root)
    fake = _pubmed(monkeypatch)
    mutate(p)
    before = _snapshot(p)
    code, out = _merge(p, stub_atlas, *args)
    assert code == 2, out
    assert message in out
    assert _snapshot(p) == before
    assert not p.merge.exists() and not p.evidence.exists()
    if "anchor" not in message:  # that one is found by the write's own check, after the fetch
        assert fake.urls == []


def test_a_paper_already_registered_gets_no_second_record_and_its_year_orders_the_ids(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """publications.yaml says 2024 for PMID:91000002 (texts.json says 2021), so its two findings
    are numbered after PMID:91000003's (2022), and no Publication is added for it."""
    p = _fresh(tmp_path, atlas_root)
    fake = _pubmed(monkeypatch)
    with (p.root / "curation/publications.yaml").open("a") as handle:
        handle.write(
            "  - id: PMID:91000002\n    title: Registered earlier\n    journal: J\n    year: 2024\n"
            "    authors:\n      - Q Q\n    study_type: case_report\n"
        )
    code, out = _merge(p, stub_atlas)
    assert code == 0, out
    assert "1 publications added" in out
    assert "id=91000003&" in fake.urls[0]
    assert json.loads(p.merge.read_text())["ids"] == {
        "CHDA:LIT:0902001": "CHDA:LIT:0009002",
        "CHDA:LIT:0901001": "CHDA:LIT:0009003",
        "CHDA:LIT:0901002": "CHDA:LIT:0009004",
    }
    ids = [r["id"] for r in _load(p.root / "curation/publications.yaml")["publications"]]
    assert ids == ["PMID:91000001", "PMID:91000002", "PMID:91000003"]


def test_a_second_merge_is_refused_and_changes_nothing(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _fresh(tmp_path, atlas_root)
    fake = _pubmed(monkeypatch)
    assert _merge(p, stub_atlas)[0] == 0
    after_first = _snapshot(p)
    fake.urls.clear()
    code, out = _merge(p, stub_atlas)
    assert code == 2
    assert "the run is already merged" in out
    assert _snapshot(p) == after_first
    assert fake.urls == []


def test_a_pubmed_failure_exits_1_and_writes_nothing(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _pubmed(monkeypatch, "91000002")  # PubMed knows nothing of 91000003
    before = _snapshot(p)
    code, out = _merge(p, stub_atlas)
    assert code == 1
    assert "PubMed returned no record for PMID:91000003" in out
    assert _snapshot(p) == before


def test_a_study_type_given_for_a_null_type_paper_is_used_for_its_publication(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _pubmed(monkeypatch)
    _null_study_type(p)
    assert _merge(p, stub_atlas, "--study-type", "PMID:91000002=family_linkage")[0] == 0
    kinds = {
        r["id"]: r["study_type"]
        for r in _load(p.root / "curation/publications.yaml")["publications"]
    }
    assert kinds == {
        "PMID:91000001": "family_linkage",
        "PMID:91000002": "family_linkage",
        "PMID:91000003": "case_report",
    }


# ---- backfill ---------------------------------------------------------------------------------

_VARIANT = {
    "id": 1,
    "as_reported": "c.1A>T",
    "locator": {"kind": "in_text", "element": "Table 1"},
    "kind": "snv_indel",
    "placement": "unplaced",
    "reported": {"c": "c.1A>T"},
    "zygosity": "heterozygous",
    "carriers": {"de_novo": 0, "inherited": 1, "origin_unknown": 0},
    "phenotypes": ["HP:0001631"],
}


_RS1 = {
    "id": 1,
    "as_reported": "rs1",
    "locator": {"kind": "in_text", "element": "Table 1"},
    "kind": "snv_indel",
    "placement": "unplaced",
    "reported": {"rsid": "rs1"},
    "zygosity": "heterozygous",
}


def _finding_text(fid: str, pmid: str, design: str, trailing: str = "", extra: str = "") -> str:
    return (
        f"  - id: {fid}\n    publication: {pmid}\n    design: {design}\n{extra}"
        "    methods: [exome]\n"
        "    phenotypes: [HP:0001631]\n    lesion_groups: [septal]\n    syndromic: isolated\n"
        "    shares_probands_with: unknown\n    locators:\n"
        "      - {kind: in_text, element: Table 1}\n"
        f"    summary: Summary of {fid}.\n"
        "    extracted_by: agent:literature-extract/claude-sonnet-5-5\n"
        f"    verified_by: agent:literature-verify/claude-opus-5-5\n{trailing}"
    )


def _backfill_run(
    tmp_path: Path,
    atlas_root: Path,
    pass_findings: dict[str, Any],
    fields: tuple[str, ...] = ("variants",),
) -> RunPaths:
    """The fixture's file plus unapproved, verified findings. A (case report) and B (common
    variant, no variant classes) have no variants; D (common variant, classes [cnv]) has none
    either; E (common variant, no classes) has one; F (case report) has none; C is in no batch.
    A blank line follows A."""
    p = _fresh(tmp_path, atlas_root)
    with _literature(p).open("a") as handle:
        handle.write(_finding_text("CHDA:LIT:0009010", "PMID:91000002", "cohort_screen", "\n"))
        handle.write(
            _finding_text("CHDA:LIT:0009011", "PMID:91000003", "common_variant_association")
        )
        handle.write(_finding_text("CHDA:LIT:0009012", "PMID:91000004", "case_report"))
        handle.write(
            _finding_text(
                "CHDA:LIT:0009013",
                "PMID:91000002",
                "common_variant_association",
                extra="    variant_classes: [cnv]\n",
            )
        )
        handle.write(
            _finding_text(
                "CHDA:LIT:0009014",
                "PMID:91000003",
                "common_variant_association",
                extra="    variants:\n      - id: 1\n        as_reported: rs1\n"
                "        locator: {kind: in_text, element: Table 1}\n        kind: snv_indel\n"
                "        placement: unplaced\n        reported: {rsid: rs1}\n"
                "        zygosity: heterozygous\n",
            )
        )
        handle.write(_finding_text("CHDA:LIT:0009015", "PMID:91000002", "case_report"))
    shutil.rmtree(p.extract)
    shutil.rmtree(p.verify)
    p.extract.mkdir()
    p.batches_json.write_text(
        json.dumps(
            {
                "format": 1,
                "mode": "backfill",
                "fields": list(fields),
                "budget_bytes": 1000,
                "batches": [
                    {
                        "id": "01",
                        "papers": ["PMID:91000002", "PMID:91000003"],
                        "findings": [
                            "CHDA:LIT:0009010",
                            "CHDA:LIT:0009011",
                            "CHDA:LIT:0009013",
                            "CHDA:LIT:0009014",
                            "CHDA:LIT:0009015",
                        ],
                        "text_bytes": 0,
                    }
                ],
            }
        )
    )
    (p.extract / "batch-01.findings.yaml").write_text(
        json.dumps({"findings": pass_findings})  # JSON is YAML
    )
    (p.extract / "batch-01.evidence.yaml").write_text(
        "CHDA:LIT:0009010:\n- field: variant 1 as_reported\n  quote: c.1A>T\n  where: Table 1\n"
    )
    return p


def test_a_backfill_that_declares_variant_classes_keeps_the_readers_value(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B is a common-variant finding gaining its first variant; the run declared
    variant_classes too, so the reader's [cnv] stands instead of the allowance's [snv_indel]."""
    _pubmed(monkeypatch)
    p = _backfill_run(
        tmp_path,
        atlas_root,
        {"CHDA:LIT:0009011": {"variants": [_VARIANT], "variant_classes": ["cnv"]}},
        ("variants", "variant_classes"),
    )
    assert _merge(p, stub_atlas)[0] == 0
    found = {f["id"]: f for f in _load(_literature(p))["findings"]}
    assert found["CHDA:LIT:0009011"]["variant_classes"] == ["cnv"]


def test_merge_backfill_changes_only_the_declared_fields(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _pubmed(monkeypatch)
    p = _backfill_run(
        tmp_path,
        atlas_root,
        {
            "CHDA:LIT:0009010": {"variants": [_VARIANT]},
            "CHDA:LIT:0009011": {"variants": [_VARIANT]},
            "CHDA:LIT:0009013": {"variants": [_VARIANT]},
            "CHDA:LIT:0009015": {"variants": []},
        },
    )
    registry = (p.root / "curation/.id_registry.yaml").read_bytes()
    publications = (p.root / "curation/publications.yaml").read_bytes()
    before = _load(_literature(p))
    code, out = _merge(p, stub_atlas)
    assert code == 0, out
    assert out == "backfilled 4 findings (variants) from 1 batch; LIT counter 9001\n"
    assert fake.urls == []
    assert (p.root / "curation/.id_registry.yaml").read_bytes() == registry
    assert (p.root / "curation/publications.yaml").read_bytes() == publications

    expected = copy.deepcopy(before)
    a, b, _c, d, _e, _f = expected["findings"][1:]
    for finding in (a, b, d):
        finding["variants"] = [_VARIANT]
        del finding["verified_by"]
    # D keeps its [cnv]; E holds a variant and is not in the pass; F's empty answer for a key it
    # lacks changed nothing, so its verified_by stays
    b["variant_classes"] = ["snv_indel"]
    assert _load(_literature(p)) == expected
    assert "\n\n  - id: CHDA:LIT:0009011\n" in _literature(p).read_text()  # A's blank line kept
    assert _load(p.evidence / "batch-01.yaml") == {
        "CHDA:LIT:0009010": [
            {"field": "variant 1 as_reported", "quote": "c.1A>T", "where": "Table 1"}
        ]
    }
    assert json.loads(p.merge.read_text()) == {
        "format": 1,
        "mode": "backfill",
        "ids": {},
        "publications_added": [],
        "triage": [],
        "replaces": [],
    }


@pytest.mark.parametrize(
    ("pass_findings", "args", "message"),
    [
        pytest.param(
            {"CHDA:LIT:0009010": {"variants": [_VARIANT]}},
            ("--study-type", "PMID:91000002=case_report"),
            "belongs to a new merge",
            id="a --study-type on a backfill",
        ),
        pytest.param(
            {"CHDA:LIT:0009010": {"variants": [_VARIANT], "summary": "A rewritten summary."}},
            (),
            "sets 'summary', which batches.json does not declare",
            id="a finding field the batch did not declare",
        ),
        pytest.param(
            {"CHDA:LIT:0009012": {"variants": [_VARIANT]}},
            (),
            "CHDA:LIT:0009012 is not a finding of batch 01",
            id="a finding outside its batch",
        ),
        pytest.param(
            {"CHDA:LIT:0009014": {"variants": [{**_VARIANT, "as_reported": "rs2"}]}},
            (),
            "CHDA:LIT:0009014 already holds variants; a backfill fills a field a finding lacks",
            id="a different value for a field the finding already holds",
        ),
        pytest.param(
            {"CHDA:LIT:0009014": {"variants": [_RS1]}},
            (),
            "CHDA:LIT:0009014 already holds variants; a backfill fills a field a finding lacks",
            id="the same value for a field the finding already holds",
        ),
        pytest.param(
            {"CHDA:LIT:0009014": {"variants": []}},
            (),
            "CHDA:LIT:0009014 already holds variants",
            id="an empty value for a field the finding already holds",
        ),
        pytest.param(
            {"CHDA:LIT:0009099": {"variants": [_VARIANT]}},
            (),
            "CHDA:LIT:0009099 is not a finding in",
            id="a finding that is not in the file",
        ),
    ],
)
def test_merge_backfill_refusals_leave_everything_as_it_was(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    pass_findings: dict[str, Any],
    args: tuple[str, ...],
    message: str,
) -> None:
    _pubmed(monkeypatch)
    p = _backfill_run(tmp_path, atlas_root, pass_findings)
    before = _snapshot(p)
    code, out = _merge(p, stub_atlas, *args)
    assert code == 2, out
    assert message in out
    assert _snapshot(p) == before


def _second_batch(p: RunPaths, ids: list[str], evidence: str = "{}\n") -> None:
    plan = json.loads(p.batches_json.read_text())
    plan["batches"].append(
        {"id": "02", "papers": ["PMID:91000004"], "findings": ids, "text_bytes": 0}
    )
    p.batches_json.write_text(json.dumps(plan))
    (p.extract / "batch-02.findings.yaml").write_text(
        json.dumps({"findings": {i: {"variants": [_VARIANT]} for i in ids}})
    )
    (p.extract / "batch-02.evidence.yaml").write_text(evidence)


BACKFILL_SETUPS = {
    "a finding named by two batches": (
        lambda p: _second_batch(p, ["CHDA:LIT:0009010"]),
        "CHDA:LIT:0009010 is in more than one batch's findings file",
    ),
    "an evidence key outside its batch": (
        lambda p: (p.extract / "batch-01.evidence.yaml").write_text(
            "CHDA:LIT:0009012:\n- field: f\n  quote: q\n  where: w\n"
        ),
        "CHDA:LIT:0009012 is not a finding of batch 01",
    ),
}


@pytest.mark.parametrize("setup, message", BACKFILL_SETUPS.values(), ids=BACKFILL_SETUPS)
def test_merge_backfill_refuses_a_plan_its_batches_contradict(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[RunPaths], None],
    message: str,
) -> None:
    _pubmed(monkeypatch)
    p = _backfill_run(tmp_path, atlas_root, {"CHDA:LIT:0009010": {"variants": [_VARIANT]}})
    setup(p)
    before = _snapshot(p)
    code, out = _merge(p, stub_atlas)
    assert code == 2
    assert message in out
    assert _snapshot(p) == before


CARELESS = {
    "a summary": (
        b"Summary of CHDA:LIT:0009012.",
        b"Changed.",
        "CHDA:LIT:0009012: fields other than the declared ones changed: ['summary']",
    ),
    "a top-level key": (
        b"gene: HGNC:11604",
        b"gene: HGNC:1",
        "a top-level key other than findings changed",
    ),
    "a finding's id": (
        b"id: CHDA:LIT:0009012",
        b"id: CHDA:LIT:0009099",
        "the findings' ids or order changed",
    ),
    "a verified_by set instead of removed": (
        b"claude-sonnet-5-5\n\n  - id: CHDA:LIT:0009011",
        b"claude-sonnet-5-5\n    verified_by: agent:someone-else\n\n  - id: CHDA:LIT:0009011",
        "CHDA:LIT:0009010: verified_by may only be removed",
    ),
}


@pytest.mark.parametrize("old, new, message", CARELESS.values(), ids=CARELESS)
def test_a_backfill_that_moved_anything_but_the_declared_fields_is_refused(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    old: bytes,
    new: bytes,
    message: str,
) -> None:
    """The comparison is the last guard: it is reached only if the setter itself misbehaves, so
    the setter is replaced by one that also changes something it must not."""
    _pubmed(monkeypatch)
    p = _backfill_run(tmp_path, atlas_root, {"CHDA:LIT:0009010": {"variants": [_VARIANT]}})
    real = merge_module.with_fields_set

    def careless(raw: bytes, fid: str, fields: Any, drop: Any = ()) -> bytes:
        out = real(raw, fid, fields, drop)
        assert old in out, old
        return out.replace(old, new)

    monkeypatch.setattr(merge_module, "with_fields_set", careless)
    before = _snapshot(p)
    code, out = _merge(p, stub_atlas)
    assert code == 2
    assert message in out
    assert _snapshot(p) == before


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param((), "declares no fields to backfill", id="no declared field"),
        pytest.param(
            ("approved_by",),
            "declares 'approved_by', which a backfill may not set",
            id="a forbidden field",
        ),
        pytest.param(
            ("variants", "bogus"),
            "declares 'bogus', which is not a Finding field",
            id="an unknown field",
        ),
    ],
)
def test_a_backfill_plan_declaring_nothing_usable_is_refused(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    fields: tuple[str, ...],
    message: str,
) -> None:
    _pubmed(monkeypatch)
    p = _backfill_run(tmp_path, atlas_root, {"CHDA:LIT:0009010": {"variants": [_VARIANT]}}, fields)
    before = _snapshot(p)
    code, out = _merge(p, stub_atlas)
    assert code == 2
    assert message in out
    assert _snapshot(p) == before


def test_append_numbered_refuses_a_repeated_local_id_and_a_counter_behind_the_file() -> None:
    raw = b"gene: HGNC:1\nfindings:\n  - id: CHDA:LIT:0000041\n    publication: PMID:1\n"
    one = {"id": "CHDA:LIT:0900001", "publication": "PMID:5", "shares_probands_with": "unknown"}
    with pytest.raises(RecordError, match="CHDA:LIT:0900001: more than one finding has this id"):
        append_numbered(raw, [one, dict(one)], {"PMID:5": 2020}, 50)
    with pytest.raises(RecordError, match="CHDA:LIT:0000041 is already in the file"):
        append_numbered(raw, [one], {"PMID:5": 2020}, 40)
    with pytest.raises(RecordError, match="no year for PMID:5"):
        append_numbered(raw, [one], {}, 50)


# ---- the helpers merge relies on ---------------------------------------------------------------


def test_a_registry_that_is_not_utf8_is_a_refusal_not_a_traceback(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UTF-16 with a BOM parses as YAML, so only the line edit sees that it is not UTF-8."""
    p = _fresh(tmp_path, atlas_root)
    _pubmed(monkeypatch)
    registry = p.root / "curation/.id_registry.yaml"
    registry.write_bytes(registry.read_text().encode("utf-16"))
    before = _snapshot(p)
    code, out = _merge(p, stub_atlas)
    assert code == 2
    assert "the id registry is not UTF-8 text" in out
    assert _snapshot(p) == before


def test_the_counter_changes_one_line_and_never_falls() -> None:
    raw = b"prefixes:\n  AST: 2\n  LIT: 9001  # keep\n"
    assert read_counter(raw, "LIT") == 9001
    assert with_counter(raw, "LIT", 9004) == b"prefixes:\n  AST: 2\n  LIT: 9004  # keep\n"
    assert with_counter(raw, "LIT", 9001) == raw
    with pytest.raises(RecordError, match="would fall from 9001 to 9000"):
        with_counter(raw, "LIT", 9000)
    flow = b"prefixes: {LIT: 9001}\n"
    assert read_counter(flow, "LIT") == 9001
    with pytest.raises(RecordError, match="not written as exactly one 'LIT: n' line"):
        with_counter(flow, "LIT", 9002)
    with pytest.raises(RecordError, match="no counter for NOPE"):
        read_counter(raw, "NOPE")


def test_deleting_a_findings_last_key_keeps_the_blank_line_that_separated_it() -> None:
    raw = (
        b"gene: HGNC:1\nfindings:\n"
        b"  - id: CHDA:LIT:0000001\n    publication: PMID:1\n    summary: s\n    verified_by: a\n"
        b"\n"
        b"  - id: CHDA:LIT:0000002\n    publication: PMID:2\n    summary: t\n"
    )
    out = with_fields_set(raw, "CHDA:LIT:0000001", {"design": "case_report"}, drop=("verified_by",))
    assert out == (
        b"gene: HGNC:1\nfindings:\n"
        b"  - id: CHDA:LIT:0000001\n    publication: PMID:1\n    design: case_report\n"
        b"    summary: s\n"
        b"\n"
        b"  - id: CHDA:LIT:0000002\n    publication: PMID:2\n    summary: t\n"
    )
    with pytest.raises(RecordError, match="no such finding: CHDA:LIT:0000009"):
        with_fields_set(raw, "CHDA:LIT:0000009", {})
    with pytest.raises(RecordError, match="'bogus' is not a Finding field"):
        with_fields_set(raw, "CHDA:LIT:0000001", {"bogus": 1})


def test_a_triage_change_needs_the_decision_it_names() -> None:
    raw = b"gene: HGNC:1\ntriage:\n  - pmid: PMID:1\n    search: 1\n    decision: pending\n"
    change = TriageChange("PMID:1", "included", "excluded", "other", "n")
    with pytest.raises(RecordError, match="its triage decision is 'pending', not 'included'"):
        with_triage(raw, [change])
    out = with_triage(raw, [TriageChange("PMID:1", "pending", "excluded", "other", "n")])
    assert out == (
        b"gene: HGNC:1\ntriage:\n  - pmid: PMID:1\n    search: 1\n    decision: excluded\n"
        b"    reason: other\n    note: n\n"
    )
    with pytest.raises(RecordError, match="PMID:2 is not in the triage"):
        with_triage(raw, [TriageChange("PMID:2", "pending", "excluded", None, None)])


@pytest.mark.parametrize(
    ("value", "empty"),
    [
        (None, True),
        ([], True),
        ({}, True),
        ([0], False),
        ({"a": 1}, False),
        ("", False),
        (0, False),
        (False, False),
    ],
)
def test_is_empty_is_absent_null_empty_list_or_empty_mapping_and_nothing_else(
    value: Any, empty: bool
) -> None:
    assert is_empty(value) is empty
