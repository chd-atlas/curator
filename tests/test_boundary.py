"""The two structural promises: no atlas import, and CI runs every test."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parent.parent


def test_nothing_in_curator_imports_chd_atlas() -> None:
    files = sorted((REPO / "src" / "chd_curator").rglob("*.py"))
    assert files, "no source files found, so the walk proved nothing"
    offenders: list[str] = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] == "chd_atlas"]
    assert offenders == []


def test_ci_checks_out_the_atlas_and_runs_every_test() -> None:
    ci: Any = YAML(typ="safe").load(REPO / ".github" / "workflows" / "ci.yml")
    steps = ci["jobs"]["ci"]["steps"]
    checkouts = [s["with"] for s in steps if "checkout" in s.get("uses", "") and "with" in s]
    assert any(
        w.get("repository") == "chd-atlas/chd-atlas.github.io" and w.get("ref") == "dev"
        for w in checkouts
    )
    pytest_steps = [s["run"] for s in steps if "pytest" in s.get("run", "")]
    assert len(pytest_steps) == 1
    words = pytest_steps[0].split()
    assert not {"-m", "-k", "--deselect"} & set(words)
    # YAML 1.2 would read the bare key `on` as a string; safe-loader keeps it so.
    triggers = ci.get("on", ci.get(True))
    assert "schedule" in triggers
