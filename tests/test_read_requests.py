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
@pytest.mark.parametrize("what", ["missing", "outside", "pdf"])
def test_a_bad_named_file_writes_nothing_and_names_it(
    tmp_path: Path, atlas_root: Path, role: str, what: str
) -> None:
    p = _batched(tmp_path, atlas_root)
    root = p.root
    target = {
        "missing": f"{root}/local/literature/texts/91000002/nope.txt",
        "outside": f"{root}/../outside.txt",
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
        if (role, plan_mode) == ("judge", "backfill"):
            continue  # the judge role has no backfill schema (roles.py), so it never assembles
        for row in _rows(out):
            assemble(role, atlas_root, row["inputs"], row["mode"])
