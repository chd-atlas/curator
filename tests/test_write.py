"""curator's one write path: three refusals, each leaving the file's bytes untouched.

Validators come from the atlas's real exported schemas, never a copy. Every refused
input here is otherwise valid, so the refusal under test is the only thing that can
stop it (a file refused for two reasons would let a mutant that drops one survive).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from chd_curator.atlas import schema_validator
from chd_curator.write import (
    APPROVAL_FIELDS,
    WriteRefused,
    refuse_anchors,
    refuse_approval_change,
    write_literature,
    write_publications,
)

HEADER = (
    "gene: HGNC:11604\nsearches:\n  - id: 1\n    source: pubmed\n    query: TBX5[tiab]\n"
    "    run_on: 2026-11-02\n    hits: 3\n    new_hits: 3\nfindings:\n"
)
APPROVED = "    approved_by: Dr. A\n    approved_on: 2026-11-03\n    approved_digest: abc123\n"


def _finding(number: int, extra: str = "") -> str:
    return (
        f"  - id: CHDA:LIT:000000{number}\n"
        f"    publication: PMID:{1000 + number}\n"
        "    design: cohort_screen\n"
        "    methods:\n      - exome\n"
        "    phenotypes:\n      - HP:0001631\n"
        "    lesion_groups:\n      - septal\n"
        "    syndromic: both\n"
        "    shares_probands_with: unknown\n"
        "    locators:\n      - kind: in_text\n        element: Table 2\n"
        "    summary: Six of seven.\n"
        "    extracted_by: agent:extract/test\n" + extra
    )


# finding 1 is approved, finding 2 is not
BEFORE = HEADER + _finding(1, APPROVED) + _finding(2)


@pytest.fixture(scope="module")
def literature_schema() -> Draft202012Validator:
    return schema_validator(Path(os.environ["CHD_ATLAS_ROOT"]), "gene_literature_file")


@pytest.fixture(scope="module")
def publication_schema() -> Draft202012Validator:
    return schema_validator(Path(os.environ["CHD_ATLAS_ROOT"]), "publication_file")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def lit(tmp_path: Path) -> Path:
    path = tmp_path / "TBX5.yaml"
    path.write_text(BEFORE)
    return path


APPROVAL_CASES = {
    "approved_by set on an unapproved finding": (
        HEADER + _finding(1, APPROVED) + _finding(2, "    approved_by: agent:x\n"),
        "CHDA:LIT:0000002.*approved_by",
    ),
    "approved_digest alone": (
        HEADER + _finding(1, APPROVED) + _finding(2, "    approved_digest: abc\n"),
        "CHDA:LIT:0000002.*approved_digest",
    ),
    "approved_on changed on an approved finding": (
        BEFORE.replace("approved_on: 2026-11-03", "approved_on: 2026-11-04"),
        "CHDA:LIT:0000001.*approved_on",
    ),
    "all three removed from an approved finding": (
        BEFORE.replace(APPROVED, ""),
        "CHDA:LIT:0000001.*approved_by",
    ),
    "a new finding added": (BEFORE + _finding(3), "CHDA:LIT:0000003.*adding a finding"),
    "a finding removed": (HEADER + _finding(1, APPROVED), "CHDA:LIT:0000002.*removing a finding"),
}


@pytest.mark.parametrize("after, message", APPROVAL_CASES.values(), ids=APPROVAL_CASES)
def test_a_write_that_would_change_an_approval_is_refused(
    lit: Path, literature_schema: Draft202012Validator, after: str, message: str
) -> None:
    before = _digest(lit)
    with pytest.raises(WriteRefused, match=message):
        write_literature(lit, after.encode(), literature_schema)
    assert _digest(lit) == before
    assert list(tmp for tmp in lit.parent.iterdir()) == [lit]  # no stray temporary


def test_a_write_that_leaves_every_approval_alone_lands(
    lit: Path, literature_schema: Draft202012Validator
) -> None:
    after = BEFORE.replace(
        "    extracted_by: agent:extract/test\n",
        "    extracted_by: agent:extract/test\n    verified_by: agent:v\n",
    )
    write_literature(lit, after.encode(), literature_schema)
    assert lit.read_bytes() == after.encode()
    assert APPROVAL_FIELDS == ("approved_by", "approved_on", "approved_digest")


def test_a_new_file_with_no_findings_is_written_and_one_with_a_finding_is_not(
    tmp_path: Path, literature_schema: Draft202012Validator
) -> None:
    fresh = tmp_path / "new.yaml"
    write_literature(fresh, HEADER.replace("findings:\n", "").encode(), literature_schema)
    assert fresh.read_bytes() == HEADER.replace("findings:\n", "").encode()
    other = tmp_path / "other.yaml"
    with pytest.raises(WriteRefused, match="CHDA:LIT:0000001.*adding a finding"):
        write_literature(other, (HEADER + _finding(1)).encode(), literature_schema)
    assert not other.exists()


def test_refuse_approval_change_compares_every_field_with_absent_as_none() -> None:
    refuse_approval_change(None, HEADER.replace("findings:\n", "").encode())  # no file yet
    refuse_approval_change(BEFORE.encode(), BEFORE.encode())
    # the approval fields are compared individually (a mutant comparing one passes the
    # two cases above this one only if it compares the right one)
    for name, old, new in [
        ("approved_by", "Dr. A", "Dr. B"),
        ("approved_on", "2026-11-03", "2026-11-05"),
        ("approved_digest", "abc123", "abc124"),
    ]:
        with pytest.raises(WriteRefused, match=f"CHDA:LIT:0000001: {name} may not change"):
            refuse_approval_change(BEFORE.encode(), BEFORE.replace(old, new).encode())


ANCHOR_CASES = {
    "anchor": (_finding(1).replace("design: cohort_screen", "design: &d cohort_screen"), "anchor"),
    "alias": (
        _finding(1).replace("methods:\n      - exome", "methods: &m\n      - exome")
        + _finding(2).replace("methods:\n      - exome", "methods: *m"),
        "anchor",
    ),
    "merge key": (
        _finding(1).replace("    syndromic: both\n", "    <<: {syndromic: both}\n"),
        "merge",
    ),
}


@pytest.mark.parametrize("body, message", ANCHOR_CASES.values(), ids=ANCHOR_CASES)
def test_the_one_write_path_refuses_anchors_aliases_and_merge_keys(
    tmp_path: Path, literature_schema: Draft202012Validator, body: str, message: str
) -> None:
    path = tmp_path / "TBX5.yaml"
    path.write_text(HEADER)  # no findings yet, so a refusal can only be the construct itself
    before = _digest(path)
    with pytest.raises(WriteRefused, match=message):
        write_literature(path, (HEADER + body).encode(), literature_schema)
    assert _digest(path) == before


def test_an_alias_is_refused_even_when_it_is_the_only_construct_the_anchor_check_sees() -> None:
    # `refuse_anchors` is exported on its own: a stream with an alias event and (here) the
    # anchor it needs both name the construct, and a plain mapping is accepted.
    with pytest.raises(WriteRefused, match=r"anchor \(&a\)"):
        refuse_anchors(b"a: &a 1\nb: *a\n")
    with pytest.raises(WriteRefused, match="merge key"):
        refuse_anchors(b"a: {x: 1}\nb:\n  <<: {y: 2}\n")
    with pytest.raises(WriteRefused, match="merge key"):  # quoted or not, a key named << goes
        refuse_anchors(b'{"<<": 1}\n')
    refuse_anchors(b'a: "<<"\nb: [<<x]\n')  # << as a value, or inside a word, is data


WRITE_CASES = {
    "verified_by empty": (
        lambda text: text.replace(
            "extracted_by: agent:extract/test\n",
            'extracted_by: agent:extract/test\n    verified_by: ""\n',
            1,
        ),
        "verified_by",
    ),
    "decision outside the vocabulary": (
        lambda text: text.replace(
            "findings:\n",
            "triage:\n  - pmid: PMID:1\n    search: 1\n    decision: maybe\nfindings:\n",
        ),
        "decision",
    ),
}


@pytest.mark.parametrize("edit, message", WRITE_CASES.values(), ids=WRITE_CASES)
def test_a_write_the_schema_refuses_is_never_made(
    lit: Path,
    literature_schema: Draft202012Validator,
    edit,
    message: str,  # type: ignore[no-untyped-def]
) -> None:
    before = _digest(lit)
    with pytest.raises(WriteRefused, match=message) as raised:
        write_literature(lit, edit(BEFORE).encode(), literature_schema)
    assert str(lit) in str(raised.value)  # the message names the file
    assert _digest(lit) == before


def test_publications_are_checked_and_written_through_the_same_path(
    tmp_path: Path, publication_schema: Draft202012Validator
) -> None:
    path = tmp_path / "publications.yaml"
    good = (
        "publications:\n  - id: PMID:1\n    title: T\n    journal: J\n    year: 2000\n"
        "    authors:\n      - A A\n    study_type: gwas\n"
    )
    path.write_text(good)
    before = _digest(path)
    for refused, message in [
        (good.replace("gwas", "not_a_type"), "study_type"),
        (good.replace("title: T", "title: &t T"), "anchor"),
    ]:
        with pytest.raises(WriteRefused, match=message):
            write_publications(path, refused.encode(), publication_schema)
        assert _digest(path) == before
    longer = good + good.replace("PMID:1", "PMID:2").split("\n", 1)[1]
    write_publications(path, longer.encode(), publication_schema)
    assert path.read_bytes() == longer.encode()
