"""``diff_findings``: a blind second extraction against the first.

Ported from the atlas's ``tests/unit/test_gene_literature_diff.py`` at b4c8191 (23 cases),
from ``Finding`` models to plain dicts checked against the atlas's real exported schema
(``CHD_ATLAS_ROOT/schemas/gene_literature_file.schema.json``). Every pair below differs
in exactly one thing, so a test that names the field names the only field the diff may
report. Values within a pair are chosen to differ from every other field's (a fixture whose fields share a value cannot tell
of the atlas's notes): screened 7 vs 9, de_novo 2 vs 3.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from chd_curator.atlas import schema_validator
from chd_curator.diff import (
    EXCLUDED_FIELDS,
    DiffError,
    FindingDiff,
    compared_fields,
    diff_findings,
    parse_findings_file,
)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    loaded: dict[str, Any] = schema_validator(
        Path(os.environ["CHD_ATLAS_ROOT"]), "gene_literature_file"
    ).schema
    return loaded


def _finding(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "CHDA:LIT:0000001",
        "publication": "PMID:8988165",
        "design": "cohort_screen",
        "methods": ["exome", "genome"],
        "count_unit": "probands",
        "screened": 7,
        "carriers": {"de_novo": 2, "inherited": 1, "origin_unknown": 3},
        "variant_classes": ["snv_indel", "cnv"],
        "phenotypes": ["HP:0001631", "HP:0001629"],
        "lesion_groups": ["septal", "avsd"],
        "syndromic": "both",
        "cohorts": ["alpha_cohort", "beta_cohort"],
        "shares_probands_with": "unknown",
        "locators": [{"kind": "in_text", "element": "Table 2"}],
        "summary": "Six of seven probands carry a variant.",
        "extracted_by": "agent:extract/test",
    }
    base.update(changes)
    return base


def _pair(schema: dict[str, Any], first: dict[str, Any], second: dict[str, Any]) -> FindingDiff:
    return diff_findings([_finding(**first)], [_finding(id="CHDA:LIT:0000009", **second)], schema)


def test_the_compared_set_is_pinned(schema: dict[str, Any]) -> None:
    """The 17 fields a blind pass is compared on.

    The atlas pins the same 17 in ``tests/unit/test_gene_literature_diff.py``
    (``test_the_compared_set_is_pinned``); the two change together (spec F18). A field the
    atlas adds to ``Finding`` lands in ``compared_fields`` by default, and this test is
    where that is noticed.
    """
    assert set(compared_fields(schema)) == {
        "publication",
        "design",
        "methods",
        "comparator",
        "burden_study",
        "count_unit",
        "screened",
        "carriers",
        "variant_classes",
        "cases",
        "controls",
        "p_value",
        "odds_ratio",
        "phenotypes",
        "lesion_groups",
        "syndromic",
        "cohorts",
    }
    assert len(compared_fields(schema)) == 17
    assert list(compared_fields(schema)) == sorted(compared_fields(schema))
    assert set(EXCLUDED_FIELDS) == {
        "id",
        "summary",
        "extracted_by",
        "verified_by",
        "approved_by",
        "approved_on",
        "approved_digest",
        "review_status",
        "review_note",
        "locators",
        "shares_probands_with",
    }


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("methods", ["exome"], ["gene_panel"]),
        ("burden_study", None, "PMID:42230622"),
        ("count_unit", "probands", "families"),
        ("screened", 7, 9),
        (
            "carriers",
            {"de_novo": 2, "inherited": 1, "origin_unknown": 3},
            {"de_novo": 3, "inherited": 1, "origin_unknown": 3},
        ),
        ("variant_classes", ["snv_indel"], ["snv_indel", "chromosomal"]),
        ("cases", None, 11),
        ("controls", None, 13),
        ("p_value", None, 0.02),
        ("odds_ratio", 1.5, 2.5),
        ("phenotypes", ["HP:0001631"], ["HP:0001631", "HP:0001642"]),
        ("lesion_groups", ["septal"], ["lvoto"]),
        ("syndromic", "isolated", "syndromic"),
    ],
)
def test_a_pair_differing_in_one_field_reports_that_field_and_only_it(
    schema: dict[str, Any], field: str, first: Any, second: Any
) -> None:
    diff = _pair(schema, {field: first}, {field: second})

    assert diff.only_first == () and diff.only_second == ()
    reported_first = first if not isinstance(first, list) else sorted(first)
    reported_second = second if not isinstance(second, list) else sorted(second)
    assert diff.fields == {"CHDA:LIT:0000001": [(field, reported_first, reported_second)]}
    assert diff.agreed == ()


def test_the_comparator_differs_inside_a_rare_variant_association(
    schema: dict[str, Any],
) -> None:
    rva = {"design": "rare_variant_association"}
    diff = _pair(
        schema, {**rva, "comparator": "control_cohort"}, {**rva, "comparator": "mutation_model"}
    )
    assert diff.fields == {"CHDA:LIT:0000001": [("comparator", "control_cohort", "mutation_model")]}


def test_fields_the_curator_reads_are_not_compared(schema: dict[str, Any]) -> None:
    diff = _pair(
        schema,
        {},
        {
            "summary": "Worded differently.",
            "locators": [{"kind": "in_text", "element": "Figure 9"}],
            "extracted_by": "agent:verify/other",
            "verified_by": "agent:verify/other",
            "review_status": "pending_review",
            "review_note": "a note",
        },
    )
    assert diff.fields == {} and diff.agreed == ("CHDA:LIT:0000001",)


def test_a_shares_probands_with_difference_alone_leaves_the_pair_agreed(
    schema: dict[str, Any],
) -> None:
    # R26: the blind pass's ids are local, so the field cannot be compared.
    diff = _pair(
        schema, {"shares_probands_with": ["CHDA:LIT:0000004"]}, {"shares_probands_with": "unknown"}
    )
    assert diff.fields == {} and diff.agreed == ("CHDA:LIT:0000001",)
    assert "shares_probands_with" not in compared_fields(schema)


def test_lists_that_carry_no_order_are_compared_as_sets(schema: dict[str, Any]) -> None:
    diff = _pair(
        schema,
        {},
        {
            "methods": ["genome", "exome"],
            "variant_classes": ["cnv", "snv_indel"],
            "phenotypes": ["HP:0001629", "HP:0001631"],
            "lesion_groups": ["avsd", "septal"],
            "cohorts": ["beta_cohort", "alpha_cohort"],
        },
    )
    # Matched despite the cohort order, and agreeing in every field.
    assert diff.fields == {} and diff.agreed == ("CHDA:LIT:0000001",)


def test_an_omitted_default_equals_an_explicit_one(schema: dict[str, Any]) -> None:
    """``cohorts`` and ``variant_classes`` omitted agree with ``[]``; a null with an omitted null.

    The exported schema carries no ``default`` for the atlas's empty-list fields, so the
    diff reads an omitted array property as ``[]``; the nullable ones carry
    ``"default": null`` in the schema.
    """
    omitted = {"id": "CHDA:LIT:0000001", **_finding()}
    for name in ("cohorts", "variant_classes", "screened", "carriers", "count_unit"):
        del omitted[name]
    explicit = _finding(
        id="CHDA:LIT:0000009",
        cohorts=[],
        variant_classes=[],
        screened=None,
        carriers=None,
        count_unit=None,
    )

    diff = diff_findings([omitted], [explicit], schema)

    assert diff.fields == {} and diff.agreed == ("CHDA:LIT:0000001",)
    # ...and the key treats the omitted cohorts as [] too: a finding with a cohort is another study.
    other = diff_findings(
        [omitted], [_finding(id="CHDA:LIT:0000009", cohorts=["a_cohort"])], schema
    )
    assert other.only_first == ("CHDA:LIT:0000001",) and other.only_second == ("CHDA:LIT:0000009",)


def test_a_finding_in_one_pass_only_lands_in_that_side(schema: dict[str, Any]) -> None:
    first = [
        _finding(),
        _finding(id="CHDA:LIT:0000002", publication="PMID:111"),
    ]
    second = [
        _finding(id="CHDA:LIT:0000008"),
        _finding(id="CHDA:LIT:0000007", publication="PMID:222"),
        # same paper and design, a different cohort set: another study
        _finding(id="CHDA:LIT:0000006", cohorts=["alpha_cohort"]),
    ]
    diff = diff_findings(first, second, schema)
    assert diff.only_first == ("CHDA:LIT:0000002",)
    assert diff.only_second == ("CHDA:LIT:0000006", "CHDA:LIT:0000007")
    assert diff.agreed == ("CHDA:LIT:0000001",)


def test_the_same_paper_and_cohorts_under_another_design_is_another_study(
    schema: dict[str, Any],
) -> None:
    first = [_finding()]
    second = [
        _finding(
            id="CHDA:LIT:0000009",
            design="case_report",
            screened=1,
            carriers={"de_novo": 1, "inherited": 0, "origin_unknown": 0},
        )
    ]
    diff = diff_findings(first, second, schema)
    assert diff.only_first == ("CHDA:LIT:0000001",)
    assert diff.only_second == ("CHDA:LIT:0000009",)
    assert diff.agreed == () and diff.fields == {}


@pytest.mark.parametrize("side", ["first", "second"])
def test_a_key_carried_twice_in_either_pass_is_never_paired(
    schema: dict[str, Any], side: str
) -> None:
    twin_a = _finding(id="CHDA:LIT:0000003", screened=8)
    twin_b = _finding(id="CHDA:LIT:0000002", screened=9)
    single = _finding(id="CHDA:LIT:0000001")
    other = _finding(id="CHDA:LIT:0000008", publication="PMID:111")
    other_again = _finding(id="CHDA:LIT:0000009", publication="PMID:111")
    if side == "first":
        diff = diff_findings([twin_a, twin_b, other], [single, other_again], schema)
        assert diff.only_first == ("CHDA:LIT:0000002", "CHDA:LIT:0000003")
        assert diff.only_second == ("CHDA:LIT:0000001",)
    else:
        diff = diff_findings([single, other], [twin_a, twin_b, other_again], schema)
        assert diff.only_first == ("CHDA:LIT:0000001",)
        assert diff.only_second == ("CHDA:LIT:0000002", "CHDA:LIT:0000003")
    # The unambiguous paper-111 pair is still paired and agrees.
    assert diff.agreed == ("CHDA:LIT:0000008",)
    assert diff.fields == {}


def test_the_report_is_sorted(schema: dict[str, Any]) -> None:
    first = [
        _finding(id="CHDA:LIT:0000030", publication="PMID:3", screened=8, odds_ratio=1.5),
        _finding(id="CHDA:LIT:0000010", publication="PMID:1", screened=8),
    ]
    second = [
        _finding(id="CHDA:LIT:0000041", publication="PMID:1", screened=9),
        _finding(id="CHDA:LIT:0000042", publication="PMID:3", screened=9, odds_ratio=2.5),
    ]
    diff = diff_findings(first, second, schema)
    assert list(diff.fields) == ["CHDA:LIT:0000010", "CHDA:LIT:0000030"]
    assert [name for name, _, _ in diff.fields["CHDA:LIT:0000030"]] == ["odds_ratio", "screened"]


# --- parse_findings_file (new: the atlas parsed these with its pydantic model) ---------


def _file(*findings: dict[str, Any]) -> bytes:
    # JSON is YAML; it keeps this fixture free of the YAML writer under test elsewhere.
    return json.dumps({"findings": list(findings)}).encode()


def test_a_findings_file_parses_to_its_findings(schema: dict[str, Any]) -> None:
    parsed = parse_findings_file(_file(_finding(), _finding(id="CHDA:LIT:0000002")), schema)
    assert [f["id"] for f in parsed] == ["CHDA:LIT:0000001", "CHDA:LIT:0000002"]
    assert parsed[0]["carriers"] == {"de_novo": 2, "inherited": 1, "origin_unknown": 3}


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        (b'{"findings": [{"id": "CHDA:LIT:0000001"}]}', "required"),
        (b'{"findings": [], "gene": "HGNC:1"}', "gene"),
        (b"[]", "object"),
        (b"findings: [{id: a}, *a]", "alias"),
    ],
)
def test_a_malformed_findings_file_is_refused(
    schema: dict[str, Any], raw: bytes, needle: str
) -> None:
    with pytest.raises(DiffError) as err:
        parse_findings_file(raw, schema)
    assert needle in str(err.value)


def test_a_repeated_id_is_refused(schema: dict[str, Any]) -> None:
    with pytest.raises(DiffError, match="duplicate finding id.*CHDA:LIT:0000001"):
        parse_findings_file(_file(_finding(), _finding(publication="PMID:2")), schema)


def test_an_impossible_date_in_a_findings_file_is_refused(schema: dict[str, Any]) -> None:
    bad = _finding(approved_by="Dr. A", approved_on="2026-13-45")
    with pytest.raises(DiffError, match="approved_on"):
        parse_findings_file(_file(bad), schema)
