"""``chd-curator read-requests``: the rows ``ask`` takes, from a run's manifests.

The run is the phase-2 fixture, batched by the real ``batch`` command (one batch, three papers).
Judge manifests are written by hand in ``compare``'s format (absolute ``- supplement:`` paths
without a size).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from chd_curator import ask as ask_module
from chd_curator.cli import app
from chd_curator.requests import named_files
from chd_curator.roles import assemble
from chd_curator.run import RunPaths
from run_fixture import copy_run_fixture

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_atlas_cli(stub_atlas: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHD_ATLAS_CLI", str(stub_atlas))


def _batched(tmp_path: Path, atlas_root: Path) -> RunPaths:
    p = copy_run_fixture(tmp_path)
    p.batches_json.unlink()
    shutil.copytree(atlas_root / "schemas", p.root / "schemas")
    result = runner.invoke(
        app, ["batch", "TBX5", "--run", "r1", "--mode", "new", "--root", str(p.root)]
    )
    assert result.exit_code == 0, result.output
    return p


def _requests(p: RunPaths, role: str, out: Path) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["read-requests", "TBX5", "--run", "r1", "--role", role, "--root", str(p.root)]
        + ["--out", str(out)],
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _judge_manifest(p: RunPaths, n: str, supplement: str) -> Path:
    texts = p.root / "local/literature/texts/91000002"
    path = p.judging / f"judge-{n}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        f"# Judging batch {n} of run r1 (gene TBX5)\n\n## A (first pass only)\n\n"
        f"- publication: PMID:91000002\n- text path: {texts}/fulltext.txt\n"
        f"- supplement: {supplement}\n\n## B (first pass only)\n\n"
        f"- text path: {texts}/fulltext.txt\n"
    )
    return path


def test_one_row_per_batch_with_the_manifest_and_every_text_it_names(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _batched(tmp_path, atlas_root)
    out = tmp_path / "req.jsonl"
    code, text = _requests(p, "extract", out)
    assert code == 0 and "wrote 1 request(s)" in text
    (row,) = _rows(out)
    manifest = (p.batches_dir / "batch-01.md").read_text()
    assert {k: row[k] for k in ("key", "gene", "batch", "mode")} == {
        "key": "01", "gene": "TBX5", "batch": "01", "mode": "new"
    }  # fmt: skip
    assert row["inputs"]["manifest"] == manifest
    texts = f"{p.root}/local/literature/texts"
    order = [
        f"{texts}/91000002/fulltext.txt",
        f"{texts}/91000002/abstract.txt",
        f"{texts}/91000002/supplements/s1.txt",
        f"{texts}/91000003/abstract.txt",
        f"{texts}/91000004/abstract.txt",
    ]
    assert named_files(manifest) == order
    expected = "\n".join(f"--- file: {n} ---\n{Path(n).read_text()}" for n in order)
    assert row["inputs"]["texts"] == expected


def test_blind_read_rows_equal_extract_rows(tmp_path: Path, atlas_root: Path) -> None:
    p = _batched(tmp_path, atlas_root)
    _requests(p, "extract", tmp_path / "a.jsonl")
    _requests(p, "blind-read", tmp_path / "b.jsonl")
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


def test_one_row_per_judge_manifest(tmp_path: Path, atlas_root: Path) -> None:
    p = _batched(tmp_path, atlas_root)
    sup = f"{p.root}/local/literature/texts/91000002/supplements/s1.txt"
    _judge_manifest(p, "01", sup)
    out = tmp_path / "j.jsonl"
    assert _requests(p, "judge", out)[0] == 0
    (row,) = _rows(out)
    assert (row["key"], row["batch"], row["mode"]) == ("01", "01", "new")
    marks = [ln for ln in row["inputs"]["texts"].splitlines() if ln.startswith("--- file: ")]
    names = [ln.removeprefix("--- file: ").removesuffix(" ---") for ln in marks]
    assert names == [  # the text named twice is inlined once, the size-less supplement parsed
        f"{p.root}/local/literature/texts/91000002/fulltext.txt",
        sup,
    ]
    _judge_manifest(p, "07", sup)  # a manifest of a batch that is not in batches.json
    code, text = _requests(p, "judge", out)
    assert code == 1 and "judge-07.md" in text


@pytest.mark.parametrize("role", ["extract", "judge"])
@pytest.mark.parametrize("what", ["missing", "outside", "absolute", "pdf"])
def test_a_bad_named_file_writes_nothing_and_names_it(
    tmp_path: Path, atlas_root: Path, role: str, what: str
) -> None:
    p = _batched(tmp_path, atlas_root)
    root = p.root
    target = {
        "missing": f"{root}/local/literature/texts/91000002/nope.txt",
        "outside": f"{root}/../outside.txt",
        "absolute": str(root.parent / "outside.txt"),
        "pdf": f"{root}/local/literature/texts/91000002/paper.pdf",
    }[what]
    (root / "local/literature/texts/91000002/paper.pdf").write_bytes(b"plain text under a pdf name")
    (root.parent / "outside.txt").write_text("secret")  # exists, so only the root check refuses it
    if role == "judge":
        _judge_manifest(p, "01", target)
    else:
        path = p.batches_dir / "batch-01.md"
        path.write_text(path.read_text() + f"- supplement: {target} (3 bytes)\n")
    out = tmp_path / "req.jsonl"
    code, text = _requests(p, role, out)
    assert code == 1 and not out.exists()
    assert Path(target).name in text


@pytest.mark.parametrize("plan_mode", ["new", "backfill", "reread"])
def test_the_rows_pass_asks_preflight_and_assemble_for_each_mode_and_role(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, plan_mode: str
) -> None:
    p = _batched(tmp_path, atlas_root)
    plan = json.loads(p.batches_json.read_text())
    plan["mode"] = plan_mode
    if plan_mode == "backfill":
        plan["fields"] = ["study_type"]
    p.batches_json.write_text(json.dumps(plan))
    sup = f"{p.root}/local/literature/texts/91000002/supplements/s1.txt"
    _judge_manifest(p, "01", sup)
    for role in ("extract", "blind-read", "judge"):
        out = tmp_path / f"{role}.jsonl"
        assert _requests(p, role, out)[0] == 0
        reqs = ask_module._load_requests(out, True)
        ask_module._preflight_check_batch(reqs, role, p.root, "r1", stub_atlas)
        for row in _rows(out):
            assemble(role, atlas_root, row["inputs"], row["mode"])


def _command(p: RunPaths, out: Path) -> tuple[int, str]:
    return _requests(p, "extract", out)


def test_a_manifest_naming_no_file_is_refused(tmp_path: Path, atlas_root: Path) -> None:
    p = _batched(tmp_path, atlas_root)
    (p.batches_dir / "batch-01.md").write_text("# Batch 01 of run r1 (gene TBX5, mode new)\n")
    out = tmp_path / "req.jsonl"
    code, text = _command(p, out)
    assert code == 1 and not out.exists() and "batch-01.md" in text and "names no file" in text


def test_a_line_that_looks_like_a_file_but_is_not_a_known_form_is_refused(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _batched(tmp_path, atlas_root)
    path = p.batches_dir / "batch-01.md"
    path.write_text(path.read_text() + "- figure path: x.txt\n")
    out = tmp_path / "req.jsonl"
    code, text = _command(p, out)
    assert code == 1 and not out.exists()
    assert "batch-01.md" in text and "- figure path: x.txt" in text
    # a title that merely mentions a supplement or a path is not a file line
    path.write_text(
        path.read_text().replace("- figure path: x.txt\n", "")
        + "- title: Supplement and path: a note\n"
    )
    assert _command(p, out)[0] == 0


# ---- the fix wave -------------------------------------------------------------------------------


def test_a_text_linked_from_inside_the_root_is_read_through_the_link(
    tmp_path: Path, atlas_root: Path
) -> None:
    """R-SWA-54: in a scratch root every pre-existing text is a link into the atlas, and the
    resolved path is outside the root; containment is lexical, the read follows the link."""
    p = _batched(tmp_path, atlas_root)
    inside = p.root / "local/literature/texts/91000002/fulltext.txt"
    elsewhere = tmp_path / "atlas-copy" / "fulltext.txt"
    elsewhere.parent.mkdir()
    elsewhere.write_text("the atlas's copy, reached through a link\n")
    inside.unlink()
    inside.symlink_to(elsewhere)
    out = tmp_path / "req.jsonl"
    code, text = _requests(p, "extract", out)
    assert code == 0, text
    (row,) = _rows(out)
    assert (
        f"--- file: {inside} ---\nthe atlas's copy, reached through a link\n"
        in (row["inputs"]["texts"])
    )


@pytest.mark.parametrize("role", ["extract", "blind-read", "judge"])
def test_reader_and_judge_rows_carry_the_registries_the_rules_cite(
    tmp_path: Path, atlas_root: Path, role: str
) -> None:
    """R-SWA-53: the cited rules send the reader to curation/cohorts.yaml and
    curation/phenotypes.yaml, which a model with no tools cannot open."""
    p = _batched(tmp_path, atlas_root)
    _judge_manifest(p, "01", f"{p.root}/local/literature/texts/91000002/supplements/s1.txt")
    out = tmp_path / "req.jsonl"
    assert _requests(p, role, out)[0] == 0
    (row,) = _rows(out)
    expected = "\n".join(
        f"--- file: {rel} ---\n{(p.root / rel).read_text()}"
        for rel in ("curation/cohorts.yaml", "curation/phenotypes.yaml")
    )
    assert row["inputs"]["registries"] == expected
    (p.root / "curation/phenotypes.yaml").unlink()
    code, text = _requests(p, role, tmp_path / "again.jsonl")
    assert code == 1 and "curation/phenotypes.yaml" in text


def _decisions_file(path: Path, findings: list[dict[str, Any]], symbol: str = "TBX5") -> Path:
    stamp = "2026-11-27T10:00:00Z"
    path.write_text(
        json.dumps(
            {
                "format": 1, "gene": "HGNC:11604", "symbol": symbol,
                "page": {"built_on": stamp, "atlas_commit": None,
                         "literature_sha256": "sha256:" + "a" * 64},
                "reviewer": {"name": "E.A.M", "role": "owner"},
                "session": {"started_at": stamp, "finished_at": stamp, "active_ms": 1},
                "findings": [
                    {"shown_digest": "sha256:" + "b" * 64, "active_ms": 1,
                     "first_opened_at": stamp, "decided_at": stamp, **f}
                    for f in findings
                ],
                "papers_seen": [], "triage": None,
            }
        )
    )  # fmt: skip
    return path


def test_the_judge_rows_carry_the_owners_fix_notes_for_the_findings_they_cover(
    tmp_path: Path, atlas_root: Path
) -> None:
    """R-SWA-45: a fix round starts at JUDGE with the owner's decisions; without the notes in
    the judge's input, the judge answers the same question and a cache hit returns the same
    answer, so the corrections vanish."""
    p = _batched(tmp_path, atlas_root)
    sup = f"{p.root}/local/literature/texts/91000002/supplements/s1.txt"
    manifest = _judge_manifest(p, "01", sup)
    manifest.write_text(
        manifest.read_text()
        .replace("## A (first pass only)", "## CHDA:LIT:0009002 (first pass only)")
        .replace("## B (first pass only)", "## CHDA:LIT:0009003 (blind pass CHDA:LIT:0801002)")
    )
    plain = tmp_path / "plain.jsonl"
    assert _requests(p, "judge", plain)[0] == 0
    assert _rows(plain)[0]["inputs"]["owner_notes"] == "none"
    decisions = _decisions_file(
        tmp_path / "d.json",
        [
            {"id": "CHDA:LIT:0009003", "decision": "fix", "note": "screened is 2, see Table 1"},
            {"id": "CHDA:LIT:0009002", "decision": "approve", "note": ""},
            {"id": "CHDA:LIT:0009999", "decision": "fix", "note": "another batch's finding"},
        ],
    )
    noted = tmp_path / "noted.jsonl"
    result = runner.invoke(
        app,
        ["read-requests", "TBX5", "--run", "r1", "--role", "judge", "--root", str(p.root),
         "--out", str(noted), "--decisions", str(decisions)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    notes = _rows(noted)[0]["inputs"]["owner_notes"]
    assert notes == "- CHDA:LIT:0009003: screened is 2, see Table 1"
    digests = [
        assemble("judge", atlas_root, _rows(path)[0]["inputs"]).digest for path in (plain, noted)
    ]
    assert digests[0] != digests[1]  # so a fix round is never answered from the cache
    readers = runner.invoke(
        app,
        ["read-requests", "TBX5", "--run", "r1", "--role", "extract", "--root", str(p.root),
         "--out", str(tmp_path / "x.jsonl"), "--decisions", str(decisions)],
    )  # fmt: skip
    assert readers.exit_code == 2 and "judge" in readers.output
    other = _decisions_file(tmp_path / "o.json", [], symbol="GATA4")
    refused = runner.invoke(
        app,
        ["read-requests", "TBX5", "--run", "r1", "--role", "judge", "--root", str(p.root),
         "--out", str(tmp_path / "y.jsonl"), "--decisions", str(other)],
    )  # fmt: skip
    assert refused.exit_code == 2 and "GATA4" in refused.output
