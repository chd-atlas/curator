"""The agree rule, ported from the atlas's ``tests/unit/test_hgvs.py`` at 9594d10.

The comparison and the residue test are the atlas's, so the parametrised cases below are the
atlas's own (D5, D5'), plus D6's no-residue forms. ``normalise`` is stricter than LIT025 only
where the atlas skips it for a ``hgvs_p`` it cannot use (see ``hgvs.py``).
"""

from __future__ import annotations

import pytest

from chd_curator.hgvs import names_a_residue, same_protein_change, three_letter


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("p.R279X", "p.(Arg279Ter)", True),
        ("p.R279X", "NP_852259.1:p.Arg279*", True),
        ("p.R279Q", "p.(Arg279Ter)", False),
        ("p.R279X", "p.(Arg280Ter)", False),
        # D5: a short frameshift equals any frameshift at its residue and position.
        ("p.R279fs", "p.(Arg279GlyfsTer5)", True),
        ("p.R279fs", "p.Arg279fsX", True),
        ("p.(Arg279GlyfsTer5)", "p.Arg279fsX", True),
        ("p.Arg279fsTer", "p.R279Gfs*5", True),
        ("p.R279fs", "p.(Arg280GlyfsTer5)", False),
        ("p.R279fs", "p.Q279fs", False),
        ("p.R279fs", "p.(Arg279Ter)", False),
        # D5': whatever its count; both pairs are TBX5 pilot findings.
        ("p.H445fsX136", "p.(His445MetfsTer137)", True),
        ("p.L435fsX146", "p.(Leu435ArgfsTer147)", True),
        ("p.H445fsX136", "p.(His446MetfsTer137)", False),
        ("p.H445fsX136", "p.(Leu445MetfsTer137)", False),
        # Two that both name the new residue still compare in full.
        ("p.R279Gfs*5", "p.Arg279GlyfsTer5", True),
        ("p.R279Gfs*5", "p.Arg279GlyfsTer9", False),
        ("p.H445Mfs*136", "p.(His445MetfsTer137)", False),
    ],
)
def test_same_protein_change_matches_the_atlas(a: str, b: str, same: bool) -> None:
    assert same_protein_change(a, b) is same
    assert same_protein_change(b, a) is same


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("?", True),
        ("=", True),
        ("p.?", False),
        ("p.(?)", False),
        ("p.=", False),
        ("NP_852259.1:p.(=)", False),
        ("p.0", False),
        ("p.0?", False),
        ("p.(0?)", False),
        ("p.R279X", True),
        ("p.(Arg279=)", True),
        ("p.Met1?", True),
    ],
)
def test_a_p_naming_no_residue_is_recognised_as_d6_does(text: str, names: bool) -> None:
    assert names_a_residue(text) is names


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("p.D111Y", "Asp111Tyr"),
        ("p.(Asp111Tyr)", "Asp111Tyr"),
        ("NP_852259.1:p.R279X", "Arg279Ter"),
        ("p.H445fsX136", "His445fsTer136"),
        ("p.R279Gfs*5", "Arg279GlyfsTer5"),
        ("p.Gln70dup", "Gln70dup"),
        ("p.D111fsX", "Asp111fs"),
        ("p.*519Qext*", "Ter519GlnextTer?"),
        ("p.*519Qext*?", "Ter519GlnextTer?"),
        ("p.*519Qext*7", "Ter519GlnextTer7"),
        ("p.D111Gfs*", "Asp111Glyfs"),
        ("p.D111fsX?", "Asp111fsTer?"),
    ],
)
def test_three_letter_form_for_the_paper_kept_hgvs_p(text: str, expected: str) -> None:
    assert three_letter(text) == expected
