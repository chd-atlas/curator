"""Whether two ``p.`` descriptions state the same change.

Reimplemented from the atlas's ``hgvs.py`` (``same_protein_change``) and
``validate/finding_variants.py`` (``_lit025``, ``_names_a_residue``) at 9594d10, plus the
same-residue reading (``_comparable``) of atlas commits 16e4703 and 2541735, without importing
``chd_atlas``. The comparison and the residue test are the atlas's. ``normalise`` is stricter
than LIT025 in three places, and reports the difference itself: it unplaces a ``hgvs_p`` on
another protein accession (the atlas: LIT016), an on-MANE ``hgvs_p`` the atlas cannot read,
such as ``p.(Met1ext-5)`` or ``p.(Gln70[3])`` (the atlas: LIT021), and it compares the paper's
``p.`` even where the paper named another transcript (the atlas skips LIT025 there). An
absent ``hgvs_p`` beside a paper residue is not stricter: LIT025 fires there too.

The rules, in the atlas's words: parts after ``p.`` (any accession dropped) are compared with
parentheses stripped, three-letter codes mapped to one-letter, and ``Ter``, ``*`` and ``X``
equal. A frameshift naming no new residue (``R279fs``, ``H445fsX136``) equals any frameshift
at the same residue and position (owner rulings D5, D5'). A ``p.`` naming no residue
(``p.?``, ``p.=``, ``p.0``) has nothing to compare on either side (D6, D6').
A substitution to its own residue (``V205V``, ``Val205Val``, ``*100*``) is read as ``Val205=``
in ``same_protein_change`` only; ``protein_key`` keeps it as written, as the atlas's does.
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
_P_DESCRIPTION: Final = re.compile(r"(?:[^:\s]+:)?p\.(\S+)")
_FRAMESHIFT: Final = re.compile(r"([A-Z*])(\d+)([A-Z*]?)fs(.*)")
_SYNONYMOUS: Final = re.compile(r"([A-Z*])(\d+)\1")


def _after_p(text: str) -> str:
    return text.split("p.", 1)[1] if "p." in text else text


def _protein_change(text: str) -> str:
    body = _after_p(text).replace("(", "").replace(")", "")
    body = re.sub(_THREE, lambda m: THREE_LETTER[m.group(0)], body)
    return body.replace("X", "*")


def protein_key(text: str) -> str:
    """The canonical reading of a ``p.`` description, for grouping (the atlas's ``protein_key``)."""
    return _protein_change(text)


def _comparable(text: str) -> str:
    """`_protein_change`, with a same-residue substitution (`V205V`, `*100*`) written `X#=`.

    The atlas's `_comparable`: used by `same_protein_change` only. `protein_key` keeps `V205V`
    as written, so grouping keys match the atlas's.
    """
    change = _protein_change(text)
    same = _SYNONYMOUS.fullmatch(change)
    return f"{same[1]}{same[2]}=" if same else change


def same_protein_change(a: str, b: str) -> bool:
    first, second = _comparable(a), _comparable(b)
    one, two = _FRAMESHIFT.fullmatch(first), _FRAMESHIFT.fullmatch(second)
    if one and two and (not one[3] or not two[3]):
        return one.group(1, 2) == two.group(1, 2)
    return first == second


def names_a_residue(text: str) -> bool:
    """False only for a ``p.`` description that names no residue, as the atlas's
    ``_names_a_residue`` is: ``p.?``, ``p.=``, ``p.0``, bracketed or not, with or without an
    accession. Anything that is not a ``p.`` description (a bare ``?``, say) is True, because the
    atlas's parser rejects it and the function reads a rejection as "names a residue".
    """
    match = _P_DESCRIPTION.fullmatch(text)
    if match is None:
        return True
    body = match[1]
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    return body not in _NO_RESIDUE


def three_letter(text: str) -> str:
    """The description after ``p.`` with every one-letter residue written in three letters.

    ``p.D111Y`` and ``p.(Asp111Tyr)`` both give ``Asp111Tyr``; ``*`` and ``X`` give ``Ter``.
    Lowercase edit words (``fs``, ``del``, ``ext``) are left as written. A frameshift's stop
    with no count (``D111fsX``) is dropped, giving ``Asp111fs``: the atlas reads ``fs`` and
    ``fsTer5`` and ``fsTer?`` but rejects a bare ``fsTer`` (LIT021). A stop-loss with no count
    (``*519Qext*``) becomes ``Ter519GlnextTer?``, VariantValidator's own form.
    """
    body = _after_p(text.strip())
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    three = re.sub(
        rf"({_THREE})|([A-Z*])",
        lambda m: m.group(1) or _ONE_TO_THREE.get(m.group(2), m.group(2)),
        body,
    )
    three = re.sub(r"fsTer(?![\d?])", "fs", three)
    return re.sub(r"extTer(?![\d?])", "extTer?", three)
