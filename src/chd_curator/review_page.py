"""The review page as a file: the template with a run's data inlined.

``templates/review.html`` is one self-contained page (inline CSS and JavaScript; the only
network it may touch is the font stylesheet). :func:`render` puts the page's data in its
``<script type="application/json" id="data">`` element. The JSON is made safe for an HTML
script element: ``</`` becomes ``<\\/`` and ``<!--`` becomes ``\\u003c!--``, both of which a
JSON parser reads back as the original characters.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

PLACEHOLDER = "__DATA__"


def slug(name: str) -> str:
    """Lowercase letters and digits of ``name``, runs of anything else as one hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def inline_json(data: Mapping[str, Any]) -> str:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return text.replace("<!--", "\\u003c!--").replace("</", "<\\/")


def render(data: Mapping[str, Any]) -> str:
    template = (files("chd_curator") / "templates/review.html").read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError(f"the template must hold {PLACEHOLDER} exactly once")
    return template.replace(PLACEHOLDER, inline_json(data))
