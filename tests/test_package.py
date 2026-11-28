"""``base`` and ``package``: the digests a run starts from, and the proposal it ends with."""

from __future__ import annotations

import hashlib
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
    folders = [f"local/literature/texts/{n}" for n in ("91000001", "91000002", "91000003")]
    folders.append("local/literature/texts/91000004")
    assert isinstance(digests, dict) and sorted(digests) == sorted([*MUTABLE_TBX5, *folders])
    one = atlas / folders[1]
    files = sorted((f.relative_to(one).as_posix(), f) for f in one.rglob("*") if f.is_file())
    formula = "".join(f"{rel}\0{hashlib.sha256(f.read_bytes()).hexdigest()}\n" for rel, f in files)
    assert digests[folders[1]] == hashlib.sha256(formula.encode()).hexdigest()
    scratch(atlas, tmp_path / "work")
    _pubmed(monkeypatch)
    merge(tmp_path / "work", stub_atlas)
    code, out = package(tmp_path / "work", tmp_path / "base.json", tmp_path / "pkg")
    assert code == 0, out
    meta = json.loads((tmp_path / "pkg/package.json").read_text())
    assert meta == {
        "format": 1,
        "symbol": "TBX5",
        "run_id": "r1",
        "base": digests,
        "atlas_commit": head,
        "models": [],
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


def _answers(path: Path, *rows: dict[str, object]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_the_models_are_the_ones_the_answers_used_with_their_register_verdicts(
    tmp_path: Path, atlas_root: Path
) -> None:
    """R-SWA-49 (spec section 5: "the models and register entries used"): one entry per role,
    model and rules the answers carry, its verdict from the register."""
    atlas = make_atlas(tmp_path, atlas_root)
    base_json(atlas, tmp_path / "base.json")
    scratch(atlas, tmp_path / "work")
    failed = "openai_compat:Qwen3-30B-A3B-Instruct-2507-Q4_K_M@7c5f79b9acd5"  # a `fail` entry
    screen = _answers(
        tmp_path / "screen.jsonl",
        {"key": "PMID:1", "model_key": failed, "rules_digest": "r1", "parsed": {}},
        {"key": "PMID:2", "model_key": "openai_compat:mystery", "rules_digest": "r1"},
        {"key": "PMID:3", "error": "HTTP 400", "attempts": 1},
    )
    extract = _answers(
        tmp_path / "extract.jsonl",
        {"key": "TBX5:01", "model_key": "claude_code:claude-sonnet-5-5", "rules_digest": "r2"},
        {"key": "TBX5:02", "model_key": "claude_code:claude-sonnet-5-5", "rules_digest": "r2"},
    )
    judge = _answers(
        tmp_path / "judge.jsonl", {"key": "TBX5:01", "model_key": "replay:m", "rules_digest": "r3"}
    )
    code, out = package(
        tmp_path / "work", tmp_path / "base.json", tmp_path / "pkg",
        "--answers", f"triage-screen={screen}", "--answers", f"extract={extract}",
        "--answers", f"judge={judge}",
    )  # fmt: skip
    assert code == 0, out
    assert json.loads((tmp_path / "pkg/package.json").read_text())["models"] == [
        {"role": "extract", "model_key": "claude_code:claude-sonnet-5-5",
         "verdict": "reference", "rules_digest": "r2"},
        {"role": "judge", "model_key": "replay:m", "verdict": "test-backend",
         "rules_digest": "r3"},
        {"role": "triage-screen", "model_key": failed, "verdict": "fail", "rules_digest": "r1"},
        {"role": "triage-screen", "model_key": "openai_compat:mystery",
         "verdict": "unqualified", "rules_digest": "r1"},
    ]  # fmt: skip
    bad, message = package(
        tmp_path / "work", tmp_path / "base.json", tmp_path / "pkg2", "--answers", f"x={judge}"
    )
    assert bad == 2 and "'x' is not a role" in message


def test_a_base_without_text_digests_is_refused_up_front(  # R-SWA-62 (3)
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Such a base would make `integrate` refuse every pre-existing text as "not in the atlas
    when the run started", which is untrue; `package` says what is wrong, writing nothing."""
    atlas = make_atlas(tmp_path, atlas_root)
    base = base_json(atlas, tmp_path / "base.json")
    assert base["texts_digested"] is True
    del base["texts_digested"]
    (tmp_path / "old.json").write_text(json.dumps(base))
    scratch(atlas, tmp_path / "work")
    _pubmed(monkeypatch)
    merge(tmp_path / "work", stub_atlas)
    code, out = package(tmp_path / "work", tmp_path / "old.json", tmp_path / "pkg")
    assert code == 2 and not (tmp_path / "pkg").exists()
    assert "texts_digested" in out and "older curator" in out
