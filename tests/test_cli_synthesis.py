"""``chd-curator synthesis``, with a stub atlas and the atlas's real exported schemas.

The command writes an UNAPPROVED ``synthesis`` block through the one write path. It does not
check the citations against the file's findings and does not run the atlas's SYN rules: the
skill runs ``chd-atlas validate`` next, and those rules live in the atlas.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from ruamel.yaml import YAML
from typer.testing import CliRunner, Result

from chd_curator import cli
from chd_curator.atlas import plain, schema_validator
from chd_curator.cli import app

runner = CliRunner()

LITERATURE = """\
# Curator's head comment -- must survive.
gene: HGNC:11604
searches:
  - id: 1
    source: pubmed
    query: TBX5[tiab]
    run_on: 2026-11-02
    hits: 1
    new_hits: 1
triage:
  - pmid: PMID:8988165
    search: 1
    decision: included
"""
DRAFT = """\
sentences:
  - text: Septal defects are the usual presentation.
    cites: [CHDA:LIT:0000001, CHDA:AST:0000002]
  - text: Segregation is reported in one family.
    cites: [CHDA:LIT:0000003]
drafted_by: agent:gene-synthesis/opus
"""
WRITTEN = (
    "synthesis:\n"
    "  sentences:\n"
    "    - text: Septal defects are the usual presentation.\n"
    "      cites:\n"
    "        - CHDA:LIT:0000001\n"
    "        - CHDA:AST:0000002\n"
    "    - text: Segregation is reported in one family.\n"
    "      cites:\n"
    "        - CHDA:LIT:0000003\n"
    "  drafted_by: agent:gene-synthesis/opus\n"
)
REDRAFT = (
    "sentences:\n  - text: Only one now.\n    cites: [CHDA:LIT:0000009]\ndrafted_by: agent:x/y\n"
)
REDRAFTED = (
    "synthesis:\n  sentences:\n    - text: Only one now.\n      cites:\n"
    "        - CHDA:LIT:0000009\n  drafted_by: agent:x/y\n"
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "curation" / "literature").mkdir(parents=True)
    (tmp_path / "schemas").symlink_to(Path(os.environ["CHD_ATLAS_ROOT"]) / "schemas")
    return tmp_path


@pytest.fixture
def lit_path(root: Path) -> Path:
    path = root / "curation" / "literature" / "TBX5.yaml"
    path.write_text(LITERATURE)
    return path


def _draft(root: Path, text: str = DRAFT) -> Path:
    path = root / "draft.yaml"
    path.write_text(text)
    return path


def _synthesis(stub: Path, root: Path, draft: Path, symbol: str = "TBX5") -> Result:
    return runner.invoke(
        app,
        ["synthesis", symbol, "--draft", str(draft), "--root", str(root), "--atlas-cli", str(stub)],
    )


def test_it_writes_an_unapproved_block_last_and_a_redraft_replaces_it(
    stub_atlas: Path, root: Path, lit_path: Path
) -> None:
    result = _synthesis(stub_atlas, root, _draft(root))
    assert result.exit_code == 0, result.stdout
    assert result.stdout == f"TBX5: wrote a 2-sentence synthesis, unapproved, to {lit_path}\n"
    assert lit_path.read_text() == LITERATURE + WRITTEN
    schema: Draft202012Validator = schema_validator(root, "gene_literature_file")
    assert list(schema.iter_errors(plain(YAML(typ="safe").load(lit_path.read_text())))) == []

    again = _synthesis(stub_atlas, root, _draft(root, REDRAFT), symbol="HOS")
    assert again.exit_code == 0, again.stdout
    assert again.stdout == f"TBX5: wrote a 1-sentence synthesis, unapproved, to {lit_path}\n"
    assert lit_path.read_text() == LITERATURE + REDRAFTED


APPROVED_BLOCK = WRITTEN + "  approved_by: Dr. A\n"
REFUSALS = {
    "an approval field in the draft": (
        DRAFT + "approved_by: Dr. A\n",
        "error: the draft may hold only sentences and drafted_by; remove approved_by\n",
        LITERATURE,
    ),
    "an unknown key in the draft": (
        DRAFT + "note: hi\napproved_digest: sha256:a\n",
        "error: the draft may hold only sentences and drafted_by; remove approved_digest, note\n",
        LITERATURE,
    ),
    "a draft that is not a mapping": (
        "- a\n- b\n",
        "error: the draft must be a mapping with sentences and drafted_by\n",
        LITERATURE,
    ),
    "an anchor in the draft": (
        DRAFT.replace("drafted_by: agent", "drafted_by: &d agent"),
        "error: the draft: a YAML anchor (&d) is not allowed\n",
        LITERATURE,
    ),
    "a stray key in a sentence": (
        DRAFT.replace("  - text: Segregation", "  - note: x\n    text: Segregation"),
        "error: a sentence may hold only text and cites; remove note\n",
        LITERATURE,
    ),
    "no drafted_by": (
        DRAFT.replace("drafted_by: agent:gene-synthesis/opus\n", ""),
        "error: the draft has no drafted_by\n",
        LITERATURE,
    ),
    "sentences that is not a list": (
        "sentences: hello\ndrafted_by: agent:x/y\n",
        "error: the draft's sentences must be a list of mappings with text and cites\n",
        LITERATURE,
    ),
    "a sentence that is not a mapping": (
        "sentences: [hello]\ndrafted_by: agent:x/y\n",
        "error: the draft's sentences must be a list of mappings with text and cites\n",
        LITERATURE,
    ),
    "a sentence with no cites": (
        "sentences:\n  - text: Hello.\ndrafted_by: agent:x/y\n",
        "error: a sentence has no cites\n",
        LITERATURE,
    ),
    "a sentence with empty cites": (
        DRAFT.replace("[CHDA:LIT:0000003]", "[]"),
        "SCHEMA'cites': []",
        LITERATURE,
    ),
    "six sentences": (
        "sentences:\n"
        + "  - text: Another one.\n    cites: [CHDA:LIT:0000001]\n" * 6
        + "drafted_by: agent:x/y\n",
        "error: the draft holds more than five sentences\n",
        LITERATURE,
    ),
    "a cite that is not a string": (
        DRAFT.replace("[CHDA:LIT:0000003]", "[3]"),
        "error: a sentence's cites must all be strings\n",
        LITERATURE,
    ),
    "a draft over 64 KiB": (
        DRAFT + "# " + "x" * 65536 + "\n",
        "error: the draft is larger than 64 KiB\n",
        LITERATURE,
    ),
    "text the file would not keep": (
        # `\N` is U+0085 (NEL); the dump writes it as a line break that reads back as spaces
        DRAFT.replace(
            "text: Segregation is reported in one family.", 'text: "Segregation\\Nis reported."'
        ),
        "error: the written synthesis would differ from the draft; nothing was written\n",
        LITERATURE,
    ),
    "a citation the schema refuses": (
        DRAFT.replace("CHDA:LIT:0000003", "CHDA:LIT:3"),
        "SCHEMA'CHDA:LIT:3'",
        LITERATURE,
    ),
    "an approved synthesis already in the file": (
        DRAFT.replace("usual", "common"),
        "error: the synthesis is approved by Dr. A on (not set); an agent never removes an"
        " approval: stop and ask that person\n",
        LITERATURE + APPROVED_BLOCK,
    ),
}


@pytest.mark.parametrize("draft, message, current", REFUSALS.values(), ids=list(REFUSALS))
def test_a_refused_draft_exits_2_and_leaves_the_file_as_it_was(
    stub_atlas: Path, root: Path, lit_path: Path, draft: str, message: str, current: str
) -> None:
    lit_path.write_text(current)
    result = _synthesis(stub_atlas, root, _draft(root, draft))
    assert result.exit_code == 2, result.stdout
    if message.startswith("SCHEMA"):  # the atlas owns the wording
        assert result.stdout.startswith(f"error: {lit_path} does not satisfy its schema:\n")
        assert "\nsynthesis: " in result.stdout
        assert message.removeprefix("SCHEMA") in result.stdout
    else:
        assert result.stdout == message
    assert lit_path.read_text() == current
    assert list(lit_path.parent.iterdir()) == [lit_path]


def test_a_missing_literature_file_or_draft_is_refused(stub_atlas: Path, root: Path) -> None:
    result = _synthesis(stub_atlas, root, _draft(root))
    assert result.exit_code == 2
    assert result.stdout.endswith("TBX5.yaml does not exist; run `chd-curator search TBX5` first\n")
    (root / "curation" / "literature" / "TBX5.yaml").write_text(LITERATURE)
    result = _synthesis(stub_atlas, root, root / "nope.yaml")
    assert result.exit_code == 2
    assert result.stdout == f"error: --draft {root / 'nope.yaml'} is not a file\n"


def test_it_does_not_overwrite_an_edit_made_since_it_read_the_file(
    stub_atlas: Path, root: Path, lit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli.with_synthesis

    def edit_then_build(existing: bytes, draft: object) -> bytes:
        lit_path.write_bytes(existing + b"# a curator's edit, made meanwhile\n")
        return real(existing, draft)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "with_synthesis", edit_then_build)
    result = _synthesis(stub_atlas, root, _draft(root))
    assert result.exit_code == 2, result.stdout
    assert result.stdout.endswith(
        f"error: {lit_path}: the file changed since it was read; nothing was written\n"
    )
    assert lit_path.read_bytes() == LITERATURE.encode() + b"# a curator's edit, made meanwhile\n"


def test_an_existing_file_that_is_not_valid_yaml_exits_1_like_the_other_commands(
    stub_atlas: Path, root: Path, lit_path: Path
) -> None:
    lit_path.write_text("gene: [unclosed\n")
    result = _synthesis(stub_atlas, root, _draft(root))
    assert result.exit_code == 1, result.stdout
    assert result.stdout.startswith("error: not valid YAML: ")
    assert lit_path.read_text() == "gene: [unclosed\n"
