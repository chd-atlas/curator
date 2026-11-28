"""A run planned in two roots gives the same ``ask`` requests (note N11, R-SWA-63).

Manifests used to carry the directory they were written in, so the same run planned in two
pipeline work directories gave different request rows, a different input digest and a cache miss
that paid a capable model again. The run is the shared fixture, merged, batched and compared by
the real commands in each of two roots.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chd_curator.batch import prepare_batches, write_plan
from chd_curator.cli import app
from run_fixture import copy_run_fixture
from test_merge import _merge, _pubmed

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_atlas_cli(stub_atlas: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHD_ATLAS_CLI", str(stub_atlas))


def _requests_in(
    where: Path, atlas_root: Path, stub: Path, mp: pytest.MonkeyPatch
) -> dict[str, bytes]:
    where.mkdir(parents=True)
    p = copy_run_fixture(where)
    shutil.copytree(atlas_root / "schemas", p.root / "schemas")
    root = str(p.root)
    _pubmed(mp)
    original = p.batches_json.read_bytes()
    p.batches_json.unlink()  # plan the papers no finding cites, as a run does before merging
    write_plan(p, prepare_batches(p, "new", [], [], 354))  # the budget of the fixture's own plan
    assert p.batches_json.read_bytes() == original  # so merge and compare find the plan they know
    code, output = _merge(p, stub)
    assert code == 0, output
    compare = ["compare", "TBX5", "--run", "r1", "--root", root, "--atlas-cli", str(stub)]
    result = runner.invoke(app, compare, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    out: dict[str, bytes] = {}
    for role in ("extract", "judge"):
        path = where / f"{role}.jsonl"
        args = ["read-requests", "TBX5", "--run", "r1", "--role", role, "--root", root]
        result = runner.invoke(app, [*args, "--out", str(path)], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        out[role] = path.read_bytes()
    return out


def test_the_same_run_planned_in_two_roots_gives_byte_identical_requests(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _requests_in(tmp_path / "one", atlas_root, stub_atlas, monkeypatch)
    second = _requests_in(tmp_path / "two-elsewhere", atlas_root, stub_atlas, monkeypatch)
    assert first["extract"] == second["extract"]
    assert first["judge"] == second["judge"]
    for payload in first.values():
        assert str(tmp_path).encode() not in payload  # nothing names the directory it ran in
