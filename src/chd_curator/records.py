"""Reading and extending the curator-edited literature files without losing a value
or a comment of what the curator wrote.

Copied from the atlas's ``chd_atlas/gene_literature_io.py`` at b4c8191 and changed
only where the atlas's pydantic models stood: records here are plain mappings, and
validating against the atlas's exported JSON Schema is ``write.py``'s job, done once
on the bytes that are about to be written.

Two files, two strategies, chosen by what each needs to preserve:

- ``curation/literature/<SYMBOL>.yaml`` is rewritten through ruamel's
  round-trip loader with ONE canonical indent (mappings 2, sequences 4, dashes
  at offset 2; no indent detection). Values and comments are kept; a redrafted
  synthesis replaces the old block's values, and a comment that followed the old block is
  carried to the new one. A file
  already in that style changes only in the lines a command adds (measured
  2026-11-03 in the atlas: marking verified on such a file changed one line, the
  added ``verified_by``). A wrapped plain scalar is reflowed onto one line, and a
  file in another indent style is re-indented, both once, on its first write.
  New entries are appended; nothing is reordered.
- ``curation/publications.yaml`` is *appended to as text*: its comments sit
  between records, folded titles are wrapped by hand, and a round trip would
  re-flow them. Every existing byte stays; one record is added at the end.

``with_verified`` sets ``verified_by`` on named findings under the same rule: on a
file in the canonical style the line it adds is the only change, and a blank line
or comment that followed the entry (even after a nested list or mapping) moves to
the new last line with it.

None of these writes anything. The ``with_*`` functions return the file's new
content as **bytes** (``with_search`` also the search id and the count of new hits);
the caller validates and only then writes (``write.write_literature``), so a failure
anywhere leaves the file as it was.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from chd_curator.run import TriageChange
from chd_curator.write import APPROVAL_FIELDS


class RecordError(Exception):
    """A literature or publications file cannot be safely extended."""


class ApprovedSynthesis(RecordError):
    """The file's synthesis carries an approval, which only a person may remove."""


def literature_path(root: Path, symbol: str) -> Path:
    return root / "curation" / "literature" / f"{symbol}.yaml"


def _round_trip_yaml() -> YAML:
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    # Wide enough that a long recorded query is never folded, and an existing
    # line is never re-wrapped on the way through.
    yaml.width = 4096
    return yaml


def dump_bytes(data: Any) -> bytes:
    buffer = BytesIO()
    _round_trip_yaml().dump(data, buffer)
    return buffer.getvalue()


def load_round_trip(raw: bytes) -> CommentedMap:
    try:
        data = _round_trip_yaml().load(raw)
    except Exception as exc:  # ruamel raises several YAMLError subclasses
        raise RecordError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, CommentedMap):
        raise RecordError("the top level is not a mapping")
    return data


def _trailing_slot(node: Any, key: Any) -> tuple[Any, Any, int] | None:
    """`(container, key, slot)` of the deepest comment trailing `node[key]`.

    ruamel hangs whatever follows a block (a `# note`, or a blank line before
    the next list item) on the last element inside it. At each level this
    checks the element's own comment slot -- slot 2 for a mapping key, slot 0
    for a sequence index -- and then walks into the last value if it is a
    non-empty collection. The deepest slot holding a comment wins, so a
    same-line comment on a collection's own key (`carriers:  # why`) does not
    shadow the blank line below it. Returns None when no level holds one.
    """
    found: tuple[Any, Any, int] | None = None
    while True:
        slot = 2 if isinstance(node, CommentedMap) else 0
        row = node.ca.items.get(key)
        if row and row[slot] is not None:
            found = (node, key, slot)
        child = node[key]
        if not isinstance(child, CommentedMap | CommentedSeq) or not len(child):
            return found
        node = child
        key = list(child)[-1] if isinstance(child, CommentedMap) else len(child) - 1


def _carry_trailing_comment(entry: CommentedMap, new_last: str) -> None:
    """Move what followed an entry's last element to its new last key.

    Appending a key after the old last one would otherwise leave a blank line
    or a note that separated two findings stranded between two fields of the
    same finding. A same-line comment (`key: v  # why`) stays where it was.
    Call it after the new key is inserted at the end; `new_last` is that key.
    """
    found = _trailing_slot(entry, list(entry)[-2])
    if found is None:
        return
    container, key, slot = found
    row = container.ca.items[key]
    token = row[slot]
    first, newline, rest = token.value.partition("\n")
    moved = copy.copy(token)
    if first.strip():  # a same-line comment stays; only what follows moves
        if not rest:
            return
        moved.value = "\n" + rest
        token.value = first + newline
    else:
        row[slot] = None
        if all(item is None for item in row):
            container.ca.items.pop(key)
    entry.ca.items[new_last] = [None, None, moved, None]


def _trailing_end(node: CommentedMap, key: str) -> Any:
    """A copy of the comment held as the end marker of the last list under `node[key]`.

    ruamel parks what follows a block on a list's `ca.end` rather than on a key when
    the list's last item ends in a flow-style value (`cites: [..]`) and the list is the
    block's last key. None when no list on the last path holds one.
    """
    child: Any = node[key]
    while isinstance(child, CommentedMap | CommentedSeq) and len(child):
        end = child.ca.end if isinstance(child, CommentedSeq) else None
        if end:
            moved = copy.copy(end[0])
            moved.value = "\n" + "".join(t.value for t in end)
            return moved
        child = child[list(child)[-1] if isinstance(child, CommentedMap) else len(child) - 1]
    return None


def _trailing_comment(node: CommentedMap, key: str) -> Any:
    """A copy of the comment that follows `node[key]`'s block, minus a same-line comment.

    None when nothing follows. A comment on the block's last line (`drafted_by: x  # why`)
    belongs to the old value and stays with it; only what comes after it is returned.
    """
    found = _trailing_slot(node, key)
    if found is None:
        return _trailing_end(node, key)
    container, inner, slot = found
    token = container.ca.items[inner][slot]
    first, _, rest = token.value.partition("\n")
    moved = copy.copy(token)
    if first.strip():
        if not rest:
            return None
        moved.value = "\n" + rest
    return moved


def _drop_trailing_after_scalar(node: CommentedMap, key: str) -> None:
    """Remove what follows a scalar `node[key]` (`synthesis: null`), keeping a same-line comment.

    The note hangs on the key itself, so replacing the value would leave it there as well
    as wherever it was carried to.
    """
    row = node.ca.items.get(key)
    if not row or row[2] is None:
        return
    first, newline, _ = row[2].value.partition("\n")
    if first.strip():
        row[2].value = first + newline
    else:
        row[2] = None
        if all(item is None for item in row):
            node.ca.items.pop(key)


def with_verified(existing: bytes, ids: Sequence[str], verifier: str) -> bytes:
    """The file's new content with ``verified_by: <verifier>`` on the named findings.

    A ``verified_by`` already in the entry keeps its place and gets the new value;
    otherwise the key goes directly after ``extracted_by``, where the atlas's
    ``Finding`` model puts it (the exported JSON Schema sorts its keys, so model
    order cannot be read from it). An unknown id, or a finding with no
    ``extracted_by`` to place it after, is refused. Everything else is
    round-tripped as `load_round_trip` keeps it. Not validated here: the caller's
    write is (``write.write_literature``).
    """
    data = load_round_trip(existing)
    entries = {str(f.get("id")): f for f in data.get("findings") or []}
    missing = sorted(set(ids) - set(entries))
    if missing:
        raise RecordError(f"no such finding: {', '.join(missing)}")
    for finding_id in ids:
        entry = entries[finding_id]
        if "verified_by" in entry:
            entry["verified_by"] = verifier
            continue
        if "extracted_by" not in entry:
            raise RecordError(f"{finding_id} has no extracted_by to place verified_by after")
        position = list(entry).index("extracted_by") + 1
        at_end = position == len(entry)
        entry.insert(position, "verified_by", verifier)
        if at_end:
            _carry_trailing_comment(entry, "verified_by")
    return dump_bytes(data)


def with_synthesis(existing: bytes, draft: Mapping[str, Any]) -> bytes:
    """The file's new content with its ``synthesis`` set to an unapproved draft.

    `draft` carries ``sentences`` (each ``text`` and ``cites``) and ``drafted_by``; the
    block is written in that order, the model's field order. A synthesis already in the
    file is replaced where it stands (a redraft); otherwise the block is appended last,
    where the atlas's ``GeneLiteratureFile`` has the field. A current block holding a
    non-null approval field is refused (`ApprovedSynthesis`, naming who approved and when):
    an agent never removes an approval. A comment that followed the old block moves to the
    new one's last key. Not validated here: the caller's write is.
    """
    data = load_round_trip(existing)
    current = data.get("synthesis")
    if isinstance(current, Mapping) and any(
        current.get(name) is not None for name in APPROVAL_FIELDS
    ):
        who, when = (current.get(name) for name in ("approved_by", "approved_on"))
        raise ApprovedSynthesis(
            f"the synthesis is approved by {'(not set)' if who is None else who} on "
            f"{'(not set)' if when is None else when}; an agent never removes an approval: "
            "stop and ask that person"
        )
    trailing = _trailing_comment(data, "synthesis") if "synthesis" in data else None
    if trailing is not None and not isinstance(current, CommentedMap | CommentedSeq):
        _drop_trailing_after_scalar(data, "synthesis")
    try:
        block = CommentedMap()
        block["sentences"] = [
            {"text": sentence["text"], "cites": list(sentence["cites"])}
            for sentence in draft["sentences"]
        ]
        block["drafted_by"] = draft["drafted_by"]
    except (KeyError, TypeError) as exc:
        raise RecordError("the draft must hold sentences, each with text and cites") from exc
    if trailing is not None:
        block.ca.items["drafted_by"] = [None, None, trailing, None]
    data["synthesis"] = block
    return dump_bytes(data)


def pmid_key(pmid: str) -> int:
    """Numeric order of a `PMID:123` string (string order puts 9 after 10)."""
    return int(pmid.removeprefix("PMID:"))


def with_search(
    existing: bytes | None,
    *,
    gene: str,
    query: str,
    run_on: date,
    hits: int,
    pmids: Sequence[str],
) -> tuple[bytes, int, int]:
    """The file's new content after recording one search: `(bytes, search_id, new_hits)`.

    `pmids` are the bare PMIDs esearch returned. Only those not already in the
    file's triage become entries, `pending` and attributed to the new search,
    appended in numeric PMID order. The search id is the largest existing id plus
    one. Not validated here: the caller's write is.
    """
    if existing is None:
        data: CommentedMap = CommentedMap()
        data["gene"] = gene
        data["searches"] = CommentedSeq()
        data["triage"] = CommentedSeq()
    else:
        data = load_round_trip(existing)
        if data.get("gene") != gene:
            raise RecordError(f"the file's gene is {data.get('gene')!r}, not {gene}")
    searches = data.setdefault("searches", CommentedSeq())
    triage = data.setdefault("triage", CommentedSeq())
    for seq in (searches, triage):
        seq.fa.set_block_style()

    search_id = max((int(s["id"]) for s in searches), default=0) + 1
    known = {str(t["pmid"]) for t in triage}
    fresh = sorted((f"PMID:{p}" for p in set(pmids)), key=pmid_key)
    fresh = [p for p in fresh if p not in known]

    searches.append(
        {
            "id": search_id,
            "source": "pubmed",
            "query": query,
            "run_on": run_on,
            "hits": hits,
            "new_hits": len(fresh),
        }
    )
    for pmid in fresh:
        triage.append({"pmid": pmid, "search": search_id, "decision": "pending"})

    return dump_bytes(data), search_id, len(fresh)


def with_publication(existing: bytes, record: Mapping[str, Any], reading: str) -> bytes:
    """`publications.yaml` with one record appended; every existing byte kept.

    `record` carries ``id``, ``title``, ``journal``, ``year``, ``authors`` and
    ``study_type`` (a string), and optionally ``doi`` and ``pmcid``. `reading` is
    why the study_type was read that way. `publications.yaml`'s head comment says
    the reading is recorded alongside the value, so it is written as comment lines
    directly above `study_type:` in the appended record (a blank reading is
    refused). The comment does not parse, so the record is unchanged.
    """
    lines_of_reading = [line.strip() for line in reading.splitlines() if line.strip()]
    if not lines_of_reading:
        raise RecordError("the reading of the study design must not be blank")
    try:
        before = YAML(typ="safe").load(existing)
        before_ids = [p["id"] for p in before["publications"]]
    except Exception as exc:
        raise RecordError(f"publications.yaml cannot be read: {exc}") from exc
    if record["id"] in before_ids:
        raise RecordError(f"{record['id']} is already in publications.yaml")

    appended: dict[str, Any] = {
        "id": record["id"],
        "title": record["title"],
        "journal": record["journal"],
        "year": record["year"],
        "authors": list(record["authors"]),
        "study_type": record["study_type"],
    }
    for field in ("doi", "pmcid"):
        value = record.get(field)
        if value is not None:
            appended[field] = value
    text = dump_bytes({"publications": [appended]}).decode("utf-8")
    snippet = text.split("\n", 1)[1]  # drop the "publications:" line, keep the item's indent
    commented: list[str] = []
    for line in snippet.split("\n"):
        if line.strip().startswith("study_type:"):
            indent = line[: len(line) - len(line.lstrip())]
            commented.extend(f"{indent}# {text_line}" for text_line in lines_of_reading)
        commented.append(line)
    snippet = "\n".join(commented)

    separator = b"" if existing.endswith(b"\n") else b"\n"
    content = existing + separator + snippet.encode("utf-8")

    try:
        after = YAML(typ="safe").load(content)["publications"]
    except Exception as exc:
        raise RecordError(f"the appended record does not parse: {exc}") from exc
    if len(after) != len(before_ids) + 1 or after[-1] != appended:
        raise RecordError("the appended record did not land as the last publication")
    return content


# The model's field order for a variant; a key a command adds goes after the nearest earlier
# key the entry already holds, so a file reads as the atlas would write it.
_VARIANT_ORDER = (
    "id",
    "as_reported",
    "locator",
    "kind",
    "placement",
    "reported",
    "hgvs_c",
    "hgvs_p",
    "grch38",
    "normalisation",
    "zygosity",
    "with",
    "phase",
    "carriers",
    "phenotypes",
    "review_status",
    "review_note",
    "review_settled_by",
    "unclear",
)
_NORMALISATION_ORDER = (
    "id",
    "tool",
    "version",
    "hgvs_version",
    "vvta",
    "vvdb",
    "seqrepo",
    "run_on",
)


@dataclass(frozen=True)
class VariantChange:
    """The fields ``normalise`` sets on one variant; None removes the key (null is the default)."""

    finding_id: str
    variant_id: int
    fields: Mapping[str, Any]


def _set_variant_key(entry: CommentedMap, key: str, value: Any) -> None:
    if value is None:
        if key in entry:
            del entry[key]
        return
    if key in entry:
        entry[key] = value
        return
    before = _VARIANT_ORDER[: _VARIANT_ORDER.index(key)]
    held = [list(entry).index(name) for name in before if name in entry]
    position = max(held) + 1 if held else len(entry)
    at_end = position == len(entry)
    entry.insert(position, key, value)
    if at_end and len(entry) > 1:
        _carry_trailing_comment(entry, key)


def with_normalised(
    existing: bytes, changes: Sequence[VariantChange], run: Mapping[str, Any]
) -> tuple[bytes, int]:
    """The file's new content after one ``normalise`` run: `(bytes, normalisation id)`.

    `run` carries ``version``, ``hgvs_version``, ``vvta``, ``vvdb``, ``seqrepo`` and ``run_on``
    (a date); the run is appended to ``normalisations`` (created after ``searches`` when
    absent) under the next free id, and every changed variant gets ``normalisation`` set to
    it. Each change's `fields` are written to its variant; nothing else in the file moves, and
    no approval field or ``verified_by`` is read or written. An unknown finding or variant is
    refused. Not validated here: the caller's write is.
    """
    data = load_round_trip(existing)
    findings = {str(f.get("id")): f for f in data.get("findings") or []}
    normalisations = data.get("normalisations")
    used = [int(n["id"]) for n in normalisations or []]
    run_id = max(used, default=0) + 1
    for change in changes:
        finding = findings.get(change.finding_id)
        if finding is None:
            raise RecordError(f"no such finding: {change.finding_id}")
        entry = next(
            (v for v in finding.get("variants") or [] if v.get("id") == change.variant_id), None
        )
        if entry is None:
            raise RecordError(f"{change.finding_id} has no variant {change.variant_id}")
        for key, value in change.fields.items():
            _set_variant_key(entry, key, value)
        _set_variant_key(entry, "normalisation", run_id)
    record = CommentedMap()
    values = {"id": run_id, "tool": "variantvalidator", **run}
    for key in _NORMALISATION_ORDER:
        record[key] = values[key]
    if normalisations is None:
        block = CommentedSeq()
        block.fa.set_block_style()
        block.append(record)
        position = list(data).index("searches") + 1 if "searches" in data else len(data)
        data.insert(position, "normalisations", block)
    else:
        normalisations.append(record)
    return dump_bytes(data), run_id


def _counter_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(
        r"(?P<head>[ \t]+"
        + re.escape(prefix)
        + r":[ \t]*)(?P<value>[0-9]+)(?P<tail>[ \t]*(?:#.*)?)"
    )


def is_empty(value: Any) -> bool:
    """Whether a finding's field holds nothing: absent (None), ``[]`` or ``{}``.

    The one definition ``batch`` and ``merge`` share for "a backfill may fill this field".
    """
    return value is None or value == [] or value == {}


def read_counter(raw: bytes, prefix: str) -> int:
    """The id counter ``prefixes.<prefix>`` of ``curation/.id_registry.yaml``."""
    try:
        data = YAML(typ="safe").load(raw)
        value = data["prefixes"][prefix]
    except Exception as exc:
        raise RecordError(f"the id registry holds no counter for {prefix}: {exc!r}") from exc
    if type(value) is not int or value < 0:
        raise RecordError(f"the id registry's {prefix} is not a non-negative integer: {value!r}")
    return value


def with_counter(raw: bytes, prefix: str, value: int) -> bytes:
    """The registry with ``<prefix>``'s counter set to `value`; that one line changes.

    A lower value than the current one is refused, and so is a registry whose counter is not
    written as one ``  <prefix>: <digits>`` line.
    """
    current = read_counter(raw, prefix)
    if value < current:
        raise RecordError(f"the {prefix} counter would fall from {current} to {value}")
    pattern = _counter_pattern(prefix)
    try:
        lines = raw.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise RecordError(f"the id registry is not UTF-8 text: {exc}") from None
    hits = [i for i, line in enumerate(lines) if pattern.fullmatch(line.rstrip("\r\n"))]
    if len(hits) != 1:
        raise RecordError(f"the {prefix} counter is not written as exactly one '{prefix}: n' line")
    line = lines[hits[0]]
    body = line.rstrip("\r\n")
    m = pattern.fullmatch(body)
    if m is None:
        raise RecordError(f"the {prefix} counter line cannot be read")
    lines[hits[0]] = f"{m['head']}{value}{m['tail']}{line[len(body) :]}"
    return "".join(lines).encode("utf-8")


# The model's field order for a finding (the exported schema sorts its keys, so the model's
# order cannot be read from it). A key the file does not know goes after these.
_FINDING_ORDER = (
    "id",
    "publication",
    "design",
    "methods",
    "comparator",
    "burden_study",
    "count_unit",
    "screened",
    "carriers",
    "variant_classes",
    "variants",
    "cases",
    "controls",
    "p_value",
    "odds_ratio",
    "phenotypes",
    "lesion_groups",
    "syndromic",
    "cohorts",
    "shares_probands_with",
    "locators",
    "summary",
    "extracted_by",
    "verified_by",
    "approved_by",
    "approved_on",
    "approved_digest",
    "review_status",
    "review_note",
    "review_settled_by",
)
_FLOW_LISTS = frozenset(
    {
        "methods",
        "variant_classes",
        "phenotypes",
        "lesion_groups",
        "cohorts",
        "shares_probands_with",
        "with",
    }
)
_FLOW_MAPS = frozenset({"carriers", "reported", "locator"})


def _flow_map(value: Mapping[str, Any]) -> CommentedMap:
    """A flow mapping; ``kind`` (a locator's) goes first, as the curated files write it."""
    out = CommentedMap(sorted(value.items(), key=lambda item: item[0] != "kind"))
    out.fa.set_flow_style()
    return out


def _flow_seq(value: Sequence[Any]) -> CommentedSeq:
    out = CommentedSeq(value)
    out.fa.set_flow_style()
    return out


def _ordered(value: Mapping[str, Any], order: Sequence[str]) -> list[str]:
    return [k for k in order if k in value] + sorted(k for k in value if k not in order)


def _styled_variant(variant: Mapping[str, Any]) -> CommentedMap:
    out = CommentedMap()
    for key in _ordered(variant, _VARIANT_ORDER):
        value = variant[key]
        if key in _FLOW_MAPS and isinstance(value, Mapping):
            out[key] = _flow_map(value)
        elif key in _FLOW_LISTS and isinstance(value, list):
            out[key] = _flow_seq(value)
        elif key == "unclear" and isinstance(value, Mapping):
            out[key] = CommentedMap(
                (f, _flow_map(e) if isinstance(e, Mapping) else e) for f, e in value.items()
            )
        else:
            out[key] = value
    return out


def _styled_value(key: str, value: Any) -> Any:
    """One finding field as the file writes it: flow lists and maps for the short ones."""
    if key == "variants" and isinstance(value, list):
        seq = CommentedSeq(_styled_variant(v) if isinstance(v, Mapping) else v for v in value)
        seq.fa.set_block_style()
        return seq
    if key == "locators" and isinstance(value, list):
        seq = CommentedSeq(_flow_map(v) if isinstance(v, Mapping) else v for v in value)
        seq.fa.set_block_style()
        return seq
    if key in _FLOW_MAPS and isinstance(value, Mapping):
        return _flow_map(value)
    if key in _FLOW_LISTS and isinstance(value, list):
        return _flow_seq(value)
    return value


def _styled_finding(finding: Mapping[str, Any]) -> CommentedMap:
    out = CommentedMap()
    for key in _ordered(finding, _FINDING_ORDER):
        out[key] = _styled_value(key, finding[key])
    return out


def with_findings_added(raw: bytes, findings: Sequence[Mapping[str, Any]]) -> bytes:
    """The file's new content with `findings` appended, in the file's own style.

    Each finding's keys follow the model's order; ``methods``, ``variant_classes``,
    ``phenotypes``, ``lesion_groups``, ``cohorts``, ``shares_probands_with`` (a list) and a
    variant's ``with`` are flow lists, ``carriers``, ``reported``, a variant's ``locator`` and
    each entry of ``locators`` flow maps. No value is dropped or defaulted. Nothing else in the
    file moves; not validated here (the caller's write is).
    """
    data = load_round_trip(raw)
    seq = data.get("findings")
    if seq is None:
        seq = CommentedSeq()
        seq.fa.set_block_style()
        data["findings"] = seq
    for finding in findings:
        seq.append(_styled_finding(finding))
    return dump_bytes(data)


def _delete_key(entry: CommentedMap, key: str) -> None:
    """Delete `key`; what followed it (a blank line, a note) moves to the new last key."""
    keys = list(entry)
    if keys[-1] != key or len(keys) == 1:
        del entry[key]
        return
    found = _trailing_slot(entry, key)
    moved: Any = None
    if found is not None:
        container, inner, slot = found
        token = container.ca.items[inner][slot]
        first, _, rest = token.value.partition("\n")
        if not first.strip() or rest:
            moved = copy.copy(token)
            moved.value = "\n" + rest if first.strip() else token.value
    del entry[key]
    if moved is None:
        return
    held = entry.ca.items.get(keys[-2])
    if held and held[2] is not None:
        held[2].value = held[2].value.rstrip("\n") + moved.value
    else:
        entry.ca.items[keys[-2]] = [None, None, moved, None]


def with_fields_set(
    raw: bytes, finding_id: str, fields: Mapping[str, Any], drop: Sequence[str] = ()
) -> bytes:
    """The file's new content with `fields` set on one finding and the `drop` keys removed.

    A key the finding holds keeps its place; a new one goes after the nearest key before it in
    the model's order, as ``with_normalised`` places a variant's. Values are styled as
    ``with_findings_added`` styles them. A blank line or note that followed the finding moves
    to its new last key. An unknown finding, or a key the model does not have, is refused.
    Nothing else changes; not validated here (the caller's write is).
    """
    data = load_round_trip(raw)
    entry = next((f for f in data.get("findings") or [] if str(f.get("id")) == finding_id), None)
    if entry is None:
        raise RecordError(f"no such finding: {finding_id}")
    for key in (*fields, *drop):
        if key not in _FINDING_ORDER:
            raise RecordError(f"{finding_id}: {key!r} is not a Finding field")
    for key, value in fields.items():
        styled = _styled_value(key, value)
        if key in entry:
            entry[key] = styled
            continue
        before = _FINDING_ORDER[: _FINDING_ORDER.index(key)]
        position = max((list(entry).index(k) + 1 for k in before if k in entry), default=0)
        at_end = position == len(entry)
        entry.insert(position, key, styled)
        if at_end and len(entry) > 1:
            _carry_trailing_comment(entry, key)
    for key in drop:
        if key in entry:
            _delete_key(entry, key)
    return dump_bytes(data)


def with_triage(raw: bytes, changes: Sequence[TriageChange]) -> bytes:
    """The file's new content with each triage entry's decision changed as `changes` say.

    An entry must hold the change's ``before`` decision (else refused, as is an unknown PMID).
    ``reason`` and ``note`` are written, in that order, directly after ``decision`` (replacing
    ones already there) when the change carries them; every other key keeps its place.
    """
    data = load_round_trip(raw)
    entries = {str(t.get("pmid")): t for t in data.get("triage") or []}
    for change in changes:
        entry = entries.get(change.pmid)
        if entry is None:
            raise RecordError(f"{change.pmid} is not in the triage")
        if entry.get("decision") != change.before:
            raise RecordError(
                f"{change.pmid}: its triage decision is {entry.get('decision')!r}, "
                f"not {change.before!r}"
            )
        entry["decision"] = change.after
        for key, value in (("reason", change.reason), ("note", change.note)):
            if value is None:
                continue
            if key in entry:
                entry[key] = value
                continue
            before = ("pmid", "search", "decision", "reason")[: 3 if key == "reason" else 4]
            position = max((list(entry).index(k) + 1 for k in before if k in entry), default=0)
            at_end = position == len(entry)
            entry.insert(position, key, value)
            if at_end and len(entry) > 1:
                _carry_trailing_comment(entry, key)
    return dump_bytes(data)
