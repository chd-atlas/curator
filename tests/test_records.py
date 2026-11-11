"""Record helpers, ported from the atlas's ``tests/unit/test_cli_literature.py`` at
b4c8191 and aimed at the functions directly instead of the atlas's commands.

Every output is also held to the atlas's real exported schema (never a copy), so a
helper that produced bytes the write path would refuse fails here too.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from chd_curator.atlas import plain, schema_validator
from chd_curator.records import (
    RecordError,
    literature_path,
    with_publication,
    with_search,
    with_verified,
)

PUBLICATIONS = """\
# Head comment: every record is transcribed from PubMed.
publications:
  - id: PMID:111
    title: First paper.
    journal: Journal One
    year: 1999
    authors:
      - Aa A
    # A comment sitting above a neighbouring field.
    study_type: case_report
  # A comment sitting between two records.
  - id: PMID:222
    title: Second paper.
    journal: Journal Two
    year: 2001
    authors:
      - Bb B
    study_type: gwas
"""

CANONICAL = (
    "# top comment\n"
    "gene: HGNC:11604\n"
    "searches:\n"
    "  - id: 1\n"
    "    source: pubmed\n"
    "    query: q\n"
    "    run_on: 2026-11-02\n"
    "    hits: 1\n"
    "    new_hits: 1\n"
    "triage:\n"
    "  # about the first paper\n"
    "  - pmid: PMID:9\n"
    "    search: 1\n"
    "    decision: included\n"
)


@pytest.fixture(scope="module")
def literature_schema() -> Draft202012Validator:
    return schema_validator(Path(os.environ["CHD_ATLAS_ROOT"]), "gene_literature_file")


@pytest.fixture(scope="module")
def publication_schema() -> Draft202012Validator:
    return schema_validator(Path(os.environ["CHD_ATLAS_ROOT"]), "publication_file")


def _load(raw: bytes | str) -> Any:
    return YAML(typ="safe").load(raw)


def _assert_valid(validator: Draft202012Validator, raw: bytes) -> None:
    assert list(validator.iter_errors(plain(_load(raw)))) == []


def test_literature_path_is_curation_literature_symbol_yaml(tmp_path: Path) -> None:
    assert literature_path(tmp_path, "TBX5") == tmp_path / "curation" / "literature" / "TBX5.yaml"


def test_a_canonical_file_keeps_every_line_and_another_indent_is_normalised_once(
    literature_schema: Draft202012Validator,
) -> None:
    # Ported (2 phases), on with_search instead of `literature search`.
    run = date(2026, 12, 1)
    content, search_id, new = with_search(
        CANONICAL.encode(), gene="HGNC:11604", query="q2", run_on=run, hits=2, pmids=["9", "25"]
    )
    assert (search_id, new) == (2, 1)
    _assert_valid(literature_schema, content)
    # Only additions: the old lines survive unchanged and in order.
    remaining = iter(content.decode().splitlines())
    assert all(line in remaining for line in CANONICAL.splitlines())
    loaded = _load(content)
    assert [t["pmid"] for t in loaded["triage"]] == ["PMID:9", "PMID:25"]
    assert loaded["triage"][1] == {"pmid": "PMID:25", "search": 2, "decision": "pending"}

    # Column-0 dashes: not the tool's style.
    other = CANONICAL.replace("  - ", "- ").replace("    ", "  ").replace("  # about", "# about")
    content, _, _ = with_search(
        other.encode(), gene="HGNC:11604", query="q2", run_on=run, hits=2, pmids=["9", "25"]
    )
    text = content.decode()
    assert "# about the first paper" in text
    assert "# top comment" in text
    old, new_loaded = _load(other), _load(content)
    assert new_loaded["searches"][0] == old["searches"][0]
    assert new_loaded["triage"][0] == old["triage"][0]
    assert "\n  - id: 1\n    source: pubmed\n" in text  # now canonical


def test_a_first_search_creates_the_file_and_a_foreign_gene_is_refused(
    literature_schema: Draft202012Validator,
) -> None:
    content, search_id, new = with_search(
        None, gene="HGNC:11604", query="q", run_on=date(2026, 11, 2), hits=3, pmids=["30", "4"]
    )
    assert (search_id, new) == (1, 2)
    _assert_valid(literature_schema, content)
    # numeric PMID order, not string order
    assert [t["pmid"] for t in _load(content)["triage"]] == ["PMID:4", "PMID:30"]
    with pytest.raises(RecordError, match="not HGNC:4173"):
        with_search(
            content, gene="HGNC:4173", query="q", run_on=date(2026, 11, 2), hits=0, pmids=[]
        )


def _record(pmid: str = "333", **extra: Any) -> dict[str, Any]:
    return {
        "id": f"PMID:{pmid}",
        "title": 'Variants in <TBX5> & "GATA4": a #study',
        "journal": "Journal of Tests",
        "year": 2024,
        "authors": ["Zed Z"],
        "study_type": "cohort_sequencing",
        **extra,
    }


def test_add_publication_appends_and_keeps_every_existing_byte(
    publication_schema: Draft202012Validator,
) -> None:
    old = PUBLICATIONS
    reading = "Methods: exome sequencing of 500 probands.\nNo controls are described."
    new = with_publication(old.encode(), _record(doi="10.1000/t333"), reading).decode()

    assert new.startswith(old)
    assert "# A comment sitting between two records." in new
    assert "# A comment sitting above a neighbouring field." in new
    _assert_valid(publication_schema, new.encode())
    publications = _load(new)["publications"]
    assert [p["id"] for p in publications] == ["PMID:111", "PMID:222", "PMID:333"]
    assert publications[-1] == {**_record(doi="10.1000/t333")}
    # The reading sits beside the value it justifies (publications.yaml's head comment).
    assert (
        new[len(old) :].count(
            "    # Methods: exome sequencing of 500 probands.\n"
            "    # No controls are described.\n"
            "    study_type: cohort_sequencing\n"
        )
        == 1
    )


def test_add_publication_to_a_file_without_a_final_newline(
    publication_schema: Draft202012Validator,
) -> None:
    bare = PUBLICATIONS.rstrip("\n")
    new = with_publication(bare.encode(), _record(study_type="gwas"), "GWAS design.").decode()
    assert new.startswith(bare + "\n")
    _assert_valid(publication_schema, new.encode())
    assert len(_load(new)["publications"]) == 3


@pytest.mark.parametrize("reading", ["   ", ""])
def test_with_publication_refuses_a_blank_reading(reading: str) -> None:
    with pytest.raises(RecordError, match="reading of the study design must not be blank"):
        with_publication(PUBLICATIONS.encode(), _record(), reading=reading)


def test_with_publication_refuses_a_duplicate_pmid() -> None:
    with pytest.raises(RecordError, match="PMID:111 is already in publications.yaml"):
        with_publication(PUBLICATIONS.encode(), _record("111"), reading="r")


# --- with_verified ------------------------------------------------------------------

FINDING_ID = re.compile(r"(?m)^  - id: (CHDA:LIT:\d{7})\n")

_CHUNKS: dict[str, str] = {
    "publication": "    publication: PMID:{pmid}\n",
    "design": "    design: cohort_screen\n",
    "methods": "    methods:\n      - exome\n",
    "count_unit": "    count_unit: probands\n",
    "screened": "    screened: 7\n",
    "carriers": "    carriers:\n      de_novo: 2\n      inherited: 1\n      origin_unknown: 3\n",
    "variant_classes": "    variant_classes:\n      - snv_indel\n",
    "phenotypes": "    phenotypes:\n      - HP:0001631\n",
    "lesion_groups": "    lesion_groups:\n      - septal\n",
    "syndromic": "    syndromic: both\n",
    "shares_probands_with": "    shares_probands_with: unknown\n",
    "locators": "    locators:\n      - kind: in_text\n        element: Table 2\n",
    "summary": "    summary: Six of seven.\n",
    "extracted_by": "    extracted_by: agent:extract/test\n",
}
_HEADER = (
    "gene: HGNC:11604\nsearches:\n  - id: 1\n    source: pubmed\n    query: TBX5[tiab]\n"
    "    run_on: 2026-11-02\n    hits: 3\n    new_hits: 3\nfindings:\n"
)


def _entry(
    number: int,
    *,
    last: str | None = None,
    verified: bool,
    trailer: str = "",
    eol: str = "",
) -> str:
    """One finding; `last` moves that key to the end (hand-ordered files), `trailer`
    is whatever separates it from the next finding."""
    keys = list(_CHUNKS)
    if last is not None:
        keys.remove(last)
        keys.append(last)
    body = "".join(_CHUNKS[k].format(pmid=1000 + number) for k in keys)
    if eol:  # a same-line comment on the last key, which holds a collection
        body = body.replace(f"    {last}:\n", f"    {last}:  {eol}\n")
    if verified:
        body = body.replace(
            _CHUNKS["extracted_by"], _CHUNKS["extracted_by"] + "    verified_by: agent:v\n"
        )
    return f"  - id: CHDA:LIT:000000{number}\n" + body + trailer


LAYOUTS = {
    "blank line": (None, "\n", ""),
    "comment then blank": (None, "  # note about the next one\n\n", ""),
    "comment": (None, "  # note about the next one\n", ""),
    "list last then comment": ("locators", "  # note about the next one\n", ""),
    "list last then blank": ("locators", "\n", ""),
    "mapping last then comment and blank": ("carriers", "  # note\n\n", ""),
    "same-line comment on a mapping, then blank": ("carriers", "\n", "# from Table 2"),
    "same-line comment on a list, then blank": ("locators", "\n", "# from Table 2"),
}


def _blocks(text: str) -> dict[str, str]:
    """Each finding's exact text, by id; "head" is everything before the first."""
    marks = list(FINDING_ID.finditer(text))
    blocks = {"head": text[: marks[0].start()]}
    for mark, nxt in zip(marks, [*marks[1:], None], strict=True):
        blocks[mark.group(1)] = text[mark.start() : nxt.start() if nxt else len(text)]
    return blocks


def _layout_file(layout: str, *, verified: bool) -> str:
    last, trailer, eol = LAYOUTS[layout]
    return (
        _HEADER
        + _entry(1, verified=verified, trailer="\n")
        + _entry(2, last=last, verified=verified, trailer=trailer, eol=eol)
        + _entry(3, verified=verified)
    )


@pytest.mark.parametrize("layout", LAYOUTS)
def test_mark_verified_adds_only_its_line_whatever_separates_the_findings(
    layout: str, literature_schema: Draft202012Validator
) -> None:
    before = _layout_file(layout, verified=False)
    ids = ["CHDA:LIT:0000001", "CHDA:LIT:0000002", "CHDA:LIT:0000003"]
    content = with_verified(before.encode(), ids, "agent:verify/blind")
    after = content.decode()
    _assert_valid(literature_schema, content)
    old_blocks, new_blocks = _blocks(before), _blocks(after)
    assert old_blocks["head"] == new_blocks["head"]
    for finding_id in ids:
        old, new = old_blocks[finding_id], new_blocks[finding_id]
        assert new.count("    verified_by: agent:verify/blind\n") == 1
        assert new.replace("    verified_by: agent:verify/blind\n", "") == old
        # directly after extracted_by unless a hand-ordered key follows it
        assert "    extracted_by: agent:extract/test\n    verified_by: agent:verify/blind\n" in new


def test_with_verified_keeps_an_existing_key_in_place_and_refuses_what_it_cannot_place() -> None:
    before = _layout_file("blank line", verified=True)
    after = with_verified(before.encode(), ["CHDA:LIT:0000002"], "agent:verify/other").decode()
    assert after == before.replace(
        _entry(2, verified=True, trailer="\n"),
        _entry(2, verified=True, trailer="\n").replace("agent:v", "agent:verify/other"),
    )
    with pytest.raises(RecordError, match="no such finding: CHDA:LIT:0000099"):
        with_verified(before.encode(), ["CHDA:LIT:0000099"], "agent:v")
    unmarked = _layout_file("blank line", verified=False)
    headless = unmarked.replace("    extracted_by: agent:extract/test\n", "", 1)
    with pytest.raises(RecordError, match="CHDA:LIT:0000001 has no extracted_by"):
        with_verified(headless.encode(), ["CHDA:LIT:0000001"], "agent:v")


def test_re_marking_the_real_tbx5_file_restores_it_byte_for_byte() -> None:
    """The atlas's own TBX5 file: delete each `verified_by` that sits directly after
    `extracted_by` (52 of its 73 findings, measured 2026-11-11), re-mark those findings,
    and the file comes back unchanged."""
    path = literature_path(Path(os.environ["CHD_ATLAS_ROOT"]), "TBX5")
    raw = path.read_text()
    lines = raw.split("\n")
    kept: list[str] = []
    ids: list[str] = []
    current = ""
    for i, line in enumerate(lines):
        mark = re.match(r"  - id: (\S+)", line)
        if mark:
            current = mark.group(1)
        if line.startswith("    verified_by:") and lines[i - 1].startswith("    extracted_by:"):
            ids.append(current)
            continue
        kept.append(line)
    verifiers = {line for line in lines if line.startswith("    verified_by: ")}
    assert len(ids) == 52 and len(verifiers) == 1
    verifier = verifiers.pop().removeprefix("    verified_by: ")
    restored = with_verified("\n".join(kept).encode(), ids, verifier)
    assert restored == raw.encode()
