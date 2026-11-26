"""``chd-curator batch``: the literal packing of 5 papers, and what each refusal leaves behind.

The packing test uses a 1,000-byte budget over papers of 600, 400 (together exactly the budget,
so the boundary joins), 700, 1,500 (its own text over the budget), 100 with two 700-byte
supplements (neither over the budget alone, together over it), and 300. PMID:9 and PMID:100
share a year, so numeric order puts 9 first where string order would put 100 first.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chd_curator.batch import BatchError, plan_batches, prepare_batches, write_plan
from chd_curator.cli import app
from chd_curator.run import Batch, PaperText, RunPaths
from run_fixture import copy_run_fixture, run_paths

runner = CliRunner()


def _paper(tmp: Path, pmid: str, size: int, supplements: tuple[int, ...] = ()) -> PaperText:
    folder = tmp / pmid.replace(":", "")
    (folder / "supplements").mkdir(parents=True)
    (folder / "fulltext.txt").write_bytes(b"x" * size)
    sups = []
    for n, supplement in enumerate(supplements):
        path = folder / "supplements" / f"s{n}.txt"
        path.write_bytes(b"y" * supplement)
        sups.append(path)
    return PaperText(pmid, "full_text", folder / "fulltext.txt", None, tuple(sups), False, "")


def test_packing_is_by_year_then_pmid_number_and_isolates_oversize_papers(tmp_path: Path) -> None:
    rows = [  # (pmid, year, text bytes, supplement bytes)
        ("PMID:100", 2001, 400, ()),
        ("PMID:9", 2001, 600, ()),
        ("PMID:7", 2002, 700, ()),
        ("PMID:1", 2003, 1500, ()),
        ("PMID:60", 2004, 100, (700, 700)),
        ("PMID:50", 2005, 300, ()),
    ]
    papers = [_paper(tmp_path, pmid, size, sup) for pmid, _, size, sup in rows]
    years = {pmid: year for pmid, year, _, _ in rows}
    assert plan_batches(papers, years, 1000) == (
        Batch("01", ("PMID:9", "PMID:100"), (), 1000),
        Batch("02", ("PMID:7",), (), 700),
        Batch("03", ("PMID:1",), (), 1500),
        Batch("04", ("PMID:60",), (), 100),
        Batch("05", ("PMID:50",), (), 300),
    )


@pytest.fixture(autouse=True)
def _stub_atlas_cli(stub_atlas: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``batch`` resolves its symbol through ``literature target``, as ``merge`` does."""
    monkeypatch.setenv("CHD_ATLAS_CLI", str(stub_atlas))


def _fresh(tmp_path: Path, atlas_root: Path) -> RunPaths:
    p = copy_run_fixture(tmp_path)
    p.batches_json.unlink()
    shutil.copytree(atlas_root / "schemas", p.root / "schemas")
    return p


def _batch(p: RunPaths, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app, ["batch", "TBX5", "--run", "r1", "--root", str(p.root), *args], catch_exceptions=False
    )
    return result.exit_code, result.output


def test_new_mode_writes_the_planned_batches_and_manifests(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    code, out = _batch(p, "--mode", "new")
    assert code == 0
    assert out == ("planned 1 batch for 3 papers (budget 170,000 bytes): 01 3 papers 637 bytes\n")
    batches = json.loads(p.batches_json.read_text())
    assert batches["batches"] == [
        {
            "findings": [],
            "id": "01",
            "papers": ["PMID:91000002", "PMID:91000003", "PMID:91000004"],
            "text_bytes": 637,
        }
    ]
    assert batches["budget_bytes"] == 170000 and batches["mode"] == "new"
    texts = f"{p.root}/local/literature/texts"
    assert (p.batches_dir / "batch-01.md").read_text() == (
        "# Batch 01 of run r1 (gene TBX5, mode new)\n"
        "\n"
        "## PMID:91000002\n"
        "\n"
        "- year: 2021\n"
        "- title: Two synthetic TBX5 variants in a proband and a family\n"
        "- text: full text\n"
        f"- text path: {texts}/91000002/fulltext.txt\n"
        f"- abstract path: {texts}/91000002/abstract.txt\n"
        f"- supplement: {texts}/91000002/supplements/s1.txt (96 bytes)\n"
        "\n"
        "## PMID:91000003\n"
        "\n"
        "- year: 2022\n"
        "- title: A synthetic TBX5 splice variant in a fetus\n"
        "- text: abstract only\n"
        f"- abstract path: {texts}/91000003/abstract.txt\n"
        "\n"
        "## PMID:91000004\n"
        "\n"
        "- year: 2023\n"
        "- title: A synthetic mouse model of Tbx5 haploinsufficiency\n"
        "- text: abstract only\n"
        f"- abstract path: {texts}/91000004/abstract.txt\n"
    )


def test_a_symbol_typed_as_a_previous_name_finds_the_approved_symbols_literature_file(
    tmp_path: Path, atlas_root: Path
) -> None:
    """m14: the run directory is named as typed (HOS), the literature file by the approved symbol
    (TBX5), as for merge, compare, assemble and check-batch."""
    p = _fresh(tmp_path, atlas_root)
    (p.root / "local/literature/runs/TBX5").rename(p.root / "local/literature/runs/HOS")
    result = runner.invoke(
        app,
        ["batch", "HOS", "--run", "r1", "--root", str(p.root), "--mode", "new"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert (p.root / "local/literature/runs/HOS/r1/batches.json").is_file()


def test_the_fixture_budget_reproduces_the_committed_two_batches(
    tmp_path: Path, atlas_root: Path
) -> None:
    committed = run_paths().batches_json.read_bytes()
    p = _fresh(tmp_path, atlas_root)
    write_plan(p, prepare_batches(p, "new", (), (), 354))
    assert p.batches_json.read_bytes() == committed
    assert sorted(f.name for f in p.batches_dir.iterdir()) == ["batch-01.md", "batch-02.md"]


def test_backfill_shows_the_curated_finding_without_its_approval_and_names_no_run_file(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    # a run file a manifest must never name sits beside it
    (p.dir / "merge.json").write_text("{}")
    code, out = _batch(p, "--mode", "backfill", "--fields", "cohorts,comparator")
    assert (code, out) == (
        0,
        "planned 1 batch for 1 paper (budget 170,000 bytes): 01 1 paper 55 bytes\n",
    )
    assert json.loads(p.batches_json.read_text())["batches"][0]["findings"] == ["CHDA:LIT:0009001"]
    assert json.loads(p.batches_json.read_text())["fields"] == ["cohorts", "comparator"]
    manifest = (p.batches_dir / "batch-01.md").read_text()
    assert "Fields to re-read: cohorts, comparator\n" in manifest
    assert "- id: CHDA:LIT:0009001\n" in manifest and "  screened: 1\n" in manifest
    assert "- title: A synthetic Holt-Oram family with a TBX5 frameshift.\n" in manifest
    for absent in ("approved_digest", "approved_by", "approved_on", "sha256:", "E.A.M"):
        assert absent not in manifest
    # a path into the run directory, not the agent names `agent:literature-extract/...`
    for text in (manifest, p.batches_json.read_text()):
        assert str(p.dir) not in text
        for run_file in ("/extract/", "/verify/", "/evidence/", "merge.json", "comparison.json"):
            assert run_file not in text
        for run_file in ("/judging/", "/judge/"):
            assert run_file not in text


def _with_findings(p: RunPaths) -> None:
    """Beside the approved 0009001 (no cohorts): 0009002 holds cohorts, 0009003 holds an empty
    list, 0009004 holds a comparator but no cohorts."""
    block = (
        "  - id: {fid}\n    publication: {pmid}\n    design: cohort_screen\n{extra}"
        "    methods: [exome]\n    phenotypes: [HP:0001631]\n    lesion_groups: [septal]\n"
        "    syndromic: isolated\n    shares_probands_with: unknown\n    locators:\n"
        "      - {{kind: in_text, element: T}}\n    summary: s\n    extracted_by: agent:x/y\n"
    )
    lit = p.root / "curation/literature/TBX5.yaml"
    with lit.open("a") as handle:
        for fid, pmid, extra in (
            ("0009002", "PMID:91000002", "    cohorts: [somewhere]\n"),
            ("0009003", "PMID:91000003", "    cohorts: []\n"),
            ("0009004", "PMID:91000004", "    comparator: null\n"),
        ):
            handle.write(block.format(fid=f"CHDA:LIT:{fid}", pmid=pmid, extra=extra))


def test_backfill_by_default_selects_only_findings_whose_declared_fields_are_all_empty(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _with_findings(p)
    code, out = _batch(p, "--mode", "backfill", "--fields", "cohorts")
    assert code == 0 and out.endswith("; skipped 1 finding already holding a declared field\n")
    plan = json.loads(p.batches_json.read_text())
    # 0009002 holds cohorts and is skipped; an empty list and an absent key are empty
    assert [(b["papers"], b["findings"]) for b in plan["batches"]] == [
        (
            ["PMID:91000001", "PMID:91000003", "PMID:91000004"],
            ["CHDA:LIT:0009001", "CHDA:LIT:0009003", "CHDA:LIT:0009004"],
        )
    ]


def test_backfill_with_two_declared_fields_skips_a_finding_filled_in_either(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _with_findings(p)
    # 0009001 holds screened, 0009002 holds cohorts: neither is empty in every declared field
    code, out = _batch(p, "--mode", "backfill", "--fields", "cohorts,screened")
    assert code == 0 and out.endswith("; skipped 2 findings already holding a declared field\n")
    plan = json.loads(p.batches_json.read_text())
    assert [b["findings"] for b in plan["batches"]] == [["CHDA:LIT:0009003", "CHDA:LIT:0009004"]]


def test_a_backfill_in_several_batches_gives_each_batch_the_findings_of_its_own_papers(
    tmp_path: Path, atlas_root: Path
) -> None:
    """``Batch.findings`` is what merge's backfill authorises a pass file against, so each batch
    lists only the findings on its own papers (measured: budget 100 gives three batches)."""
    p = _fresh(tmp_path, atlas_root)
    _with_findings(p)
    prepared = prepare_batches(p, "backfill", ("cohorts",), (), 100)
    assert [(b.id, b.papers, b.findings) for b in prepared.plan.batches] == [
        ("01", ("PMID:91000001",), ("CHDA:LIT:0009001",)),
        ("02", ("PMID:91000003",), ("CHDA:LIT:0009003",)),
        ("03", ("PMID:91000004",), ("CHDA:LIT:0009004",)),
    ]
    assert prepared.skipped == 1  # 0009002 holds cohorts


def test_an_explicit_findings_list_skips_nothing_so_the_summary_says_nothing_of_skips(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _with_findings(p)
    code, out = _batch(
        p, "--mode", "backfill", "--fields", "cohorts", "--findings", "CHDA:LIT:0009001"
    )
    assert (code, out) == (
        0,
        "planned 1 batch for 1 paper (budget 170,000 bytes): 01 1 paper 55 bytes\n",
    )


def test_prepare_refuses_a_mode_the_cli_does_not_plan(tmp_path: Path, atlas_root: Path) -> None:
    p = _fresh(tmp_path, atlas_root)
    with pytest.raises(BatchError, match="not planned by this command"):
        prepare_batches(p, "reread", (), (), 1000)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(f.relative_to(root)): f.read_bytes() for f in sorted(root.rglob("*")) if f.is_file()
    }


def _edit(p: RunPaths, path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def _already_batched(p: RunPaths) -> None:
    p.batches_json.write_text("{}")


def _leftover(p: RunPaths) -> None:
    p.batches_dir.mkdir()
    (p.batches_dir / "batch-09.md").write_text("old")


def _no_texts(p: RunPaths) -> None:
    p.texts.unlink()


def _unlisted(p: RunPaths) -> None:
    _edit(p, p.texts, '"pmid": "PMID:91000003"', '"pmid": "PMID:91000099"')


def _nothing_to_read(p: RunPaths) -> None:
    lit = p.root / "curation/literature/TBX5.yaml"
    for _ in range(4):
        _edit(p, lit, "decision: included", "decision: excluded")


def _no_text_no_abstract(p: RunPaths) -> None:
    _edit(
        p,
        p.texts,
        '"source": "abstract",\n      "text": null,\n      "abstract": '
        '"local/literature/texts/91000004/abstract.txt"',
        '"source": "none",\n      "text": null,\n      "abstract": null',
    )


def _no_year(p: RunPaths) -> None:
    _edit(p, p.texts, '"year": "2023"', '"year": ""')


def _no_literature(p: RunPaths) -> None:
    (p.root / "curation/literature/TBX5.yaml").unlink()


REFUSALS = [
    (_already_batched, ["--mode", "new"], "already batched"),
    (_leftover, ["--mode", "new"], "batch-09.md"),
    (_no_texts, ["--mode", "new"], "no texts.json"),
    (_unlisted, ["--mode", "new"], "PMID:91000003: not in texts.json"),
    (_nothing_to_read, ["--mode", "new"], "no paper to read"),
    (_no_text_no_abstract, ["--mode", "new"], "PMID:91000004: has neither"),
    (_no_year, ["--mode", "new"], "PMID:91000004: has no year"),
    (_no_literature, ["--mode", "new"], "no literature file"),
    (lambda p: None, ["--mode", "new", "--fields", "screened"], "belong to --mode backfill"),
    (lambda p: None, ["--mode", "backfill"], "needs --fields"),
    (lambda p: None, ["--mode", "backfill", "--fields", "nonesuch"], "'nonesuch'"),
    (lambda p: None, ["--mode", "backfill", "--fields", "approved_digest"], "may not re-read"),
    (lambda p: None, ["--mode", "backfill", "--fields", "id"], "may not re-read"),
    *[
        (
            lambda p, name=name: None,
            ["--mode", "backfill", "--fields", name],
            f"--fields '{name}': compare does not compare it as a field of its own",
        )
        for name in ("summary", "locators", "shares_probands_with", "review_status", "review_note")
    ],
    (
        lambda p: None,
        ["--mode", "backfill", "--fields", "screened", "--findings", "CHDA:LIT:0000042"],
        "no finding CHDA:LIT:0000042",
    ),
    (lambda p: None, ["--mode", "reread"], "not new or backfill"),
    (
        _with_findings,
        ["--mode", "backfill", "--fields", "cohorts", "--findings", "CHDA:LIT:0009002"],
        "CHDA:LIT:0009002 already holds cohorts",
    ),
    (
        lambda p: None,
        ["--mode", "backfill", "--fields", "screened"],
        "no paper to read",
    ),
]


@pytest.mark.parametrize("setup, args, message", REFUSALS)
def test_a_refusal_exits_2_and_leaves_every_file_unchanged(
    tmp_path: Path,
    atlas_root: Path,
    setup,
    args,
    message: str,  # type: ignore[no-untyped-def]
) -> None:
    p = _fresh(tmp_path, atlas_root)
    setup(p)
    before = _snapshot(p.root)
    code, out = _batch(p, *args)
    assert code == 2 and message in out
    assert _snapshot(p.root) == before
