"""The phase 2 run files: loaders return the fixture's literal contents, writers round-trip
through their loaders, and every refusal names the file and the key. The fixture is described
in ``run_fixture.py``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from chd_curator.atlas import plain, schema_validator
from chd_curator.notation import NotationChange
from chd_curator.run import (
    AssembleRecord,
    Batch,
    BatchPlan,
    Comparison,
    MergeRecord,
    PaperOutcome,
    Quote,
    Replacement,
    RunFormatError,
    TriageChange,
    load_assemble,
    load_backfill_pass,
    load_batches,
    load_comparison,
    load_evidence,
    load_evidence_file,
    load_judge,
    load_merge,
    load_papers,
    load_pass,
    write_assemble,
    write_batches,
    write_comparison,
    write_evidence,
    write_merge,
)
from run_fixture import RUN_FIXTURE, copy_run_fixture, run_paths

R = RUN_FIXTURE / "local/literature/runs/TBX5/r1"


def test_the_fixture_is_tracked_by_git() -> None:
    import subprocess

    listed = subprocess.run(
        ["git", "ls-files", str(RUN_FIXTURE)], capture_output=True, text=True, check=True
    ).stdout.split()
    assert len(listed) == len([f for f in RUN_FIXTURE.rglob("*") if f.is_file()]) > 0


def test_the_new_paths_are_the_contracts_children() -> None:
    p = run_paths(Path("/r"))
    assert [p.batches_json, p.batches_dir, p.extract, p.merge, p.judging, p.assemble, p.verify] == [
        Path("/r/local/literature/runs/TBX5/r1") / n
        for n in ("batches.json", "batches", "extract", "merge.json", "judging", "assemble.json")
    ] + [Path("/r/local/literature/runs/TBX5/r1/verify")]


def test_the_fixture_loads_as_literal_objects() -> None:
    p = run_paths()
    assert load_batches(p) == BatchPlan(
        "new",
        (),
        400000,
        (
            Batch("01", ("PMID:91000001",), (), 150),
            Batch("02", ("PMID:91000002",), (), 90),
        ),
    )
    assert load_comparison(p) == Comparison(
        {"CHDA:LIT:0901001": "CHDA:LIT:0801001", "CHDA:LIT:0901002": "CHDA:LIT:0801002"},
        ("CHDA:LIT:0901001",),
        (),
        {"CHDA:LIT:0901002": ("phenotypes",)},
        (),
        ("CHDA:LIT:0801003",),
    )
    assert load_papers(R / "extract/batch-01.papers.json") == (
        PaperOutcome("PMID:91000001", "family_linkage", "full text and abstract", None),
    )
    assert load_pass(R / "verify/batch-02.findings.yaml") == [
        {
            "id": "CHDA:LIT:0801002",
            "publication": "PMID:91000002",
            "design": "case_report",
            "summary": "A synthetic finding.",
        },
        {
            "id": "CHDA:LIT:0801003",
            "publication": "PMID:91000002",
            "design": "functional_only",
            "summary": "A second synthetic finding the first reader did not report.",
        },
    ]
    assert load_evidence_file(R / "extract/batch-02.evidence.yaml") == {
        "CHDA:LIT:0901002": (Quote("design", "Synthetic case report of a proband", "Abstract"),)
    }
    assert list(load_judge(p)) == ["CHDA:LIT:0901002"]
    assert load_merge(p) is None and load_assemble(p) is None


def test_papers_accept_both_shapes_and_refuse_everything_else(tmp_path: Path) -> None:
    f = tmp_path / "batch-01.papers.json"
    f.write_text(
        json.dumps(
            [
                {"pmid": "PMID:1", "study_type": None, "reading": "abstract"},
                {"pmid": "PMID:2", "exclude": {"reason": "not_chd", "note": "a mouse"}},
            ]
        )
    )
    assert load_papers(f) == (
        PaperOutcome("PMID:1", None, "abstract", None),
        PaperOutcome("PMID:2", None, "", ("not_chd", "a mouse")),
    )
    one = {"pmid": "PMID:1", "study_type": "case_report", "reading": "x"}
    gone = {"pmid": "PMID:1", "exclude": {"reason": "r", "note": "n"}}
    bad: list[tuple[Any, str]] = [
        ({"pmid": "PMID:1"}, "is not a list"),
        ([one | {"extra": 1}], "extra"),
        ([{"pmid": "PMID:1", "study_type": "x"}], "reading"),
        ([one, one], r"PMID:1 appears twice"),
        ([one | {"pmid": "1"}], "'1'"),
        ([one | {"reading": ""}], "'reading' is empty"),
        ([one | {"study_type": ""}], "'study_type' is empty"),
        ([gone | {"extra": 1}], "extra"),
        ([{"pmid": "PMID:1", "exclude": {"reason": "r"}}], r"missing \['note'\]"),
        ([{"pmid": "PMID:1", "exclude": {"reason": "", "note": "n"}}], "'reason' is empty"),
        ([{"pmid": "PMID:1", "exclude": {"reason": "r", "note": ""}}], "'note' is empty"),
        ([one, gone], "PMID:1 appears twice"),
    ]
    for content, expected in bad:
        f.write_text(json.dumps(content))
        with pytest.raises(RunFormatError, match=expected) as caught:
            load_papers(f)
        assert str(f) in str(caught.value)


@pytest.mark.parametrize(
    ("loader", "text", "expected"),
    [
        (load_pass, "findings: []\nextra: 1\n", "extra"),
        (load_pass, "findings: {}\n", "'findings' is not a list"),
        (load_pass, "findings: [x]\n", "not a mapping"),
        (load_pass, "findings: [{design: a}]\n", "'id' is None"),
        (load_pass, "findings: [{id: 'CHDA:LIT:12'}]\n", "'CHDA:LIT:12'"),
        (load_pass, "findings: [{id: 'CHDA:LIT:0901001'}, {id: 'CHDA:LIT:0901001'}]\n", "twice"),
        (load_backfill_pass, "findings: []\n", "not a mapping"),
        (load_backfill_pass, "findings: {'CHDA:LIT:1': {a: 1}}\n", "'CHDA:LIT:1'"),
        (load_backfill_pass, "findings: {'CHDA:LIT:0000901': {}}\n", "non-empty mapping"),
        (load_backfill_pass, "findings: {'CHDA:LIT:0000901': [a]}\n", "non-empty mapping"),
        (load_backfill_pass, "findings: {}\nextra: 1\n", "extra"),
    ],
)
def test_an_agent_findings_file_that_breaks_its_shape_is_refused(
    tmp_path: Path, loader: Any, text: str, expected: str
) -> None:
    f = tmp_path / "pass.yaml"
    f.write_text(text)
    with pytest.raises(RunFormatError, match=expected) as caught:
        loader(f)
    assert str(f) in str(caught.value)


def test_a_backfill_pass_returns_plain_values_with_dates_as_iso_strings(tmp_path: Path) -> None:
    f = tmp_path / "b.yaml"
    f.write_text("findings:\n  CHDA:LIT:0000901:\n    when: 2026-11-19\n    n: [1, 2]\n")
    assert load_backfill_pass(f) == {"CHDA:LIT:0000901": {"when": "2026-11-19", "n": [1, 2]}}
    assert load_pass(_write(tmp_path, "findings: [{id: 'CHDA:LIT:0000901', d: 2026-11-19}]\n")) == [
        {"id": "CHDA:LIT:0000901", "d": "2026-11-19"}
    ]


def _write(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "x.yaml"
    f.write_text(text)
    return f


MERGE = MergeRecord(
    "reread",
    {"CHDA:LIT:0901001": "CHDA:LIT:0009002"},
    ("PMID:91000002",),
    (
        TriageChange("PMID:91000002", "none", "included", None, None),
        TriageChange("PMID:91000003", "included", "excluded", "not_chd", "a mouse paper"),
    ),
    (Replacement("PMID:91000001", ("CHDA:LIT:0009001",), ("CHDA:LIT:0009002",)),),
)
ASSEMBLE = AssembleRecord(
    ("CHDA:LIT:0009001",),
    {"CHDA:LIT:0701001": "CHDA:LIT:0009003"},
    (
        NotationChange(
            "CHDA:LIT:0009003", 1, "reported.p", "p.Arg34Ter", "p.(Arg34Ter)", ("a", "b")
        ),
        NotationChange("CHDA:LIT:0009003", None, "summary", "é", "e", ()),
    ),
    ("CHDA:LIT:0009003",),
    "agent:literature-verify/claude-opus-5-5",
)
COMPARISON = Comparison(
    {"CHDA:LIT:0901001": "CHDA:LIT:0801001"},
    ("CHDA:LIT:0901001",),
    ("CHDA:LIT:0901002",),
    {"CHDA:LIT:0901003": ("phenotypes", "variants")},
    ("CHDA:LIT:0901004",),
    ("CHDA:LIT:0801005",),
)
PLAN = BatchPlan(
    "backfill", ("variants", "summary"), 1000, (Batch("01", ("PMID:1",), ("CHDA:LIT:0000901",), 0),)
)


def test_each_writer_round_trips_through_its_loader(tmp_path: Path) -> None:
    p = copy_run_fixture(tmp_path)
    write_merge(p, MERGE)
    write_assemble(p, ASSEMBLE)
    write_comparison(p, COMPARISON)
    write_batches(p, PLAN)
    assert load_merge(p) == MERGE
    assert load_assemble(p) == ASSEMBLE
    assert load_comparison(p) == COMPARISON
    assert load_batches(p) == PLAN
    # The bytes are the contract: sorted keys, indent 1, a trailing newline, no ASCII escaping.
    assert p.assemble.read_text(encoding="utf-8").startswith(
        '{\n "added": {\n  "CHDA:LIT:0701001": "CHDA:LIT:0009003"\n },\n "format": 1,'
    )
    assert '"after": "e"' in p.assemble.read_text(encoding="utf-8")
    assert '"before": "é"' in p.assemble.read_text(encoding="utf-8")
    assert p.assemble.read_text(encoding="utf-8").endswith("}\n")


def test_a_writer_creates_the_missing_directory(tmp_path: Path) -> None:
    p = run_paths(tmp_path)
    write_batches(p, PLAN)
    write_evidence(p, "01", {"CHDA:LIT:0901001": [Quote("design", "q", "Abstract")]})
    assert p.batches_json.is_file() and (p.evidence / "batch-01.yaml").is_file()


def test_evidence_written_for_a_batch_round_trips_awkward_values(tmp_path: Path) -> None:
    p = run_paths(tmp_path)
    quotes = {
        "CHDA:LIT:0901001": [
            Quote("variant 1 zygosity", "a: b, c [d] # e", "Table 2, row 3: the proband"),
            Quote("summary", 'Müller\'s "gene" — ß', "Abstract"),
        ],
        "CHDA:LIT:0901002": [],
    }
    write_evidence(p, "07", quotes)
    assert "ß" in (p.evidence / "batch-07.yaml").read_text(encoding="utf-8")
    expected = {k: tuple(v) for k, v in quotes.items()}
    assert load_evidence_file(p.evidence / "batch-07.yaml") == expected
    assert load_evidence(p) == expected


def test_an_evidence_key_that_is_not_a_finding_id_is_refused(tmp_path: Path) -> None:
    p = run_paths(tmp_path)
    p.evidence.mkdir(parents=True)
    (p.evidence / "batch-01.yaml").write_text(
        "CHDA:LIT:12:\n  - {field: design, quote: q, where: Abstract}\n"
    )
    with pytest.raises(RunFormatError, match=r"batch-01\.yaml.*'CHDA:LIT:12'"):
        load_evidence(p)
    p.judge.mkdir()
    (p.judge / "judge-01.decisions.yaml").write_text("bogus: []\n")
    with pytest.raises(RunFormatError, match=r"judge-01\.decisions\.yaml.*'bogus'"):
        load_judge(p)


def _edit(path: Path, change: Any) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def _plan() -> dict[str, Any]:
    return json.loads((R / "batches.json").read_text())


@pytest.mark.parametrize(
    ("file", "change", "expected"),
    [
        ("batches.json", lambda d: d.update(extra=1), "extra"),
        ("batches.json", lambda d: d.pop("fields"), r"missing \['fields'\]"),
        ("batches.json", lambda d: d.update(format=2), "format is 2"),
        ("batches.json", lambda d: d.update(mode="fresh"), "'mode' is 'fresh'"),
        ("batches.json", lambda d: d.update(budget_bytes=True), "'budget_bytes'"),
        ("batches.json", lambda d: d.update(budget_bytes=0), "'budget_bytes'"),
        ("batches.json", lambda d: d["batches"][0].update(id="1"), "'1'"),
        ("batches.json", lambda d: d["batches"][1].update(id="01"), "batch 01 appears twice"),
        ("batches.json", lambda d: d["batches"][0].update(text_bytes=-1), "'text_bytes'"),
        ("batches.json", lambda d: d["batches"][0].update(papers=["91"]), "'91'"),
        ("batches.json", lambda d: d["batches"][0].update(findings=["x"]), "'x'"),
        ("batches.json", lambda d: d["batches"][0].update(extra=1), "extra"),
        ("comparison.json", lambda d: d.update(only_first=[1]), "'only_first'"),
        ("comparison.json", lambda d: d.update(extra=[]), "extra"),
    ],
)
def test_an_unknown_or_malformed_key_in_a_run_file_is_refused_naming_file_and_key(
    tmp_path: Path, file: str, change: Any, expected: str
) -> None:
    p = copy_run_fixture(tmp_path)
    target = p.dir / file
    _edit(target, change)
    with pytest.raises(RunFormatError, match=expected) as caught:
        load_batches(p) if file == "batches.json" else load_comparison(p)
    assert str(target) in str(caught.value)


def test_comparison_without_the_optional_keys_loads_them_empty(tmp_path: Path) -> None:
    p = copy_run_fixture(tmp_path)
    _edit(p.comparison, lambda d: (d.pop("only_first"), d.pop("only_second")))
    c = load_comparison(p)
    assert (c.only_first, c.only_second) == ((), ())


def _merge_base() -> dict[str, Any]:
    return {
        "format": 1,
        "mode": "new",
        "ids": {"CHDA:LIT:0901001": "CHDA:LIT:0009002"},
        "publications_added": ["PMID:1"],
        "triage": [{"pmid": "PMID:1", "before": "a", "after": "b", "reason": None, "note": None}],
        "replaces": [{"pmid": "PMID:1", "old": [], "new": ["CHDA:LIT:0009002"]}],
    }


def _assemble_base() -> dict[str, Any]:
    return {
        "format": 1,
        "replaced": [],
        "added": {"CHDA:LIT:0701001": "CHDA:LIT:0009003"},
        "notation": [
            {
                "finding": "CHDA:LIT:0009003",
                "variant": None,
                "field": "f",
                "before": "a",
                "after": "b",
                "rules": ["r"],
            }
        ],
        "verified": [],
        "verifier": None,
    }


@pytest.mark.parametrize(
    ("which", "change", "expected"),
    [
        ("merge", lambda d: d.update(extra=1), "extra"),
        ("merge", lambda d: d.pop("replaces"), r"missing \['replaces'\]"),
        ("merge", lambda d: d.update(format=2), "format is 2"),
        ("merge", lambda d: d.update(mode="x"), "'mode' is 'x'"),
        ("merge", lambda d: d.update(ids={"a": "CHDA:LIT:0009002"}), "'a'"),
        ("merge", lambda d: d.update(ids={"CHDA:LIT:0901001": "b"}), "'b'"),
        ("merge", lambda d: d.update(publications_added=["1"]), "'1'"),
        ("merge", lambda d: d["triage"][0].pop("note"), r"missing \['note'\]"),
        ("merge", lambda d: d["triage"][0].update(reason=3), "'reason'"),
        ("merge", lambda d: d["replaces"][0].update(old=["x"]), "'x'"),
        ("merge", lambda d: d["replaces"][0].update(extra=1), "extra"),
        ("assemble", lambda d: d.update(extra=1), "extra"),
        ("assemble", lambda d: d.pop("verifier"), r"missing \['verifier'\]"),
        ("assemble", lambda d: d.update(verifier=3), "'verifier'"),
        ("assemble", lambda d: d.update(added={"x": "CHDA:LIT:0009003"}), "'x'"),
        ("assemble", lambda d: d.update(replaced=["x"]), "'x'"),
        ("assemble", lambda d: d["notation"][0].update(variant=True), "'variant'"),
        ("assemble", lambda d: d["notation"][0].update(variant=-1), "'variant'"),
        ("assemble", lambda d: d["notation"][0].update(rules="r"), "'rules'"),
        ("assemble", lambda d: d["notation"][0].update(finding="f"), "'f'"),
        ("assemble", lambda d: d["notation"][0].update(extra=1), "extra"),
    ],
)
def test_merge_and_assemble_refuse_a_misshapen_file_naming_file_and_key(
    tmp_path: Path, which: str, change: Any, expected: str
) -> None:
    p = run_paths(tmp_path)
    p.dir.mkdir(parents=True)
    target = p.merge if which == "merge" else p.assemble
    data = _merge_base() if which == "merge" else _assemble_base()
    target.write_text(json.dumps(data))
    # The base is valid: it loads, so the refusal below is the edit's.
    (load_merge if which == "merge" else load_assemble)(p)
    change(data)
    target.write_text(json.dumps(data))
    with pytest.raises(RunFormatError, match=expected) as caught:
        (load_merge if which == "merge" else load_assemble)(p)
    assert str(target) in str(caught.value)


@pytest.mark.parametrize(
    ("relative", "schema"),
    [
        ("curation/literature/TBX5.yaml", "gene_literature_file"),
        ("curation/publications.yaml", "publication_file"),
    ],
)
def test_the_fixture_records_validate_against_the_atlas_schema(
    atlas_root: Path, relative: str, schema: str
) -> None:
    data = plain(YAML(typ="safe").load((RUN_FIXTURE / relative).read_text(encoding="utf-8")))
    assert [e.message for e in schema_validator(atlas_root, schema).iter_errors(data)] == []
