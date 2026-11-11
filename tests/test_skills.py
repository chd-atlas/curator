"""The plugin's sentences, held to the code and schemas they describe.

The four skills are plain Markdown that tells an agent which commands to run, which options
they take, which validator codes catch which mistake and which vocabulary values a record may
hold. Nothing executes them, so a misspelt option or a code that does not exist would pass
every other test and mislead the next agent. They were moved here from the atlas
(b4c8191, ``tests/test_curator_plugin.py``); this is that file's skills half, and the half
about ``curator-rules.md`` stays in the atlas.

curator never imports ``chd_atlas``. Every name below is read at test time from curator's own
typer app, from the atlas's exported JSON Schemas under ``$CHD_ATLAS_ROOT/schemas/``, or from
the rules file in the atlas checkout. Two deliberate literals are the contract under test:
the design -> StudyType table and ``ATLAS_COMMANDS`` (which atlas commands the files may name).
``tests/test_contract.py`` runs the atlas's own models over the same examples.
"""

from __future__ import annotations

import json
import os
import re
import textwrap
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import typer
from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from chd_curator.atlas import plain
from chd_curator.cli import app

REPO = Path(__file__).resolve().parent.parent
SKILLS = ("literature-search", "literature-triage", "literature-extract", "literature-verify")
SKILL_FILES = [REPO / "skills" / name / "SKILL.md" for name in SKILLS]
README = REPO / "README.md"
# The skills and the README are curator's; the rules file is read from the atlas checkout.
CURATOR_FILES = [*SKILL_FILES, README]


def _atlas_root() -> Path:
    return Path(os.environ["CHD_ATLAS_ROOT"])


def _rules() -> Path:
    return _atlas_root() / "curator" / "curator-rules.md"


# The atlas commands the files may name: a literal, because it is the contract under test.
# `tests/test_contract.py` runs the atlas's real --help against it.
ATLAS_COMMANDS: dict[str, set[str]] = {
    "validate": set(),
    "literature target": set(),
    "literature review": {
        "--approve",
        "--by",
        "--on",
        "--allow-unverified",
        "--against",
        "--pending-review",
        "--note",
        "--root",
    },
}

# The contract: the mapping `literature-extract` tells an agent to apply.
DESIGN_TO_STUDY_TYPE = [
    ("case_report", "case_report"),
    ("family_segregation", "family_linkage"),
    ("cohort_screen", "cohort_sequencing"),
    ("rare_variant_association with control_cohort", "case_control"),
    ("rare_variant_association with mutation_model and exome", "trio_wes"),
    ("rare_variant_association with mutation_model and genome", "trio_wgs"),
    (
        "rare_variant_association with mutation_model and any other method",
        "ask the curator; no StudyType is guessed",
    ),
    ("common_variant_association with genotyping_array", "gwas"),
    ("common_variant_association otherwise", "candidate_variant"),
]

# Options a file may name that belong to no curator or pinned atlas command.
# `--marketplace` is Claude Code's, named once in the README.
OTHER_OPTIONS = {"--plugin-dir", "--marketplace"}

# The burden mirror's count units. The atlas exports no enum for them (the column is a plain
# string in schemas/tables/burden.v1.json), so they are a literal; a Finding refuses them.
BURDEN_COUNT_UNITS = {"individuals", "alleles", "de_novo_mutations"}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_files() -> list[Path]:
    return [*CURATOR_FILES, _rules()]


def _commands() -> dict[str, Any]:
    # typer vendors click, so the command objects are duck-typed here.
    group = typer.main.get_command(app)
    return dict(group.commands)  # type: ignore[attr-defined]


def _options(command: Any) -> set[str]:
    return {opt for param in command.params for opt in getattr(param, "opts", [])}


def _schema(name: str) -> dict[str, Any]:
    path = _atlas_root() / "schemas" / f"{name}.schema.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _defs() -> dict[str, Any]:
    defs: dict[str, Any] = {}
    for name in ("gene_literature_file", "publication_file"):
        defs.update(_schema(name)["$defs"])
    return defs


def _enum_values() -> dict[str, set[str]]:
    return {name: set(d["enum"]) for name, d in _defs().items() if "enum" in d}


def _all_enum_values() -> set[str]:
    return set().union(*_enum_values().values())


def _model_fields() -> set[str]:
    defs = _defs()
    fields: set[str] = set()
    for name in (
        "Finding",
        "Search",
        "TriageEntry",
        "Carriers",
        "Publication",
        "InTextLocator",
        "SupplementaryLocator",
    ):
        fields |= set(defs[name]["properties"])
    return fields | {"findings", "triage", "searches", "gene"}


def _option_names() -> set[str]:
    """Parameter names of curator's commands and of the pinned atlas options."""
    names = {param.name for cmd in _commands().values() for param in cmd.params if param.name}
    pinned = set().union(*ATLAS_COMMANDS.values())
    return names | {option.removeprefix("--").replace("-", "_") for option in pinned}


def _command_lines(text: str, prefix: str) -> Iterator[str]:
    """Lines naming ``prefix``; backslash continuations are joined."""
    joined = re.sub(r"\\\n\s*", " ", text)
    for line in joined.splitlines():
        if prefix in line:
            yield line


def _fenced_yaml(text: str) -> list[Any]:
    # Fences inside a numbered step are indented; dedent before parsing.
    blocks = re.findall(r"^[ ]*```yaml\n(.*?)^[ ]*```", text, flags=re.S | re.M)
    return [YAML(typ="safe").load(textwrap.dedent(block)) for block in blocks]


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def skill_examples() -> tuple[list[Any], list[Any]]:
    """``(findings, triage entries)`` from the skills' YAML examples, as plain data."""
    findings: list[Any] = []
    triage: list[Any] = []
    for path in SKILL_FILES:
        for block in _fenced_yaml(_text(path)):
            if isinstance(block, dict) and "findings" in block:
                findings += plain(block["findings"])
            if isinstance(block, list):
                for record in block:
                    if "pmid" in record:
                        triage.append(plain(record))
                    if "publication" in record:
                        findings.append(plain(record))
    return findings, triage


def test_the_marketplace_and_the_plugin_are_named_curator() -> None:
    plugin = json.loads(_text(REPO / ".claude-plugin" / "plugin.json"))
    market = json.loads(_text(REPO / ".claude-plugin" / "marketplace.json"))
    version = tomllib.loads(_text(REPO / "pyproject.toml"))["project"]["version"]
    assert plugin["name"] == "curator"
    assert plugin["license"] == "Apache-2.0"
    assert plugin["version"] == version
    assert plugin["description"].strip()
    assert market["name"] == "curator"
    assert market["owner"] == {"name": "chd-atlas"}
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [("curator", "./")]
    assert market["plugins"][0]["description"].strip()


def test_the_plugin_ships_exactly_these_skills() -> None:
    found = sorted(p.parent.name for p in (REPO / "skills").glob("*/SKILL.md"))
    assert found == sorted(SKILLS)
    assert all(path.is_file() for path in CURATOR_FILES)
    assert _rules().is_file()


@pytest.mark.parametrize("path", SKILL_FILES, ids=SKILLS)
def test_a_skill_has_its_frontmatter_its_sections_and_its_hand_offs(path: Path) -> None:
    text = _text(path)
    match = re.match(r"---\nname: (\S+)\ndescription: (.+)\n---\n", text)
    assert match is not None, "frontmatter must be exactly `name` then `description`"
    assert match.group(1) == path.parent.name
    assert match.group(2).startswith("Use when ")
    for heading in ("## Inputs", "## Steps", "## Done when", "## Never"):
        assert len(re.findall(rf"^{heading}$", text, flags=re.M)) == 1, heading
    assert len(text.splitlines()) <= 120
    assert "the atlas checkout's `curator/curator-rules.md`" in text
    # Every skill ends by validating and handing the work to the human, in the atlas.
    tail = text[text.rindex("## Done when") :]
    assert ".venv/bin/chd-atlas validate" in tail
    assert ".venv/bin/chd-atlas literature review <SYMBOL>" in tail


def test_every_curator_command_and_option_the_files_name_exists() -> None:
    commands = _commands()
    assert set(commands) == {"search", "abstracts", "add-publication", "diff"}
    allowed = set().union(*(_options(c) for c in commands.values()))
    allowed |= set().union(*ATLAS_COMMANDS.values()) | OTHER_OPTIONS
    named: set[str] = set()
    assert {*SKILL_FILES, README, _rules()} == set(_all_files())  # the rules file is read too
    for path in _all_files():
        text = _text(path)
        for line in _command_lines(text, "chd-curator "):
            for form in re.finditer(r"chd-curator (\S+)", line):
                name = form.group(1).strip("`.,;:)")
                assert name in commands, f"{path.name}: no command `chd-curator {name}`"
                named.add(name)
                rest = line.split(form.group(0), 1)[1]
                for option in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", rest):
                    if option in OTHER_OPTIONS | set().union(*ATLAS_COMMANDS.values()):
                        continue
                    assert option in _options(commands[name]), f"{path.name}: `{name}` {option}"
        for option in re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", text):
            assert option in allowed, f"{path.name}: {option}"
    # Each command a curator runs is taught somewhere, so a removed one is noticed.
    assert named == set(commands)


def test_every_atlas_command_the_files_name_is_pinned() -> None:
    named: set[str] = set()
    # The rules file is the atlas's and names `chd-atlas literature search` (line 26 when
    # measured), which is not in ATLAS_COMMANDS, so only curator's files are pinned here.
    # `test_every_curator_command_and_option_the_files_name_exists` reads the rules file too.
    for path in CURATOR_FILES:
        for line in _command_lines(_text(path), "chd-atlas "):
            for form in re.finditer(r"chd-atlas (?:literature )?([a-z][a-z-]*)", line):
                name = form.group(0).removeprefix("chd-atlas ")
                assert name in ATLAS_COMMANDS, f"{path.name}: unpinned `chd-atlas {name}`"
                named.add(name)
                rest = line.split(form.group(0), 1)[1]
                for option in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", rest):
                    assert option in ATLAS_COMMANDS[name] | OTHER_OPTIONS, (
                        f"{path.name}: `{name}` {option}"
                    )
    assert named == set(ATLAS_COMMANDS)


def test_every_code_the_skills_name_is_one_the_rules_cite() -> None:
    cited = set(re.findall(r"\b([A-Z]{2,6}\d{3})\b", _text(_rules())))
    assert {"LIT002", "LIT015"} <= cited, "the rules file cites no codes, so the check is vacuous"
    named: set[str] = set()
    for path in SKILL_FILES:
        for code in re.findall(r"\b([A-Z]{2,6}\d{3})\b", _text(path)):
            assert code in cited, f"{path.name}: {code} is not cited in curator-rules.md"
            named.add(code)
    assert named, "no skill names a code, so the check is vacuous"


def test_the_yaml_examples_hold_real_vocabulary_and_validate_against_the_schema() -> None:
    enums = _enum_values()
    by_field = {
        "design": enums["FindingDesign"],
        "methods": enums["DetectionMethod"],
        "count_unit": enums["ScreenUnit"],
        "variant_classes": enums["FindingVariantClass"],
        "comparator": enums["BurdenComparator"],
        "decision": enums["TriageDecision"],
        "reason": enums["ExclusionReason"],
        "syndromic": enums["SyndromicStatus"],
        "lesion_groups": enums["LesionGroup"],
        "review_status": enums["ReviewStatus"],
    }
    defs = _defs()
    validators = {
        name: Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": defs})
        for name in ("Finding", "TriageEntry")
    }
    seen_fields: set[str] = set()
    for path in CURATOR_FILES:
        for block in _fenced_yaml(_text(path)):
            for key, value in _walk(block):
                assert key in _model_fields(), f"{path.name}: unknown field {key}"
                seen_fields.add(key)
                if key in by_field:
                    values = value if isinstance(value, list) else [value]
                    assert set(values) <= by_field[key], f"{path.name}: {key}: {values}"
    findings, triage = skill_examples()
    assert findings and triage
    for record in findings:
        validators["Finding"].validate(record)
    for record in triage:
        validators["TriageEntry"].validate(record)
    # The examples cover both record kinds, so they are not vacuous.
    assert {"findings", "decision", "design", "locators"} <= seen_fields


def test_backticked_names_are_real_values_fields_or_options() -> None:
    known = _all_enum_values() | _model_fields() | _option_names()
    for path in _all_files():
        # Fenced blocks are checked structurally above.
        prose = re.sub(r"^[ ]*```.*?^[ ]*```", "", _text(path), flags=re.S | re.M)
        for token in re.findall(r"`([a-z]+(?:_[a-z]+)+)`", prose):
            assert token in known, f"{path.name}: `{token}` is not a value, field or option"


def test_a_paragraph_naming_count_unit_names_only_a_findings_units() -> None:
    # The rules once offered `individuals`, a burden-mirror unit a Finding refuses.
    # It is one word and a real value of another vocabulary, so the check above passed it.
    enums = _enum_values()
    units = BURDEN_COUNT_UNITS | set().union(*(v for n, v in enums.items() if n.endswith("Unit")))
    for path in _all_files():
        prose = re.sub(r"^[ ]*```.*?^[ ]*```", "", _text(path), flags=re.S | re.M)
        for paragraph in re.split(r"\n(?=\s*\n|\s*(?:- |\d+\. ))", prose):
            if "`count_unit`" in paragraph:
                named = set(re.findall(r"`([a-z_]+)`", paragraph)) & units
                assert named <= enums["ScreenUnit"], f"{path.name}: {sorted(named)}"


def test_every_study_type_the_files_name_exists_and_the_table_is_the_contract() -> None:
    study_types = _enum_values()["StudyType"]
    text = _text(REPO / "skills" / "literature-extract" / "SKILL.md")
    # A study cell is a backticked StudyType, or the one plain-text row that tells the
    # agent to stop and ask instead of guessing one.
    rows = re.findall(
        r"^\s*\| (`[^|]+?`(?: with `[^|]+?`(?: and [^|`]+?)?| otherwise)?) \| "
        r"(?:`([^|]+?)`|([^|`]+?)) \|$",
        text,
        re.M,
    )
    table = [(design.replace("`", ""), study or ask) for design, study, ask in rows]
    assert table == DESIGN_TO_STUDY_TYPE
    assert {study for _, study in table} <= study_types | {
        "ask the curator; no StudyType is guessed"
    }
    for path in _all_files():
        for study in re.findall(r"--study-type (\S+)", _text(path)):
            assert study.strip("`") in study_types | {"<StudyType>"}, path.name


def test_the_skills_state_the_verified_rulings_literally() -> None:
    extract = _text(REPO / "skills" / "literature-extract" / "SKILL.md")
    verify = _text(REPO / "skills" / "literature-verify" / "SKILL.md")
    assert "local/literature/<SYMBOL>.verify.yaml" in verify
    assert "--mark-verified agent:literature-verify/<model>" in verify
    assert "curation/.id_registry.yaml" in extract
    assert "agent:literature-extract/<model>" in extract
    # A refused write exits 2 (not 1), and the skills must not say otherwise.
    search = _text(REPO / "skills" / "literature-search" / "SKILL.md")
    assert "or the file changed while the search ran)" in search
    assert "exit 2, a bad argument or a schema refusal" in extract
    assert "the file is damaged" not in search and "does not validate" not in extract
    # The verifier writes `unknown`; the curator reads the field in the listing.
    assert "always set `shares_probands_with: unknown`" in verify
    assert "field in the `review` listing" in extract
    for path in _all_files():
        text = _text(path)
        assert not re.search(r"NCBI_API_KEY\s*=\s*\S", text), path.name
        assert not re.search(r"api_key\s*[=:]\s*\S", text), path.name
