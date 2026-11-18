"""Whether two ``p.`` descriptions state the same change.

Reimplemented from the atlas's ``hgvs.py`` (``same_protein_change``) and
``validate/finding_variants.py`` (``_lit025``, ``_names_a_residue``) at 3af62c5, without
importing ``chd_atlas``. ``normalise`` must call a variant "agreeing" exactly when the atlas's
LIT025 would, or a variant curator places would be refused by ``chd-atlas validate``.

The rules, in the atlas's words: parts after ``p.`` (any accession dropped) are compared with
parentheses stripped, three-letter codes mapped to one-letter, and ``Ter``, ``*`` and ``X``
equal. A frameshift naming no new residue (``R279fs``, ``H445fsX136``) equals any frameshift
at the same residue and position (owner rulings D5, D5'). A ``p.`` naming no residue
(``p.?``, ``p.=``, ``p.0``) has nothing to compare (D6).
"""

from __future__ import annotations

import re
from typing import Final

THREE_LETTER: Final[dict[str, str]] = {
    "Ala": "A",
    "Arg": "R",
    "Asn": "N",
    "Asp": "D",
    "Cys": "C",
    "Gln": "Q",
    "Glu": "E",
    "Gly": "G",
    "His": "H",
    "Ile": "I",
    "Leu": "L",
    "Lys": "K",
    "Met": "M",
    "Phe": "F",
    "Pro": "P",
    "Ser": "S",
    "Thr": "T",
    "Trp": "W",
    "Tyr": "Y",
    "Val": "V",
    "Sec": "U",
    "Ter": "*",
}
_ONE_TO_THREE: Final[dict[str, str]] = {one: three for three, one in THREE_LETTER.items()}
_ONE_TO_THREE["X"] = "Ter"
_THREE: Final = "|".join(THREE_LETTER)
_NO_RESIDUE: Final = frozenset({"?", "=", "0", "0?"})
# Reference residue, position, the new residue (empty when none is named), the rest.
_FRAMESHIFT: Final = re.compile(r"([A-Z*])(\d+)([A-Z*]?)fs(.*)")


def _after_p(text: str) -> str:
    return text.split("p.", 1)[1] if "p." in text else text


def _protein_change(text: str) -> str:
    body = _after_p(text).replace("(", "").replace(")", "")
    body = re.sub(_THREE, lambda m: THREE_LETTER[m.group(0)], body)
    return body.replace("X", "*")


def same_protein_change(a: str, b: str) -> bool:
    first, second = _protein_change(a), _protein_change(b)
    one, two = _FRAMESHIFT.fullmatch(first), _FRAMESHIFT.fullmatch(second)
    if one and two and (not one[3] or not two[3]):
        return one.group(1, 2) == two.group(1, 2)
    return first == second


def names_a_residue(text: str) -> bool:
    """False only for a ``p.`` naming no residue: ``p.?``, ``p.=``, ``p.0``, bracketed or not."""
    body = _after_p(text.strip())
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    return body not in _NO_RESIDUE


def three_letter(text: str) -> str:
    """The description after ``p.`` with every one-letter residue written in three letters.

    ``p.D111Y`` and ``p.(Asp111Tyr)`` both give ``Asp111Tyr``; ``*`` and ``X`` give ``Ter``.
    Lowercase edit words (``fs``, ``del``, ``ext``) are left as written.
    """
    body = _after_p(text.strip())
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    return re.sub(
        rf"({_THREE})|([A-Z*])",
        lambda m: m.group(1) or _ONE_TO_THREE.get(m.group(2), m.group(2)),
        body,
    )
