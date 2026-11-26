"""Roles: each model step's input, assembled from a task file and the house rules it cites.

A role file names rule sections by heading and states no rule itself (CP section 10); the
rules reach the model verbatim, from the atlas's ``curator/curator-rules.md``. These tests
hold the assembly byte for byte, refuse a cite that matches nothing, and keep the digests
honest. The fixture rules file (``tests/fixtures/roles``) is small and carries every heading
a packaged role cites; one test assembles every role against the real file in
``$CHD_ATLAS_ROOT``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from chd_curator import roles
from chd_curator.roles import ROLES, RoleError, assemble
from rules_text import restated

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FIXTURE_ROOT = HERE / "fixtures" / "roles"
RUN = HERE / "fixtures" / "run" / "local" / "literature" / "runs" / "TBX5" / "r1"
JUDGE = HERE / "fixtures" / "run-judge"
TRIAGE = {"gene": "TBX5", "hit": "PMID:91000001\nA title\nAn abstract."}
READER = {"manifest": '{"papers": []}', "texts": "== PMID:91000001 ==\nSome text."}
INPUTS: dict[str, dict[str, str]] = {
    "triage-screen": TRIAGE,
    "triage-check": TRIAGE,
    "extract": READER,
    "blind-read": READER,
    "judge": READER,
}


def _fixture_rules() -> str:
    return (FIXTURE_ROOT / "curator" / "curator-rules.md").read_text(encoding="utf-8")


def _root_with(tmp_path: Path, rules: str) -> Path:
    (tmp_path / "curator").mkdir(parents=True, exist_ok=True)
    (tmp_path / "curator" / "curator-rules.md").write_text(rules, encoding="utf-8")
    return tmp_path


def _between(text: str, start: str, stop: str) -> str:
    """The fixture's text from line ``start`` up to, not including, line ``stop``, minus the
    trailing blank lines: an independent reading of what a section is."""
    lines = text.split("\n")
    begin = lines.index(start)
    end = lines.index(stop, begin + 1)
    kept = lines[begin:end]
    while not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def _cites(role: str) -> list[str]:
    head = (roles.WORKFLOW / "roles" / f"{role}.md").read_text(encoding="utf-8")
    front = YAML(typ="safe").load(head.split("---\n")[1])
    cites: list[str] = front["cites"]
    return cites


def test_a_cited_section_reaches_the_prompt_byte_for_byte() -> None:
    rules = _fixture_rules()
    triage = _between(rules, "## Triage", "## Findings")
    assert triage.endswith("with `code` and a trailing space.  ")  # trailing blank lines are cut
    got = assemble("triage-check", FIXTURE_ROOT, INPUTS["triage-check"])
    assert f"--- rules: Triage ---\n{triage}\n--- end rules: Triage ---" in got.system
    # A section runs to the next heading of its own or a higher level: `Findings` takes the
    # `### Variants` beneath it, and stops before `## Attribution and approval`.
    findings = _between(rules, "## Findings", "## Attribution and approval")
    assert "### Variants" in findings
    got = assemble("extract", FIXTURE_ROOT, INPUTS["extract"])
    assert f"--- rules: Findings ---\n{findings}\n--- end rules: Findings ---" in got.system
    assert got.system.index("rules: Principles") < got.system.index("rules: Findings")  # cite order
    assert "Cited by no role" not in got.system and "cited by no role" not in got.system


def test_the_system_text_is_the_task_then_the_rules_then_the_answer_schema() -> None:
    got = assemble("judge", FIXTURE_ROOT, INPUTS["judge"])
    schema = json.dumps(got.schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    task = (roles.WORKFLOW / "roles" / "judge.md").read_text(encoding="utf-8")
    task = task.split("## Task\n", 1)[1].split("\n## Inputs", 1)[0].strip()
    assert got.system.startswith(task + "\n\n--- rules: ")
    assert got.system.endswith(f"--- answer schema ---\n{schema}\n--- end answer schema ---")
    assert got.user.count("Some text.") == 1 and '{"papers": []}' in got.user
    assert got.role == "judge"


@pytest.mark.parametrize("missing", ["Triage", "Findings", "Principles"])
def test_a_heading_that_does_not_exist_is_refused(missing: str, tmp_path: Path) -> None:
    rules = _fixture_rules().replace(f"## {missing}\n", f"## {missing} renamed\n")
    root = _root_with(tmp_path, rules)
    role = "triage-check" if missing == "Triage" else "extract"
    with pytest.raises(RoleError, match=missing):
        assemble(role, root, INPUTS[role])


def test_a_heading_that_matches_twice_is_refused(tmp_path: Path) -> None:
    root = _root_with(tmp_path, _fixture_rules() + "\n## Triage\n\nA second one.\n")
    with pytest.raises(RoleError, match="Triage.*more than one|more than one.*Triage"):
        assemble("triage-check", root, TRIAGE)


def test_a_missing_rules_file_an_unknown_role_and_wrong_inputs_are_refused(tmp_path: Path) -> None:
    with pytest.raises(RoleError, match="curator-rules.md"):
        assemble("triage-check", tmp_path, TRIAGE)
    with pytest.raises(RoleError, match="no-such-role"):
        assemble("no-such-role", FIXTURE_ROOT, TRIAGE)
    with pytest.raises(RoleError, match="hit"):
        assemble("triage-check", FIXTURE_ROOT, {"gene": "TBX5"})
    with pytest.raises(RoleError, match="extra"):
        assemble("triage-check", FIXTURE_ROOT, {**TRIAGE, "extra": "x"})


def test_an_input_is_inserted_as_written_and_never_read_as_a_placeholder() -> None:
    got = assemble("triage-check", FIXTURE_ROOT, {"gene": "{{hit}}", "hit": "{{gene}} $1 \\1"})
    assert "{{hit}}" in got.user and "{{gene}} $1 \\1" in got.user


@pytest.mark.parametrize("role", ROLES)
def test_no_role_file_restates_a_rule(role: str) -> None:
    task = (roles.WORKFLOW / "roles" / f"{role}.md").read_text(encoding="utf-8")
    task = task.split("## Task\n", 1)[1].split("\n## Inputs", 1)[0]
    assert len(task.split()) > 30, "a role's task says what the call does"
    assert restated(task, _fixture_rules()) == []
    rules = Path(os.environ["CHD_ATLAS_ROOT"], "curator", "curator-rules.md")
    assert restated(task, rules.read_text(encoding="utf-8")) == []
    # The detector can fail: a sentence copied from the fixture is found.
    sentence = (
        "A fixture rule that no role file may repeat: zebras graze quietly beside "
        "the old river bank."
    )
    assert restated(task + "\n" + sentence, _fixture_rules()) != []


def test_the_digest_moves_with_the_rules_the_inputs_and_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = assemble("triage-check", FIXTURE_ROOT, TRIAGE)
    assert len(base.digest) == len(base.rules_digest) == 64
    again = assemble("triage-check", FIXTURE_ROOT, TRIAGE)
    assert again == base
    # An input moves the digest and leaves the rules digest.
    moved = assemble("triage-check", FIXTURE_ROOT, {**TRIAGE, "hit": TRIAGE["hit"] + "!"})
    assert moved.digest != base.digest and moved.rules_digest == base.rules_digest
    # A cited section moves both.
    cited = _root_with(tmp_path / "a", _fixture_rules().replace("fixture paper", "fixture work"))
    changed = assemble("triage-check", cited, TRIAGE)
    assert changed.digest != base.digest and changed.rules_digest != base.rules_digest
    # An uncited section moves neither.
    uncited = _root_with(
        tmp_path / "b", _fixture_rules().replace("changing it moves", "editing it moves")
    )
    same = assemble("triage-check", uncited, TRIAGE)
    assert same.digest == base.digest and same.rules_digest == base.rules_digest
    # The schema moves the digest and leaves the rules digest.
    copy = tmp_path / "workflow"
    shutil.copytree(roles.WORKFLOW, copy)
    path = copy / "schemas" / "triage.answer.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["properties"]["why"]["maxLength"] = 7
    path.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.setattr(roles, "WORKFLOW", copy)
    other = assemble("triage-check", FIXTURE_ROOT, TRIAGE)
    assert other.digest != base.digest and other.rules_digest == base.rules_digest


def test_the_digests_are_the_documented_hashes() -> None:
    got = assemble("extract", FIXTURE_ROOT, INPUTS["extract"])
    canonical = json.dumps(got.schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = hashlib.sha256(f"{got.system}\0{got.user}\0{canonical}".encode()).hexdigest()
    assert got.digest == expected
    rules = _fixture_rules()
    sections = [
        _between(rules, "## Principles", "## Sources"),
        _between(rules, "## Findings", "## Attribution and approval"),
        _between(rules, "## Triage", "## Findings"),
        _between(rules, "## Attribution and approval", "## Synthesis"),
    ]
    by_cite = {"Principles": 0, "Triage": 2, "Findings": 1, "Attribution and approval": 3}
    joined = "".join(sections[by_cite[cite]] for cite in _cites("extract"))
    assert got.rules_digest == hashlib.sha256(joined.encode()).hexdigest()


@pytest.mark.parametrize("role", ROLES)
def test_every_packaged_role_assembles_against_the_atlas_rules(role: str) -> None:
    root = Path(os.environ["CHD_ATLAS_ROOT"])
    got = assemble(role, root, INPUTS[role])
    rules = (root / "curator" / "curator-rules.md").read_text(encoding="utf-8")
    blocks = re.findall(r"--- rules: (.+?) ---\n(.*?)\n--- end rules: \1 ---", got.system, re.S)
    assert [cite for cite, _ in blocks] == _cites(role)
    for cite, section in blocks:
        assert section.startswith("#") and section.split("\n", 1)[0].lstrip("# ") == cite
        assert section in rules  # the atlas's own bytes
    if role.startswith("triage"):
        assert "Triage" in [cite for cite, _ in blocks]


def test_python_dash_m_runs_the_cli() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "chd_curator", "--help"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "check-batch" in done.stdout


# ---- the answer schemas -------------------------------------------------------------------


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (roles.WORKFLOW / "schemas" / f"{name}.answer.json").read_text(encoding="utf-8")
    )
    return loaded


def _yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["triage", "reader", "judge"])
def test_an_answer_schema_is_a_valid_closed_object_schema(name: str) -> None:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert sorted(schema["required"]) == sorted(schema["properties"])


def test_the_triage_reasons_are_the_atlas_reasons_less_two() -> None:
    atlas = json.loads(
        Path(
            os.environ["CHD_ATLAS_ROOT"], "schemas", "gene_literature_file.schema.json"
        ).read_text()
    )
    reasons = set(atlas["$defs"]["ExclusionReason"]["enum"])
    allowed = {"none"} | (reasons - {"same_data_as", "full_text_not_accessible"})
    schema = _schema("triage")
    assert set(schema["properties"]["reason"]["enum"]) == allowed
    assert set(schema["properties"]["decision"]["enum"]) == {"included", "excluded"}
    assert sorted(schema["required"]) == ["decision", "pmid", "reason", "why"]


def test_the_triage_schema_accepts_an_answer_and_refuses_what_is_not_one() -> None:
    validator = Draft202012Validator(_schema("triage"))
    good = {"pmid": "PMID:91000001", "decision": "excluded", "reason": "not_chd", "why": "AF only"}
    assert validator.is_valid(good)
    assert validator.is_valid({**good, "decision": "included", "reason": "none"})
    for bad in (
        {**good, "reason": "same_data_as"},
        {**good, "reason": "full_text_not_accessible"},
        {**good, "decision": "maybe"},
        {**good, "pmid": "91000001"},
        {**good, "extra": 1},
        {k: v for k, v in good.items() if k != "why"},
        {**good, "why": ""},
    ):
        assert not validator.is_valid(bad), bad


def _reader_answer(batch: str) -> dict[str, Any]:
    stem = RUN / "extract" / f"batch-{batch}"
    return {
        "findings": _yaml(Path(f"{stem}.findings.yaml"))["findings"],
        "evidence": _yaml(Path(f"{stem}.evidence.yaml")),
        "papers": json.loads(Path(f"{stem}.papers.json").read_text()),
        "report": Path(f"{stem}.report.md").read_text(),
    }


@pytest.mark.parametrize("batch", ["01", "02"])
def test_the_reader_schema_accepts_the_phase_two_files_and_refuses_a_broken_one(batch: str) -> None:
    validator = Draft202012Validator(_schema("reader"))
    answer = _reader_answer(batch)
    assert validator.is_valid(answer)
    # Round trip through JSON, which is how a backend delivers it.
    assert validator.is_valid(json.loads(json.dumps(answer, default=str)))
    assert not validator.is_valid({k: v for k, v in answer.items() if k != "report"})
    assert not validator.is_valid({**answer, "extra": 1})
    assert not validator.is_valid({**answer, "findings": [{"summary": "no id"}]})
    assert not validator.is_valid({**answer, "evidence": {"not-an-id": []}})
    quote = {"field": "design", "quote": "q", "where": "w"}
    assert not validator.is_valid({**answer, "evidence": {"CHDA:LIT:0901001": [{"field": "x"}]}})
    assert validator.is_valid({**answer, "evidence": {"CHDA:LIT:0901001": [quote]}})
    excluded = [{"pmid": "PMID:91000002", "exclude": {"reason": "other", "note": "an erratum"}}]
    assert validator.is_valid({**answer, "papers": excluded})
    assert not validator.is_valid({**answer, "papers": [{"pmid": "PMID:91000002"}]})
    assert not validator.is_valid(
        {**answer, "papers": [{"pmid": "PMID:91000002", "study_type": None, "reading": ""}]}
    )


def test_the_judge_schema_accepts_the_phase_two_files_and_refuses_a_broken_one() -> None:
    validator = Draft202012Validator(_schema("judge"))
    answer = {
        "findings": _yaml(JUDGE / "judging" / "judge-01.findings.yaml")["findings"],
        "decisions": _yaml(JUDGE / "judge" / "judge-01.decisions.yaml"),
    }
    assert answer["decisions"] and validator.is_valid(answer)
    first = next(iter(answer["decisions"].values()))[0]
    assert not validator.is_valid(
        {**answer, "decisions": {"CHDA:LIT:0009003": [{**first, "decision": "both"}]}}
    )
    assert not validator.is_valid(
        {
            **answer,
            "decisions": {"CHDA:LIT:0009003": [{k: v for k, v in first.items() if k != "rule"}]},
        }
    )
    assert not validator.is_valid({"findings": answer["findings"]})


# ---- the skills point at the role files ---------------------------------------------------

POINTS_AT = {
    "literature-triage": ("triage-check", "triage-screen"),
    "literature-extract": ("extract",),
    "literature-verify": ("blind-read", "judge"),
}


@pytest.mark.parametrize("skill", POINTS_AT)
def test_each_skill_points_at_its_role_file_and_restates_no_rule(skill: str) -> None:
    text = (REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    for role in POINTS_AT[skill]:
        assert (roles.WORKFLOW / "roles" / f"{role}.md").is_file()
        assert f"`src/chd_curator/workflow/roles/{role}.md`" in " ".join(text.split())
    rules = Path(os.environ["CHD_ATLAS_ROOT"], "curator", "curator-rules.md")
    assert restated(text, rules.read_text(encoding="utf-8")) == []
    assert restated(text, _fixture_rules()) == []
    # The detector can fail: a rule sentence pasted into the skill is found.
    pasted = text + "\nExclude only for a reason the abstract shows, and when unsure, include it.\n"
    assert (
        restated(
            pasted, "x Exclude only for a reason the abstract shows, and when unsure, include it. y"
        )
        != []
    )


def test_every_role_is_pointed_at_by_a_skill() -> None:
    assert sorted(r for roles_ in POINTS_AT.values() for r in roles_) == sorted(ROLES)
