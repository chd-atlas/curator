"""The run-directory contract and the atlas's digests, against a small atlas-shaped fixture.

``tests/fixtures/review/`` is laid out as an atlas root (curation/, ontologies/, local/) so
the review page can find a run, the publications and the HPO file the way it will in a real
checkout. Its PMIDs and digests are made up.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from tests.test_atlas import make_stub

from chd_curator.atlas import (
    AtlasError,
    Digests,
    FindingDigests,
    digests,
    plain,
    schema_validator,
)
from chd_curator.run import (
    Comparison,
    JudgeDecision,
    PaperText,
    Quote,
    RunFormatError,
    RunPaths,
    load_comparison,
    load_evidence,
    load_judge,
    load_texts,
)

FIXTURE = Path(__file__).parent / "fixtures" / "review"


def paths(root: Path = FIXTURE) -> RunPaths:
    return RunPaths(root, "TBX5", "r1")


@pytest.fixture
def run_copy(tmp_path: Path) -> RunPaths:
    shutil.copytree(FIXTURE, tmp_path / "root")
    return paths(tmp_path / "root")


def test_the_fixture_is_tracked_by_git() -> None:
    """curator's .gitignore must not swallow ``local/``: the fixture lives under it."""
    listed = subprocess.run(
        ["git", "ls-files", str(FIXTURE)], capture_output=True, text=True, check=True
    ).stdout.split()
    on_disk = [f for f in FIXTURE.rglob("*") if f.is_file()]
    assert len(listed) == len(on_disk) > 0


def test_the_run_paths_are_the_contracts_children() -> None:
    p = paths(Path("/r"))
    assert p.dir == Path("/r/local/literature/runs/TBX5/r1")
    assert [
        p.texts,
        p.evidence,
        p.verify,
        p.comparison,
        p.judge,
        p.quotes,
        p.review_html,
        p.decisions,
    ] == [
        p.dir / n
        for n in (
            "texts.json",
            "evidence",
            "verify.yaml",
            "comparison.json",
            "judge",
            "quotes.json",
            "review.html",
            "decisions",
        )
    ]


def test_texts_load_as_paths_under_the_root() -> None:
    d = "local/literature/"
    assert load_texts(paths()) == (
        PaperText(
            "PMID:90000001",
            "full_text",
            FIXTURE / d / "fulltext/90000001.txt",
            FIXTURE / d / "fulltext/90000001.abstract.txt",
            (FIXTURE / d / "fulltext/90000001.s1.txt",),
            False,
            "",
        ),
        PaperText(
            "PMID:90000002",
            "abstract",
            None,
            FIXTURE / d / "abstracts/90000002.txt",
            (),
            True,
            "the proband's table is in the full text, which is not open access",
        ),
        PaperText(
            "PMID:90000003", "abstract", None, FIXTURE / d / "abstracts/90000003.txt", (), False, ""
        ),
    )


def test_evidence_is_keyed_by_finding_and_merged_across_files(run_copy: RunPaths) -> None:
    evidence = load_evidence(run_copy)
    assert list(evidence) == ["CHDA:LIT:0000901", "CHDA:LIT:0000902"]
    assert evidence["CHDA:LIT:0000901"][0] == Quote(
        "design", "We describe a Holt-Oram family with a TBX5 frameshift", "Abstract"
    )
    assert len(evidence["CHDA:LIT:0000901"]) == 5
    (run_copy.evidence / "batch-02.yaml").write_text(
        'CHDA:LIT:0000903:\n  - {field: design, quote: "q", where: "Abstract"}\n'
    )
    assert load_evidence(run_copy)["CHDA:LIT:0000903"] == (Quote("design", "q", "Abstract"),)
    (run_copy.evidence / "batch-03.yaml").write_text(
        'CHDA:LIT:0000903:\n  - {field: design, quote: "q", where: "Abstract"}\n'
    )
    with pytest.raises(RunFormatError, match=r"batch-03\.yaml.*CHDA:LIT:0000903.*two"):
        load_evidence(run_copy)


def test_comparison_loads_and_a_missing_one_is_empty(run_copy: RunPaths) -> None:
    assert load_comparison(run_copy) == Comparison(
        {
            "CHDA:LIT:0000901": "CHDA:LIT:0000001",
            "CHDA:LIT:0000902": "CHDA:LIT:0000002",
            "CHDA:LIT:0000903": "CHDA:LIT:0000003",
        },
        ("CHDA:LIT:0000901",),
        ("CHDA:LIT:0000903",),
        {"CHDA:LIT:0000902": ("phenotypes", "variants")},
    )
    run_copy.comparison.unlink()
    assert load_comparison(run_copy) == Comparison({}, (), (), {})


def test_judge_decisions_load_with_their_fields(run_copy: RunPaths) -> None:
    judged = load_judge(run_copy)
    assert list(judged) == ["CHDA:LIT:0000902"]
    decisions = judged["CHDA:LIT:0000902"]
    assert decisions[0] == JudgeDecision(
        "phenotypes",
        "HP:0001629 Ventricular septal defect",
        "HP:0001629 Ventricular septal defect, HP:0001631 Atrial septal defect",
        "first",
        "HP:0001629 Ventricular septal defect",
        "a proband with a ventricular septal defect",
        "Abstract",
        "Phenotypes are the carrier's own lesions; "
        "the paper names only the ventricular septal defect.",
    )
    assert [d.field for d in decisions[1:]] == [
        "variant c.100C>T, p.(Arg34Ter) phase",
        "variant c.999A>G, p.(Lys333Glu) carriers",
        "variant c.100C>T, p.(Arg34Ter) as_reported",
        "variant c.250G>A, p.(Gly84Arg) reported.p",
    ]
    assert "Whitespace" in decisions[4].rule
    for empty in (run_copy.evidence, run_copy.judge):
        shutil.rmtree(empty)
    assert load_evidence(run_copy) == {} and load_judge(run_copy) == {}


def test_the_fixture_carries_what_tasks_5_and_6_need() -> None:
    """A quote absent from the paper, a long one, a typographic variant and a missing variant."""
    rows = load_evidence(paths())["CHDA:LIT:0000901"]
    full = (FIXTURE / "local/literature/fulltext/90000001.txt").read_text(encoding="utf-8")
    absent = [q for q in rows if q.quote not in full]
    long = [q for q in rows if len(q.quote.split()) > 30]
    assert [q.field for q in absent] == ["design", "carriers", "variant 1 carriers"]
    assert [q.field for q in long] == ["summary"] and long[0].quote in full
    assert "‐" in full and "ﬁ" in full


@pytest.mark.parametrize(
    ("loader", "relative", "edit", "needle"),
    [
        ("texts", "texts.json", lambda d: d.update(extra=1), r"unexpected key\(s\) \['extra'\]"),
        (
            "texts",
            "texts.json",
            lambda d: d["papers"][0].update(extra=1),
            r"unexpected key\(s\) \['extra'\]",
        ),
        (
            "texts",
            "texts.json",
            lambda d: d["papers"][0].pop("why"),
            r"missing \['why'\]",
        ),
        (
            "texts",
            "texts.json",
            lambda d: d["papers"][0].update(source="scan"),
            "'source' is 'scan'",
        ),
        ("texts", "texts.json", lambda d: d.update(format=2), "format is 2"),
        (
            "comparison",
            "comparison.json",
            lambda d: d.update(extra=1),
            r"unexpected key\(s\) \['extra'\]",
        ),
        (
            "comparison",
            "comparison.json",
            lambda d: d.pop("agreed"),
            r"missing \['agreed'\]",
        ),
        ("comparison", "comparison.json", lambda d: d.update(format=True), "format is True"),
    ],
)
def test_an_unexpected_json_key_names_the_file_and_the_key(
    run_copy: RunPaths, loader: str, relative: str, edit: Any, needle: str
) -> None:
    path = run_copy.dir / relative
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))
    load = {"texts": load_texts, "comparison": load_comparison}[loader]
    with pytest.raises(RunFormatError) as caught:
        load(run_copy)
    message = str(caught.value)
    assert str(path) in message
    assert re.search(needle, message), message


@pytest.mark.parametrize(
    ("subdir", "file", "loader", "old", "new", "needle"),
    [
        (
            "evidence",
            "batch-01.yaml",
            load_evidence,
            'quote: "We describe',
            'extra: x, quote: "We describe',
            "extra",
        ),
        (
            "evidence",
            "batch-01.yaml",
            load_evidence,
            'field: "carriers", quote',
            'field: "carriers", qoute',
            "qoute",
        ),
        (
            "judge",
            "judge-01.decisions.yaml",
            load_judge,
            '    rule: "A phase',
            '    ruel: "A phase',
            "ruel",
        ),
        (
            "judge",
            "judge-01.decisions.yaml",
            load_judge,
            'decision: first\n    value: "trans"',
            'decision: third\n    value: "trans"',
            "third",
        ),
    ],
)
def test_an_unexpected_yaml_key_names_the_file_and_the_key(
    run_copy: RunPaths, subdir: str, file: str, loader: Any, old: str, new: str, needle: str
) -> None:
    path = run_copy.dir / subdir / file
    text = path.read_text()
    assert text.count(old) == 1
    path.write_text(text.replace(old, new))
    with pytest.raises(RunFormatError) as caught:
        loader(run_copy)
    assert str(path) in str(caught.value) and needle in str(caught.value)


def test_a_missing_texts_file_is_an_error(run_copy: RunPaths) -> None:
    run_copy.texts.unlink()
    with pytest.raises(RunFormatError, match=r"texts\.json.*no texts"):
        load_texts(run_copy)


GOOD_DIGESTS = {
    "format": 1,
    "gene": "HGNC:11604",
    "symbol": "TBX5",
    "literature_sha256": "sha256:" + "a" * 64,
    "findings": [
        {
            "id": "CHDA:LIT:0000901",
            "finding_digest": "sha256:" + "1" * 64,
            "shown_digest": "sha256:" + "2" * 64,
            "approved": True,
            "verified": False,
        }
    ],
}


def test_digests_parse_the_stubs_line(stub_atlas: Path, tmp_path: Path) -> None:
    result = digests(tmp_path, "HOS", stub_atlas)
    assert result.gene == "HGNC:11604" and result.symbol == "TBX5"
    assert result.literature_sha256 == "sha256:" + "a" * 64
    assert [f.id for f in result.findings] == [
        "CHDA:LIT:0000901",
        "CHDA:LIT:0000902",
        "CHDA:LIT:0000903",
    ]
    assert result.findings[0] == FindingDigests(
        "CHDA:LIT:0000901", "sha256:" + "1" * 64, "sha256:" + "2" * 64, True, True
    )
    assert result.findings[1].approved is False and result.findings[1].verified is False


def test_digests_of_a_gene_without_a_literature_file_are_refused(
    stub_atlas: Path, tmp_path: Path
) -> None:
    with pytest.raises(AtlasError, match="no literature file"):
        digests(tmp_path, "GATA4", stub_atlas)


def _with_finding(**changes: object) -> dict[str, Any]:
    data = copy.deepcopy(GOOD_DIGESTS)
    data["findings"][0].update(changes)
    return data


def _without_finding_key(key: str) -> dict[str, Any]:
    data = copy.deepcopy(GOOD_DIGESTS)
    del data["findings"][0][key]
    return data


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (GOOD_DIGESTS | {"format": 2}, "format 2"),
        (GOOD_DIGESTS | {"format": 1.0}, r"answered format 1\.0"),
        (GOOD_DIGESTS | {"x": 1}, r"extra \['x'\]"),
        (GOOD_DIGESTS | {"findings": {}}, "findings is not a list"),
        (GOOD_DIGESTS | {"literature_sha256": 5}, "literature_sha256 is not a non-empty"),
        (_without_finding_key("shown_digest"), r"missing \['shown_digest'\]"),
        (
            _with_finding(extra=1),
            r"finding in the atlas's answer has unexpected keys: extra \['extra'\]",
        ),
        (_with_finding(approved=1), "approved is not a boolean"),
        (_with_finding(verified="no"), "verified is not a boolean"),
        (_with_finding(shown_digest=None), "shown_digest is not a non-empty string"),
    ],
)
def test_a_malformed_digests_answer_is_refused(
    tmp_path: Path, answer: dict[str, Any], expected: str
) -> None:
    stub, _ = make_stub(tmp_path, json.dumps(answer))
    with pytest.raises(AtlasError, match=expected):
        digests(tmp_path, "TBX5", stub)
    assert digests(tmp_path, "TBX5", make_stub(tmp_path, json.dumps(GOOD_DIGESTS))[0]) == Digests(
        "HGNC:11604",
        "TBX5",
        "sha256:" + "a" * 64,
        (
            FindingDigests(
                "CHDA:LIT:0000901", "sha256:" + "1" * 64, "sha256:" + "2" * 64, True, False
            ),
        ),
    )


def test_the_stub_answers_validate_with_a_summary_line(stub_atlas: Path, tmp_path: Path) -> None:
    run = subprocess.run(
        [str(stub_atlas), "validate", "--root", str(tmp_path)], capture_output=True, text=True
    )
    assert run.returncode == 0
    assert run.stdout.rstrip("\n").splitlines()[-1] == "0 error(s), 3 warning(s)"


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
    data = plain(YAML(typ="safe").load((FIXTURE / relative).read_text(encoding="utf-8")))
    errors = [e.message for e in schema_validator(atlas_root, schema).iter_errors(data)]
    assert errors == []


@pytest.mark.parametrize(
    ("loader", "subdir", "stray"),
    [
        (load_evidence, "evidence", "batch-02.yml"),
        (load_evidence, "evidence", "notes.txt"),
        (load_judge, "judge", "judge-02.decisions.yml"),
        (load_judge, "judge", "judge-02.yaml"),
    ],
)
def test_a_misnamed_file_in_evidence_or_judge_is_refused_not_skipped(
    run_copy: RunPaths, loader: Any, subdir: str, stray: str
) -> None:
    path = run_copy.dir / subdir / stray
    path.write_text("CHDA:LIT:0000903: []\n")
    with pytest.raises(RunFormatError) as caught:
        loader(run_copy)
    assert str(path) in str(caught.value)


def test_a_judged_finding_in_two_files_is_an_error(run_copy: RunPaths) -> None:
    first = run_copy.judge / "judge-01.decisions.yaml"
    second = run_copy.judge / "judge-02.decisions.yaml"
    shutil.copy(first, second)
    with pytest.raises(RunFormatError, match=r"judge-02\.decisions\.yaml.*CHDA:LIT:0000902.*two"):
        load_judge(run_copy)
