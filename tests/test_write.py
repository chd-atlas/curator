"""curator's one write path: three refusals, each leaving the file's bytes untouched.

Validators come from the atlas's real exported schemas, never a copy. Each refused
input is built so the refusal under test is the only thing wrong with it: the anchor
cases rewrite a file that already holds the same findings with the same approvals, the
approval cases are schema-valid, and the schema cases change no approval. (A file
refused for two reasons would let a mutant that drops one survive.)
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from chd_curator.atlas import plain, schema_validator
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
    "a finding id repeated with the original's approval": (
        BEFORE + _finding(1, APPROVED),
        "CHDA:LIT:0000001.*more than one finding has this id",
    ),
    "a finding removed": (HEADER + _finding(1, APPROVED), "CHDA:LIT:0000002.*removing a finding"),
}


@pytest.mark.parametrize("after, message", APPROVAL_CASES.values(), ids=APPROVAL_CASES)
def test_a_write_that_would_change_an_approval_is_refused(
    lit: Path, literature_schema: Draft202012Validator, after: str, message: str
) -> None:
    before = _digest(lit)
    with pytest.raises(WriteRefused, match=message):
        write_literature(lit, after.encode(), literature_schema, expected=BEFORE.encode())
    assert _digest(lit) == before
    assert list(tmp for tmp in lit.parent.iterdir()) == [lit]  # no stray temporary


def test_a_write_that_leaves_every_approval_alone_lands(
    lit: Path, literature_schema: Draft202012Validator
) -> None:
    after = BEFORE.replace(
        "    extracted_by: agent:extract/test\n",
        "    extracted_by: agent:extract/test\n    verified_by: agent:v\n",
    )
    write_literature(lit, after.encode(), literature_schema, expected=BEFORE.encode())
    assert lit.read_bytes() == after.encode()
    assert APPROVAL_FIELDS == ("approved_by", "approved_on", "approved_digest")


def test_a_new_file_with_no_findings_is_written_and_one_with_a_finding_is_not(
    tmp_path: Path, literature_schema: Draft202012Validator
) -> None:
    fresh = tmp_path / "new.yaml"
    write_literature(
        fresh, HEADER.replace("findings:\n", "").encode(), literature_schema, expected=None
    )
    assert fresh.read_bytes() == HEADER.replace("findings:\n", "").encode()
    other = tmp_path / "other.yaml"
    with pytest.raises(WriteRefused, match="CHDA:LIT:0000001.*adding a finding"):
        write_literature(other, (HEADER + _finding(1)).encode(), literature_schema, expected=None)
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
    "anchor defined and never used": (
        BEFORE.replace("design: cohort_screen", "design: &d cohort_screen", 1),
        "a YAML anchor (&d) is not allowed",
    ),
    # The stream reaches the anchor before its alias, so this one is refused as an anchor.
    "anchor and its alias": (
        BEFORE.replace("methods:\n      - exome", "methods: &m\n      - exome", 1).replace(
            "methods:\n      - exome", "methods: *m", 1
        ),
        "a YAML anchor (&m) is not allowed",
    ),
    "merge key": (
        BEFORE.replace("    syndromic: both\n", "    <<: {syndromic: both}\n", 1),
        "a YAML merge key (<<) is not allowed",
    ),
}


@pytest.mark.parametrize("after, message", ANCHOR_CASES.values(), ids=ANCHOR_CASES)
def test_the_one_write_path_refuses_anchors_aliases_and_merge_keys(
    lit: Path, literature_schema: Draft202012Validator, after: str, message: str
) -> None:
    # The file already holds these findings with these approvals, and the loaded
    # content validates and changes no approval: the construct is the only fault.
    assert after != BEFORE
    assert list(literature_schema.iter_errors(plain(YAML(typ="safe").load(after)))) == []
    refuse_approval_change(BEFORE.encode(), after.encode())
    before = _digest(lit)
    with pytest.raises(WriteRefused) as raised:
        write_literature(lit, after.encode(), literature_schema, expected=BEFORE.encode())
    assert str(raised.value) == f"{lit}: {message}"  # the file first, then the construct
    assert _digest(lit) == before


def test_an_alias_with_no_anchor_is_refused_as_an_alias(
    lit: Path, literature_schema: Draft202012Validator
) -> None:
    # ruamel's parser accepts `*m` with no `&m` (it yields an AliasEvent), so only the
    # alias check can name it; the content cannot even be loaded, so no schema or
    # approval check could have refused it first.
    after = BEFORE.replace("methods:\n      - exome", "methods: *m", 1)
    before = _digest(lit)
    with pytest.raises(WriteRefused) as raised:
        write_literature(lit, after.encode(), literature_schema, expected=BEFORE.encode())
    assert str(raised.value) == f"{lit}: a YAML alias (*m) is not allowed"
    assert _digest(lit) == before


def test_refuse_anchors_names_the_construct_and_lets_plain_data_through() -> None:
    with pytest.raises(WriteRefused, match=re.escape("a YAML anchor (&a) is not allowed")):
        refuse_anchors(b"a: &a 1\nb: *a\n")  # the anchor comes first in the stream
    with pytest.raises(WriteRefused, match=re.escape("a YAML alias (*a) is not allowed")):
        refuse_anchors(b"x: *a\n")
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
        write_literature(lit, edit(BEFORE).encode(), literature_schema, expected=BEFORE.encode())
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
        (good.replace("title: T", "title: &t T"), f"{path}: a YAML anchor (&t) is not allowed"),
    ]:
        with pytest.raises(WriteRefused, match=re.escape(message)):
            write_publications(path, refused.encode(), publication_schema)
        assert _digest(path) == before
    longer = good + good.replace("PMID:1", "PMID:2").split("\n", 1)[1]
    write_publications(path, longer.encode(), publication_schema)
    assert path.read_bytes() == longer.encode()


def test_every_schema_error_is_reported_in_path_order_with_the_file_named(
    lit: Path, literature_schema: Draft202012Validator
) -> None:
    # Three errors; jsonschema yields the root one last, so only the sort puts it first.
    after = (
        BEFORE.replace("gene: HGNC:11604\n", "")
        .replace("    syndromic: both\n", "    syndromic: sometimes\n", 1)
        .replace(
            "extracted_by: agent:extract/test\n",
            'extracted_by: agent:extract/test\n    verified_by: ""\n',
            1,
        )
    )
    before = _digest(lit)
    with pytest.raises(WriteRefused) as raised:
        write_literature(lit, after.encode(), literature_schema, expected=BEFORE.encode())
    head, *lines = str(raised.value).split("\n")
    assert head == f"{lit} does not satisfy its schema:"
    assert [line.split(":")[0] for line in lines] == [
        "<root>",
        "findings/0/syndromic",
        "findings/0/verified_by",
    ]
    assert lines[0] == "<root>: 'gene' is a required property"
    assert _digest(lit) == before


@pytest.mark.parametrize(
    "expected",
    [None, b"gene: something else\n", BEFORE.encode() + b"# edited meanwhile\n"],
    ids=["file-existed-after-all", "different-bytes", "bytes-plus-an-edit"],
)
def test_a_file_that_is_not_the_bytes_the_caller_read_is_never_overwritten(
    lit: Path, literature_schema: Draft202012Validator, expected: bytes | None
) -> None:
    """The content below is otherwise writable, so the stale read is the only fault."""
    after = BEFORE.replace(
        "    extracted_by: agent:extract/test\n",
        "    extracted_by: agent:extract/test\n    verified_by: agent:v\n",
    )
    before = _digest(lit)
    with pytest.raises(WriteRefused) as raised:
        write_literature(lit, after.encode(), literature_schema, expected=expected)
    assert str(raised.value) == f"{lit}: the file changed since it was read; nothing was written"
    assert _digest(lit) == before
    assert list(lit.parent.iterdir()) == [lit]


def test_a_file_that_appeared_after_the_caller_found_none_is_not_overwritten(
    tmp_path: Path, literature_schema: Draft202012Validator
) -> None:
    path = tmp_path / "TBX5.yaml"
    path.write_text("# a curator created this meanwhile\n")
    before = _digest(path)
    with pytest.raises(WriteRefused, match="the file changed since it was read"):
        write_literature(
            path, HEADER.replace("findings:\n", "").encode(), literature_schema, expected=None
        )
    assert _digest(path) == before
