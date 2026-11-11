"""curator's one write path.

Every file curator writes goes through ``write_literature`` or
``write_publications``. Each checks first and writes only if every check passes,
so a refused write leaves the file's bytes as they were. Three things are refused:

- content the atlas's exported JSON Schema refuses;
- any change to an approval, because an agent must never approve (spec section 4.5,
  F9): approval is a named human's act, done in the atlas, not here;
- a YAML anchor, alias or merge key, which the atlas refuses (YAML002) and which
  would otherwise copy an approval onto a finding nobody approved while still
  validating: the atlas found that with a merge key.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import MappingStartEvent, ScalarEvent, SequenceStartEvent

from chd_curator.atlas import plain
from chd_curator.fs import write_bytes_atomically

APPROVAL_FIELDS: Final = ("approved_by", "approved_on", "approved_digest")


class WriteRefused(Exception):
    """A write was refused; nothing was written."""


def refuse_anchors(content: bytes) -> None:
    """Refuse any anchor, alias or ``<<`` merge key, from ruamel's event stream.

    There is no separate alias branch: an alias must name an anchor defined earlier
    in the same document, and the stream reaches that anchor first, so refusing every
    anchor refuses every alias (an alias with no anchor is a parse error, also refused).

    Read from events rather than the loaded data, because loading resolves all
    three: the file would look like ordinary mappings and validate.
    """
    # One entry per open collection: True for a mapping, with the count of
    # scalars/collections seen so far, so a mapping key is the even-numbered one.
    stack: list[list[Any]] = []
    try:
        for event in YAML(typ="safe").parse(content):
            if getattr(event, "anchor", None) is not None:
                raise WriteRefused(f"a YAML anchor (&{event.anchor}) is not allowed")
            is_node = isinstance(event, ScalarEvent | MappingStartEvent | SequenceStartEvent)
            if is_node and stack and stack[-1][0] is True:
                is_key = stack[-1][1] % 2 == 0
                stack[-1][1] += 1
                if is_key and isinstance(event, ScalarEvent) and event.value == "<<":
                    raise WriteRefused("a YAML merge key (<<) is not allowed")
            elif is_node and stack:
                stack[-1][1] += 1
            if isinstance(event, MappingStartEvent):
                stack.append([True, 0])
            elif isinstance(event, SequenceStartEvent):
                stack.append([False, 0])
            elif type(event).__name__ in ("MappingEndEvent", "SequenceEndEvent"):
                stack.pop()
    except YAMLError as exc:
        raise WriteRefused(f"not valid YAML: {exc}") from exc


def _safe_load(content: bytes, what: str) -> Any:
    try:
        return YAML(typ="safe").load(content)
    except YAMLError as exc:
        raise WriteRefused(f"{what} is not valid YAML: {exc}") from exc


def _step(part: str | int) -> tuple[int, int, str]:
    """Sort key for one path step: list indexes numerically (10 after 2), then names."""
    return (0, part, "") if isinstance(part, int) else (1, 0, part)


def _refuse_schema(path: Path, content: bytes, validator: Draft202012Validator) -> None:
    data = plain(_safe_load(content, str(path)))
    errors = sorted(validator.iter_errors(data), key=lambda e: [_step(p) for p in e.absolute_path])
    if errors:
        lines = [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors
        ]
        raise WriteRefused(f"{path} does not satisfy its schema:\n" + "\n".join(lines))


def _approvals(content: bytes | None, what: str) -> list[tuple[str, tuple[Any, ...]]]:
    """`(finding id, approval tuple)` per finding, in file order, duplicates kept."""
    if content is None:
        return []
    data = plain(_safe_load(content, what))
    findings = ((data or {}).get("findings") or []) if isinstance(data, dict) else []
    return [
        (str(f.get("id")), tuple(f.get(name) for name in APPROVAL_FIELDS))
        for f in findings
        if isinstance(f, dict)
    ]


def refuse_approval_change(before: bytes | None, after: bytes) -> None:
    """Refuse a write that adds, removes or alters any finding's approval.

    Per finding id the approval is the tuple of `APPROVAL_FIELDS`, an absent key
    reading as None. The set of finding ids is compared too: a new finding is
    refused (it could arrive pre-approved), and so is a removed one (an approved
    finding would vanish), and so is an id that appears twice in the new content
    (a copy of an approved finding would otherwise collapse into its original).
    `before` None is a file that does not exist yet.
    """
    new_list = _approvals(after, "the new content")
    counts = Counter(finding_id for finding_id, _ in new_list)
    for finding_id in sorted(i for i, n in counts.items() if n > 1):
        raise WriteRefused(f"{finding_id}: more than one finding has this id")
    old = dict(_approvals(before, "the current file"))
    new = dict(new_list)
    for finding_id in sorted(set(new) - set(old)):
        raise WriteRefused(f"{finding_id}: adding a finding is not allowed here")
    for finding_id in sorted(set(old) - set(new)):
        raise WriteRefused(f"{finding_id}: removing a finding is not allowed here")
    for finding_id in sorted(old):
        for name, was, now in zip(APPROVAL_FIELDS, old[finding_id], new[finding_id], strict=True):
            if was != now:
                raise WriteRefused(f"{finding_id}: {name} may not change ({was!r} -> {now!r})")


def write_literature(path: Path, content: bytes, validator: Draft202012Validator) -> None:
    """Write a gene's literature file, or raise `WriteRefused` and leave it as it was."""
    before = path.read_bytes() if path.exists() else None
    refuse_anchors(content)
    _refuse_schema(path, content, validator)
    refuse_approval_change(before, content)
    write_bytes_atomically(path, content)


def write_publications(path: Path, content: bytes, validator: Draft202012Validator) -> None:
    """Write ``publications.yaml``, or raise `WriteRefused` and leave it as it was."""
    refuse_anchors(content)
    _refuse_schema(path, content, validator)
    write_bytes_atomically(path, content)
