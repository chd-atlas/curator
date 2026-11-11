"""``chd-curator diff``, with a stub atlas and the atlas's real exported schemas.

Ported from the six diff cases of the atlas's ``tests/unit/test_cli_literature.py`` at
b4c8191 (prints and writes nothing; stamps only agreeing unverified findings; an
ambiguous key verifies nothing; a verifier that is not an agent, two cases; a missing
file). The 8-layout ``--mark-verified`` test is ``test_records.py``'s. Not ported: the
atlas's last sub-case (a blind pass carrying ``approved_by: agent:x`` is refused),
because that rule lives in the atlas's pydantic model and not in the exported JSON
Schema curator checks against; a blind pass's approval fields are never compared.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import typer
from typer.core import TyperGroup
from typer.testing import CliRunner, Result

from chd_curator.cli import app

runner = CliRunner()

LITERATURE = """\
# Curator's head comment -- must survive every write.
gene: HGNC:11604
searches:
  - id: 1
    source: pubmed
    query: TBX5[tiab]
    run_on: 2026-11-02
    hits: 3
    new_hits: 3
triage:
  - pmid: PMID:8988165
    search: 1
    decision: included
findings:
  - id: CHDA:LIT:0000001
    publication: PMID:8988165
    design: cohort_screen
    methods:
      - exome
    count_unit: probands
    screened: 7
    carriers:
      de_novo: 2
      inherited: 1
      origin_unknown: 3
    variant_classes:
      - snv_indel
    phenotypes:
      - HP:0001631
    lesion_groups:
      - septal
    syndromic: both
    cohorts:
      - alpha_cohort
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Table 2 & <3>
    summary: Six of seven probands carry a variant.
    extracted_by: agent:extract/test
  # A comment between two findings.
  - id: CHDA:LIT:0000002
    publication: PMID:111
    design: common_variant_association
    methods:
      - genotyping_array
    cases: 11
    controls: 13
    p_value: 0.02
    odds_ratio: 1.5
    phenotypes:
      - HP:0001631
    lesion_groups:
      - septal
    syndromic: isolated
    shares_probands_with: unknown
    locators:
      - kind: supplementary
        file: s1.xlsx
        sheet: Sheet 1
        row: 4
    summary: A GWAS hit.
    extracted_by: agent:extract/test
    verified_by: agent:verify/earlier
  - id: CHDA:LIT:0000003
    publication: PMID:222
    design: case_report
    methods:
      - targeted_sequencing
    count_unit: probands
    screened: 1
    carriers:
      de_novo: 1
      inherited: 0
      origin_unknown: 0
    variant_classes:
      - snv_indel
    phenotypes:
      - HP:0001631
    lesion_groups:
      - conotruncal
    syndromic: syndromic
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Abstract
    summary: A proband.
    extracted_by: agent:extract/test
    approved_by: Dr. Ada Curator
    approved_on: 2026-10-01
  - id: CHDA:LIT:0000004
    publication: PMID:333
    design: case_report
    methods:
      - targeted_sequencing
    count_unit: probands
    screened: 1
    carriers:
      de_novo: 0
      inherited: 1
      origin_unknown: 0
    variant_classes:
      - snv_indel
    phenotypes:
      - HP:0001631
    lesion_groups:
      - septal
    syndromic: isolated
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Abstract
    summary: Another proband.
    extracted_by: agent:extract/test
"""

VERIFIER = """\
findings:
  - id: CHDA:LIT:0000001
    publication: PMID:8988165
    design: cohort_screen
    methods:
      - exome
    count_unit: probands
    screened: 7
    carriers:
      de_novo: 2
      inherited: 1
      origin_unknown: 3
    variant_classes:
      - snv_indel
    phenotypes:
      - HP:0001631
    lesion_groups:
      - septal
    syndromic: both
    cohorts:
      - alpha_cohort
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Table 2, worded differently
    summary: Different words, same finding.
    extracted_by: agent:verify/blind
  - id: CHDA:LIT:0000002
    publication: PMID:111
    design: common_variant_association
    methods:
      - genotyping_array
    cases: 11
    controls: 13
    p_value: 0.02
    odds_ratio: 1.5
    phenotypes:
      - HP:0001631
    lesion_groups:
      - septal
    syndromic: isolated
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Abstract
    summary: A GWAS hit.
    extracted_by: agent:verify/blind
  - id: CHDA:LIT:0000003
    publication: PMID:222
    design: case_report
    methods:
      - targeted_sequencing
    count_unit: probands
    screened: 1
    carriers:
      de_novo: 1
      inherited: 0
      origin_unknown: 0
    variant_classes:
      - snv_indel
    phenotypes:
      - HP:0001631
    lesion_groups:
      - conotruncal
    syndromic: syndromic
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Abstract
    summary: A proband.
    extracted_by: agent:verify/blind
  - id: CHDA:LIT:0000004
    publication: PMID:333
    design: case_report
    methods:
      - targeted_sequencing
    count_unit: probands
    screened: 1
    carriers:
      de_novo: 1
      inherited: 0
      origin_unknown: 0
    variant_classes:
      - snv_indel
    phenotypes:
      - HP:0001631
    lesion_groups:
      - septal
    syndromic: isolated
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Abstract
    summary: Another proband, counted differently.
    extracted_by: agent:verify/blind
  - id: CHDA:LIT:0000005
    publication: PMID:999
    design: case_report
    methods:
      - exome
    count_unit: probands
    screened: 1
    carriers:
      de_novo: 1
      inherited: 0
      origin_unknown: 0
    variant_classes:
      - snv_indel
    phenotypes:
      - HP:0001631
    lesion_groups:
      - septal
    syndromic: isolated
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Abstract
    summary: Only the blind pass found this.
    extracted_by: agent:verify/blind
"""

FINDING_ID = re.compile(r"(?m)^  - id: (CHDA:LIT:\d{7})\n")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "curation").mkdir()
    (tmp_path / "schemas").symlink_to(Path(os.environ["CHD_ATLAS_ROOT"]) / "schemas")
    return tmp_path


@pytest.fixture
def lit_path(root: Path) -> Path:
    path = root / "curation" / "literature" / "TBX5.yaml"
    path.parent.mkdir()
    path.write_text(LITERATURE)
    return path


def _blocks(text: str) -> dict[str, str]:
    """Each finding's exact text, by id; "head" is everything before the first."""
    marks = list(FINDING_ID.finditer(text))
    blocks = {"head": text[: marks[0].start()]}
    for mark, nxt in zip(marks, [*marks[1:], None], strict=True):
        blocks[mark.group(1)] = text[mark.start() : nxt.start() if nxt else len(text)]
    return blocks


def _second(root: Path, text: str = VERIFIER) -> Path:
    path = root / "local" / "second.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def _diff(stub: Path, root: Path, against: Path, *args: str, symbol: str = "TBX5") -> Result:
    return runner.invoke(
        app,
        [
            "diff",
            symbol,
            "--against",
            str(against),
            *args,
            "--root",
            str(root),
            "--atlas-cli",
            str(stub),
        ],
    )


def test_the_diff_prints_what_differs_and_writes_nothing(
    stub_atlas: Path, root: Path, lit_path: Path
) -> None:
    before = lit_path.read_bytes()
    result = _diff(stub_atlas, root, _second(root))

    assert result.exit_code == 0, result.stdout  # differences are output, not an error
    assert result.stdout == (
        "agreed (3): CHDA:LIT:0000001, CHDA:LIT:0000002, CHDA:LIT:0000003\n"
        "only in the first pass (0): none\n"
        "only in the second pass (1): CHDA:LIT:0000005\n"
        "differ (1):\n"
        "  CHDA:LIT:0000004\n"
        '    carriers: first {"de_novo": 0, "inherited": 1, "origin_unknown": 0}'
        ' | second {"de_novo": 1, "inherited": 0, "origin_unknown": 0}\n'
    )
    assert lit_path.read_bytes() == before


def test_mark_verified_stamps_only_agreeing_unverified_findings(
    stub_atlas: Path, root: Path, lit_path: Path
) -> None:
    before = _blocks(lit_path.read_text())
    result = _diff(stub_atlas, root, _second(root), "--mark-verified", "agent:verify/blind")

    assert result.exit_code == 0, result.stdout
    assert result.stdout.endswith("marked verified (2): CHDA:LIT:0000001, CHDA:LIT:0000003\n")
    after = _blocks(lit_path.read_text())
    assert after["head"] == before["head"]
    # 1 and 3 agreed and were unverified: one line each, right after extracted_by.
    for finding in ("CHDA:LIT:0000001", "CHDA:LIT:0000003"):
        assert after[finding] == before[finding].replace(
            "    extracted_by: agent:extract/test\n",
            "    extracted_by: agent:extract/test\n    verified_by: agent:verify/blind\n",
        )
    # 2 agreed but already carries a verifier: untouched. 4 differs: untouched.
    assert after["CHDA:LIT:0000002"] == before["CHDA:LIT:0000002"]
    assert after["CHDA:LIT:0000004"] == before["CHDA:LIT:0000004"]
    assert (
        "approved_by: Dr. Ada Curator\n    approved_on: 2026-10-01\n" in after["CHDA:LIT:0000003"]
    )


def test_a_second_pass_key_shared_by_two_findings_verifies_nothing(
    stub_atlas: Path, root: Path, lit_path: Path
) -> None:
    twin = (
        VERIFIER.split("  - id: CHDA:LIT:0000002")[0]
        .removeprefix("findings:\n")
        .replace("CHDA:LIT:0000001", "CHDA:LIT:0000006")
    )
    before = lit_path.read_bytes()
    second = _second(
        root, VERIFIER.replace("  - id: CHDA:LIT:0000002", twin + "  - id: CHDA:LIT:0000002", 1)
    )

    result = _diff(stub_atlas, root, second, "--mark-verified", "agent:verify/blind")

    assert result.exit_code == 0, result.stdout
    assert "only in the first pass (1): CHDA:LIT:0000001" in result.stdout
    assert (
        "only in the second pass (3): CHDA:LIT:0000001, CHDA:LIT:0000005, CHDA:LIT:0000006"
        in result.stdout
    )
    assert result.stdout.endswith("marked verified (1): CHDA:LIT:0000003\n")
    assert (
        "verified_by: agent:verify/blind" not in _blocks(lit_path.read_text())["CHDA:LIT:0000001"]
    )
    assert lit_path.read_bytes() != before


@pytest.mark.parametrize("verifier", ["Dr. Ada", "", "agent:", "agent:   "])
def test_diff_refuses_a_verifier_that_is_not_an_agent(
    stub_atlas: Path, root: Path, lit_path: Path, verifier: str
) -> None:
    before = lit_path.read_bytes()
    result = _diff(stub_atlas, root, _second(root), "--mark-verified", verifier)
    assert result.exit_code == 2 and "agent:<skill>/<model>" in result.stdout
    assert lit_path.read_bytes() == before


def test_diff_names_a_missing_file_a_missing_second_pass_and_a_broken_one(
    stub_atlas: Path, root: Path, lit_path: Path
) -> None:
    missing = _diff(stub_atlas, root, _second(root), symbol="GATA4")
    assert missing.exit_code == 2
    assert str(root / "curation" / "literature" / "GATA4.yaml") in missing.stdout

    absent = _diff(stub_atlas, root, root / "local" / "nope.yaml")
    assert absent.exit_code == 2 and "nope.yaml" in absent.stdout

    refused = _diff(stub_atlas, root, _second(root), symbol="NOPE")
    assert refused.exit_code == 2 and "not a known gene symbol" in refused.stdout

    broken = _diff(stub_atlas, root, _second(root, "findings:\n  - id: CHDA:LIT:0000001\n"))
    assert broken.exit_code == 1 and "error:" in broken.stdout

    duplicate = _diff(
        stub_atlas, root, _second(root, VERIFIER + VERIFIER.split("findings:\n", 1)[1])
    )
    assert duplicate.exit_code == 1 and "duplicate" in duplicate.stdout


def test_the_cli_has_no_command_or_option_that_approves() -> None:
    """curator never approves: spec section 4.5 and F9.

    Spec section 4.5: "It has no approve command and never writes ``approved_by`` or
    ``approved_digest``." F9 keeps approval in the atlas (``chd-atlas literature review
    --approve``), because only a named person's approval makes a finding reach a page.
    The names are read from the registered typer/click objects, not from help text.
    """
    group = typer.main.get_command(app)
    assert isinstance(group, TyperGroup)
    assert set(group.commands) == {"search", "abstracts", "add-publication", "diff"}
    forbidden = {"--approve", "--by", "--on"}
    for name, command in group.commands.items():
        options = {opt for param in command.params for opt in param.opts + param.secondary_opts}
        assert not options & forbidden, (name, options & forbidden)
