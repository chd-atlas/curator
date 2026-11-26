"""Finding rule text restated outside the rules file, shared by the role and skill tests.

R-SWA-4: a skill, or a role file's ``## Task``, restates a rule when any sentence of eight
or more words, whitespace-normalised, also occurs in ``curator-rules.md``. Sentences end at
``. ! ? : ;``, at a table cell border or at a list marker; fenced blocks and front matter
are not prose and are left out.
"""

from __future__ import annotations

import re

MIN_WORDS = 8


def normalised(text: str) -> str:
    return " ".join(text.split())


def sentences(text: str) -> list[str]:
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"^[ ]*```.*?^[ ]*```", "", text, flags=re.S | re.M)
    pieces = re.split(r"(?<=[.!?:;])\s+|\s+\|\s+|\s+(?:- |\d+\. )(?=\S)", normalised(text))
    stripped = (piece.strip(" -|") for piece in pieces)
    return [piece for piece in stripped if len(piece.split()) >= MIN_WORDS]


def restated(text: str, rules: str) -> list[str]:
    """The sentences of ``text`` that also occur in ``rules``."""
    flat = normalised(rules)
    return [sentence for sentence in sentences(text) if sentence in flat]
