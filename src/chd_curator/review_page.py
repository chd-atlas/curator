"""The review page as a file: the template with a run's data inlined.

``templates/review.html`` is one self-contained page (inline CSS and JavaScript; the only
network it may touch is the font stylesheet). :func:`render` puts the page's data in its
``<script type="application/json" id="data">`` element. The JSON is made safe for an HTML
script element: ``</`` becomes ``<\\/`` and ``<!--`` becomes ``\\u003c!--``, both of which a
JSON parser reads back as the original characters.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

PLACEHOLDER = "__DATA__"
TITLE_PLACEHOLDER = "__TITLE__"


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
    if template.count(TITLE_PLACEHOLDER) != 1:
        raise ValueError(f"the template must hold {TITLE_PLACEHOLDER} exactly once")
    # the title names the gene (a claude.ai artifact refuses a bare category label as a page name);
    # it is filled before the data goes in, so a data value can never be mistaken for the marker
    title = html.escape(f"{data['page']['symbol']} literature review")
    return template.replace(TITLE_PLACEHOLDER, title).replace(PLACEHOLDER, inline_json(data))
