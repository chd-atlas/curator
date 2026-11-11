"""curator's link to the atlas: the target command and the exported schemas."""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from chd_curator.atlas import AtlasError, Target, plain, schema_validator, target

GOOD = {"format": 1, "gene": "HGNC:11604", "published": True, "query": "q", "symbol": "TBX5"}


def make_stub(tmp_path: Path, stdout: str, code: int = 0) -> tuple[Path, Path]:
    """An executable Python script that records its argv and prints a canned answer."""
    argv_file = tmp_path / "argv.json"
    stub = tmp_path / "stub-cli"
    stub.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import json, sys
            open({str(argv_file)!r}, "w").write(json.dumps(sys.argv[1:]))
            sys.stdout.write({stdout!r})
            sys.exit({code})
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, argv_file


def test_target_reads_the_atlas_answer(tmp_path: Path) -> None:
    root = tmp_path / "root"
    stub, argv_file = make_stub(tmp_path, json.dumps(GOOD, sort_keys=True))
    result = target(root, "HOS", stub)
    assert result == Target("HGNC:11604", "TBX5", True, "q")
    assert result.published is True
    assert json.loads(argv_file.read_text()) == [
        "literature",
        "target",
        "HOS",
        "--root",
        str(root),
    ]


def bad(**changes: object) -> str:
    data = copy.deepcopy(GOOD) | changes
    return json.dumps(data)


@pytest.mark.parametrize(
    ("stdout", "code", "expected"),
    [
        (
            "error: 'NOPE' is not a known gene symbol\n",
            2,
            r"^the atlas refused 'HOS': 'NOPE' is not a known gene symbol$",
        ),
        ("not json at all", 0, "not JSON"),
        (bad(format=2), 0, "format 2"),
        (bad(x=1), 0, r"unexpected keys: extra \['x'\], missing \[\]"),
        (bad(published="true"), 0, "published is not a boolean"),
        # json.dumps writes these as 1.0 and true; both equal 1 in Python.
        (bad(format=1.0), 0, r"answered format 1\.0;"),
        (bad(format=True), 0, "answered format True;"),
        (bad(gene=11604), 0, "gene is not a string: 11604"),
        (bad(symbol=None), 0, "symbol is not a string: None"),
        (bad(query=["q"]), 0, r"query is not a string: \['q'\]"),
    ],
)
def test_an_unusable_atlas_cli_is_refused(
    tmp_path: Path, stdout: str, code: int, expected: str
) -> None:
    stub, _ = make_stub(tmp_path, stdout, code)
    with pytest.raises(AtlasError, match=expected):
        target(tmp_path, "HOS", stub)


def test_a_missing_atlas_cli_names_the_path_and_the_variable(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "chd-atlas"
    with pytest.raises(AtlasError) as info:
        target(tmp_path, "HOS", missing)
    assert str(missing) in str(info.value)
    assert "CHD_ATLAS_CLI" in str(info.value)


def test_an_existing_cli_that_cannot_run_is_not_called_missing(tmp_path: Path) -> None:
    """A script whose shebang interpreter is absent raises FileNotFoundError for the
    script itself, which says nothing about the script being there."""
    stub = tmp_path / "chd-atlas"
    stub.write_text(f"#!{tmp_path}/no-such-interpreter\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(AtlasError) as info:
        target(tmp_path, "HOS", stub)
    message = str(info.value)
    assert message.startswith(f"the atlas CLI {stub} exists but could not be run: ")
    assert "No such file or directory" in message  # the OS error is passed on
    assert "does not exist" not in message


def test_a_missing_exported_schema_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AtlasError, match=r"schemas/gene_literature_file\.schema\.json"):
        schema_validator(tmp_path, "gene_literature_file")


def test_the_exported_schema_takes_the_real_records_and_checks_dates(atlas_root: Path) -> None:
    yaml = YAML(typ="safe")
    validator = schema_validator(atlas_root, "gene_literature_file")
    publications = schema_validator(atlas_root, "publication_file")
    lit = atlas_root / "curation" / "literature"
    for name in ("TBX5", "NR2F2"):
        data = plain(yaml.load(lit / f"{name}.yaml"))
        assert list(validator.iter_errors(data)) == []
    pubs = plain(yaml.load(atlas_root / "curation" / "publications.yaml"))
    assert list(publications.iter_errors(pubs)) == []

    # ruamel refuses an impossible date while parsing, so the bad copy is made on the
    # plain data: the first approval date becomes the string a hand edit would leave.
    broken = plain(yaml.load(lit / "TBX5.yaml"))
    findings = [f for f in broken["findings"] if "approved_on" in f]
    assert findings, "no approved finding to corrupt, so the check proved nothing"
    findings[0]["approved_on"] = "2026-13-45"
    assert len(list(validator.iter_errors(broken))) >= 1


def test_the_suite_refuses_to_run_without_the_atlas(tmp_path: Path) -> None:
    (tmp_path / "test_one.py").write_text("def test_one():\n    pass\n")
    conftest = Path(__file__).with_name("conftest.py")
    (tmp_path / "conftest.py").write_text(conftest.read_text())
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CHD_ATLAS_ROOT", "CHD_ATLAS_CLI", "CHD_ATLAS_PYTHON")
    }
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert run.returncode != 0
    out = run.stdout + run.stderr
    for name in ("CHD_ATLAS_ROOT", "CHD_ATLAS_CLI", "CHD_ATLAS_PYTHON"):
        assert name in out
