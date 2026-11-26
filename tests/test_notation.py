"""The notation rulings as code: seven rules, none of which may change what the paper says."""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

import pytest

from chd_curator.notation import NotationChange, apply_rules, fix_c, fix_p

C_CASES = [
    ("c.980-16 T > A", "c.980-16T>A", ("R-F8 spaces",)),
    ("c.1024_1025delAA", "c.1024_1025delAA", ()),
    ("c.G496T", "c.496G>T", ("R-F8 legacy",)),
    ("c.1281C/T", "c.1281C>T", ("R-F8 allele slash",)),
    ("c.668C greater than T", "c.668C>T", ("R-F8 greater than",)),
    ("c.∗12A>G", "c.*12A>G", ("R-F4 star",)),
    ("c.1A>G", "c.1A>G", ()),
    ("c.123 T", "c.123 T", ()),
    ("c.100 + 5 G > A", "c.100+5G>A", ("R-F8 spaces",)),
    ("c.835c>t", "c.835c>t", ()),  # a base's case is not rewritten (compare's manifest says so)
]
P_CASES = [
    ("p. K342fs", "p.K342fs", ("R-F8 spaces",)),
    ("p.Trp401∗", "p.Trp401*", ("R-F4 star",)),
    ("p.Ile227fster5", "p.Ile227fsTer5", ("R-F4 Ter",)),
    ("p.Arg100ter", "p.Arg100Ter", ("R-F4 Ter",)),
    ("p.(Gly84Arg)", "p.(Gly84Arg)", ()),
    ("p.Arg100 C", "p.Arg100 C", ()),
    ("p.Arg279TER", "p.Arg279TER", ()),  # only a lowercase ter becomes Ter
    # spacing is settled after `p.`, not inside the name (compare's manifest says so)
    ("p. Arg 279 Ter", "p.Arg 279 Ter", ("R-F8 spaces",)),
    ("p.Arg 279Ter", "p.Arg 279Ter", ()),
]


@pytest.mark.parametrize("before, after, rules", C_CASES)
def test_fix_c(before: str, after: str, rules: tuple[str, ...]) -> None:
    assert fix_c(before) == (after, rules)


@pytest.mark.parametrize("before, after, rules", P_CASES)
def test_fix_p(before: str, after: str, rules: tuple[str, ...]) -> None:
    assert fix_p(before) == (after, rules)


def test_no_rule_changes_a_digit_a_base_or_a_residue() -> None:
    """Digits compare as the exact sequence; letters as a multiset, case-insensitively
    (`ter` becomes `Ter`), once the words "greater than" are removed from `before`: R-F8
    greater than is the one rule that deletes letters.
    The legacy rule moves a letter, so a multiset is the right test."""
    for before, after, _ in [*C_CASES, *P_CASES]:
        stripped = before.replace("greater than", "")
        digits = lambda s: [ch for ch in s if ch.isdigit()]  # noqa: E731
        letters = lambda s: Counter(ch.lower() for ch in s if ch.isalpha())  # noqa: E731
        assert digits(before) == digits(after), before
        assert letters(stripped) == letters(after), before


def _finding(
    design: str, zygosity: str, c: str = "c.G496T", p: str = "p. K342fs"
) -> dict[str, Any]:
    return {
        "id": "CHDA:LIT:0000001",
        "design": design,
        "variants": [
            {
                "id": 1,
                "as_reported": "c.G496T  p. K342fs ∗",
                "reported": {"c": c, "p": p},
                "zygosity": zygosity,
            },
            {"id": 2, "as_reported": "x", "reported": {"c": None, "p": "p.Trp1∗"}},
        ],
    }


def test_apply_rules_sets_cva_zygosity_and_never_touches_as_reported() -> None:
    original = _finding("common_variant_association", "heterozygous")
    snapshot = copy.deepcopy(original)
    fixed, changes = apply_rules(original)
    assert original == snapshot  # the input is not mutated
    assert fixed is not original
    assert [v["as_reported"] for v in fixed["variants"]] == [
        v["as_reported"] for v in original["variants"]
    ]
    assert fixed["variants"][0]["reported"] == {"c": "c.496G>T", "p": "p.K342fs"}
    assert fixed["variants"][0]["zygosity"] == "not_stated"
    assert changes == (
        NotationChange(
            "CHDA:LIT:0000001", 1, "reported.c", "c.G496T", "c.496G>T", ("R-F8 legacy",)
        ),
        NotationChange(
            "CHDA:LIT:0000001", 1, "reported.p", "p. K342fs", "p.K342fs", ("R-F8 spaces",)
        ),
        NotationChange(
            "CHDA:LIT:0000001", 1, "zygosity", "heterozygous", "not_stated", ("R-F7 zygosity",)
        ),
        NotationChange("CHDA:LIT:0000001", 2, "reported.p", "p.Trp1∗", "p.Trp1*", ("R-F4 star",)),
    )


def test_two_rules_on_one_field_are_one_change_listing_both_in_order() -> None:
    _, changes = apply_rules(_finding("cohort_screen", "heterozygous", c="c.∗12 A > G", p="p.X"))
    assert changes[0] == NotationChange(
        "CHDA:LIT:0000001", 1, "reported.c", "c.∗12 A > G", "c.*12A>G", ("R-F4 star", "R-F8 spaces")
    )
    assert [c.field for c in changes if c.variant == 1] == ["reported.c"]


def test_zygosity_is_left_alone_outside_a_common_variant_association() -> None:
    fixed, changes = apply_rules(_finding("cohort_screen", "heterozygous"))
    assert fixed["variants"][0]["zygosity"] == "heterozygous"
    assert all("R-F7 zygosity" not in change.rules for change in changes)


def test_a_non_string_name_is_refused() -> None:
    bad = _finding("cohort_screen", "heterozygous", c=12)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="CHDA:LIT:0000001 variant 1: reported.c"):
        apply_rules(bad)
