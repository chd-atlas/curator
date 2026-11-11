"""curator against the REAL atlas: ``CHD_ATLAS_CLI`` and a copy of ``CHD_ATLAS_ROOT``.

The unit tests use a stub atlas, so nothing there shows that curator's output is
what the atlas accepts. These tests run the atlas's own ``literature target`` and
``validate`` over a throwaway copy of the checkout. PubMed is still faked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator import cli
from chd_curator.cli import app
from fake_pubmed import FakePubMed, papers
from test_skills import ATLAS_COMMANDS, skill_examples

runner = CliRunner()
# .git, .venv, local, dist, .superpowers, CLAUDE.md, .claude and superpowers are
# gitignored local files or symlinks on a development machine and absent on CI.
IGNORED = (".git", ".venv", "local", "dist", ".superpowers", "CLAUDE.md", ".claude", "superpowers")


def _atlas(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.environ["CHD_ATLAS_CLI"], *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def atlas_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "atlas"
    shutil.copytree(
        os.environ["CHD_ATLAS_ROOT"],
        copy,
        symlinks=False,
        ignore=shutil.ignore_patterns(*IGNORED),
    )
    return copy


def _validate(root: Path) -> None:
    run = _atlas("validate", "--root", str(root))
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_real_target_answers_for_tbx5() -> None:
    run = _atlas("literature", "target", "TBX5", "--root", os.environ["CHD_ATLAS_ROOT"])
    assert run.returncode == 0, run.stdout + run.stderr
    answer = json.loads(run.stdout)
    assert answer["gene"] == "HGNC:11604"
    assert answer["published"] is True
    assert answer["query"].startswith('("TBX5"[tiab]')


def test_a_search_on_a_copy_of_the_atlas_validates(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = None
    for symbol in ("GATA4", "NKX2-5", "GATA6"):
        run = _atlas("literature", "target", symbol, "--root", str(atlas_copy))
        answer = json.loads(run.stdout) if run.returncode == 0 else {}
        file = atlas_copy / "curation" / "literature" / f"{answer.get('symbol', symbol)}.yaml"
        if answer.get("published") is True and not file.exists():
            chosen = (symbol, answer, file)
            break
    assert chosen is not None, "none of GATA4, NKX2-5, GATA6 is published without a literature file"
    symbol, answer, file = chosen
    monkeypatch.setattr(cli, "_fetch", FakePubMed(papers("9", "25", "100")))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "search",
            symbol,
            "--run-on",
            "2026-11-11",
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 0, result.stdout
    written = YAML(typ="safe").load(file.read_text())
    assert written["gene"] == answer["gene"]
    assert written["searches"][0]["query"] == answer["query"]
    assert [t["pmid"] for t in written["triage"]] == ["PMID:9", "PMID:25", "PMID:100"]
    _validate(atlas_copy)


def test_add_publication_on_a_copy_of_the_atlas_validates(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = atlas_copy / "curation" / "publications.yaml"
    pmid = "9"
    assert f"PMID:{pmid}\n" not in path.read_text()
    monkeypatch.setattr(cli, "_fetch", FakePubMed(papers(pmid)))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "add-publication",
            pmid,
            "--study-type",
            "case_report",
            "--reading",
            "A single-family report in the abstract.",
            "--root",
            str(atlas_copy),
        ],
    )

    assert result.exit_code == 0, result.stdout
    ids = [p["id"] for p in YAML(typ="safe").load(path.read_text())["publications"]]
    assert ids[-1] == "PMID:9"
    _validate(atlas_copy)


def _tbx5_target(text: str) -> tuple[int, str, str]:
    """`(line index, finding id, verifier)` of an approved, verified, unambiguous finding
    whose ``verified_by`` line directly follows ``extracted_by``."""
    data = YAML(typ="safe").load(text)
    keys = [
        (f["publication"], f["design"], tuple(sorted(f.get("cohorts") or [])))
        for f in data["findings"]
    ]
    unique = {
        f["id"]
        for f, k in zip(data["findings"], keys, strict=True)
        if keys.count(k) == 1 and f.get("approved_by") and f.get("verified_by")
    }
    lines = text.splitlines(keepends=True)
    current = ""
    for index, line in enumerate(lines):
        if line.startswith("  - id: "):
            current = line.removeprefix("  - id: ").strip()
        if (
            current in unique
            and line.startswith("    extracted_by: ")
            and lines[index + 1].startswith("    verified_by: ")
        ):
            return index + 1, current, lines[index + 1].removeprefix("    verified_by: ").strip()
    raise AssertionError("no approved, unambiguous finding has verified_by after extracted_by")


def test_mark_verified_restores_a_removed_mark_on_the_real_tbx5_byte_for_byte(
    atlas_copy: Path,
) -> None:
    """The phase's acceptance point: curator's one fact-writing field round-trips the real file.

    One approved finding loses its ``verified_by`` line; the unchanged findings are fed back
    as a blind pass; ``diff --mark-verified`` restores exactly that line. The pass holds
    only the findings that carried a verified_by originally: the others (8 of 73 when
    measured) agree with themselves too, and marking them would add lines the original
    does not have.
    """
    path = atlas_copy / "curation" / "literature" / "TBX5.yaml"
    original = path.read_text()
    index, finding_id, verifier = _tbx5_target(original)
    lines = original.splitlines(keepends=True)
    del lines[index]
    path.write_text("".join(lines))
    assert path.read_text() != original

    findings = [f for f in YAML(typ="safe").load(original)["findings"] if f.get("verified_by")]
    second = atlas_copy / "second-pass.yaml"
    with second.open("w") as handle:
        YAML(typ="safe").dump({"findings": findings}, handle)

    result = runner.invoke(
        app,
        [
            "diff",
            "TBX5",
            "--against",
            str(second),
            "--mark-verified",
            verifier,
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert f"marked verified (1): {finding_id}\n" in result.stdout
    assert path.read_text() == original
    run = _atlas("validate", "--root", str(atlas_copy))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "LIT015" not in run.stdout + run.stderr


_VALIDATE_SCRIPT = """
import json, sys
from chd_atlas.models.gene_literature import Finding, TriageEntry
data = json.load(sys.stdin)
for record in data["findings"]:
    Finding.model_validate(record)
for record in data["triage"]:
    TriageEntry.model_validate(record)
"""


def _through_the_atlas_models(
    findings: list[Any], triage: list[Any]
) -> subprocess.CompletedProcess[str]:
    """The atlas's own interpreter validates the records; curator imports nothing of it."""
    return subprocess.run(
        [os.environ["CHD_ATLAS_PYTHON"], "-c", _VALIDATE_SCRIPT],
        input=json.dumps({"findings": findings, "triage": triage}),
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_skill_examples_validate_through_the_atlas_models() -> None:
    findings, triage = skill_examples()
    assert findings and triage
    good = _through_the_atlas_models(findings, triage)
    assert good.returncode == 0, good.stdout + good.stderr
    # Without this, a script that validated nothing would also exit 0.
    broken = {**findings[0], "design": "case_report", "screened": 5}
    bad = _through_the_atlas_models([broken], [])
    assert bad.returncode != 0
    assert "ValidationError" in bad.stderr


def test_the_atlas_commands_the_skills_name_exist() -> None:
    review = _atlas("literature", "review", "--help")
    assert review.returncode == 0, review.stdout + review.stderr
    for option in sorted(ATLAS_COMMANDS["literature review"]):
        assert option in review.stdout, option
    validate = _atlas("validate", "--help")
    assert validate.returncode == 0, validate.stdout + validate.stderr
