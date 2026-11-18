"""curator against the REAL atlas: ``CHD_ATLAS_CLI`` and a copy of ``CHD_ATLAS_ROOT``.

The unit tests use a stub atlas, so nothing there shows that curator's output is
what the atlas accepts. These tests run the atlas's own ``literature target`` and
``validate`` over a throwaway copy of the checkout. PubMed is still faked.
"""

from __future__ import annotations

import copy
import csv
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator import cli
from chd_curator import variantvalidator as vv
from chd_curator.cli import app
from fake_pubmed import FakePubMed, papers
from test_skills import ATLAS_COMMANDS, skill_examples

runner = CliRunner()
# .git, .venv, local, dist, .superpowers, CLAUDE.md, .claude and superpowers are
# gitignored local files or symlinks on a development machine and absent on CI.
IGNORED = (".git", ".venv", "local", "dist", ".superpowers", "CLAUDE.md", ".claude", "superpowers")


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """``text`` without ANSI escape sequences. With GITHUB_ACTIONS=true the atlas's
    typer/Rich help styles ``--against`` as two segments, ``\\x1b[1;36m-\\x1b[0m
    \\x1b[1;36m-against\\x1b[0m``, which no option-name match can see through."""
    return _ANSI.sub("", text)


def _atlas(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the atlas CLI with colour and wrapping off, and ANSI stripped from the output."""
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    run = subprocess.run(
        [os.environ["CHD_ATLAS_CLI"], *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    run.stdout = _plain(run.stdout)
    run.stderr = _plain(run.stderr)
    return run


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


def _registry_targets(root: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """`(symbol, the real target's answer)` for each gene of ``mirrors/genes.tsv``, in file
    order, asking the atlas lazily so a caller stops at the first gene it wants."""
    with (root / "mirrors" / "genes.tsv").open(newline="", encoding="utf-8") as handle:
        symbols = [row["symbol"] for row in csv.DictReader(handle, delimiter="\t")]
    for symbol in symbols:
        run = _atlas("literature", "target", symbol, "--root", str(root))
        assert run.returncode == 0, run.stdout + run.stderr
        yield symbol, json.loads(run.stdout)


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
    for symbol, answer in _registry_targets(atlas_copy):
        file = atlas_copy / "curation" / "literature" / f"{answer['symbol']}.yaml"
        if answer["published"] is True and not file.exists():
            chosen = (symbol, answer, file)
            break
    assert chosen is not None, "no registered gene is published without a literature file"
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


def test_a_search_on_a_registered_gene_outside_the_gate_is_refused_before_pubmed(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real registry holds genes the atlas does not publish (BRCA1 is not one of them:
    it is not registered), so the gate is asked of the real atlas, not a stub."""
    unpublished = next(
        ((s, a) for s, a in _registry_targets(atlas_copy) if a["published"] is False), None
    )
    assert unpublished is not None, "every registered gene is published, so the gate is untested"
    symbol, answer = unpublished
    literature = atlas_copy / "curation" / "literature"
    files_before = sorted(p.name for p in literature.iterdir())
    fake = FakePubMed(papers("9"))
    monkeypatch.setattr(cli, "_fetch", fake)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "search",
            symbol,
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 2, result.stdout
    assert result.stdout == (
        f"error: {answer['symbol']} ({answer['gene']}) is outside the publication gate; "
        "its literature would reach no page\n"
    )
    assert fake.urls == []
    assert sorted(p.name for p in literature.iterdir()) == files_before


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
    only the findings that carried a verified_by originally: the others (those with none)
    agree with themselves too, and marking them would add lines the original does not have.
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


def _names_option(text: str, option: str) -> bool:
    """Whether ``text`` names the whole option, not a prefix of a longer one."""
    return re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", text) is not None


def test_an_option_name_is_matched_whole() -> None:
    assert _names_option("  --on TEXT  Date.\n", "--on") is True
    assert _names_option("  --only TEXT\n  --not-on X\n  x--on\n", "--on") is False


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("\x1b[1;36m-\x1b[0m\x1b[1;36m-against\x1b[0m  TEXT\n", True),
        ("\x1b[1;36m-\x1b[0m\x1b[1;36m-against-all\x1b[0m  TEXT\n", False),
        ("--against\n", True),
    ],
)
def test_a_coloured_split_option_is_recognised_after_stripping(text: str, named: bool) -> None:
    if text.startswith("\x1b[1;36m-\x1b[0m\x1b[1;36m-against "):
        assert _names_option(text, "--against") is False  # unstripped, the split hides it
    assert _names_option(_plain(text), "--against") is named


def test_the_atlas_commands_the_skills_name_exist() -> None:
    review = _atlas("literature", "review", "--help")
    assert review.returncode == 0, review.stdout + review.stderr
    for option in sorted(ATLAS_COMMANDS["literature review"]):
        assert _names_option(review.stdout, option), option
    target = _atlas("literature", "target", "--help")
    assert target.returncode == 0, target.stdout + target.stderr
    validate = _atlas("validate", "--help")
    assert validate.returncode == 0, validate.stdout + validate.stderr


def test_normalise_on_a_copy_of_the_atlas_validates_without_variant_issues(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A placed variant, a refused one keeping the paper's residue (D18) and one whose names
    disagree, written by ``normalise`` into the real TBX5 file, raise no LIT016-LIT025.

    VariantValidator answers from two real fixtures; the atlas is the real atlas. The new
    finding is a copy of an existing one with its approval removed and three variants added.
    """
    file = atlas_copy / "curation" / "literature" / "TBX5.yaml"
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    registry = atlas_copy / "curation" / ".id_registry.yaml"
    counter = re.search(r"^  LIT: (\d+)$", registry.read_text(), re.M)
    assert counter is not None
    next_number = int(counter[1]) + 1
    registry.write_text(registry.read_text().replace(counter[0], f"  LIT: {next_number}"))
    new_id = f"CHDA:LIT:{next_number:07d}"
    assert new_id not in "".join(p.read_text() for p in file.parent.glob("*.yaml"))
    data = yaml.load(file.read_text())
    source = next(f for f in data["findings"] if f["id"] == "CHDA:LIT:0000027")
    new = copy.deepcopy(source)
    new["id"] = new_id
    for name in ("approved_by", "approved_on", "approved_digest"):
        del new[name]
    new["design"] = "cohort_screen"
    new["screened"] = 3
    new["carriers"] = {"de_novo": 3, "inherited": 0, "origin_unknown": 0}

    def named(number: int, c: str, p: str) -> dict[str, Any]:
        return {
            "id": number,
            "as_reported": f"{c} ({p})",
            "locator": {"kind": "in_text", "element": "Table 2"},
            "kind": "snv_indel",
            "placement": "mane",
            "reported": {"c": c, "p": p},
            "carriers": {"de_novo": 1, "inherited": 0, "origin_unknown": 0},
        }

    new["variants"] = [
        named(1, "c.835C>T", "p.R279X"),
        named(2, "c.998G>T", "p.D111Y"),
        named(3, "c.835C>T", "p.R279Q"),
    ]
    data["findings"].append(new)
    with file.open("w") as handle:
        yaml.dump(data, handle)

    fixtures = Path(__file__).parent / "fixtures" / "variantvalidator"
    answers = {
        vv.url("NM_181486.4:c.835C>T", "NM_181486.4"): (fixtures / "tbx5_c835.json").read_bytes(),
        vv.url("NM_181486.4:c.998G>T", "NM_181486.4"): (
            fixtures / "tbx5_c998_warning.json"
        ).read_bytes(),
    }
    monkeypatch.setattr(cli, "_vv_fetch", lambda u: answers[u])

    result = runner.invoke(
        app,
        [
            "normalise",
            "TBX5",
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 0, result.stdout
    run = _atlas("validate", "--root", str(atlas_copy))
    report = run.stdout + run.stderr
    assert run.returncode == 0, report
    assert not re.search(r"LIT0(1[6-9]|2[0-5])", report), report
