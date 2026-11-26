"""``chd-curator assemble``: the literal outcome on the shared run fixture (merged, compared,
then a judge round), the notation and verification it adds, a fix-round re-run, the backfill
mode, and every refusal.

Expected values are written out, never rebuilt from the module's own constants. A refusal test
compares the bytes of every file under the root before and after, because an assemble that wrote
``assemble.json`` and then refused would leave a run the next assemble reads as part-way done.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator import assemble as assemble_module
from chd_curator.assemble import AssembleError, assemble_run, commit
from chd_curator.atlas import Digests, FindingDigests
from chd_curator.cli import app
from chd_curator.quotes import check_quotes
from chd_curator.records import (
    RecordError,
    dump_bytes,
    load_round_trip,
    with_findings_replaced,
    with_triage,
    with_verified,
)
from chd_curator.review_data import ReviewDataError, Reviewer, build_review_data
from chd_curator.run import (
    AssembleRecord,
    RunFormatError,
    RunPaths,
    TriageChange,
    load_assemble,
    load_judge,
    write_assemble,
)
from chd_curator.write import WriteRefused
from run_fixture import JUDGE_FIXTURE, copy_judge_round
from test_compare import _compare
from test_merge import _fresh, _merge, _pubmed

runner = CliRunner()
YAML_SAFE = YAML(typ="safe")
VERIFIER = "agent:literature-verify/claude-opus-5-5"
ADDED = "CHDA:LIT:0701001"
ADDED_FINAL = "CHDA:LIT:0009005"
JUDGED = "CHDA:LIT:0009003"

EXPECTED = Path(__file__).parent / "fixtures" / "run-judge-expected"
ASSEMBLED_FILE = (EXPECTED / "assembled-TBX5.yaml").read_text()


def _assemble(p: RunPaths, stub: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["assemble", "TBX5", "--run", "r1", "--root", str(p.root), "--atlas-cli", str(stub), *args],
        catch_exceptions=False,
    )
    return result.exit_code, result.output


def _ready(
    tmp_path: Path, atlas_root: Path, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> RunPaths:
    """The fixture merged, compared, and given its judge round: just before ``assemble``."""
    p = _fresh(tmp_path, atlas_root)
    _pubmed(monkeypatch)
    assert _merge(p, stub)[0] == 0
    assert _compare(p, stub)[0] == 0
    copy_judge_round(p)
    return p


def _snapshot(p: RunPaths) -> dict[str, str]:
    return {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(p.root.rglob("*"))
        if f.is_file()
    }


def _lit(p: RunPaths) -> Path:
    return p.root / "curation" / "literature" / "TBX5.yaml"


def _findings(p: RunPaths) -> dict[str, Any]:
    return {f["id"]: f for f in YAML_SAFE.load(_lit(p).read_text())["findings"]}


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, (path, old, text.count(old))
    path.write_text(text.replace(old, new))


def _judge1(p: RunPaths) -> Path:
    return p.judging / "judge-01.findings.yaml"


def _expect_refused(p: RunPaths, stub: Path, message: str, *args: str) -> None:
    before = _snapshot(p)
    code, out = _assemble(p, stub, *args)
    assert code == 2, out
    assert message in out, out
    assert _snapshot(p) == before


def test_assemble_on_the_fixture_gives_the_literal_file_record_and_summary(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    code, out = _assemble(p, stub_atlas)
    assert code == 0, out
    assert out == (
        "replaced 1 judged findings, added 1 (CHDA:LIT:0009005), fixed 0 names by rule, "
        "marked 2 verified\n"
    )
    assert _lit(p).read_text() == ASSEMBLED_FILE
    assert (
        p.root / "curation" / ".id_registry.yaml"
    ).read_text() == "prefixes:\n  AST: 2\n  LIT: 9005\n"
    assert (p.dir / "assemble.json").read_text() == (
        "{\n"
        ' "added": {\n  "CHDA:LIT:0701001": "CHDA:LIT:0009005"\n },\n'
        ' "format": 1,\n'
        ' "notation": [],\n'
        ' "replaced": [\n  "CHDA:LIT:0009003"\n ],\n'
        ' "verified": [\n  "CHDA:LIT:0009002",\n  "CHDA:LIT:0009004"\n ],\n'
        ' "verifier": "agent:literature-verify/claude-opus-5-5"\n'
        "}\n"
    )
    found = _findings(p)
    assert {fid: f.get("verified_by") for fid, f in found.items()} == {
        "CHDA:LIT:0009001": VERIFIER,  # approved before this run, untouched
        "CHDA:LIT:0009002": VERIFIER,  # agreed
        JUDGED: None,  # judged
        "CHDA:LIT:0009004": VERIFIER,  # span only
        ADDED_FINAL: None,  # added by the judge
    }
    assert found[JUDGED]["screened"] == 2
    assert "approved_by" not in found[ADDED_FINAL] and "approved_by" not in found[JUDGED]


def test_a_second_assemble_changes_no_byte_and_numbers_nothing_again(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    first = _assemble(p, stub_atlas)
    after_first = _snapshot(p)
    second = _assemble(p, stub_atlas)
    assert second == first
    assert _snapshot(p) == after_first
    assert [i for i in _findings(p) if i >= "CHDA:LIT:0009005"] == [ADDED_FINAL]


def test_a_fix_round_replaces_the_added_finding_under_its_final_id(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge edits the added finding, still under its 07 id: it keeps 0009005, is not
    numbered again, and the counter does not move. A second added finding is numbered after it."""
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    assert _assemble(p, stub_atlas)[0] == 0
    _edit(_judge1(p), "A third carrier listed", "A third carrier, edited, listed")
    both = YAML_SAFE.load(_judge1(p).read_text())
    both["findings"].append(
        {
            **both["findings"][1],
            "id": "CHDA:LIT:0701002",
            "summary": "A fourth carrier listed.",
            "shares_probands_with": [ADDED, "CHDA:LIT:0009002"],
        }
    )
    both["findings"][0]["shares_probands_with"] = [ADDED]
    _judge1(p).write_text(json.dumps(both))
    code, out = _assemble(p, stub_atlas)
    assert code == 0, out
    found = _findings(p)
    assert found[ADDED_FINAL]["summary"].startswith("A third carrier, edited, listed")
    assert found["CHDA:LIT:0009006"]["summary"].startswith("A fourth carrier listed")
    assert list(found) == [
        "CHDA:LIT:0009001",
        "CHDA:LIT:0009002",
        JUDGED,
        "CHDA:LIT:0009004",
        ADDED_FINAL,
        "CHDA:LIT:0009006",
    ]
    assert (p.root / "curation" / ".id_registry.yaml").read_text().endswith("LIT: 9006\n")
    record = load_assemble(p)
    assert record is not None
    assert record.added == {ADDED: ADDED_FINAL, "CHDA:LIT:0701002": "CHDA:LIT:0009006"}
    assert record.replaced == (JUDGED,)
    assert found[JUDGED]["shares_probands_with"] == [ADDED_FINAL]
    assert found["CHDA:LIT:0009006"]["shares_probands_with"] == [ADDED_FINAL, "CHDA:LIT:0009002"]


def test_a_judge_may_rewrite_an_added_finding_under_the_final_id_an_earlier_assemble_gave_it(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    assert _assemble(p, stub_atlas)[0] == 0
    _edit(_judge1(p), ADDED, ADDED_FINAL)
    _edit(_judge1(p), "A third carrier listed", "A third carrier, rewritten, listed")
    code, out = _assemble(p, stub_atlas)
    assert code == 0, out
    assert _findings(p)[ADDED_FINAL]["summary"].startswith("A third carrier, rewritten")
    assert list(_findings(p))[-1] == ADDED_FINAL
    assert (p.root / "curation" / ".id_registry.yaml").read_text().endswith("LIT: 9005\n")


def test_decisions_keyed_by_the_judges_local_id_are_read_under_the_final_id_once_assembled(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    before = (p.judge / "judge-01.decisions.yaml").read_bytes()
    assert sorted(load_judge(p)) == [JUDGED, ADDED]
    with pytest.raises(ReviewDataError, match="CHDA:LIT:0701001"):
        _review(p)
    assert _assemble(p, stub_atlas)[0] == 0
    decisions = load_judge(p)
    assert sorted(decisions) == [JUDGED, ADDED_FINAL]
    assert [(d.field, d.value) for d in decisions[ADDED_FINAL]] == [("design", "case_report")]
    assert (p.judge / "judge-01.decisions.yaml").read_bytes() == before  # the judge's file stays
    data = _review(p)
    assert [c["id"] for c in data["findings"]] == [
        "CHDA:LIT:0009002",
        JUDGED,
        "CHDA:LIT:0009004",
        ADDED_FINAL,
    ] or sorted(c["id"] for c in data["findings"]) == sorted(
        ["CHDA:LIT:0009002", JUDGED, "CHDA:LIT:0009004", ADDED_FINAL]
    )
    checks = [(c.finding, c.origin, c.paper) for c in check_quotes(p)]
    assert (ADDED_FINAL, "judge", "PMID:91000002") in checks
    assert (ADDED, "judge", "PMID:91000002") not in checks


def _review(p: RunPaths) -> dict[str, Any]:
    ids = ["CHDA:LIT:0009002", JUDGED, "CHDA:LIT:0009004", ADDED_FINAL]
    digests = Digests(
        "HGNC:11604",
        "TBX5",
        "sha256:" + "a" * 64,
        tuple(
            FindingDigests(i, "sha256:" + "1" * 64, "sha256:" + "2" * 64, False, False, False)
            for i in ids
        ),
    )
    from datetime import UTC, datetime

    return build_review_data(
        p.root,
        p,
        digests,
        Reviewer("E.A.M", "owner"),
        datetime(2026, 11, 26, tzinfo=UTC),
        None,
        "0 error(s), 3 warning(s)",
    )


def test_a_mapped_decision_key_that_is_already_a_key_is_refused(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    assert _assemble(p, stub_atlas)[0] == 0
    path = p.judge / "judge-01.decisions.yaml"
    both = YAML_SAFE.load(path.read_text())
    both[ADDED_FINAL] = both[JUDGED]  # the judge also decided it under its final id
    path.write_text(json.dumps(both))
    with pytest.raises(RunFormatError, match="already decide"):
        load_judge(p)


def test_notation_fixes_every_finding_of_the_run_and_records_each_with_its_rule(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    # a merged finding the judges did not touch, a judged one and an added one, each misspelt
    _edit(
        _lit(p),
        "reported: {c: c.100C>T, p: p.(Arg34Ter)}",
        "reported: {c: c.100 C > T, p: p.(Arg34Ter)}",
    )
    _edit(_lit(p), "reported: {c: c.700+1G>A}", "reported: {c: c.700+1G > A}")
    _edit(_judge1(p), "      c: c.835C>T\n", "      c: c.835 C>T\n")
    _edit(_judge1(p), "      p: p.(Gly333Glu)\n", "      p: p.(Gly333ter)\n")
    code, out = _assemble(p, stub_atlas)
    assert code == 0, out
    assert out.endswith("fixed 4 names by rule, marked 2 verified\n")
    record = load_assemble(p)
    assert record is not None
    assert [
        (n.finding, n.variant, n.field, n.before, n.after, n.rules) for n in record.notation
    ] == [
        ("CHDA:LIT:0009002", 1, "reported.c", "c.100 C > T", "c.100C>T", ("R-F8 spaces",)),
        (JUDGED, 1, "reported.c", "c.835 C>T", "c.835C>T", ("R-F8 spaces",)),
        ("CHDA:LIT:0009004", 1, "reported.c", "c.700+1G > A", "c.700+1G>A", ("R-F8 spaces",)),
        (ADDED_FINAL, 1, "reported.p", "p.(Gly333ter)", "p.(Gly333Ter)", ("R-F4 Ter",)),
    ]
    found = _findings(p)
    assert found["CHDA:LIT:0009002"]["variants"][0]["reported"] == {
        "c": "c.100C>T",
        "p": "p.(Arg34Ter)",
    }
    assert found[JUDGED]["variants"][0]["reported"]["c"] == "c.835C>T"
    assert found[ADDED_FINAL]["variants"][0]["reported"]["p"] == "p.(Gly333Ter)"
    assert found[JUDGED]["variants"][0]["as_reported"] == "c.835C>T, p.(Arg279Ter)"  # never touched
    # a re-run keeps the record of fixes the first run made, and changes no byte
    after = _snapshot(p)
    assert _assemble(p, stub_atlas) == (0, out)
    assert _snapshot(p) == after


# -- what a judge may change -------------------------------------------------------------------


def test_a_judge_that_changes_a_field_no_decision_names_is_refused_and_a_listed_one_is_not(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``differs`` lists only ``screened`` for 0009003; changing ``design`` too has no decision."""
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _edit(_judge1(p), "design: family_segregation", "design: cohort_screen")
    _expect_refused(p, stub_atlas, f"{JUDGED}: the judge changed ['design']")


def test_a_spacing_difference_between_the_file_and_the_judge_is_no_difference(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notation is settled by rule on both copies before they are compared, as ``compare`` does."""
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _edit(_lit(p), "reported: {c: c.835C>T,", "reported: {c: c.835 C > T,")
    code, out = _assemble(p, stub_atlas)
    assert code == 0, out
    assert _findings(p)[JUDGED]["variants"][0]["reported"]["c"] == "c.835C>T"


def test_a_judge_may_correct_a_variants_span_without_a_decision(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The span is settled by rule, so a changed ``as_reported`` is no undecided difference."""
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _edit(_judge1(p), "c.835C>T, p.(Arg279Ter)", "c.835C>T / p.(Arg279Ter)")
    assert _assemble(p, stub_atlas)[0] == 0
    assert _findings(p)[JUDGED]["variants"][0]["as_reported"] == "c.835C>T / p.(Arg279Ter)"


def test_a_free_field_the_comparison_never_reads_may_be_corrected_by_a_judge(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _edit(_judge1(p), "One family carried", "Two related families carried")
    assert _assemble(p, stub_atlas)[0] == 0
    assert _findings(p)[JUDGED]["summary"].startswith("Two related families carried")


def _only_first_run(
    tmp_path: Path, atlas_root: Path, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> RunPaths:
    """Merged, then the blind reader's findings on 0009002's paper removed, so compare lists
    0009002 as reported by the first pass only."""
    p = _fresh(tmp_path, atlas_root)
    _pubmed(monkeypatch)
    assert _merge(p, stub)[0] == 0
    path = p.verify / "batch-01.findings.yaml"
    data = YAML_SAFE.load(path.read_text())
    data["findings"] = [
        f for f in data["findings"] if f["id"] not in ("CHDA:LIT:0801001", "CHDA:LIT:0801003")
    ]
    path.write_text(json.dumps(data))
    assert _compare(p, stub)[0] == 0
    return p


def test_a_finding_only_one_pass_reported_may_change_in_any_field_and_stays_as_merged_otherwise(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _only_first_run(tmp_path, atlas_root, stub_atlas, monkeypatch)
    merged = _findings(p)["CHDA:LIT:0009002"]
    judge = {**merged, "design": "cohort_screen", "summary": "A judge's summary."}
    _judge1(p).parent.mkdir(exist_ok=True)
    _judge1(p).write_text(json.dumps({"findings": [judge]}))
    # the judge round keeps 0009003 (differs) undecided, which is refused ...
    _expect_refused(p, stub_atlas, f"{JUDGED}: differs, but no judge finding decides it")
    # ... so decide it as the fixture's judge did, next to the free change
    copy_judge_round(p)
    both = json.loads(json.dumps(YAML_SAFE.load(_judge1(p).read_text())))
    both["findings"].append(judge)
    _judge1(p).write_text(json.dumps(both))
    code, out = _assemble(p, stub_atlas)
    assert code == 0, out
    found = _findings(p)
    assert found["CHDA:LIT:0009002"]["design"] == "cohort_screen"
    assert found["CHDA:LIT:0009002"]["summary"] == "A judge's summary."
    assert "verified_by" not in found["CHDA:LIT:0009002"]  # judged, so not marked
    assert load_assemble(p).replaced == ("CHDA:LIT:0009002", JUDGED)  # type: ignore[union-attr]


def test_a_finding_only_one_pass_reported_may_move_to_a_paper_only_with_its_publication(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0009004 (paper 91000003) is read by one pass only; the judge moves it to 91000004, which
    merge excluded and publications.yaml lacks: refused until registered, then triage is back."""
    p = _fresh(tmp_path, atlas_root)
    _pubmed(monkeypatch)
    assert _merge(p, stub_atlas)[0] == 0
    path = p.verify / "batch-02.findings.yaml"
    path.write_text(json.dumps({"findings": []}))
    assert _compare(p, stub_atlas)[0] == 0
    copy_judge_round(p)
    moved = {**_findings(p)["CHDA:LIT:0009004"], "publication": "PMID:91000004"}
    (p.judging / "judge-02.findings.yaml").write_text(json.dumps({"findings": [moved]}))
    _expect_refused(p, stub_atlas, "chd-curator add-publication PMID:91000004")
    _judge_on_the_excluded_paper(p, register=True)
    (p.judging / "judge-02.findings.yaml").write_text(json.dumps({"findings": [moved]}))
    code, out = _assemble(p, stub_atlas)
    assert code == 0, out
    assert "decision: included\nfindings:" in _lit(p).read_text()
    assert _findings(p)["CHDA:LIT:0009004"]["publication"] == "PMID:91000004"


def test_a_finding_only_one_pass_reported_that_the_judge_did_not_write_stays_as_merged(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _only_first_run(tmp_path, atlas_root, stub_atlas, monkeypatch)
    copy_judge_round(p)
    merged = _findings(p)["CHDA:LIT:0009002"]
    assert _assemble(p, stub_atlas)[0] == 0
    assert _findings(p)["CHDA:LIT:0009002"] == merged
    record = load_assemble(p)
    assert record is not None and "CHDA:LIT:0009002" not in record.verified


# -- the judge's findings: which are accepted --------------------------------------------------


def _drop_the_judged_finding(p: RunPaths) -> None:
    data = YAML_SAFE.load(_judge1(p).read_text())
    data["findings"] = [f for f in data["findings"] if f["id"] != JUDGED]
    _judge1(p).write_text(json.dumps(data))


def _id_not_a_judge_id(p: RunPaths) -> None:
    _edit(_judge1(p), "id: CHDA:LIT:0701001", "id: CHDA:LIT:0009004")  # span only: not judged


def _approval_in_judge(p: RunPaths) -> None:
    _edit(_judge1(p), "  id: CHDA:LIT:0701001\n", "  id: CHDA:LIT:0701001\n  approved_by: E.A.M\n")


def _verified_in_judge(p: RunPaths) -> None:
    _edit(
        _judge1(p),
        "  id: CHDA:LIT:0701001\n",
        "  id: CHDA:LIT:0701001\n  verified_by: agent:literature-verify/claude-opus-5-5\n",
    )


def _paper_outside_the_batch(p: RunPaths) -> None:
    _edit(
        _judge1(p),
        "publication: PMID:91000002\n  screened: 1",
        "publication: PMID:91000003\n  screened: 1",
    )


def _misnamed_judge_file(p: RunPaths) -> None:
    (_judge1(p)).rename(p.judging / "judge-01.findings.yml")


def _judge_file_of_no_batch(p: RunPaths) -> None:
    shutil.copy(_judge1(p), p.judging / "judge-07.findings.yaml")


def _agreed_and_judged(p: RunPaths) -> None:
    path = p.dir / "comparison.json"
    data = json.loads(path.read_text())
    data["agreed"].append(JUDGED)
    path.write_text(json.dumps(data))


def _unknown_shared_id(p: RunPaths) -> None:
    _edit(
        _judge1(p),
        "shares_probands_with: unknown\n  summary: A third",
        "shares_probands_with: [CHDA:LIT:0555555]\n  summary: A third",
    )


def _unknown_shared_id_on_a_replacement(p: RunPaths) -> None:
    _edit(
        _judge1(p),
        "shares_probands_with: unknown\n  summary: One family",
        "shares_probands_with: [CHDA:LIT:0555555]\n  summary: One family",
    )


def _no_merge(p: RunPaths) -> None:
    p.merge.unlink()


def _no_comparison(p: RunPaths) -> None:
    p.comparison.unlink()


def _modes_disagree(p: RunPaths) -> None:
    _edit(p.batches_json, '"mode": "new"', '"mode": "reread"')


def _judge_key_added_twice(p: RunPaths) -> None:
    both = YAML_SAFE.load(_judge1(p).read_text())
    both["findings"].append(both["findings"][1])
    _judge1(p).write_text(json.dumps(both))


def _stale_assemble(p: RunPaths) -> None:
    write_assemble(
        p, AssembleRecord((), {ADDED: "CHDA:LIT:0009005"}, (), (), None)
    )  # the file has no 0009005


def _merge_did_not_exclude(p: RunPaths) -> None:
    """A judge finding on 91000004, excluded in the triage, with its Publication registered."""
    _judge_on_the_excluded_paper(p, register=True)
    merge = json.loads(p.merge.read_text())
    merge["triage"] = []
    p.merge.write_text(json.dumps(merge))


def _judge_on_the_excluded_paper(p: RunPaths, *, register: bool) -> None:
    findings = YAML_SAFE.load(_judge1(p).read_text())["findings"]
    added = dict(findings[1], id="CHDA:LIT:0702001", publication="PMID:91000004")
    (p.judging / "judge-02.findings.yaml").write_text(json.dumps({"findings": [added]}))
    if register:
        with (p.root / "curation" / "publications.yaml").open("a") as handle:
            handle.write(
                "  - id: PMID:91000004\n    title: A synthetic mouse paper.\n"
                "    journal: Journal of synthetic genetics\n    year: 2023\n"
                "    authors:\n      - Gamma G\n    study_type: case_report\n"
            )


def _same_id_in_two_judge_files(p: RunPaths) -> None:
    _judge_on_the_excluded_paper(p, register=True)
    _edit(p.judging / "judge-02.findings.yaml", "CHDA:LIT:0702001", ADDED)


def _id_missing_from_the_file(p: RunPaths) -> None:
    path = p.dir / "comparison.json"
    data = json.loads(path.read_text())
    data["only_first"].append("CHDA:LIT:0009999")
    path.write_text(json.dumps(data))
    _edit(_judge1(p), "id: CHDA:LIT:0701001", "id: CHDA:LIT:0009999")


def _excluded_by_someone_else(p: RunPaths) -> None:
    """Merge excluded 91000004; 91000002 was excluded by hand, and a judge finding cites it."""
    _edit(
        _lit(p),
        "  - pmid: PMID:91000002\n    search: 1\n    decision: included\n",
        "  - pmid: PMID:91000002\n    search: 1\n    decision: excluded\n    reason: other\n"
        "    note: by hand\n",
    )


def _verify_files_gone(p: RunPaths) -> None:
    shutil.rmtree(p.verify)


def _verify_finding_without_a_name(p: RunPaths) -> None:
    _edit(
        p.verify / "batch-02.findings.yaml",
        "extracted_by: agent:literature-verify/claude-opus-5-5\n",
        "extracted_by: ''\n",
    )


def _review_marks_not_in_differs(p: RunPaths) -> None:
    _edit(
        _judge1(p),
        "  publication: PMID:91000002\n  screened: 2",
        "  publication: PMID:91000002\n  review_note: look again\n"
        "  review_status: pending_review\n  screened: 2",
    )


REFUSALS = {
    "no-merge-json": (_no_merge, "the run has no merge.json"),
    "no-comparison-json": (_no_comparison, "the run has no comparison.json"),
    "modes-disagree": (_modes_disagree, "merge.json is a new merge but batches.json plans reread"),
    "differs-without-a-judge-finding": (
        _drop_the_judged_finding,
        f"{JUDGED}: differs, but no judge finding decides it",
    ),
    "an-id-the-judge-may-not-write": (_id_not_a_judge_id, "CHDA:LIT:0009004 is neither"),
    "approval-in-a-judge-finding": (_approval_in_judge, "carries approved_by"),
    "verified-by-in-a-judge-finding": (_verified_in_judge, "carries verified_by"),
    "paper-outside-the-batch": (_paper_outside_the_batch, "which batch 01 does not read"),
    "misnamed-judge-file": (_misnamed_judge_file, "is not a judge file"),
    "judge-file-of-no-batch": (_judge_file_of_no_batch, "is not a judge file"),
    "agreed-and-judged": (_agreed_and_judged, "lists it as agreed and as judged"),
    "shares-probands-with-an-unknown-id": (_unknown_shared_id, "CHDA:LIT:0555555"),
    "shares-probands-with-an-unknown-id-on-a-replacement": (
        _unknown_shared_id_on_a_replacement,
        "CHDA:LIT:0555555, which is neither a finding the judges added nor one in the file",
    ),
    "a-judge-id-twice": (_judge_key_added_twice, "duplicate finding id"),
    "same-id-in-two-judge-files": (_same_id_in_two_judge_files, "is also in"),
    "judge-finding-for-an-id-the-file-lacks": (_id_missing_from_the_file, "but"),
    "verify-files-gone": (_verify_files_gone, "holds no finding to read the verifier's name from"),
    "verify-finding-without-a-name": (_verify_finding_without_a_name, "has no extracted_by"),
    "review-marks-not-in-differs": (_review_marks_not_in_differs, "the judge changed"),
    "stale-assemble-json": (_stale_assemble, "stopped part-way"),
    "added-paper-has-no-publication": (
        lambda p: _judge_on_the_excluded_paper(p, register=False),
        "chd-curator add-publication PMID:91000004",
    ),
    "added-paper-excluded-by-someone-else": (
        _merge_did_not_exclude,
        "which merge did not set: someone else decided it",
    ),
    "added-paper-excluded-by-hand-while-merge-excluded-another": (
        _excluded_by_someone_else,
        "PMID:91000002: a judge's finding cites it but its triage decision is 'excluded'",
    ),
}


@pytest.mark.parametrize("setup, message", REFUSALS.values(), ids=REFUSALS)
def test_every_refusal_exits_2_and_leaves_every_file_as_it_was(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Any,
    message: str,
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    setup(p)
    _expect_refused(p, stub_atlas, message)


def test_a_judge_added_finding_on_a_paper_merge_excluded_sets_its_triage_back_to_included(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _judge_on_the_excluded_paper(p, register=True)
    code, out = _assemble(p, stub_atlas)
    assert code == 0, out
    assert out.startswith(
        "replaced 1 judged findings, added 2 (CHDA:LIT:0009005..CHDA:LIT:0009006)"
    )
    text = _lit(p).read_text()
    assert "  - pmid: PMID:91000004\n    search: 1\n    decision: included\nfindings:\n" in text
    # numbered after 91000002's (2021) because 91000004 is 2023
    found = _findings(p)
    assert found["CHDA:LIT:0009005"]["publication"] == "PMID:91000002"
    assert found["CHDA:LIT:0009006"]["publication"] == "PMID:91000004"
    after = _snapshot(p)
    assert _assemble(p, stub_atlas)[0] == 0
    assert _snapshot(p) == after


def test_two_added_findings_may_name_each_other_by_their_local_ids(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    both = YAML_SAFE.load(_judge1(p).read_text())
    both["findings"].append(
        {**both["findings"][1], "id": "CHDA:LIT:0701002", "shares_probands_with": [ADDED]}
    )
    _judge1(p).write_text(json.dumps(both))
    assert _assemble(p, stub_atlas)[0] == 0
    assert _findings(p)["CHDA:LIT:0009006"]["shares_probands_with"] == [ADDED_FINAL]


def test_a_judge_finding_naming_a_local_id_in_shares_probands_with_gets_the_final_id(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _edit(
        _judge1(p),
        "shares_probands_with: unknown\n  summary: One family",
        "shares_probands_with: [CHDA:LIT:0701001, CHDA:LIT:0009002]\n  summary: One family",
    )
    assert _assemble(p, stub_atlas)[0] == 0
    assert _findings(p)[JUDGED]["shares_probands_with"] == [ADDED_FINAL, "CHDA:LIT:0009002"]


# -- verification ------------------------------------------------------------------------------


def test_two_names_in_the_blind_pass_are_refused_and_so_is_a_verifier_argument(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _expect_refused(p, stub_atlas, "--verifier belongs to a backfill", "--verifier", "agent:x/y")
    _edit(
        p.verify / "batch-02.findings.yaml",
        "extracted_by: agent:literature-verify/claude-opus-5-5",
        "extracted_by: agent:literature-verify/another-model",
    )
    _expect_refused(
        p,
        stub_atlas,
        "agent:literature-verify/another-model, agent:literature-verify/claude-opus-5-5",
    )


def test_assemble_refuses_a_file_that_changed_after_it_was_read(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    built = assemble_run(p.root, p, "TBX5", None)
    _lit(p).write_text(_lit(p).read_text() + "# edited meanwhile\n")
    before = _snapshot(p)
    with pytest.raises(WriteRefused, match="changed since it was read"):
        commit(p, built)
    assert _snapshot(p) == before  # no assemble.json, no counter move


# -- the write order and the last check ---------------------------------------------------------


def test_a_crash_after_assemble_json_leaves_it_in_place_and_a_rerun_refuses_until_it_is_removed(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    lit = _lit(p).read_bytes()

    def crash(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(assemble_module, "write_literature", crash)
    code, out = _assemble(p, stub_atlas)
    assert (code, "disk full" in out) == (1, True)
    assert _lit(p).read_bytes() == lit
    assert load_assemble(p) is not None  # written first: a re-run cannot number the finding again
    monkeypatch.undo()
    _pubmed(monkeypatch)
    _expect_refused(p, stub_atlas, "stopped part-way: remove assemble.json")
    (p.dir / "assemble.json").unlink()
    assert _assemble(p, stub_atlas)[0] == 0


def test_a_crash_writing_the_counter_leaves_the_literature_file_unwritten(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    lit = _lit(p).read_bytes()

    def crash(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(assemble_module, "write_registry", crash)
    assert _assemble(p, stub_atlas)[0] == 1
    assert _lit(p).read_bytes() == lit  # the counter is written before the file that uses it


def test_a_registry_changed_after_it_was_read_is_refused_before_anything_is_written(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    built = assemble_run(p.root, p, "TBX5", None)
    registry = p.root / "curation" / ".id_registry.yaml"
    registry.write_text(registry.read_text() + "# edited meanwhile\n")
    before = _snapshot(p)
    with pytest.raises(WriteRefused, match="changed since it was read"):
        commit(p, built)
    assert _snapshot(p) == before


def _careless_verified(raw: bytes, ids: Any, verifier: str) -> bytes:
    data = load_round_trip(with_verified(raw, ids, verifier))
    for f in data["findings"]:
        if f["id"] == "CHDA:LIT:0009002":
            f["summary"] = "Quietly reworded."
    return dump_bytes(data)


def _careless_verifier_name(raw: bytes, ids: Any, verifier: str) -> bytes:
    return with_verified(raw, ids, "agent:literature-verify/someone-else")


def _careless_replace(raw: bytes, findings: Any) -> bytes:
    data = load_round_trip(with_findings_replaced(raw, findings))
    for f in data["findings"]:
        if f["id"] == "CHDA:LIT:0009001":
            f["screened"] = 9
    return dump_bytes(data)


def _careless_drop(raw: bytes, findings: Any) -> bytes:
    data = load_round_trip(with_findings_replaced(raw, findings))
    del data["findings"][0]
    return dump_bytes(data)


def _careless_reorder(raw: bytes, findings: Any) -> bytes:
    data = load_round_trip(with_findings_replaced(raw, findings))
    data["findings"].reverse()
    return dump_bytes(data)


def _careless_duplicate(raw: bytes, findings: Any) -> bytes:
    data = load_round_trip(with_findings_replaced(raw, findings))
    data["findings"].append(copy.deepcopy(data["findings"][-1]))
    return dump_bytes(data)


def _careless_triage(raw: bytes, changes: Any) -> bytes:
    data = load_round_trip(with_triage(raw, changes))
    data["triage"][0]["decision"] = "excluded"
    return dump_bytes(data)


def _careless_triage_order(raw: bytes, changes: Any) -> bytes:
    data = load_round_trip(with_triage(raw, changes))
    data["triage"].reverse()
    return dump_bytes(data)


def _careless_top_level(raw: bytes, changes: Any) -> bytes:
    data = load_round_trip(with_triage(raw, changes))
    data["gene"] = "HGNC:1"
    return dump_bytes(data)


CARELESS = {
    "verified-finding-reworded": ("with_verified", _careless_verified, "CHDA:LIT:0009002: fields"),
    "verified-by-another-name": ("with_verified", _careless_verifier_name, "verified_by is"),
    "unrelated-finding-changed": ("with_findings_replaced", _careless_replace, "CHDA:LIT:0009001"),
    "a-finding-dropped": ("with_findings_replaced", _careless_drop, "dropped, reordered"),
    "an-added-finding-twice": ("with_findings_replaced", _careless_duplicate, "appears twice"),
    "findings-reordered": ("with_findings_replaced", _careless_reorder, "dropped, reordered"),
    "unrelated-triage-entry-changed": ("with_triage", _careless_triage, "its triage entry changed"),
    "triage-entries-reordered": ("with_triage", _careless_triage_order, "triage entries or their"),
    "top-level-key-changed": ("with_triage", _careless_top_level, "a top-level key"),
}


@pytest.mark.parametrize("name, careless, message", CARELESS.values(), ids=CARELESS)
def test_a_step_that_moves_more_than_it_is_entitled_to_is_caught_by_the_last_check(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    careless: Any,
    message: str,
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _judge_on_the_excluded_paper(p, register=True)  # so the triage step has a change to make
    monkeypatch.setattr(assemble_module, name, careless)
    _expect_refused(p, stub_atlas, message)


# -- backfill ----------------------------------------------------------------------------------


def _backfill(
    p: RunPaths, verify: str, *, cohorts: str = "", fields: tuple[str, ...] = ("cohorts",)
) -> None:
    """The fixture as a one-batch backfill of ``cohorts`` over the approved finding 0009001."""
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
                        "papers": ["PMID:91000001"],
                        "findings": ["CHDA:LIT:0009001"],
                        "text_bytes": 1,
                    }
                ],
            }
        )
    )
    merge = {
        "format": 1,
        "mode": "backfill",
        "ids": {},
        "publications_added": [],
        "triage": [],
        "replaces": [],
    }
    p.merge.write_text(json.dumps(merge))
    if cohorts:
        _edit(
            _lit(p),
            "    phenotypes: [HP:0001631]\n    lesion_groups: [septal]\n    syndromic: syndromic",
            "    phenotypes: [HP:0001631]\n    lesion_groups: [septal]\n"
            f"    syndromic: syndromic\n    cohorts: {cohorts}",
        )
    for old in p.verify.glob("batch-*"):
        old.unlink()
    (p.verify / "batch-01.findings.yaml").write_text(verify)


def _backfill_ready(
    tmp_path: Path, atlas_root: Path, stub: Path, verify: str, **kw: Any
) -> RunPaths:
    p = _fresh(tmp_path, atlas_root)
    shutil.rmtree(p.extract)
    _backfill(p, verify, **kw)
    assert _compare(p, stub)[0] == 0
    return p


def test_a_backfill_marks_agreement_with_the_named_verifier_and_needs_one(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path
) -> None:
    p = _backfill_ready(
        tmp_path,
        atlas_root,
        stub_atlas,
        "findings:\n  CHDA:LIT:0009001:\n    cohorts: [alpha]\n",
        cohorts="[alpha]",
    )
    _expect_refused(p, stub_atlas, "a backfill needs --verifier NAME")
    code, out = _assemble(p, stub_atlas, "--verifier", "agent:literature-verify/test-model")
    assert code == 0, out
    assert out == (
        "replaced 0 judged findings, added 0, fixed 0 names by rule, marked 1 verified\n"
    )
    f = _findings(p)["CHDA:LIT:0009001"]
    assert f["verified_by"] == "agent:literature-verify/test-model"
    assert f["approved_by"] == "E.A.M"  # an approval is never touched
    record = load_assemble(p)
    assert record is not None and record.verifier == "agent:literature-verify/test-model"


def test_a_backfill_that_marks_nothing_records_no_verifier_and_a_judge_may_empty_a_field(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path
) -> None:
    p = _backfill_ready(
        tmp_path,
        atlas_root,
        stub_atlas,
        "findings:\n  CHDA:LIT:0009001:\n    cohorts: [beta]\n",
        cohorts="[alpha]",
    )
    p.judging.mkdir(exist_ok=True)
    (p.judging / "judge-01.findings.yaml").write_text(
        JUDGED_BACKFILL.replace("  cohorts: [elsewhere]\n", "")
    )
    code, out = _assemble(p, stub_atlas, "--verifier", "agent:literature-verify/test-model")
    assert code == 0, out
    assert "cohorts" not in _findings(p)["CHDA:LIT:0009001"]
    record = load_assemble(p)
    assert record is not None and (record.verified, record.verifier) == ((), None)


JUDGED_BACKFILL = (EXPECTED / "judged-backfill.yaml").read_text()


def test_a_backfill_takes_only_the_declared_fields_from_a_judge_and_drops_verified_by(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path
) -> None:
    p = _backfill_ready(
        tmp_path,
        atlas_root,
        stub_atlas,
        "findings:\n  CHDA:LIT:0009001:\n    cohorts: [somewhere]\n",
    )
    p.judging.mkdir(exist_ok=True)
    (p.judging / "judge-01.findings.yaml").write_text(JUDGED_BACKFILL)
    code, out = _assemble(p, stub_atlas, "--verifier", "agent:literature-verify/test-model")
    assert code == 0, out
    f = _findings(p)["CHDA:LIT:0009001"]
    assert f["cohorts"] == ["elsewhere"]
    assert "verified_by" not in f  # judged
    assert (f["approved_by"], f["approved_on"].isoformat()) == ("E.A.M", "2026-11-19")
    assert f["approved_digest"].startswith("sha256:da89e4f2")
    assert out.startswith(
        "replaced 1 judged findings, added 0, fixed 0 names by rule, marked 0 verified"
    )
    # a judge that changed anything else in a backfill is refused
    _edit(p.judging / "judge-01.findings.yaml", "screened: 1", "screened: 3")
    _expect_refused(
        p,
        stub_atlas,
        "the judge changed fields the run did not declare: ['screened']",
        "--verifier",
        "agent:literature-verify/test-model",
    )


def test_a_backfill_adds_no_finding(tmp_path: Path, atlas_root: Path, stub_atlas: Path) -> None:
    p = _backfill_ready(
        tmp_path,
        atlas_root,
        stub_atlas,
        "findings:\n  CHDA:LIT:0009001:\n    cohorts: [somewhere]\n",
    )
    p.judging.mkdir(exist_ok=True)
    (p.judging / "judge-01.findings.yaml").write_text(
        JUDGED_BACKFILL.replace("CHDA:LIT:0009001", "CHDA:LIT:0701001")
    )
    _expect_refused(
        p,
        stub_atlas,
        "a backfill adds no finding",
        "--verifier",
        "agent:literature-verify/test-model",
    )


VARIANT_PASS = """\
findings:
  CHDA:LIT:0009001:
    variants:
    - id: 1
      as_reported: "c.1085delC, p.(Pro362Argfs*8)"
      locator: {kind: in_text, element: Results - Patients}
      kind: snv_indel
      reported: {c: "c.1085G>A", p: "p.(Pro362Argfs*8)"}
      placement: unplaced
      zygosity: heterozygous
      carriers: {de_novo: 0, inherited: 1, origin_unknown: 0}
      phenotypes: [HP:0001631]
"""


@pytest.mark.parametrize(
    ("fields", "fixed"),
    [(("variants",), 1), (("cohorts",), 0)],
    ids=["variants-declared", "variants-not-declared"],
)
def test_a_backfill_applies_the_notation_rules_only_when_variants_is_a_declared_field(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, fields: tuple[str, ...], fixed: int
) -> None:
    """The file's variant spells its name with spaces; the blind pass agrees under the rules, so
    the finding is paired either way. Fixing it rewrites the variants of an approved finding,
    which a run that did not declare them must not do."""
    p = _fresh(tmp_path, atlas_root)
    shutil.rmtree(p.extract)
    cohorts = fields == ("cohorts",)
    verify = "findings:\n  CHDA:LIT:0009001:\n    cohorts: [alpha]\n" if cohorts else VARIANT_PASS
    _backfill(p, verify, fields=fields, cohorts="[alpha]" if cohorts else "")
    _edit(_lit(p), "reported: {c: c.1085delC,", 'reported: {c: "c.1085 G > A",')
    assert _compare(p, stub_atlas)[0] == 0
    code, out = _assemble(p, stub_atlas, "--verifier", "agent:literature-verify/test-model")
    assert code == 0, out
    reported = _findings(p)["CHDA:LIT:0009001"]["variants"][0]["reported"]["c"]
    assert reported == ("c.1085G>A" if fixed else "c.1085 G > A")
    assert f"fixed {fixed} names by rule" in out


def _first_variant_run(tmp_path: Path, atlas_root: Path, stub: Path) -> RunPaths:
    """A backfill of ``variants`` over 0009001 turned into a common-variant finding with no
    variants and no ``variant_classes``; merge gave it none, the blind pass gives one."""
    p = _fresh(tmp_path, atlas_root)
    shutil.rmtree(p.extract)
    verify = VARIANT_PASS.replace('reported: {c: "c.1085 del C"', 'reported: {c: "c.1085delC"')
    verify = verify.replace("zygosity: heterozygous", "zygosity: not_stated")
    _backfill(p, verify, fields=("variants",))
    text = _lit(p).read_text()
    a = text.index("    variant_classes: [snv_indel]\n")
    b = text.index("    phenotypes: [HP:0001631]\n    lesion_groups")
    text = text[:a] + text[b:]
    text = text.replace("design: family_segregation", "design: common_variant_association", 1)
    _lit(p).write_text(text)
    assert _compare(p, stub)[0] == 0
    return p


FIRST_VARIANT = {
    "id": 1,
    "as_reported": "c.1085delC, p.(Pro362Argfs*8)",
    "locator": {"kind": "in_text", "element": "Results - Patients"},
    "kind": "snv_indel",
    "reported": {"c": "c.1085delC", "p": "p.(Pro362Argfs*8)"},
    "placement": "unplaced",
    "zygosity": "not_stated",
    "carriers": {"de_novo": 0, "inherited": 1, "origin_unknown": 0},
    "phenotypes": ["HP:0001631"],
}


@pytest.mark.parametrize("judge_writes_it", [False, True], ids=["left-out", "written"])
def test_a_judge_that_gives_a_common_variant_finding_its_first_variant_gets_snv_indel(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, judge_writes_it: bool
) -> None:
    """merge's R-P2-20 allowance, applied through the one shared rule, whether or not the judge
    also writes ``variant_classes: [snv_indel]``."""
    p = _first_variant_run(tmp_path, atlas_root, stub_atlas)
    judge = JUDGED_BACKFILL.replace(
        "design: family_segregation", "design: common_variant_association"
    )
    judge = judge.replace("  cohorts: [elsewhere]\n", "").replace(
        "zygosity: heterozygous", "zygosity: not_stated"
    )
    if not judge_writes_it:
        judge = judge.replace("  variant_classes: [snv_indel]\n", "")
    p.judging.mkdir(exist_ok=True)
    (p.judging / "judge-01.findings.yaml").write_text(judge)
    code, out = _assemble(p, stub_atlas, "--verifier", "agent:literature-verify/test-model")
    assert code == 0, out
    f = _findings(p)["CHDA:LIT:0009001"]
    assert f["variants"] == [FIRST_VARIANT]
    assert f["variant_classes"] == ["snv_indel"]
    assert "verified_by" not in f


# -- records -----------------------------------------------------------------------------------


def test_with_findings_replaced_replaces_whole_findings_and_refuses_strangers() -> None:
    raw = (
        b"findings:\n"
        b"  - id: CHDA:LIT:0000001\n    publication: PMID:1\n    design: case_report\n"
        b"    verified_by: agent:a/b\n    review_note: old\n\n"
        b"  - id: CHDA:LIT:0000002\n    publication: PMID:2\n    design: case_report\n"
    )
    new = {
        "id": "CHDA:LIT:0000001",
        "publication": "PMID:1",
        "design": "cohort_screen",
        "methods": ["exome"],
    }
    out = with_findings_replaced(raw, [new])
    assert out == (
        b"findings:\n"
        b"  - id: CHDA:LIT:0000001\n    publication: PMID:1\n    design: cohort_screen\n"
        b"    methods: [exome]\n\n"
        b"  - id: CHDA:LIT:0000002\n    publication: PMID:2\n    design: case_report\n"
    )
    with pytest.raises(RecordError, match="no such finding: CHDA:LIT:0000009"):
        with_findings_replaced(raw, [{**new, "id": "CHDA:LIT:0000009"}])
    with pytest.raises(RecordError, match="more than one replacement"):
        with_findings_replaced(raw, [new, new])


def test_a_triage_change_back_to_included_drops_reason_and_note_and_keeps_the_rest() -> None:
    raw = (
        b"triage:\n"
        b"  - pmid: PMID:1\n    search: 1\n    decision: excluded\n    reason: other\n"
        b"    note: a note\n"
        b"  - pmid: PMID:2\n    search: 1\n    decision: excluded\n    reason: other\n"
    )
    out = with_triage(raw, [TriageChange("PMID:1", "excluded", "included", None, None)])
    assert out == (
        b"triage:\n"
        b"  - pmid: PMID:1\n    search: 1\n    decision: included\n"
        b"  - pmid: PMID:2\n    search: 1\n    decision: excluded\n    reason: other\n"
    )


def test_the_judge_fixture_is_the_one_the_literal_expects() -> None:
    """The literal above depends on the fixture's two findings; fail here if they move."""
    ids = [
        f["id"]
        for f in YAML_SAFE.load((JUDGE_FIXTURE / "judging" / "judge-01.findings.yaml").read_text())[
            "findings"
        ]
    ]
    assert ids == [JUDGED, ADDED]
    assert assemble_module.summary(AssembleRecord((), {}, (), (), None)) == (
        "replaced 0 judged findings, added 0, fixed 0 names by rule, marked 0 verified"
    )
    assert AssembleError.__name__ == "AssembleError"
