"""Comparing a blind second extraction against the first. Pure: no I/O.

Copied from the atlas's ``gene_literature_diff.py`` at b4c8191 (``EXCLUDED_FIELDS``,
``FindingDiff``, ``_key``, ``_values``, ``diff_findings``, the ambiguous-key rule R11)
and reworked to run on plain dicts and the atlas's exported JSON Schema instead of the
``Finding`` model, which curator never imports.

Two findings are the same study when they share a key, ``(publication, design, sorted
cohorts)``. A key carried by more than one finding *within either pass* is ambiguous
and is never paired (ruling R11): pairing it would let ``--mark-verified`` verify a
finding against an arbitrarily chosen twin, so every finding carrying it is reported as
present in one pass only and a curator reads it.

Compared: every ``Finding`` property except ``EXCLUDED_FIELDS``. The set is read from
the schema's ``$defs.Finding.properties``, so a field added later is compared by
default instead of being skipped without anyone noticing. Summaries and locators are
free wording and the curator reads them; the identifier, the extraction and approval
bookkeeping say who did what, not what the paper says. ``shares_probands_with`` is
excluded too (ruling R26): it names finding ids, the blind pass uses ids local to its
own file, so a list from the first pass could never agree with anything.

A property a finding omits is read as the schema's ``default`` when it has one, and as
``[]`` when it is an array property with none (``cohorts``, ``variant_classes``: the
exported schema carries no default for the atlas's empty-list fields). Lists compared
as sets, because their order carries no meaning: ``methods``, ``variant_classes``,
``phenotypes``, ``lesion_groups`` and ``cohorts``. ``carriers`` is a mapping and is
compared by value. Reported values have the set-like lists sorted.

The atlas keeps its own copy of the compared set in ``tests/unit/test_gene_literature_diff.py``
and curator pins the same 17 in ``tests/test_diff.py``; the two change together (spec F18).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator.atlas import plain
from chd_curator.write import WriteRefused, refuse_anchors

EXCLUDED_FIELDS: Final = (
    "id",
    "summary",
    "extracted_by",
    "verified_by",
    "approved_by",
    "approved_on",
    "approved_digest",
    "review_status",
    "review_note",
    "locators",
    "shares_probands_with",
)
_SET_LIKE: Final = frozenset(
    {
        "methods",
        "variant_classes",
        "phenotypes",
        "lesion_groups",
        "cohorts",
    }
)

FieldDifference = tuple[str, Any, Any]
_Key = tuple[str, str, tuple[str, ...]]


class DiffError(Exception):
    """A findings file that cannot be compared (exit 1 at the CLI)."""


def _finding_schema(literature_schema: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        schema = literature_schema["$defs"]["Finding"]
        if not isinstance(schema["properties"], Mapping):
            raise TypeError
    except (KeyError, TypeError) as exc:
        raise DiffError("the atlas's gene_literature_file schema has no $defs.Finding") from exc
    return schema  # type: ignore[no-any-return]


def compared_fields(literature_schema: Mapping[str, Any]) -> tuple[str, ...]:
    """The sorted ``Finding`` property names minus ``EXCLUDED_FIELDS``."""
    properties = _finding_schema(literature_schema)["properties"]
    return tuple(sorted(name for name in properties if name not in EXCLUDED_FIELDS))


def parse_findings_file(raw: bytes, literature_schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The findings of a blind pass: a YAML file holding ``findings:`` and nothing else.

    Each finding must satisfy the atlas's ``Finding`` schema (formats checked) and no
    id may appear twice. Anything else raises `DiffError`.
    """
    try:
        refuse_anchors(raw)
        data = plain(YAML(typ="safe").load(raw))
    except (WriteRefused, YAMLError) as exc:
        raise DiffError(str(exc)) from exc
    wrapper = {
        "type": "object",
        "properties": {
            "findings": {"type": "array", "items": {"$ref": "#/$defs/Finding"}},
        },
        "required": ["findings"],
        "additionalProperties": False,
        "$defs": literature_schema["$defs"],
    }
    validator = Draft202012Validator(wrapper, format_checker=Draft202012Validator.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        lines = [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors
        ]
        raise DiffError(
            "the findings file does not satisfy the Finding schema:\n" + "\n".join(lines)
        )
    findings: list[dict[str, Any]] = data["findings"]
    counts = Counter(f["id"] for f in findings)
    duplicated = sorted(i for i, n in counts.items() if n > 1)
    if duplicated:
        raise DiffError(f"duplicate finding id(s) in the findings file: {', '.join(duplicated)}")
    return findings


@dataclass(frozen=True)
class FindingDiff:
    """The result of `diff_findings`; every sequence is sorted.

    ``only_first`` / ``only_second``: ids with no partner (including every finding
    of an ambiguous key). ``agreed``: first-pass ids paired with a finding that
    has no field difference. ``fields``: first-pass id -> its differences,
    sorted by field name, only for paired findings that differ.
    """

    only_first: tuple[str, ...]
    only_second: tuple[str, ...]
    agreed: tuple[str, ...]
    fields: dict[str, list[FieldDifference]]


def _filled(finding: Mapping[str, Any], name: str, prop: Mapping[str, Any]) -> Any:
    if name in finding:
        return finding[name]
    if "default" in prop:
        return prop["default"]
    return [] if prop.get("type") == "array" else None


def _values(
    finding: Mapping[str, Any], names: Sequence[str], properties: Mapping[str, Any]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in names:
        value = _filled(finding, name, properties[name])
        out[name] = sorted(value) if name in _SET_LIKE and isinstance(value, list) else value
    return out


def diff_findings(
    first: Sequence[Mapping[str, Any]],
    second: Sequence[Mapping[str, Any]],
    literature_schema: Mapping[str, Any],
) -> FindingDiff:
    names = compared_fields(literature_schema)
    properties = _finding_schema(literature_schema)["properties"]

    def key(finding: Mapping[str, Any]) -> _Key:
        cohorts = _filled(finding, "cohorts", properties["cohorts"])
        return (str(finding["publication"]), str(finding["design"]), tuple(sorted(cohorts)))

    first_counts = Counter(key(f) for f in first)
    second_counts = Counter(key(f) for f in second)
    pairable = {k for k, n in first_counts.items() if n == 1 and second_counts.get(k) == 1}
    partner = {key(f): f for f in second if key(f) in pairable}

    only_first: list[str] = []
    agreed: list[str] = []
    fields: dict[str, list[FieldDifference]] = {}
    for finding in sorted(first, key=lambda f: str(f["id"])):
        k = key(finding)
        if k not in pairable:
            only_first.append(str(finding["id"]))
            continue
        mine = _values(finding, names, properties)
        theirs = _values(partner[k], names, properties)
        differences = [(n, mine[n], theirs[n]) for n in names if mine[n] != theirs[n]]
        if differences:
            fields[str(finding["id"])] = differences
        else:
            agreed.append(str(finding["id"]))
    only_second = sorted(str(f["id"]) for f in second if key(f) not in pairable)
    return FindingDiff(tuple(only_first), tuple(only_second), tuple(agreed), fields)
