"""Roles: the input of one model step, assembled from a task file and the house rules it cites.

Every model step the pipeline runs is one structured call. Its input is built here, never
typed into a prompt by hand: the role's ``## Task`` says what the call does and what it
returns, the rule sections the role cites are copied byte for byte from the atlas's
``curator/curator-rules.md``, and the answer schema follows, so a backend that does not
enforce schemas still sees the contract. A role file states no rule of its own (CP section
10): the rules are the atlas's, and cited by heading, so that one copy is the one every
model reads.

A role file is ``workflow/roles/<role>.md``::

    ---
    cites:
      - <a heading of curator-rules.md, without its hashes>
    schema: <name of workflow/schemas/<name>.answer.json>
    backfill_schema: <the schema a backfill run's call uses; readers only>
    ---
    ## Task
    ...
    ## Inputs
    ... {{placeholder}} ...

A section of the rules runs from its heading line to the next heading of the same or a higher
level, so citing ``Findings`` takes the ``### Variants`` beneath it. Its text is the file's own
bytes from the heading line to its last non-blank line, that line's trailing spaces included.
A cite that matches no heading, or more than one, raises :class:`RoleError` naming the
cite, because a role that silently lost a rule would still answer.

``digest`` identifies the whole input (system, user and schema) and ``rules_digest`` the cited
rule text alone, so a run can say which rules a verdict was reached under.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

ROLES: Final = ("triage-screen", "triage-check", "extract", "blind-read", "judge")
WORKFLOW: Path = Path(__file__).resolve().parent / "workflow"
RULES_PATH: Final = Path("curator") / "curator-rules.md"

_HEADING: Final = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_FENCE: Final = re.compile(r"^[ ]*(```|~~~)")
_PLACEHOLDER: Final = re.compile(r"\{\{([a-z_]+)\}\}")
_FRONT: Final = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


class RoleError(Exception):
    """A role cannot be assembled: unknown, malformed, a cite or an input that does not fit."""


@dataclass(frozen=True)
class RoleInput:
    role: str
    system: str
    user: str
    schema: dict[str, Any]
    digest: str
    rules_digest: str


def canonical(obj: Any) -> str:
    """The one JSON form a digest and the prompt's schema block are taken over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """``(line index, level, text)`` of every heading outside a fenced block."""
    found: list[tuple[int, int, str]] = []
    fenced = False
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and (match := _HEADING.match(line)):
            found.append((index, len(match.group(1)), match.group(2)))
    return found


def section(rules: str, cite: str) -> str:
    """The text of the one section of ``rules`` whose heading reads ``cite``."""
    lines = rules.split("\n")
    headings = _headings(lines)
    matches = [i for i, (_, _, text) in enumerate(headings) if text == cite]
    if not matches:
        raise RoleError(f"the rules file has no heading {cite!r}")
    if len(matches) > 1:
        raise RoleError(f"the heading {cite!r} matches more than one section of the rules file")
    start, level, _ = headings[matches[0]]
    end = next((at for at, lvl, _ in headings[matches[0] + 1 :] if lvl <= level), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1  # trailing blank lines are not part of the section; a trailing space is
    return "\n".join(lines[start:end])


def _read_role(role: str, mode: str) -> tuple[list[str], str, str, str]:
    """``(cites, schema name, task text, input template)`` of a role file. `mode` is the run's:
    ``new`` (also a reread) uses ``schema``, ``backfill`` the role's ``backfill_schema``."""
    if mode not in ("new", "backfill"):
        raise RoleError(f"unknown mode {mode!r}; the modes are new and backfill")
    if role not in ROLES:
        raise RoleError(f"unknown role {role!r}; the roles are {', '.join(ROLES)}")
    path = WORKFLOW / "roles" / f"{role}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RoleError(f"{path}: cannot read the role file: {error}") from error
    match = _FRONT.match(text)
    if match is None:
        raise RoleError(f"{path}: no front matter")
    try:
        front = YAML(typ="safe").load(match.group(1))
    except YAMLError as error:
        raise RoleError(f"{path}: front matter is not YAML: {error}") from error
    cites = front.get("cites") if isinstance(front, dict) else None
    schema = front.get("schema") if isinstance(front, dict) else None
    if not (isinstance(cites, list) and cites and all(isinstance(c, str) for c in cites)):
        raise RoleError(f"{path}: front matter needs `cites`, a list of headings")
    if not isinstance(schema, str):
        raise RoleError(f"{path}: front matter needs `schema`, a schema file name")
    parts = re.split(r"^## (Task|Inputs)[ \t]*$", match.group(2), flags=re.M)
    sections = dict(zip(parts[1::2], parts[2::2], strict=True))
    if set(sections) != {"Task", "Inputs"} or len(parts) != 5:
        raise RoleError(f"{path}: needs exactly one `## Task` and one `## Inputs`")
    if mode == "backfill":
        schema = front.get("backfill_schema")
        if not isinstance(schema, str):
            raise RoleError(
                f"{path}: the role has no `backfill_schema`; it is not run in a backfill"
            )
    return cites, schema, sections["Task"].strip(), sections["Inputs"].strip()


def _read_schema(name: str) -> dict[str, Any]:
    path = WORKFLOW / "schemas" / f"{name}.answer.json"
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RoleError(f"{path}: cannot read the answer schema: {error}") from error
    return loaded


def _fill(template: str, inputs: Mapping[str, str], role: str) -> str:
    wanted = set(_PLACEHOLDER.findall(template))
    missing, extra = sorted(wanted - set(inputs)), sorted(set(inputs) - wanted)
    if missing or extra:
        raise RoleError(f"{role}: inputs missing {missing}, extra {extra}")
    # One pass with a function, so a value that looks like a placeholder is never expanded.
    return _PLACEHOLDER.sub(lambda m: inputs[m.group(1)], template)


def assemble(role: str, root: Path, inputs: Mapping[str, str], mode: str = "new") -> RoleInput:
    """The input of one call of ``role``, with the rules read from ``root`` (an atlas checkout).
    ``mode`` is the run's (``new`` or ``backfill``) and picks the answer schema."""
    cites, schema_name, task, template = _read_role(role, mode)
    schema = _read_schema(schema_name)
    path = root / RULES_PATH
    try:
        rules = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RoleError(f"cannot read the rules file {path}: {error}") from error
    try:
        sections = [section(rules, cite) for cite in cites]
    except RoleError as error:
        raise RoleError(f"{role}: {error}") from error
    user = _fill(template, inputs, role)
    blocks = [
        f"--- rules: {cite} ---\n{text}\n--- end rules: {cite} ---"
        for cite, text in zip(cites, sections, strict=True)
    ]
    wire = canonical(schema)
    system = "\n\n".join(
        [task, *blocks, f"--- answer schema ---\n{wire}\n--- end answer schema ---"]
    )
    return RoleInput(
        role=role,
        system=system,
        user=user,
        schema=schema,
        digest=_sha256(f"{system}\0{user}\0{wire}"),
        rules_digest=_sha256("".join(sections)),
    )
