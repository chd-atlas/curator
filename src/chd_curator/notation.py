"""Deterministic notation fixes for a variant's ``reported`` names, and one zygosity ruling.

The seven rules named below are the C0 ledger's rulings R-F4, R-F7 and R-F8. R-F8 spaces is
wider than a ruling's own words: it also removes the space after ``c.`` or ``p.`` and the one
between a digit and a base before ``>``, steps that come from the pilot's ``assemble_new.py``
``fix()`` and the plan's fixtures, and the rules apply to ``p.`` names as well as ``c.`` ones.
A rule rewrites punctuation, spacing, a word or a letter's case, or (R-F8 legacy)
moves the reference base after the position. The digits stay in order, and the letters are
the same multiset (case-insensitive) once the words "greater than", which R-F8 replaces with
">", are left out; so no base or residue is added, dropped or changed.
``as_reported`` (what the paper printed) is never read or written.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

R_SPACES = "R-F8 spaces"
R_GREATER = "R-F8 greater than"
R_SLASH = "R-F8 allele slash"
R_LEGACY = "R-F8 legacy"
R_STAR = "R-F4 star"
R_TER = "R-F4 Ter"
R_ZYGOSITY = "R-F7 zygosity"

_STAR_U2217 = "∗"
_SPACE_STEPS = (
    (r"^([cp])\.\s+", r"\1."),
    (r"\s*>\s*", ">"),
    (r"\s*\+\s*", "+"),
    (r"(?<=\d)\s+(?=[ACGT]>)", ""),
)
_SLASH = re.compile(r"^(c\..*\d[ACGT])/([ACGT])$")
_LEGACY = re.compile(r"c\.([ACGT])(\d+)([ACGT])")
_TER = re.compile(r"(?<=\d)ter(?![a-z])|(?<=fs)ter(?![a-z])")


@dataclass(frozen=True)
class NotationChange:
    """One changed field: its whole value before and after, and the rules that changed it.

    There is one change per field, however many rules touched it; ``rules`` lists them in the
    order they were applied. The zygosity rule is one rule on one field.
    """

    finding: str
    variant: int | None
    field: str
    before: str
    after: str
    rules: tuple[str, ...]


def _star(name: str, rules: list[str]) -> str:
    if _STAR_U2217 in name:
        rules.append(R_STAR)
        return name.replace(_STAR_U2217, "*")
    return name


def _spaces(name: str, rules: list[str]) -> str:
    out = name
    for pattern, repl in _SPACE_STEPS:
        out = re.sub(pattern, repl, out)
    if out != name:
        rules.append(R_SPACES)
    return out


def fix_c(name: str) -> tuple[str, tuple[str, ...]]:
    """The name with the c. rules applied, and the rules that changed it, in order."""
    rules: list[str] = []
    out = _star(name, rules)
    greater = re.sub(r"\s*greater than\s*", ">", out)
    if greater != out:
        rules.append(R_GREATER)
        out = greater
    out = _spaces(out, rules)
    slashed = _SLASH.sub(r"\1>\2", out)
    if slashed != out:
        rules.append(R_SLASH)
        out = slashed
    legacy = _LEGACY.fullmatch(out)
    if legacy:
        rules.append(R_LEGACY)
        out = f"c.{legacy.group(2)}{legacy.group(1)}>{legacy.group(3)}"
    return out, tuple(rules)


def fix_p(name: str) -> tuple[str, tuple[str, ...]]:
    """The name with the p. rules applied, and the rules that changed it, in order."""
    rules: list[str] = []
    out = _star(name, rules)
    out = _spaces(out, rules)
    capital = _TER.sub("Ter", out)
    if capital != out:
        rules.append(R_TER)
        out = capital
    return out, tuple(rules)


def apply_rules(
    finding: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[NotationChange, ...]]:
    """A deep copy of ``finding`` with the rules applied to every variant, and the changes.

    The changes are in file order of the variants; within a variant ``c`` precedes ``p``,
    which precede zygosity. ``as_reported`` is not touched.
    """
    out = copy.deepcopy(dict(finding))
    fid = str(out.get("id"))
    changes: list[NotationChange] = []
    variants = out.get("variants") or []
    for variant in variants:
        vid = variant.get("id")
        number = vid if isinstance(vid, int) else None
        reported = variant.get("reported") or {}
        for key, fixer in (("c", fix_c), ("p", fix_p)):
            value = reported.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(f"finding {fid} variant {vid}: reported.{key} is not a string")
            fixed, rules = fixer(value)
            if rules:
                changes.append(NotationChange(fid, number, f"reported.{key}", value, fixed, rules))
            reported[key] = fixed
        zygosity = variant.get("zygosity")
        if (
            out.get("design") == "common_variant_association"
            and zygosity is not None
            and zygosity != "not_stated"
        ):
            changes.append(
                NotationChange(fid, number, "zygosity", str(zygosity), "not_stated", (R_ZYGOSITY,))
            )
            variant["zygosity"] = "not_stated"
    return out, tuple(changes)
