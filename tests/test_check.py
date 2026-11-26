"""``chd-curator check-batch``: an agent's batch applied to a scratch copy and put through the
atlas's validator, reporting only the errors that are not expected before merge and normalise.

Three kinds of test. The real atlas CLI on a root built from the atlas's own curation (the
brief's end-to-end cases). Fake atlas CLIs, small scripts that record what they were given and
print a canned answer, for R-P2-65(a) (a crashed or unparseable validator is never "OK") and for
what the scratch root holds. And the shared run fixture, for every refusal: each must leave
``<root>/curation/``, the run directory and the temporary directory exactly as they were.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator import check as check_module
from chd_curator.atlas import AtlasError
from chd_curator.cli import app
from run_fixture import RUN_FIXTURE
from test_assemble import (
    JUDGED,
    JUDGED_BACKFILL,
    _approve_in_the_file,
    _assemble,
    _backfill_ready,
    _drop_the_judged_finding,
    _edit,
    _id_not_a_judge_id,
    _judge1,
    _judge_on_the_excluded_paper,
    _ready,
    _review_marks_not_in_differs,
)
from test_merge import _VARIANT, _backfill_run, _fresh, _merge, _pubmed

runner = CliRunner()
YAML_SAFE = YAML(typ="safe")

OK_LINE = "OK: only the errors expected before merge and normalise\n"
_FILE = "curation/literature/TBX5.yaml"
_VARIANT_1 = f"{_FILE}:finding CHDA:LIT:0009002:variant 1: "
LIT003 = (
    f"ERROR [LIT003] {_FILE}:pmid PMID:91000003: PMID:91000003 is included but no finding cites it"
)
LIT011 = (
    f"ERROR [LIT011] {_FILE}:finding CHDA:LIT:0009002: "
    "PMID:91000002 is not in curation/publications.yaml"
)
LIT022 = f"ERROR [LIT022] {_VARIANT_1}a mane variant needs "
LIT025 = f"ERROR [LIT025] {_VARIANT_1}the paper names p.(Arg34Ter) but the variant has no hgvs_p"
EXPECTED_LINES = [
    LIT003,
    LIT011,
    LIT022 + "grch38",
    LIT022 + "hgvs_c",
    LIT022 + "normalisation",
    LIT025,
]
CRASH = (
    "Traceback (most recent call last):\n"
    '  File "validate.py", line 1, in <module>\n'
    "ValueError: boom\n"
)


@pytest.fixture(autouse=True)
def scratch_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every scratch root of a test is made under ``tmp_path/scratch``, so "removed" is a
    measurement: the directory must hold nothing afterwards."""
    parent = tmp_path / "scratch"
    parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(parent))
    return parent


def _fake(
    tmp_path: Path, stdout: str = "", stderr: str = "", code: int = 0, name: str = "fake"
) -> Path:
    """A ``chd-atlas`` that records the root it was given (and a copy of its ``curation/`` and
    its top-level entries) under ``tmp_path/seen``, then prints a canned answer."""
    seen = tmp_path / "seen"
    script = tmp_path / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, shutil, sys\n"
        "from pathlib import Path\n"
        'if sys.argv[1] == "literature":\n'
        '    print(json.dumps({"format": 1, "gene": "HGNC:11604", "published": True,\n'
        '                      "query": "q", "symbol": "TBX5"}))\n'
        "    sys.exit(0)\n"
        f"seen = Path({str(seen)!r})\n"
        'root = Path(sys.argv[sys.argv.index("--root") + 1])\n'
        "seen.mkdir(exist_ok=True)\n"
        '(seen / "args").write_text(" ".join(sys.argv[1:2]))\n'
        '(seen / "root").write_text(str(root))\n'
        "entries = {p.name: p.is_symlink() for p in sorted(root.iterdir())}\n"
        '(seen / "entries").write_text(json.dumps(entries))\n'
        'shutil.copytree(root / "curation", seen / "curation", dirs_exist_ok=True)\n'
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({code})\n"
    )
    script.chmod(0o755)
    return script


def _check(
    root: Path, cli: Path, *args: str, batch: str = "01", pass_: str = "extract", run: str = "r1"
) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["check-batch", "TBX5", "--run", run, "--batch", batch, "--pass", pass_]
        + ["--root", str(root), "--atlas-cli", str(cli), *args],
        catch_exceptions=False,
    )
    return result.exit_code, result.output


def _snapshot(root: Path) -> dict[str, str]:
    """Every file under ``curation/`` and ``local/``; ``mirrors`` and the like are symlinks to
    the atlas and not part of what a check could change."""
    return {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for d in ("curation", "local")
        for f in sorted((root / d).rglob("*"))
        if f.is_file()
    }


def _seen(tmp_path: Path) -> Path:
    return tmp_path / "seen"


def _stdout(*errors: str, summary: str | None = None, warnings: int = 3) -> str:
    n = len(errors)
    return (
        "WARNING [SCP003] curation/chd_scope.yaml: a candidate report\n"
        + "".join(e + "\n" for e in errors)
        + f"\n{summary or f'{n} error(s), {warnings} warning(s)'}\n"
    )


def _ids(path: Path) -> list[str]:
    return [f["id"] for f in YAML_SAFE.load(path.read_text())["findings"]]


# ---- the brief's end-to-end cases: the real atlas CLI ------------------------------------------


def _real_root(tmp_path: Path, atlas_root: Path) -> tuple[Path, int]:
    """The atlas's own curation (copied), its mirrors, ontologies and schemas (symlinked), and
    the fixture run, with the three papers of the run's batches included in TBX5's triage.
    Returns the root and the LIT counter of the copy.

    Nothing here pins the atlas's live curation: TBX5's last search totals are read from the copy
    and raised by the three papers, and the counter is read from the copy's registry. What the
    tests still read from the atlas is its validator and its rules (the 16 expected errors the
    batch's two mane findings draw), its schemas and mirrors, and that TBX5.yaml keeps
    ``hits:`` and ``new_hits:`` on adjacent lines in each search and one ``findings:`` key.
    """
    root = tmp_path / "real"
    root.mkdir()
    shutil.copytree(atlas_root / "curation", root / "curation")
    for name in ("mirrors", "ontologies", "schemas"):
        (root / name).symlink_to(atlas_root / name)
    shutil.copytree(RUN_FIXTURE / "local", root / "local")
    lit = root / "curation/literature/TBX5.yaml"
    text = lit.read_text()
    entries = "".join(
        f"  - pmid: PMID:{n}\n    search: 1\n    decision: included\n"
        for n in (91000002, 91000003, 91000004)
    )
    searches = list(re.finditer(r"^    hits: (\d+)\n    new_hits: (\d+)\n", text, re.MULTILINE))
    assert text.count("\nfindings:\n") == 1 and searches
    last = searches[-1]
    raised = f"    hits: {int(last.group(1)) + 3}\n    new_hits: {int(last.group(2)) + 3}\n"
    text = text[: last.start()] + raised + text[last.end() :]
    lit.write_text(text.replace("\nfindings:\n", "\n" + entries + "findings:\n"))
    batch = root / "local/literature/runs/TBX5/r1/extract/batch-01.findings.yaml"
    batch.write_text(batch.read_text().replace("placement: unplaced", "placement: mane"))
    counter = int(re.search(r"LIT: (\d+)", (root / "curation/.id_registry.yaml").read_text())[1])  # type: ignore[index]
    return root, counter


def _baseline(tmp_path: Path, atlas_root: Path, atlas_cli: Path) -> tuple[int, int]:
    """The atlas's own validate summary on a plain copy of its curation: (errors, warnings)."""
    root = tmp_path / "baseline"
    root.mkdir()
    shutil.copytree(atlas_root / "curation", root / "curation")
    for name in ("mirrors", "ontologies", "schemas"):
        (root / name).symlink_to(atlas_root / name)
    done = subprocess.run(
        [str(atlas_cli), "validate", "--root", str(root)], capture_output=True, text=True
    )
    last = [line for line in done.stdout.splitlines() if line.strip()][-1]
    found = re.fullmatch(r"(\d+) error\(s\), (\d+) warning\(s\)", last)
    assert found, done.stdout[-300:]
    return int(found[1]), int(found[2])


def test_a_clean_batch_passes_on_the_real_validator_and_touches_nothing(
    tmp_path: Path, atlas_root: Path, atlas_cli: Path, scratch_parent: Path
) -> None:
    errors, warnings = _baseline(tmp_path, atlas_root, atlas_cli)
    root, _ = _real_root(tmp_path, atlas_root)
    before = _snapshot(root)
    code, out = _check(root, atlas_cli)
    # the batch draws 16 expected errors (2 LIT011, 9 LIT022, 3 LIT025, 2 LIT003) on top of
    # whatever the atlas's own curation already reports
    assert (code, out) == (0, f"{errors + 16} error(s), {warnings} warning(s)\n" + OK_LINE)
    assert _snapshot(root) == before
    assert list(scratch_parent.iterdir()) == []


def test_a_variant_whose_phenotypes_are_not_the_findings_is_reported_and_nothing_else(
    tmp_path: Path, atlas_root: Path, atlas_cli: Path, scratch_parent: Path
) -> None:
    errors, warnings = _baseline(tmp_path, atlas_root, atlas_cli)
    root, counter = _real_root(tmp_path, atlas_root)
    batch = root / "local/literature/runs/TBX5/r1/extract/batch-01.findings.yaml"
    _edit(
        batch,
        "    phenotypes:\n    - HP:0001631\n    placement: mane\n",
        "    phenotypes:\n    - HP:0001629\n    placement: mane\n",
    )
    before = _snapshot(root)
    code, out = _check(root, atlas_cli)
    assert code == 1
    assert out == (
        f"{errors + 17} error(s), {warnings} warning(s)\nUNEXPECTED ERRORS:\n"
        f"ERROR [LIT024] {_FILE}:finding CHDA:LIT:{counter + 2:07d}:variant 1: "
        "phenotype HP:0001629 is not among the finding's phenotypes\n"
    )
    assert _snapshot(root) == before
    assert list(scratch_parent.iterdir()) == []


# ---- the scratch root --------------------------------------------------------------------------


def test_the_scratch_root_is_a_copy_of_curation_and_symlinks_to_the_rest(
    tmp_path: Path, atlas_root: Path, scratch_parent: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    (p.root / ".git").mkdir()
    (p.root / ".venv").mkdir()
    (p.root / "docs.md").write_text("x")
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    code, out = _check(p.root, cli)
    assert (code, out) == (0, "0 error(s), 3 warning(s)\n" + OK_LINE)
    assert json.loads((_seen(tmp_path) / "entries").read_text()) == {
        "curation": False,
        "docs.md": True,
        "ontologies": True,
        "schemas": True,
    }
    assert (_seen(tmp_path) / "args").read_text() == "validate"
    scratch = Path((_seen(tmp_path) / "root").read_text())
    assert scratch.parent == scratch_parent and not scratch.exists()
    assert list(scratch_parent.iterdir()) == []


def test_an_extract_batch_is_numbered_from_the_counter_with_no_publication_or_triage_change(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    before = _snapshot(p.root)
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    assert _check(p.root, cli)[0] == 0
    seen = _seen(tmp_path) / "curation"
    assert _ids(seen / "literature/TBX5.yaml") == [
        "CHDA:LIT:0009001",
        "CHDA:LIT:0009002",
        "CHDA:LIT:0009003",
    ]
    assert (seen / ".id_registry.yaml").read_text() == "prefixes:\n  AST: 2\n  LIT: 9003\n"
    assert (seen / "publications.yaml").read_bytes() == (
        p.root / "curation/publications.yaml"
    ).read_bytes()
    lit = YAML_SAFE.load((seen / "literature/TBX5.yaml").read_text())
    assert [t["decision"] for t in lit["triage"]] == ["included"] * 4
    assert _snapshot(p.root) == before


def test_a_verify_batch_is_read_from_the_verify_directory(tmp_path: Path, atlas_root: Path) -> None:
    p = _fresh(tmp_path, atlas_root)
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    assert _check(p.root, cli, pass_="verify")[0] == 0
    lit = (_seen(tmp_path) / "curation/literature/TBX5.yaml").read_text()
    assert _ids(_seen(tmp_path) / "curation/literature/TBX5.yaml") == [
        "CHDA:LIT:0009001",
        "CHDA:LIT:0009002",
        "CHDA:LIT:0009003",
        "CHDA:LIT:0009004",
    ]
    assert lit.count("A third carrier") == 1  # verify batch 01's finding that extract lacks
    assert (_seen(tmp_path) / "curation/.id_registry.yaml").read_text().endswith("LIT: 9004\n")


# ---- what is expected, and what is not ---------------------------------------------------------


def test_the_errors_expected_before_merge_and_normalise_are_hidden_and_warnings_are_not_printed(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    cli = _fake(tmp_path, _stdout(*EXPECTED_LINES), code=1)
    code, out = _check(p.root, cli)
    assert (code, out) == (0, "6 error(s), 3 warning(s)\n" + OK_LINE)


def test_every_other_error_is_listed_in_the_order_the_validator_gave_it(
    tmp_path: Path, atlas_root: Path
) -> None:
    other = [
        f"ERROR [LIT022] {_VARIANT_1}a mane variant needs hgvs_p",
        "ERROR [ID002] CHDA:LIT:0009002: ordinal exceeds the allocated counter for 'LIT' (9001)",
        f"ERROR [LIT025] {_VARIANT_1}the paper names p.(Arg34Ter) but hgvs_p is p.(Arg35Ter)",
        f"ERROR [LIT004] {_FILE}:finding CHDA:LIT:0009002: cites PMID:91000002, not included",
    ]
    p = _fresh(tmp_path, atlas_root)
    cli = _fake(
        tmp_path, _stdout(other[0], LIT003, other[1], *EXPECTED_LINES[1:], *other[2:]), code=1
    )
    code, out = _check(p.root, cli)
    assert code == 1
    assert out == ("10 error(s), 3 warning(s)\nUNEXPECTED ERRORS:\n" + "\n".join(other) + "\n")


# ---- R-P2-65(a): a crashed or unparseable validator is never OK -------------------------------


@pytest.mark.parametrize(
    ("stdout", "stderr", "code"),
    [
        pytest.param("", CRASH, 1, id="a traceback and nothing else"),
        pytest.param(
            _stdout(LIT003, summary="2 error(s), 3 warning(s)"), "", 1, id="count mismatch"
        ),
        pytest.param(_stdout(LIT003), "", 2, id="exit 2 over a listed error"),
        pytest.param(_stdout(summary="0 error(s), 3 warning(s)"), "", 2, id="exit 2 over none"),
        pytest.param(_stdout(LIT003), "", 0, id="exit 0 over an error"),
        pytest.param(_stdout(summary="0 error(s), 3 warning(s)"), "", 1, id="exit 1 over none"),
        pytest.param(_stdout(LIT003).rsplit("\n\n", 1)[0] + "\n", "", 1, id="no summary line"),
        pytest.param(
            _stdout(summary="0 error(s), 3 warning(s)"), CRASH, 0, id="traceback beside ok"
        ),
        pytest.param("", "", 0, id="no output at all"),
    ],
)
def test_a_validator_that_cannot_be_trusted_is_never_ok(
    tmp_path: Path,
    atlas_root: Path,
    scratch_parent: Path,
    stdout: str,
    stderr: str,
    code: int,
) -> None:
    p = _fresh(tmp_path, atlas_root)
    before = _snapshot(p.root)
    exit_code, out = _check(p.root, _fake(tmp_path, stdout, stderr, code))
    assert exit_code == 1
    assert "UNEXPECTED ERRORS:\n" in out and "OK:" not in out
    for line in (stdout + stderr).splitlines():
        if line.strip():
            assert line in out.splitlines()  # the raw output is shown, line for line
    assert _snapshot(p.root) == before
    assert list(scratch_parent.iterdir()) == []


def test_a_validator_that_cannot_be_run_is_never_ok(
    tmp_path: Path, atlas_root: Path, scratch_parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``target`` has answered, then the validate run itself fails (an OS error or a timeout)."""

    def broken(root: Path, atlas_cli: Path) -> tuple[int, str, str]:
        raise AtlasError(f"the atlas CLI {atlas_cli} could not be run: boom")

    monkeypatch.setattr(check_module, "run_validate", broken)
    p = _fresh(tmp_path, atlas_root)
    cli = _fake(tmp_path, _stdout())
    code, out = _check(p.root, cli)
    assert (code, out) == (1, f"UNEXPECTED ERRORS:\nthe atlas CLI {cli} could not be run: boom\n")
    assert list(scratch_parent.iterdir()) == []


# ---- refusals: exit 2, nothing validated, nothing left behind ----------------------------------


def _drop_pass(p: Any) -> None:
    (p.extract / "batch-01.findings.yaml").unlink()


def _malformed_pass(p: Any) -> None:
    (p.extract / "batch-01.findings.yaml").write_text("findings: [\n")


def _unknown_shared(p: Any) -> None:
    path = p.extract / "batch-01.findings.yaml"
    path.write_text(
        path.read_text().replace(
            "shares_probands_with: unknown", "shares_probands_with: [CHDA:LIT:0000099]", 1
        )
    )


def _approved_pass(p: Any) -> None:
    path = p.extract / "batch-01.findings.yaml"
    path.write_text(
        path.read_text().replace(
            "  id: CHDA:LIT:0901001\n", "  id: CHDA:LIT:0901001\n  verified_by: x\n"
        )
    )


def _no_plan(p: Any) -> None:
    p.batches_json.unlink()


@pytest.mark.parametrize(
    ("setup", "args", "message"),
    [
        pytest.param(None, {"batch": "03"}, "batch 03 is not in batches.json", id="unknown batch"),
        pytest.param(None, {"pass_": "polish"}, "--pass 'polish' is not one of", id="unknown pass"),
        pytest.param(None, {"pass_": "judge"}, "judge-01.findings.yaml", id="no judge file"),
        pytest.param(_drop_pass, {}, "batch-01.findings.yaml", id="no pass file"),
        pytest.param(_malformed_pass, {}, "batch-01.findings.yaml", id="unreadable pass file"),
        pytest.param(
            _unknown_shared, {}, "shares_probands_with names CHDA:LIT:0000099", id="unknown ref"
        ),
        pytest.param(
            _approved_pass, {}, "carries verified_by; an agent's finding may not", id="verified_by"
        ),
        pytest.param(_no_plan, {}, "no batches.json", id="no plan"),
    ],
)
def test_a_batch_file_that_does_not_load_exits_2_and_validates_nothing(
    tmp_path: Path,
    atlas_root: Path,
    scratch_parent: Path,
    setup: Any,
    args: dict[str, str],
    message: str,
) -> None:
    p = _fresh(tmp_path, atlas_root)
    if setup is not None:
        setup(p)
    before = _snapshot(p.root)
    code, out = _check(p.root, _fake(tmp_path, _stdout()), **args)
    assert code == 2 and out.startswith("error: ") and message in out, out
    assert not _seen(tmp_path).exists()  # the validator was never run
    assert _snapshot(p.root) == before
    assert list(scratch_parent.iterdir()) == []


def test_a_root_that_is_not_a_directory_exits_2(tmp_path: Path) -> None:
    code, out = _check(tmp_path / "nope", _fake(tmp_path, _stdout()))
    assert code == 2 and "--root" in out


# ---- the judge pass ----------------------------------------------------------------------------


def test_a_judge_pass_replaces_the_judged_finding_and_numbers_the_added_one(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    before = _snapshot(p.root)
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    code, out = _check(p.root, cli, pass_="judge")
    assert (code, out) == (0, "0 error(s), 3 warning(s)\n" + OK_LINE)
    seen = _seen(tmp_path) / "curation"
    found = {
        f["id"]: f for f in YAML_SAFE.load((seen / "literature/TBX5.yaml").read_text())["findings"]
    }
    assert list(found) == [
        "CHDA:LIT:0009001",
        "CHDA:LIT:0009002",
        "CHDA:LIT:0009003",
        "CHDA:LIT:0009004",
        "CHDA:LIT:0009005",
    ]
    assert found["CHDA:LIT:0009003"]["screened"] == 2  # the judge's; merge had written 1
    assert found["CHDA:LIT:0009005"]["summary"].startswith("A third carrier listed")
    assert (seen / ".id_registry.yaml").read_text() == "prefixes:\n  AST: 2\n  LIT: 9005\n"
    assert _snapshot(p.root) == before


def test_a_judge_id_an_earlier_assemble_numbered_replaces_that_finding_instead_of_adding_another(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    assert _assemble(p, stub_atlas)[0] == 0
    _edit(_judge1(p), "A third carrier listed", "A third carrier, edited, listed")
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    code, out = _check(p.root, cli, pass_="judge")
    assert (code, out) == (0, "0 error(s), 3 warning(s)\n" + OK_LINE)
    seen = _seen(tmp_path) / "curation"
    found = {
        f["id"]: f for f in YAML_SAFE.load((seen / "literature/TBX5.yaml").read_text())["findings"]
    }
    assert list(found)[-2:] == ["CHDA:LIT:0009004", "CHDA:LIT:0009005"]
    assert found["CHDA:LIT:0009005"]["summary"].startswith("A third carrier, edited, listed")
    assert (seen / ".id_registry.yaml").read_text().endswith("LIT: 9005\n")


def test_a_judge_pass_after_the_owner_approved_a_judged_finding_answers_as_assemble_does(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-P2-66(a): the pass goes through assemble's own acceptance, so the approved finding is
    left as it is when the judge edits another one, and a change to it exits 2."""
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    assert _assemble(p, stub_atlas)[0] == 0
    _approve_in_the_file(p, JUDGED)
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    _edit(_judge1(p), "A third carrier listed", "A third carrier, fixed, listed")
    code, out = _check(p.root, cli, pass_="judge")
    assert (code, out) == (0, "0 error(s), 3 warning(s)\n" + OK_LINE)
    seen = {
        f["id"]: f
        for f in YAML_SAFE.load((_seen(tmp_path) / "curation/literature/TBX5.yaml").read_text())[
            "findings"
        ]
    }
    assert seen[JUDGED]["approved_by"] == "E.A.M" and seen[JUDGED]["screened"] == 2
    assert seen["CHDA:LIT:0009005"]["summary"].startswith("A third carrier, fixed, listed")
    _edit(_judge1(p), "three relatives had", "four relatives had")
    before = _snapshot(p.root)
    code, out = _check(p.root, cli, pass_="judge")
    assert code == 2
    assert f"{JUDGED}: approved; a judge may not change an approved finding" in out
    assert _snapshot(p.root) == before


def test_another_batchs_judge_file_is_not_read(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    (p.judging / "judge-02.findings.yaml").write_text("findings: [\n")
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    assert _check(p.root, cli, pass_="judge")[0] == 0


def test_a_judge_finding_of_no_known_kind_is_refused(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _edit(_judge1(p), "id: CHDA:LIT:0701001", "id: CHDA:LIT:0123456")
    code, out = _check(p.root, _fake(tmp_path, _stdout()), pass_="judge")
    assert code == 2 and "CHDA:LIT:0123456" in out
    assert not _seen(tmp_path).exists()


# ---- backfill ----------------------------------------------------------------------------------

_BACKFILL_PASS = {
    "CHDA:LIT:0009010": {"variants": [_VARIANT]},
    "CHDA:LIT:0009011": {"variants": [_VARIANT]},
    "CHDA:LIT:0009013": {"variants": [_VARIANT]},
    "CHDA:LIT:0009015": {"variants": []},
}


@pytest.mark.parametrize("pass_", ["extract", "verify"])
def test_a_backfill_pass_is_applied_as_merge_would_write_it(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch, pass_: str
) -> None:
    _pubmed(monkeypatch)
    p = _backfill_run(tmp_path, atlas_root, _BACKFILL_PASS)
    if pass_ == "verify":
        p.verify.mkdir()
        shutil.copy(p.extract / "batch-01.findings.yaml", p.verify / "batch-01.findings.yaml")
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    before = _snapshot(p.root)
    assert _check(p.root, cli, pass_=pass_)[0] == 0
    assert _snapshot(p.root) == before
    checked = (_seen(tmp_path) / "curation/literature/TBX5.yaml").read_bytes()
    assert _merge(p, stub_atlas)[0] == 0
    assert checked == (p.root / "curation/literature/TBX5.yaml").read_bytes()
    assert checked.count(b"verified_by") == 4  # A, B and D lost theirs; 0009001, C, E and F keep it


def test_a_backfill_pass_that_fills_a_filled_field_is_refused(
    tmp_path: Path, atlas_root: Path, scratch_parent: Path
) -> None:
    p = _backfill_run(tmp_path, atlas_root, {"CHDA:LIT:0009014": {"variants": [_VARIANT]}})
    code, out = _check(p.root, _fake(tmp_path, _stdout()))
    assert code == 2 and "CHDA:LIT:0009014 already holds variants" in out
    assert not _seen(tmp_path).exists() and list(scratch_parent.iterdir()) == []


# ---- the judge pass is checked by assemble's own rules ----------------------------------------


def _judge_refused(
    p: Any, cli: Path, message: str, scratch_parent: Path, batch: str = "01"
) -> None:
    before = _snapshot(p.root)
    code, out = _check(p.root, cli, pass_="judge", batch=batch)
    assert code == 2 and out.startswith("error: ") and message in out, out
    assert not _seen(p.root.parent).exists()
    assert _snapshot(p.root) == before
    assert list(scratch_parent.iterdir()) == []


def test_a_judge_that_changes_a_field_no_decision_names_is_refused_by_assembles_rule(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    scratch_parent: Path,
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _review_marks_not_in_differs(p)
    cli = _fake(tmp_path, _stdout())
    _judge_refused(
        p,
        cli,
        "CHDA:LIT:0009003: the judge changed ['review marks'], which no decision covers",
        scratch_parent,
    )


def test_a_judge_id_outside_the_comparison_is_refused(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    scratch_parent: Path,
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _id_not_a_judge_id(p)
    _judge_refused(
        p,
        _fake(tmp_path, _stdout()),
        "judging: CHDA:LIT:0009004 is neither a finding that differs",
        scratch_parent,
    )


def test_a_judge_pass_needs_the_runs_comparison_and_the_decided_differences(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    scratch_parent: Path,
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _drop_the_judged_finding(p)
    cli = _fake(tmp_path, _stdout())
    _judge_refused(
        p, cli, "CHDA:LIT:0009003: differs, but no judge finding decides it", scratch_parent
    )
    p.comparison.unlink()
    _judge_refused(p, cli, "the run has no comparison.json", scratch_parent)


def test_a_judge_added_finding_on_a_paper_without_a_publication_is_refused(
    tmp_path: Path,
    atlas_root: Path,
    stub_atlas: Path,
    monkeypatch: pytest.MonkeyPatch,
    scratch_parent: Path,
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _judge_on_the_excluded_paper(p, register=False)
    _judge_refused(
        p,
        _fake(tmp_path, _stdout()),
        "chd-curator add-publication PMID:91000004",
        scratch_parent,
        batch="02",
    )


def test_a_judge_added_finding_on_a_paper_merge_excluded_goes_back_to_included_in_the_copy(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _judge_on_the_excluded_paper(p, register=True)
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    assert _check(p.root, cli, pass_="judge", batch="02")[0] == 0
    text = (_seen(tmp_path) / "curation/literature/TBX5.yaml").read_text()
    assert "  - pmid: PMID:91000004\n    search: 1\n    decision: included\nfindings:\n" in text
    assert "decision: excluded" not in text.split("findings:")[0]


def test_a_backfill_judge_pass_takes_the_declared_fields_by_assembles_edit(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, scratch_parent: Path
) -> None:
    p = _backfill_ready(
        tmp_path,
        atlas_root,
        stub_atlas,
        "findings:\n  CHDA:LIT:0009001:\n    cohorts: [somewhere]\n",
    )
    p.judging.mkdir(exist_ok=True)
    (p.judging / "judge-01.findings.yaml").write_text(JUDGED_BACKFILL)
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    before = _snapshot(p.root)
    assert _check(p.root, cli, pass_="judge")[0] == 0
    assert _snapshot(p.root) == before
    found = YAML_SAFE.load((_seen(tmp_path) / "curation/literature/TBX5.yaml").read_text())
    f = {x["id"]: x for x in found["findings"]}["CHDA:LIT:0009001"]
    assert f["cohorts"] == ["elsewhere"] and "verified_by" not in f
    assert f["approved_by"] == "E.A.M"  # the edit keeps what a judge may not touch
    _edit(p.judging / "judge-01.findings.yaml", "screened: 1", "screened: 3")
    shutil.rmtree(_seen(tmp_path))
    _judge_refused(
        p, cli, "the judge changed fields the run did not declare: ['screened']", scratch_parent
    )


# ---- a ref to another batch's local id is deferred to merge ------------------------------------

_NOT_CHECKED = (
    "NOT CHECKED: CHDA:LIT:0902001 shares_probands_with CHDA:LIT:0901001 "
    "(batch 01; merge resolves it)\n"
)


def _share(p: Any, ref: str) -> None:
    path = p.extract / "batch-02.findings.yaml"
    path.write_text(
        path.read_text().replace(
            "shares_probands_with: unknown", f"shares_probands_with: [{ref}]", 1
        )
    )


def test_a_ref_to_another_batchs_local_id_is_dropped_in_the_copy_and_reported_as_not_checked(
    tmp_path: Path, atlas_root: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _share(p, "CHDA:LIT:0901001")
    before = _snapshot(p.root)
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    code, out = _check(p.root, cli, batch="02")
    assert (code, out) == (0, _NOT_CHECKED + "0 error(s), 3 warning(s)\n" + OK_LINE)
    assert _snapshot(p.root) == before
    seen = YAML_SAFE.load((_seen(tmp_path) / "curation/literature/TBX5.yaml").read_text())
    shared = {f["id"]: f["shares_probands_with"] for f in seen["findings"]}
    assert shared["CHDA:LIT:0009002"] == []


def test_a_ref_to_an_id_of_no_batch_exits_2_saying_what_it_is(
    tmp_path: Path, atlas_root: Path, scratch_parent: Path
) -> None:
    p = _fresh(tmp_path, atlas_root)
    _share(p, "CHDA:LIT:0999999")
    code, out = _check(p.root, _fake(tmp_path, _stdout()), batch="02")
    assert (code, out) == (
        2,
        "error: CHDA:LIT:0009002: shares_probands_with names CHDA:LIT:0999999, which is neither "
        "a finding of this run nor one in the file\n",
    )
    assert list(scratch_parent.iterdir()) == []


def test_a_judge_ref_to_another_batchs_local_id_is_deferred_too(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    _edit(
        _judge1(p),
        "shares_probands_with: unknown\n  summary: One family",
        "shares_probands_with: [CHDA:LIT:0902001]\n  summary: One family",
    )
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    code, out = _check(p.root, cli, pass_="judge")
    assert (code, out) == (
        0,
        "NOT CHECKED: CHDA:LIT:0009003 shares_probands_with CHDA:LIT:0902001 "
        "(batch 02; merge resolves it)\n0 error(s), 3 warning(s)\n" + OK_LINE,
    )
