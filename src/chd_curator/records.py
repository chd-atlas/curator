"""Reading and extending the curator-edited literature files without losing a value
or a comment of what the curator wrote.

Copied from the atlas's ``chd_atlas/gene_literature_io.py`` at b4c8191 and changed
only where the atlas's pydantic models stood: records here are plain mappings, and
validating against the atlas's exported JSON Schema is ``write.py``'s job, done once
on the bytes that are about to be written.

Two files, two strategies, chosen by what each needs to preserve:

- ``curation/literature/<SYMBOL>.yaml`` is rewritten through ruamel's
  round-trip loader with ONE canonical indent (mappings 2, sequences 4, dashes
  at offset 2; no indent detection). Values and comments are kept. A file
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
from collections.abc import Mapping, Sequence
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


class RecordError(Exception):
    """A literature or publications file cannot be safely extended."""


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
