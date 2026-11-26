"""The phase 2 run files: loaders return the fixture's literal contents, writers round-trip
through their loaders, and every refusal names the file and the key. The fixture is described
in ``run_fixture.py``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from chd_curator.atlas import digests, plain, schema_validator
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
    load_texts,
    write_assemble,
    write_batches,
    write_comparison,
    write_evidence,
    write_merge,
)
from run_fixture import (
    JUDGE_FIXTURE,
    RUN_FIXTURE,
    atlas_digest_root,
    copy_judge_round,
    copy_run_fixture,
    run_paths,
)

R = RUN_FIXTURE / "local/literature/runs/TBX5/r1"


def test_the_fixture_is_tracked_by_git() -> None:
    import subprocess

    for fixture in (RUN_FIXTURE, JUDGE_FIXTURE):
        listed = subprocess.run(
            ["git", "ls-files", str(fixture)], capture_output=True, text=True, check=True
        ).stdout.split()
        assert len(listed) == len([f for f in fixture.rglob("*") if f.is_file()]) > 0


def test_the_new_paths_are_the_contracts_children() -> None:
    p = run_paths(Path("/r"))
    assert [p.batches_json, p.batches_dir, p.extract, p.merge, p.judging, p.assemble, p.verify] == [
        Path("/r/local/literature/runs/TBX5/r1") / n
        for n in ("batches.json", "batches", "extract", "merge.json", "judging", "assemble.json")
    ] + [Path("/r/local/literature/runs/TBX5/r1/verify")]


def test_the_fixture_loads_as_literal_objects(tmp_path: Path) -> None:
    p = run_paths()
    assert load_batches(p) == BatchPlan(
        "new",
        (),
        354,
        (
            Batch("01", ("PMID:91000002",), (), 283),
            Batch("02", ("PMID:91000003", "PMID:91000004"), (), 354),
        ),
    )
    assert [t.pmid for t in load_texts(p)] == [f"PMID:9100000{n}" for n in (1, 2, 3, 4)]
    assert load_papers(R / "extract/batch-02.papers.json") == (
        PaperOutcome("PMID:91000003", "case_report", "abstract only", None),
        PaperOutcome(
            "PMID:91000004",
            None,
            "",
            ("not_human_genetics", "A mouse model; no human carrier is reported."),
        ),
    )
    first, second = load_pass(R / "extract/batch-01.findings.yaml")
    assert (first["id"], first["design"], first["screened"]) == (
        "CHDA:LIT:0901001",
        "case_report",
        1,
    )
    assert (second["id"], second["design"], second["screened"]) == (
        "CHDA:LIT:0901002",
        "family_segregation",
        1,
    )
    assert [f["id"] for f in load_pass(R / "verify/batch-01.findings.yaml")] == [
        "CHDA:LIT:0801001",
        "CHDA:LIT:0801002",
        "CHDA:LIT:0801003",
    ]
    assert load_evidence_file(R / "extract/batch-02.evidence.yaml") == {
        "CHDA:LIT:0902001": (
            Quote("design", "Exome sequencing of a fetus with an atrial septal defect", "Abstract"),
            Quote(
                "variant 1 as_reported",
                "a heterozygous TBX5 splice-donor variant, c.700+1G>A",
                "Abstract",
            ),
        )
    }
    # The run stops just before merge: nothing a later command writes is in it.
    assert sorted(f.name for f in p.dir.iterdir()) == [
        "batches.json",
        "extract",
        "texts.json",
        "verify",
    ]
    assert load_merge(p) is None and load_assemble(p) is None and load_judge(p) == {}
    assert load_comparison(p) == Comparison({}, (), (), {})


def test_the_judge_round_copies_into_a_run_and_loads_under_the_final_ids(tmp_path: Path) -> None:
    p = copy_run_fixture(tmp_path)
    copy_judge_round(p)
    assert {k: [d.decision for d in v] for k, v in load_judge(p).items()} == {
        "CHDA:LIT:0009003": ["second"],
        "CHDA:LIT:0701001": ["second"],
        "CHDA:LIT:0801003": ["second"],
    }
    assert [f["id"] for f in load_pass(p.judging / "judge-01.findings.yaml")] == [
        "CHDA:LIT:0009003",
        "CHDA:LIT:0701001",
    ]


def _passes() -> list[Path]:
    return sorted(
        [*R.glob("*/*.findings.yaml"), *(JUDGE_FIXTURE / "judging").glob("*.findings.yaml")]
    )


@pytest.mark.parametrize("path", _passes(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_agent_finding_in_the_fixture_is_a_complete_unapproved_schema_valid_finding(
    atlas_root: Path, path: Path
) -> None:
    findings = load_pass(path)
    assert findings
    schema = schema_validator(atlas_root, "gene_literature_file")
    for finding in findings:
        wrapped = {
            "gene": "HGNC:11604",
            "searches": [
                {
                    "id": 1,
                    "source": "pubmed",
                    "query": "q",
                    "run_on": "2026-11-19",
                    "hits": 1,
                    "new_hits": 1,
                }
            ],
            "triage": [],
            "findings": [finding],
        }
        assert [e.message for e in schema.iter_errors(wrapped)] == []
        assert finding["extracted_by"].startswith("agent:literature-")
        assert not {"verified_by", "approved_by", "approved_on", "approved_digest"} & set(finding)


def test_the_two_readers_differ_exactly_where_the_comparison_cases_need(tmp_path: Path) -> None:
    ex = {f["id"]: f for f in load_pass(R / "extract/batch-01.findings.yaml")}
    ve = {f["id"]: f for f in load_pass(R / "verify/batch-01.findings.yaml")}
    ex |= {f["id"]: f for f in load_pass(R / "extract/batch-02.findings.yaml")}
    ve |= {f["id"]: f for f in load_pass(R / "verify/batch-02.findings.yaml")}

    def differing(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
        return sorted(
            k
            for k in a.keys() | b.keys()
            if k not in ("id", "extracted_by") and a.get(k) != b.get(k)
        )

    assert differing(ex["CHDA:LIT:0901001"], ve["CHDA:LIT:0801001"]) == []
    assert differing(ex["CHDA:LIT:0901002"], ve["CHDA:LIT:0801002"]) == ["screened"]
    assert differing(ex["CHDA:LIT:0902001"], ve["CHDA:LIT:0802001"]) == ["variants"]
    v1, v2 = ex["CHDA:LIT:0902001"]["variants"][0], ve["CHDA:LIT:0802001"]["variants"][0]
    assert {k for k in v1 if v1[k] != v2[k]} == {"as_reported"}
    assert "CHDA:LIT:0801003" in ve and ve["CHDA:LIT:0801003"]["publication"] == "PMID:91000002"
    assert len({f["extracted_by"] for f in ve.values()}) == 1
    assert {f["extracted_by"] for f in ve.values()} != {f["extracted_by"] for f in ex.values()}


def test_every_quote_in_the_fixture_is_a_verbatim_substring_of_the_text_it_cites() -> None:
    texts = {t.pmid: t for t in load_texts(run_paths())}
    for side in ("extract", "verify"):
        for ev in sorted((R / side).glob("*.evidence.yaml")):
            by_id = {
                f["id"]: f["publication"]
                for f in load_pass(ev.with_name(ev.name.replace("evidence", "findings")))
            }
            for finding_id, quotes in load_evidence_file(ev).items():
                t = texts[by_id[finding_id]]
                body = " ".join(
                    f.read_text(encoding="utf-8")
                    for f in [t.text, t.abstract, *t.supplements]
                    if f is not None
                )
                for quote in quotes:
                    assert quote.quote in body, (ev.name, finding_id, quote.quote)


def test_the_atlas_reads_the_curated_finding_as_approved_and_not_void(
    tmp_path: Path, atlas_root: Path, atlas_cli: Path
) -> None:
    """The approval digest is the atlas's own: see ``run_fixture.py`` for the command."""
    result = digests(atlas_digest_root(tmp_path, atlas_root), "TBX5", atlas_cli)
    assert [(f.id, f.approved, f.approval_void) for f in result.findings] == [
        ("CHDA:LIT:0009001", True, False)
    ]


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
    {"CHDA:LIT:0009001": "a" * 64, "CHDA:LIT:0009003": "0123456789abcdef" * 4},
    {"CHDA:LIT:0009001": "sha256:" + "b" * 64, "CHDA:LIT:0009003": None},
    {"CHDA:LIT:0801005": "neither"},
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
        '{\n "added": {\n  "CHDA:LIT:0701001": "CHDA:LIT:0009003"\n },\n "approvals": {\n'
        '  "CHDA:LIT:0009001": "sha256:'
        + "b" * 64
        + '",\n  "CHDA:LIT:0009003": null\n },\n "blind_only": {\n  "CHDA:LIT:0801005": "neither"\n'
        + ' },\n "format": 1,'
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
    write_comparison(p, COMPARISON)
    target = p.dir / file
    _edit(target, change)
    with pytest.raises(RunFormatError, match=expected) as caught:
        load_batches(p) if file == "batches.json" else load_comparison(p)
    assert str(target) in str(caught.value)


def test_comparison_without_the_optional_keys_loads_them_empty(tmp_path: Path) -> None:
    p = copy_run_fixture(tmp_path)
    write_comparison(p, COMPARISON)
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
        "judged": {"CHDA:LIT:0009003": "0" * 64},
        "approvals": {"CHDA:LIT:0009003": None},
        "blind_only": {},
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
        ("assemble", lambda d: d.pop("judged"), r"missing \['judged'\]"),
        ("assemble", lambda d: d.update(judged=[]), "'judged' is not an object"),
        ("assemble", lambda d: d.pop("approvals"), r"missing \['approvals'\]"),
        ("assemble", lambda d: d.update(approvals=[]), "'approvals' is not an object"),
        ("assemble", lambda d: d.update(approvals={}), "do not name the same findings"),
        ("assemble", lambda d: d.pop("blind_only"), r"missing \['blind_only'\]"),
        ("assemble", lambda d: d.update(blind_only=[]), "'blind_only' is not an object"),
        ("assemble", lambda d: d.update(blind_only={"x": "second"}), "'x'"),
        ("assemble", lambda d: d.update(blind_only={"CHDA:LIT:0801005": "both"}), "'both'"),
        ("assemble", lambda d: d.update(approvals={"x": None}), "'x'"),
        ("assemble", lambda d: d.update(approvals={"CHDA:LIT:0009003": 7}), "7"),
        ("assemble", lambda d: d.update(judged={"x": "0" * 64}), "'x'"),
        ("assemble", lambda d: d.update(judged={"CHDA:LIT:0009003": "abc"}), "'abc'"),
        ("assemble", lambda d: d.update(judged={"CHDA:LIT:0009003": "A" * 64}), "'AAAA"),
        ("assemble", lambda d: d.update(judged={"CHDA:LIT:0009003": 7}), "7"),
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
