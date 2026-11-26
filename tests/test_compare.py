"""``chd-curator compare``: the literal outcome on the shared run fixture (merged first), the
rules that decide span-only, review marks and differences, the backfill mode, and every refusal.

A refusal test compares the bytes of every file under the root before and after, because a
compare that wrote some manifests and then refused would leave a run the judges could start on.
Expected values are written out, never rebuilt from the module's own constants.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator.cli import app
from chd_curator.compare import CompareError, compare_run
from chd_curator.run import (
    Batch,
    BatchPlan,
    MergeRecord,
    RunPaths,
    load_comparison,
    write_merge,
)
from run_fixture import copy_judge_round
from test_merge import _fresh, _merge, _pubmed

runner = CliRunner()
YAML_SAFE = YAML(typ="safe")
FIRST = "CHDA:LIT:0009004"
SECOND = "CHDA:LIT:0802001"


def _merged(
    tmp_path: Path, atlas_root: Path, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> RunPaths:
    p = _fresh(tmp_path, atlas_root)
    _pubmed(monkeypatch)
    assert _merge(p, stub)[0] == 0
    return p


def _compare(p: RunPaths, stub: Path) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["compare", "TBX5", "--run", "r1", "--root", str(p.root), "--atlas-cli", str(stub)],
        catch_exceptions=False,
    )
    return result.exit_code, result.output


def _snapshot(p: RunPaths) -> dict[str, str]:
    return {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(p.root.rglob("*"))
        if f.is_file()
    }


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1))


def test_compare_on_the_merged_fixture_gives_the_literal_comparison_and_one_manifest(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _merged(tmp_path, atlas_root, stub_atlas, monkeypatch)
    code, out = _compare(p, stub_atlas)
    assert code == 0
    assert out == (
        "compared 3 pairs: 1 agreed, 1 span only, 1 differ; "
        "0 only in the first pass, 1 only in the blind pass\n"
    )
    c = load_comparison(p)
    assert c.pairs == {
        "CHDA:LIT:0009002": "CHDA:LIT:0801001",
        "CHDA:LIT:0009003": "CHDA:LIT:0801002",
        "CHDA:LIT:0009004": "CHDA:LIT:0802001",
    }
    assert c.agreed == ("CHDA:LIT:0009002",)
    assert c.span_only == ("CHDA:LIT:0009004",)
    assert c.differs == {"CHDA:LIT:0009003": ("screened",)}
    assert c.only_first == ()
    assert c.only_second == ("CHDA:LIT:0801003",)
    assert sorted(f.name for f in p.judging.iterdir()) == ["judge-01.md"]

    text = (p.judging / "judge-01.md").read_text()
    assert text.startswith("# Judging batch 01 of run r1 (gene TBX5)\n")
    assert "compared after the notation rules" in text
    # what the sentence says is settled is what notation.py rewrites, no more (measured: a base's
    # case and an uppercase TER are not rewritten)
    assert "any other difference in a variant name, a base's case included, is listed" in text
    assert "spacing and case" not in text
    root = str(p.root.resolve())
    assert f"- text path: {root}/local/literature/texts/91000002/fulltext.txt\n" in text
    assert f"- abstract path: {root}/local/literature/texts/91000002/abstract.txt\n" in text
    assert f"- supplement: {root}/local/literature/texts/91000002/supplements/s1.txt\n" in text
    assert "## CHDA:LIT:0009003 (blind pass CHDA:LIT:0801002)\n" in text
    assert "- screened: first 1 | second 2\n" in text
    assert "## CHDA:LIT:0801003 (blind pass only)\n" in text
    assert "id: CHDA:LIT:0801003\n" in text  # the blind pass's YAML, as written
    # the agreed and the span-only pair are not shown; the merged first pass is shown without
    # the approval fields
    assert "CHDA:LIT:0009002" not in text and "CHDA:LIT:0009004" not in text
    assert "id: CHDA:LIT:0009003\n" in text
    for absent in ("approved_by", "approved_on", "approved_digest"):
        assert absent not in text


# -- the verdict on a pair, on one finding of the fixture --------------------------------------


@pytest.fixture
def schema(atlas_root: Path) -> dict[str, Any]:
    loaded = json.loads((atlas_root / "schemas" / "gene_literature_file.schema.json").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _blind(p: RunPaths, batch: str, index: int) -> dict[str, Any]:
    data = YAML_SAFE.load((p.verify / f"batch-{batch}.findings.yaml").read_text())
    return copy.deepcopy(data["findings"][index])  # type: ignore[no-any-return]


@pytest.fixture
def base() -> dict[str, Any]:
    """The blind pass's finding 0802001 of the fixture: one abstract-only splice variant."""
    from run_fixture import run_paths

    return _blind(run_paths(), "02", 0)


def _plan() -> BatchPlan:
    return BatchPlan("new", (), 1000, (Batch("01", ("PMID:91000003",), (), 10),))


def _record(first: dict[str, Any]) -> MergeRecord:
    return MergeRecord("new", {"CHDA:LIT:0902001": first["id"]}, (), (), ())


def _verdict(
    schema: dict[str, Any],
    base: dict[str, Any],
    first_edit: Callable[[dict[str, Any]], None] = lambda f: None,
    second_edit: Callable[[dict[str, Any]], None] = lambda f: None,
) -> tuple[str, tuple[str, ...]]:
    first = copy.deepcopy(base)
    first["id"] = FIRST
    first_edit(first)
    second = copy.deepcopy(base)
    second["id"] = SECOND
    second_edit(second)
    c = compare_run({"findings": [first]}, _plan(), _record(first), [second], schema)
    assert c.pairs == {FIRST: SECOND}
    assert c.only_first == () and c.only_second == ()
    if FIRST in c.differs:
        return "differs", c.differs[FIRST]
    return ("span_only" if FIRST in c.span_only else "agreed"), ()


def _variant(finding: dict[str, Any]) -> dict[str, Any]:
    return finding["variants"][0]  # type: ignore[no-any-return]


def _spaced(finding: dict[str, Any]) -> None:
    _variant(finding)["reported"]["c"] = "c. 700+1G>A"
    _variant(finding)["as_reported"] = "c. 700 +1 G>A"


def _marked(finding: dict[str, Any]) -> None:
    finding["review_status"] = "pending_review"
    finding["review_settled_by"] = "authors"


def _variant_marked(finding: dict[str, Any]) -> None:
    _variant(finding)["review_status"] = "not_accessible"
    _variant(finding)["review_settled_by"] = "full_text"


def _two_phenotypes(order: list[str]) -> Callable[[dict[str, Any]], None]:
    def edit(finding: dict[str, Any]) -> None:
        _variant(finding)["phenotypes"] = order

    return edit


def _screened(finding: dict[str, Any]) -> None:
    finding["screened"] = 2


def _zygosity(finding: dict[str, Any]) -> None:
    _variant(finding)["zygosity"] = "homozygous"


def _note(finding: dict[str, Any]) -> None:
    finding["review_note"] = "free text"


def _both(*edits: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    def edit(finding: dict[str, Any]) -> None:
        for e in edits:
            e(finding)

    return edit


def test_span_notation_and_phenotype_order_alone_are_span_only_or_agreed(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    ab = ["HP:0001631", "HP:0001629"]
    ba = ["HP:0001629", "HP:0001631"]
    assert _verdict(schema, base) == ("agreed", ())
    # phenotypes in another order, a spaced name and another span: span only, not differs
    assert _verdict(schema, base, _two_phenotypes(ab), _both(_two_phenotypes(ba), _spaced)) == (
        "span_only",
        (),
    )
    # the order of phenotypes alone is no difference at all
    assert _verdict(schema, base, _two_phenotypes(ab), _two_phenotypes(ba)) == ("agreed", ())


def test_p_space_k342fs_against_p_k342fs_is_span_only(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    def spaced(f: dict[str, Any]) -> None:
        _variant(f)["reported"]["p"] = "p. K342fs"
        _variant(f)["as_reported"] = "p. K342fs"

    def tight(f: dict[str, Any]) -> None:
        _variant(f)["reported"]["p"] = "p.K342fs"
        _variant(f)["as_reported"] = "p.K342fs (c.1024del)"

    assert _verdict(schema, base, spaced, tight) == ("span_only", ())


@pytest.mark.parametrize(
    ("first_edit", "second_edit", "expected"),
    [
        # a mark only the blind pass set
        (None, _marked, ("differs", ("review marks",))),
        # a mark only the first pass carries, with a span difference beside it
        (_marked, _spaced, ("differs", ("review marks",))),
        # the same mark on both sides is no difference
        (_marked, _marked, ("agreed", ())),
        # a variant mark on one side only
        (None, _variant_marked, ("differs", ("review marks",))),
        # a variant carrying the same mark on both sides stays span only
        (_variant_marked, _both(_variant_marked, _spaced), ("span_only", ())),
        # a variant mark on a variant that differs in the span only on the marked side
        (_variant_marked, _spaced, ("differs", ("review marks",))),
        # the same status with another settler is another mark
        (
            _marked,
            _both(_marked, lambda f: f.update(review_settled_by="expert")),
            ("differs", ("review marks",)),
        ),
        # review_note is free text and is not compared
        (None, _note, ("agreed", ())),
        # a substantive difference beside a span difference lists only the substantive one
        (None, _both(_screened, _spaced), ("differs", ("screened",))),
        (None, _both(_zygosity, _spaced), ("differs", ("variants",))),
        # names are sorted, with the pseudo-field in its sorted place
        (None, _both(_screened, _marked), ("differs", ("review marks", "screened"))),
    ],
    ids=[
        "mark-blind-only",
        "mark-first-only-with-span",
        "same-mark",
        "variant-mark-one-side",
        "variant-mark-both-sides-span-only",
        "variant-mark-only-where-span-differs",
        "other-settler",
        "review-note-ignored",
        "screened-and-span",
        "zygosity-and-span",
        "sorted-names",
    ],
)
def test_a_pairs_verdict(
    schema: dict[str, Any],
    base: dict[str, Any],
    first_edit: Callable[[dict[str, Any]], None] | None,
    second_edit: Callable[[dict[str, Any]], None],
    expected: tuple[str, tuple[str, ...]],
) -> None:
    assert _verdict(schema, base, first_edit or (lambda f: None), second_edit) == expected


def test_twins_sharing_a_key_are_never_paired_and_go_to_the_judge_as_unpaired(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    first_a, first_b = copy.deepcopy(base), copy.deepcopy(base)
    first_a["id"], first_b["id"] = "CHDA:LIT:0009004", "CHDA:LIT:0009005"
    second = copy.deepcopy(base)
    second["id"] = SECOND
    merged = MergeRecord("new", {"a": first_a["id"], "b": first_b["id"]}, (), (), ())
    c = compare_run({"findings": [first_a, first_b]}, _plan(), merged, [second], schema)
    assert c.pairs == {} and c.agreed == ()
    assert c.only_first == ("CHDA:LIT:0009004", "CHDA:LIT:0009005")
    assert c.only_second == (SECOND,)


def test_a_merged_id_the_file_lacks_and_a_blind_finding_outside_every_batch_are_refused(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    first = copy.deepcopy(base)
    first["id"] = FIRST
    second = copy.deepcopy(base)
    second["id"] = SECOND
    with pytest.raises(CompareError, match="CHDA:LIT:0009004, which the literature file lacks"):
        compare_run({"findings": []}, _plan(), _record(first), [second], schema)
    second["publication"] = "PMID:91000009"
    with pytest.raises(CompareError, match=f"{SECOND} cites PMID:91000009, which no batch reads"):
        compare_run({"findings": [first]}, _plan(), _record(first), [second], schema)


# -- backfill ----------------------------------------------------------------------------------


def _backfill_plan(*ids: str, fields: tuple[str, ...] = ("cohorts",)) -> BatchPlan:
    return BatchPlan("backfill", fields, 1000, (Batch("01", ("PMID:91000003",), ids, 10),))


def _backfill(
    schema: dict[str, Any],
    base: dict[str, Any],
    held: dict[str, list[str] | None],
    given: dict[str, dict[str, Any]],
    fields: tuple[str, ...] = ("cohorts",),
) -> Any:
    findings = []
    for fid, cohorts in held.items():
        finding = copy.deepcopy(base)
        finding["id"] = fid
        if cohorts is not None:
            finding["cohorts"] = cohorts
        findings.append(finding)
    return compare_run(
        {"findings": findings},
        _backfill_plan(*held, fields=fields),
        MergeRecord("backfill", {}, (), (), ()),
        given,
        schema,
    )


def test_backfill_pairs_twins_that_share_a_key_by_id_and_compares_declared_fields_only(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    a, b, c_, d = "CHDA:LIT:0009001", "CHDA:LIT:0009002", "CHDA:LIT:0009003", "CHDA:LIT:0009004"
    # a and b share (publication, design, cohorts) so a key pairing would have refused both
    result = _backfill(
        schema,
        base,
        {a: ["x"], b: ["x"], c_: ["x"], d: None},
        {a: {"cohorts": ["x"]}, b: {"cohorts": ["y"]}},
    )
    assert result.pairs == {a: a, b: b, c_: c_}  # d: neither reader filled it
    assert result.agreed == (a,)
    # c filled by the first reader, the blind reader said nothing: not agreement
    assert result.differs == {b: ("cohorts",), c_: ("cohorts",)}
    assert result.only_first == () and result.only_second == () and result.span_only == ()


def test_backfill_a_field_the_first_reader_left_empty_and_the_blind_reader_filled_differs(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    a = "CHDA:LIT:0009001"
    result = _backfill(schema, base, {a: None}, {a: {"cohorts": ["x"]}})
    assert result.differs == {a: ("cohorts",)}
    # an empty value the blind reader gave is no filling
    assert _backfill(schema, base, {a: None}, {a: {"cohorts": []}}).pairs == {}


def test_backfill_refuses_a_second_copy_the_schema_rejects_and_a_field_never_compared(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    a = "CHDA:LIT:0009001"
    with pytest.raises(CompareError, match=f"{a}: the blind pass's fields do not satisfy"):
        _backfill(schema, base, {a: None}, {a: {"screened": "many"}}, fields=("screened",))
    with pytest.raises(CompareError, match="'summary', which a comparison never reads"):
        _backfill(schema, base, {a: None}, {a: {"summary": "s"}}, fields=("summary",))
    with pytest.raises(CompareError, match=f"{a}: the blind pass sets 'screened'"):
        _backfill(schema, base, {a: None}, {a: {"screened": 2}})
    with pytest.raises(CompareError, match="CHDA:LIT:0009009 is in a blind pass but in no batch"):
        _backfill(schema, base, {a: None}, {"CHDA:LIT:0009009": {"cohorts": ["x"]}})
    with pytest.raises(CompareError, match="which the literature file lacks"):
        compare_run(
            {"findings": []},
            _backfill_plan(a),
            MergeRecord("backfill", {}, (), (), ()),
            {},
            schema,
        )


def _backfill_run(p: RunPaths, verify: str) -> None:
    """The fixture as a one-batch backfill of ``cohorts`` over the approved finding."""
    (p.dir / "batches.json").write_text(
        json.dumps(
            {
                "format": 1,
                "mode": "backfill",
                "fields": ["cohorts"],
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
    write_merge(p, MergeRecord("backfill", {}, (), (), ()))
    for old in p.verify.glob("batch-*"):
        old.unlink()
    (p.verify / "batch-01.findings.yaml").write_text(verify)


def test_backfill_through_the_command_lists_what_the_blind_reader_filled(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _backfill_run(p, "findings:\n  CHDA:LIT:0009001:\n    cohorts: [somewhere]\n")
    code, out = _compare(p, stub_atlas)
    assert code == 0, out
    assert out == (
        "compared 1 pairs: 0 agreed, 0 span only, 1 differ; "
        "0 only in the first pass, 0 only in the blind pass\n"
    )
    assert load_comparison(p).differs == {"CHDA:LIT:0009001": ("cohorts",)}
    text = (p.judging / "judge-01.md").read_text()
    assert '- cohorts: first [] | second ["somewhere"]\n' in text
    assert "  cohorts:\n  - somewhere\n" in text  # the blind copy's YAML
    assert "id: CHDA:LIT:0009001\n" in text  # the first pass, an approved finding ...
    for absent in ("approved_by", "approved_on", "approved_digest", "E.A.M"):
        assert absent not in text  # ... shown without its approval


@pytest.mark.parametrize(
    ("verify", "message"),
    [
        ("findings:\n  CHDA:LIT:0009009:\n    cohorts: [x]\n", "not a finding of batch 01"),
        ("findings:\n  CHDA:LIT:0009001:\n    screened: 2\n", "batches.json does not declare"),
    ],
    ids=["id-outside-the-batch", "undeclared-field"],
)
def test_backfill_refusals_through_the_command(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, verify: str, message: str
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _backfill_run(p, verify)
    before = _snapshot(p)
    code, out = _compare(p, stub_atlas)
    assert code == 2 and message in out
    assert _snapshot(p) == before


# -- refusals and re-runs through the command --------------------------------------------------


def _drop_a_verify_file(p: RunPaths) -> None:
    (p.verify / "batch-02.findings.yaml").unlink()


def _extra_verify_file(p: RunPaths) -> None:
    (p.verify / "batch-03.findings.yaml").write_text("findings: []\n")


def _misnamed_verify_file(p: RunPaths) -> None:
    (p.verify / "second.findings.yaml").write_text("findings: []\n")


def _foreign_paper(p: RunPaths) -> None:
    _edit(
        p.verify / "batch-01.findings.yaml",
        "publication: PMID:91000002",
        "publication: PMID:91000003",
    )


def _repeated_id(p: RunPaths) -> None:
    _edit(p.verify / "batch-02.findings.yaml", "CHDA:LIT:0802001", "CHDA:LIT:0801001")


def _bad_schema(p: RunPaths) -> None:
    _edit(p.verify / "batch-02.findings.yaml", "design: case_report", "design: nonsense")


def _no_merge(p: RunPaths) -> None:
    p.merge.unlink()


def _first_pass_gone(p: RunPaths) -> None:
    lit = p.root / "curation" / "literature" / "TBX5.yaml"
    _edit(lit, "CHDA:LIT:0009004", "CHDA:LIT:0009099")


def _judge_decisions(p: RunPaths) -> None:
    p.judge.mkdir()
    (p.judge / "judge-01.decisions.yaml").write_text("{}\n")


def _judge_findings(p: RunPaths) -> None:
    p.judging.mkdir()
    (p.judging / "judge-01.findings.yaml").write_text("findings: []\n")


def _no_texts_entry(p: RunPaths) -> None:
    _edit(p.texts, '"pmid": "PMID:91000002"', '"pmid": "PMID:91000099"')


@pytest.mark.parametrize(
    ("break_it", "message"),
    [
        (_drop_a_verify_file, "has no findings file for batch 02"),
        (_extra_verify_file, "is not the verify file of a batch in batches.json"),
        (_misnamed_verify_file, "is not the verify file of a batch in batches.json"),
        (_foreign_paper, "cites PMID:91000003, which batch 01 does not read"),
        (_repeated_id, "is also in another batch's blind pass"),
        (_bad_schema, "does not satisfy the Finding schema"),
        (_no_merge, "has no merge.json"),
        (_first_pass_gone, "which the literature file lacks"),
        (_judge_decisions, "the judges have already read a comparison"),
        (_judge_findings, "the judges have already read a comparison"),
        (_no_texts_entry, "PMID:91000002"),
    ],
    ids=[
        "missing-verify-file",
        "verify-file-of-no-batch",
        "misnamed-verify-file",
        "paper-outside-its-batch",
        "id-repeated-across-batches",
        "schema-invalid",
        "no-merge-json",
        "merged-id-missing-from-the-file",
        "judge-decisions-present",
        "judging-findings-present-alone",
        "paper-missing-from-texts",
    ],
)
def test_every_refusal_exits_2_and_writes_nothing(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    break_it: Callable[[RunPaths], None],
    message: str,
) -> None:
    p = _merged(tmp_path, atlas_root, stub_atlas, monkeypatch)
    break_it(p)
    before = _snapshot(p)
    code, out = _compare(p, stub_atlas)
    assert code == 2, out
    assert message in out
    assert _snapshot(p) == before
    assert not p.comparison.exists()


def test_a_corrected_verify_file_can_be_compared_again_and_a_stale_manifest_goes(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _merged(tmp_path, atlas_root, stub_atlas, monkeypatch)
    verify = p.verify / "batch-02.findings.yaml"
    _edit(verify, "zygosity: heterozygous", "zygosity: homozygous")
    assert _compare(p, stub_atlas)[0] == 0
    assert sorted(f.name for f in p.judging.iterdir()) == ["judge-01.md", "judge-02.md"]
    assert load_comparison(p).differs["CHDA:LIT:0009004"] == ("variants",)
    (p.judging / "notes.txt").write_text("a curator's own file")

    _edit(verify, "zygosity: homozygous", "zygosity: heterozygous")
    assert _compare(p, stub_atlas)[0] == 0
    assert sorted(f.name for f in p.judging.iterdir()) == ["judge-01.md", "notes.txt"]
    assert load_comparison(p).span_only == ("CHDA:LIT:0009004",)


def test_a_run_with_nothing_to_judge_writes_no_manifest_and_a_real_judge_round_is_refused(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _merged(tmp_path, atlas_root, stub_atlas, monkeypatch)
    (p.verify / "batch-01.findings.yaml").write_text(
        (p.extract / "batch-01.findings.yaml").read_text()
    )
    assert _compare(p, stub_atlas)[0] == 0
    c = load_comparison(p)
    assert c.agreed == ("CHDA:LIT:0009002", "CHDA:LIT:0009003") and c.differs == {}
    assert not p.judging.exists()
    copy_judge_round(p)
    before = _snapshot(p)
    assert _compare(p, stub_atlas)[0] == 2
    assert _snapshot(p) == before


def test_an_unwritable_judging_directory_is_exit_1_not_a_traceback(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _merged(tmp_path, atlas_root, stub_atlas, monkeypatch)
    p.judging.write_text("a file where the directory belongs")
    code, out = _compare(p, stub_atlas)
    assert code == 1 and "cannot be written" in out
    assert not p.comparison.exists()


def _twins(
    schema: dict[str, Any],
    base: dict[str, Any],
    firsts: list[Callable[[dict[str, Any]], None]],
    seconds: list[Callable[[dict[str, Any]], None]],
) -> Any:
    first = []
    for n, edit in enumerate(firsts):
        finding = copy.deepcopy(base)
        finding["id"] = f"CHDA:LIT:00090{n + 1:02d}"
        edit(finding)
        first.append(finding)
    second = []
    for n, edit in enumerate(seconds):
        finding = copy.deepcopy(base)
        finding["id"] = f"CHDA:LIT:08010{n + 1:02d}"
        edit(finding)
        second.append(finding)
    merged = MergeRecord(
        "new", {f"CHDA:LIT:09010{n}": f["id"] for n, f in enumerate(first)}, (), (), ()
    )
    return compare_run({"findings": first}, _plan(), merged, second, schema)


def _plain(finding: dict[str, Any]) -> None:
    return None


def test_twins_are_paired_only_when_exactly_one_finding_on_each_side_is_identical(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    one, two, three = "CHDA:LIT:0009001", "CHDA:LIT:0009002", "CHDA:LIT:0801001"
    # one identical match on each side pairs, the other findings stay unpaired
    c = _twins(schema, base, [_plain, _screened], [_plain, _both(_screened, _screened, _zygosity)])
    assert c.pairs == {one: three} and c.agreed == (one,)
    assert c.only_first == (two,) and c.only_second == ("CHDA:LIT:0801002",)
    # two identical blind findings for one first finding: choosing would be arbitrary
    c = _twins(schema, base, [_plain, _screened], [_plain, _plain])
    assert c.pairs == {} and c.only_second == (three, "CHDA:LIT:0801002")
    # identical in every field but a review mark is not identical
    c = _twins(schema, base, [_plain, _screened], [_marked, _screened])
    assert c.pairs == {two: "CHDA:LIT:0801002"} and c.only_first == (one,)
    assert c.only_second == (three,)


def test_leftovers_with_different_keys_are_not_paired_however_alike_they_are(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    def other_design(finding: dict[str, Any]) -> None:
        finding["design"] = "family_segregation"

    c = _twins(schema, base, [_plain], [other_design])
    assert c.pairs == {} and c.agreed == ()
    assert c.only_first == ("CHDA:LIT:0009001",) and c.only_second == ("CHDA:LIT:0801001",)


def test_manifest_paths_are_absolute_even_when_the_root_is_given_relative(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _merged(tmp_path, atlas_root, stub_atlas, monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["compare", "TBX5", "--run", "r1", "--root", "root", "--atlas-cli", str(stub_atlas)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    paths = [
        line.split(": ", 1)[1]
        for line in (p.judging / "judge-01.md").read_text().splitlines()
        if line.startswith(("- text path: ", "- abstract path: ", "- supplement: "))
    ]
    assert len(paths) == 6 and all(path.startswith("/") for path in paths)


def test_a_first_pass_finding_the_blind_reader_never_reported_is_in_the_manifest(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _merged(tmp_path, atlas_root, stub_atlas, monkeypatch)
    verify = p.verify / "batch-01.findings.yaml"
    data = YAML_SAFE.load(verify.read_text())
    data["findings"] = [f for f in data["findings"] if f["id"] != "CHDA:LIT:0801002"]
    YAML_SAFE.dump(data, verify)
    assert _compare(p, stub_atlas)[0] == 0
    c = load_comparison(p)
    assert c.only_first == ("CHDA:LIT:0009003",) and c.differs == {}
    assert c.only_second == ("CHDA:LIT:0801003",)
    text = (p.judging / "judge-01.md").read_text()
    assert "## CHDA:LIT:0009003 (first pass only)\n" in text
    assert text.index("(first pass only)") < text.index("(blind pass only)")
    assert "Blind pass:" in text and text.count("First pass, as merged:") == 1


def test_merge_json_of_another_mode_and_two_extract_ids_on_one_final_id_are_refused(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    first = copy.deepcopy(base)
    first["id"] = FIRST
    second = copy.deepcopy(base)
    second["id"] = SECOND
    with pytest.raises(CompareError, match="merge.json is a backfill merge"):
        compare_run(
            {"findings": [first]},
            _plan(),
            MergeRecord("backfill", {"CHDA:LIT:0902001": FIRST}, (), (), ()),
            [second],
            schema,
        )
    twice = MergeRecord("new", {"CHDA:LIT:0902001": FIRST, "CHDA:LIT:0902002": FIRST}, (), (), ())
    with pytest.raises(CompareError, match="two extract ids to one final id"):
        compare_run({"findings": [first]}, _plan(), twice, [second], schema)


def test_the_comparison_is_sorted_by_id_however_the_batches_are_ordered(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    def make(fid: str, pmid: str, design: str, screened: int = 1) -> dict[str, Any]:
        finding = copy.deepcopy(base)
        finding.update(id=fid, publication=pmid, design=design, screened=screened)
        return finding

    three, four = "PMID:91000003", "PMID:91000004"
    plan = BatchPlan("new", (), 1000, (Batch("01", (three,), (), 1), Batch("02", (four,), (), 1)))
    first = [
        make("CHDA:LIT:0009009", three, "case_report"),
        make("CHDA:LIT:0009008", three, "family_segregation"),
        make("CHDA:LIT:0009001", four, "case_report"),
        make("CHDA:LIT:0009002", four, "family_segregation"),
    ]
    second = [
        make("CHDA:LIT:0801009", three, "case_report", 2),
        make("CHDA:LIT:0801008", three, "family_segregation"),
        make("CHDA:LIT:0801007", three, "cohort_screen"),
        make("CHDA:LIT:0801001", four, "case_report", 2),
        make("CHDA:LIT:0801002", four, "family_segregation"),
        make("CHDA:LIT:0801003", four, "cohort_screen"),
    ]
    merged = MergeRecord(
        "new", {f"CHDA:LIT:09{n}": f["id"] for n, f in enumerate(first)}, (), (), ()
    )
    c = compare_run({"findings": first}, plan, merged, second, schema)
    assert list(c.pairs) == [
        "CHDA:LIT:0009001",
        "CHDA:LIT:0009002",
        "CHDA:LIT:0009008",
        "CHDA:LIT:0009009",
    ]
    assert c.agreed == ("CHDA:LIT:0009002", "CHDA:LIT:0009008")
    assert list(c.differs) == ["CHDA:LIT:0009001", "CHDA:LIT:0009009"]
    assert c.only_second == ("CHDA:LIT:0801003", "CHDA:LIT:0801007")


def test_a_backfills_judged_items_follow_the_batch_order(
    schema: dict[str, Any], base: dict[str, Any]
) -> None:
    from chd_curator.compare import analyse

    low, high = "CHDA:LIT:0009001", "CHDA:LIT:0009009"
    findings = []
    for fid in (low, high):
        finding = copy.deepcopy(base)
        finding["id"] = fid
        findings.append(finding)
    plan = BatchPlan(
        "backfill",
        ("cohorts",),
        1000,
        (Batch("01", ("PMID:91000003",), (high,), 1), Batch("02", ("PMID:91000003",), (low,), 1)),
    )
    given = {low: {"cohorts": ["x"]}, high: {"cohorts": ["x"]}}
    result = analyse(
        {"findings": findings}, plan, MergeRecord("backfill", {}, (), (), ()), given, schema
    )
    assert [(j.batch, j.first_id) for j in result.judged] == [("01", high), ("02", low)]
