"""``base`` and ``package``: the digests a run starts from, and the proposal it ends with."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chd_curator.cli import app
from test_merge import _pubmed
from test_scratch import MUTABLE_TBX5, make_atlas, merge, scratch, snapshot

runner = CliRunner()


def base_json(atlas: Path, out: Path) -> dict[str, object]:
    result = runner.invoke(
        app, ["base", "--atlas", str(atlas), "--gene", "TBX5"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    out.write_text(result.output)
    loaded: dict[str, object] = json.loads(result.output)
    return loaded


def package(work: Path, base: Path, out: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["package", "--root", str(work), "--gene", "TBX5", "--run", "r1", "--base", str(base)]
        + ["--out", str(out), *args],
        catch_exceptions=False,
    )
    return result.exit_code, result.output


def test_package_json_records_the_atlas_commit_and_the_models(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    (atlas / "RELEASE").write_text("x")
    git = ["git", "-C", str(atlas)]
    import subprocess

    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, *ident, "commit", "-q", "-m", "x"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    base = base_json(atlas, tmp_path / "base.json")
    assert base["atlas_commit"] == head
    digests = base["digests"]
    assert isinstance(digests, dict) and list(digests) == sorted(MUTABLE_TBX5)
    scratch(atlas, tmp_path / "work")
    _pubmed(monkeypatch)
    merge(tmp_path / "work", stub_atlas)
    models = tmp_path / "models.json"
    models.write_text(json.dumps([{"role": "extract", "model": "m-1"}]))
    code, out = package(
        tmp_path / "work", tmp_path / "base.json", tmp_path / "pkg", "--models", str(models)
    )
    assert code == 0, out
    meta = json.loads((tmp_path / "pkg/package.json").read_text())
    assert meta == {
        "format": 1,
        "symbol": "TBX5",
        "run_id": "r1",
        "base": digests,
        "atlas_commit": head,
        "models": [{"role": "extract", "model": "m-1"}],
        "review": None,
    }
    work = snapshot(tmp_path / "work")
    pkg = snapshot(tmp_path / "pkg")
    for rel in MUTABLE_TBX5:
        assert pkg[f"proposal/{rel}"] == work[rel]
    assert pkg["run/merge.json"] == work["local/literature/runs/TBX5/r1/merge.json"]
    assert "texts/91000002/supplements/s1.txt" in pkg
    assert "review.html" not in pkg
    again, message = package(tmp_path / "work", tmp_path / "base.json", tmp_path / "pkg")
    assert again == 2 and "already exists" in message


def test_a_git_free_atlas_has_a_null_commit_and_a_review_page_is_packaged(
    tmp_path: Path, atlas_root: Path
) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    base = base_json(atlas, tmp_path / "base.json")
    assert base["atlas_commit"] is None
    scratch(atlas, tmp_path / "work")
    (tmp_path / "work/local/literature/runs/TBX5/r1/review.html").write_text("<html>")
    assert package(tmp_path / "work", tmp_path / "base.json", tmp_path / "pkg")[0] == 0
    assert (tmp_path / "pkg/review.html").read_text() == "<html>"
    assert json.loads((tmp_path / "pkg/package.json").read_text())["review"] == "review.html"
