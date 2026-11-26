"""curator's one write path.

Every file curator writes goes through ``write_literature``, ``write_publications`` or
``write_registry``. Each checks first and writes only if every check passes, so a refused
write leaves the file's bytes as they were. These are refused:

- content the atlas's exported JSON Schema refuses;
- any change to an approval (a finding's or the synthesis's), a finding added (except the
  ids the caller names as added, which must arrive with no approval field and no
  ``verified_by``) or removed, or a finding id repeated, because an agent must never
  approve: approval is a named person's act, done in the atlas
  (``chd-atlas literature review --approve`` for a finding, ``--approve-synthesis`` for the
  synthesis), not here;
- a YAML anchor, alias or merge key, which the atlas refuses (YAML002) and which
  would otherwise copy an approval onto a finding nobody approved while still
  validating: the atlas found that with a merge key;
- a file whose bytes are no longer the ones the caller built
  its content from, so a stale read cannot overwrite an edit made meanwhile. The bytes are
  compared twice: on entry, and again after the checks (which parse the YAML three times and
  take seconds on a real file) just before the rename. A window remains between that last
  read and the rename, so an edit landing in those few instructions is not caught.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import AliasEvent, MappingStartEvent, ScalarEvent, SequenceStartEvent

from chd_curator.atlas import plain
from chd_curator.fs import write_bytes_atomically

APPROVAL_FIELDS: Final = ("approved_by", "approved_on", "approved_digest")


class WriteRefused(Exception):
    """A write was refused; nothing was written."""


def refuse_anchors(content: bytes) -> None:
    """Refuse any anchor, alias or ``<<`` merge key, from ruamel's event stream.

    An alias is its own event (``AliasEvent``) whose ``.anchor`` is the name it
    refers to, so it is checked first and refused as an alias. Measured with ruamel's
    safe parser: ``*a`` with no ``&a`` still parses, as an ``AliasEvent``. In a document
    that defines ``&a`` and then uses ``*a``, the stream reaches the anchor first, so
    that document is refused as an anchor.

    Read from events rather than the loaded data, because loading resolves all
    three: the file would look like ordinary mappings and validate.
    """
    # One entry per open collection: True for a mapping, with the count of
    # scalars/collections seen so far, so a mapping key is the even-numbered one.
    stack: list[list[Any]] = []
    try:
        for event in YAML(typ="safe").parse(content):
            if isinstance(event, AliasEvent):
                raise WriteRefused(f"a YAML alias (*{event.anchor}) is not allowed")
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


def schema_error_lines(data: Any, validator: Draft202012Validator) -> list[str]:
    """Every schema error as ``path: message``, in path order (list indexes numerically).

    The one ordering and format for every schema refusal curator prints. jsonschema
    yields the root error last, so the sort is what puts ``<root>`` first.
    """
    errors = sorted(validator.iter_errors(data), key=lambda e: [_step(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]


def _refuse_schema(path: Path, content: bytes, validator: Draft202012Validator) -> None:
    lines = schema_error_lines(plain(_safe_load(content, str(path))), validator)
    if lines:
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


def _synthesis_approval(content: bytes | None, what: str) -> tuple[Any, ...]:
    """The synthesis block's approval tuple; an absent block reads as all None."""
    data = plain(_safe_load(content, what)) if content is not None else None
    block = data.get("synthesis") if isinstance(data, dict) else None
    block = block if isinstance(block, dict) else {}
    return tuple(block.get(name) for name in APPROVAL_FIELDS)


def _unreviewed_additions(after: bytes, added: frozenset[str]) -> None:
    """Refuse an added finding that carries an approval field or ``verified_by``."""
    data = plain(_safe_load(after, "the new content"))
    findings = ((data or {}).get("findings") or []) if isinstance(data, dict) else []
    for finding in findings:
        if isinstance(finding, dict) and str(finding.get("id")) in added:
            for name in (*APPROVAL_FIELDS, "verified_by"):
                if finding.get(name) is not None:
                    raise WriteRefused(
                        f"{finding['id']}: a finding added here may not carry {name} "
                        f"({finding[name]!r})"
                    )


def refuse_approval_change(
    before: bytes | None, after: bytes, added: frozenset[str] = frozenset()
) -> None:
    """Refuse a write that adds, removes or alters any finding's approval.

    `added` names the ids that may be new: each must be absent from `before`, present in
    `after`, and carry no approval field and no ``verified_by``. Any other new id is refused.

    The synthesis block's approval is held to the same rule, as one more tuple of
    `APPROVAL_FIELDS`: an approved synthesis cannot be added, and no field of an existing
    one can change or go.

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
    for finding_id in sorted(set(new) - set(old) - added):
        raise WriteRefused(f"{finding_id}: adding a finding is not allowed here")
    for finding_id in sorted(added & set(old)):
        raise WriteRefused(f"{finding_id}: named as added but already in the file")
    for finding_id in sorted(added - set(new)):
        raise WriteRefused(f"{finding_id}: named as added but not in the new content")
    _unreviewed_additions(after, added)
    for finding_id in sorted(set(old) - set(new)):
        raise WriteRefused(f"{finding_id}: removing a finding is not allowed here")
    for finding_id in sorted(old):
        for name, was, now in zip(APPROVAL_FIELDS, old[finding_id], new[finding_id], strict=True):
            if was != now:
                raise WriteRefused(f"{finding_id}: {name} may not change ({was!r} -> {now!r})")
    synthesis_was = _synthesis_approval(before, "the current file")
    synthesis_now = _synthesis_approval(after, "the new content")
    for name, was, now in zip(APPROVAL_FIELDS, synthesis_was, synthesis_now, strict=True):
        if was != now:
            raise WriteRefused(f"synthesis: {name} may not change ({was!r} -> {now!r})")


def refused_after_writes(exc: WriteRefused, written: Sequence[Path]) -> WriteRefused:
    """`exc`, a refusal raised by a write that came after others in one command, worded for that
    case: its own "; nothing was written" is true of its own file only, so the files the command
    had already written are named. With none written, `exc` itself."""
    if not written:
        return exc
    names = ", ".join(str(path) for path in written)
    return WriteRefused(
        f"{str(exc).removesuffix('; nothing was written')}; this command had already written "
        f"{names}, which {'stays' if len(written) == 1 else 'stay'} as written"
    )


def _refuse_constructs(path: Path, content: bytes) -> None:
    try:
        refuse_anchors(content)
    except WriteRefused as exc:
        raise WriteRefused(f"{path}: {exc}") from exc


def _refuse_if_changed(path: Path, expected: bytes | None) -> None:
    """Raise `WriteRefused` unless the file holds exactly `expected` (None: no file) now."""
    current = path.read_bytes() if path.exists() else None
    if current != expected:
        raise WriteRefused(f"{path}: the file changed since it was read; nothing was written")


def check_literature(
    path: Path,
    content: bytes,
    validator: Draft202012Validator,
    *,
    expected: bytes | None,
    added: frozenset[str] = frozenset(),
) -> None:
    """Every refusal `write_literature` makes before it writes, without writing.

    A command that writes several files checks all of them first, so a refusal in the last
    one cannot leave the earlier ones written.
    """
    _refuse_if_changed(path, expected)
    _refuse_constructs(path, content)
    _refuse_schema(path, content, validator)
    refuse_approval_change(expected, content, added)


def write_literature(
    path: Path,
    content: bytes,
    validator: Draft202012Validator,
    *,
    expected: bytes | None,
    added: frozenset[str] = frozenset(),
) -> None:
    """Write a gene's literature file, or raise `WriteRefused` and leave it as it was.

    `expected` is the file's bytes as the caller read them and built `content` from
    (None: the file did not exist). If the file now holds anything else, the write is
    refused, so a read made before a slow PubMed call cannot overwrite an edit made
    during it. `added` names the finding ids the write may add (see `refuse_approval_change`).
    """
    check_literature(path, content, validator, expected=expected, added=added)
    _refuse_if_changed(path, expected)
    write_bytes_atomically(path, content)


def check_publications(
    path: Path, content: bytes, validator: Draft202012Validator, *, expected: bytes | None
) -> None:
    """Every refusal `write_publications` makes before it writes, without writing."""
    _refuse_if_changed(path, expected)
    _refuse_constructs(path, content)
    _refuse_schema(path, content, validator)


def write_publications(
    path: Path, content: bytes, validator: Draft202012Validator, *, expected: bytes | None
) -> None:
    """Write ``publications.yaml``, or raise `WriteRefused` and leave it as it was.

    `expected` is the file's bytes as the caller read them and built `content` from
    (None: the file did not exist), with the same meaning as for `write_literature`.
    """
    check_publications(path, content, validator, expected=expected)
    _refuse_if_changed(path, expected)
    write_bytes_atomically(path, content)


def _counters(content: bytes, what: str) -> dict[str, int]:
    data = plain(_safe_load(content, what))
    prefixes = data.get("prefixes") if isinstance(data, dict) else None
    if not isinstance(data, dict) or set(data) != {"prefixes"} or not isinstance(prefixes, dict):
        raise WriteRefused(f"{what} is not a mapping holding only 'prefixes'")
    for name, value in prefixes.items():
        if type(value) is not int or value < 0:
            raise WriteRefused(f"{what}: prefix {name!r} is not a non-negative integer: {value!r}")
    return {str(name): value for name, value in prefixes.items()}


def check_registry(path: Path, content: bytes, *, expected: bytes) -> None:
    """Every refusal `write_registry` makes before it writes, without writing."""
    _refuse_if_changed(path, expected)
    _refuse_constructs(path, content)
    before = _counters(expected, "the current registry")
    after = _counters(content, "the new registry")
    for name in sorted(before):
        if name not in after:
            raise WriteRefused(f"{path}: prefix {name} would vanish")
        if after[name] < before[name]:
            raise WriteRefused(
                f"{path}: prefix {name} would fall from {before[name]} to {after[name]}"
            )


def write_registry(path: Path, content: bytes, *, expected: bytes) -> None:
    """Write ``curation/.id_registry.yaml``, or raise `WriteRefused` and leave it as it was.

    The file must hold exactly ``prefixes:`` mapping each name to a non-negative integer, with
    no anchor, alias or merge key; no prefix may vanish or decrease; and the file must still
    hold `expected`, the bytes the caller built `content` from (checked again just before the
    rename). A registry that did not exist is not created here.
    """
    check_registry(path, content, expected=expected)
    _refuse_if_changed(path, expected)
    write_bytes_atomically(path, content)
