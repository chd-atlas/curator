"""``chd-curator quotes``: every quote a review page shows, checked against its paper.

The fixture run (tests/fixtures/review, run r1) holds 11 quotes: 5 evidence on 0000901, 1
evidence and 5 judge on 0000902. Counts below were read from those files: one quote is
invented (found nowhere), one is present only through normalisation (a U+FB01 ligature in
the source; measured by swapping ``normalise_text`` for whitespace-and-case folding alone, which
changes only that quote's answer) and one is 40 words and present. The fixture source also
carries a U+2010 hyphen, but its quote is found without any mapping, so this fixture does not
pin the U+2010 entry of the translation table; the parametrised test does.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chd_curator.cli import app
from chd_curator.quotes import QuoteCheck, check_quotes, normalise_text, quote_found
from chd_curator.run import RunFormatError, RunPaths

FIXTURE = Path(__file__).parent / "fixtures" / "review"
runner = CliRunner()


@pytest.fixture
def run_copy(tmp_path: Path) -> RunPaths:
    shutil.copytree(FIXTURE, tmp_path / "root")
    return RunPaths(tmp_path / "root", "TBX5", "r1")


@pytest.mark.parametrize(
    "quote, source",
    [
        ("ventricular septal‐defect", "ventricular septal-defect"),  # U+2010 in the quote
        ("ventricular septal-defect", "ventricular septal‐defect"),  # U+2010 in the source
        ("the ﬁrst proband", "the first proband"),  # ligature
        ("P61 PAD/AVSD/SV", "P61 | PAD/AVSD/SV"),  # flattened table cell
        ("row one and two", "row one---row and two"),  # table row marker
        ("heterozygous deletion at 10q26", "heterozygous\ndeletion at 10q26"),
        ("a “quoted” term, d’un", 'a "quoted" term, d\'un'),
        ("a 3–4 x 5−6 × 7 ∗", "a 3-4 x 5-6 x 7 *"),
        ("a b c d", "a b c d"),
        ("THE Variant", "the variant"),
    ],
)
def test_a_quote_is_found_despite_typography(quote: str, source: str) -> None:
    assert quote_found(quote, [normalise_text(source)])


def test_a_paraphrase_or_blank_quote_is_not_found() -> None:
    source = normalise_text("The proband had a ventricular septal defect.")
    assert not quote_found("the proband had a septal defect of the ventricle", [source])
    assert not quote_found("   ...  ", [source])
    assert not quote_found("anything", [])


def test_an_ellipsis_needs_every_part_in_one_source() -> None:
    a = normalise_text("alpha one beta two")
    b = normalise_text("alpha one only")
    c = normalise_text("beta two only")
    assert quote_found("alpha one ... beta two", [a])
    assert quote_found("alpha one … beta two", [a])
    assert not quote_found("alpha one ... beta two", [b, c])  # parts in different sources


SOURCE = normalise_text("the proband had beta and then alpha. row 3 a")


@pytest.mark.parametrize(
    "quote",
    [
        "a ... b",  # parts under 3 characters
        "he",  # inside "the"
        "a",
        "alpha ... beta",  # parts out of order
        "roband had",  # starts inside "proband"
        "proban",  # ends inside "proband"
    ],
)
def test_a_fragment_or_out_of_order_quote_is_not_found(quote: str) -> None:
    assert not quote_found(quote, [SOURCE])


def test_parts_in_order_at_word_boundaries_are_found() -> None:
    assert quote_found("the proband ... beta ... alpha", [SOURCE])
    assert quote_found("beta and then alpha", [normalise_text("beta, beta and then alpha")])
    # the first occurrence of a part fails the boundary; a later one is still found
    assert quote_found("cat sat", [normalise_text("concat sat ... cat sat")])


def test_check_quotes_on_the_fixture_run(run_copy: RunPaths) -> None:
    checks = check_quotes(run_copy)
    assert [(c.finding[-4:], c.origin) for c in checks] == [("0901", "evidence")] * 5 + [
        ("0902", "evidence")
    ] + [("0902", "judge")] * 5
    not_found = [c.field for c in checks if not c.found]
    assert not_found == ["variant 1 carriers"]
    by_field = {c.field: c for c in checks}
    assert by_field["design"].found and by_field["carriers"].found  # carriers: ligature
    assert [c.words for c in checks if c.words > 30] == [40]
    assert by_field["design"].words == 9
    assert all(isinstance(c, QuoteCheck) for c in checks)


def test_a_finding_with_no_source_text_is_unchecked_not_a_miss(run_copy: RunPaths) -> None:
    lit = run_copy.root / "curation/literature/TBX5.yaml"
    lit.write_text(
        lit.read_text(encoding="utf-8").replace(
            "publication: PMID:90000002", "publication: PMID:90000099"
        ),
        encoding="utf-8",
    )
    checks = [c for c in check_quotes(run_copy) if c.finding.endswith("0902")]
    assert len(checks) == 6 and not any(c.found or c.checked for c in checks)
    assert {c.paper for c in checks} == {"PMID:90000099"}

    texts = run_copy.texts
    data = json.loads(texts.read_text(encoding="utf-8"))
    data["papers"][0].update(source="none", text=None, abstract=None, supplements=[])
    texts.write_text(json.dumps(data), encoding="utf-8")
    no_text = [c for c in check_quotes(run_copy) if c.finding.endswith("0901")]
    assert len(no_text) == 5 and not any(c.found or c.checked for c in no_text)


def test_a_source_with_nothing_in_it_is_no_text(run_copy: RunPaths) -> None:
    """An empty or blank listed file must not make a quote "not found" in nothing."""
    for name in ("90000001.txt", "90000001.abstract.txt", "90000001.s1.txt"):
        (run_copy.root / "local/literature/fulltext" / name).write_text(" \n\t\n", encoding="utf-8")
    checks = [c for c in check_quotes(run_copy) if c.finding.endswith("0901")]
    assert len(checks) == 5 and not any(c.found or c.checked for c in checks)
    (run_copy.root / "local/literature/fulltext/90000001.s1.txt").write_text(
        "this sentence is not in the paper", encoding="utf-8"
    )
    again = {c.field: c for c in check_quotes(run_copy) if c.finding.endswith("0901")}
    assert all(c.checked for c in again.values()) and again["variant 1 carriers"].found


def test_a_judge_decision_with_no_quote_is_not_a_quote(run_copy: RunPaths) -> None:
    before = check_quotes(run_copy)
    path = run_copy.judge / "judge-02.decisions.yaml"
    path.write_text(
        "CHDA:LIT:0000901:\n  - {field: design, first: a, second: b, decision: first,"
        ' value: a, quote: "  ", where: "", rule: r}\n',
        encoding="utf-8",
    )
    assert check_quotes(run_copy) == before


def test_a_quote_records_how_many_parts_it_has(run_copy: RunPaths) -> None:
    ev = run_copy.evidence / "batch-01.yaml"  # 0902's list is the last one in the file
    ev.write_text(
        ev.read_text(encoding="utf-8")
        + '  - {field: "x", quote: "a proband ... septal defect", where: "A"}\n',
        encoding="utf-8",
    )
    extra = next(c for c in check_quotes(run_copy) if c.field == "x")
    assert (extra.found, extra.parts) == (True, 2)
    assert {c.parts for c in check_quotes(run_copy) if c.field != "x"} == {1}
    run_copy.quotes.unlink(missing_ok=True)
    assert (
        runner.invoke(
            app, ["quotes", "TBX5", "--run", "r1", "--root", str(run_copy.root)]
        ).exit_code
        == 0
    )
    written = json.loads(run_copy.quotes.read_bytes())["checks"]
    assert [c["parts"] for c in written if c["field"] == "x"] == [2]


def test_a_listed_source_that_cannot_be_read_is_an_error(run_copy: RunPaths) -> None:
    (run_copy.root / "local/literature/fulltext/90000001.s1.txt").unlink()
    with pytest.raises(RunFormatError, match="90000001.s1.txt"):
        check_quotes(run_copy)


def test_the_command_writes_quotes_json_and_names_what_it_found(run_copy: RunPaths) -> None:
    args = ["quotes", "TBX5", "--run", "r1", "--root", str(run_copy.root)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "11 quotes checked, 1 not found, 1 over 30 words"
    assert len(lines) == 3
    assert "not found: CHDA:LIT:0000901 evidence variant 1 carriers" in lines[1]
    assert "over 30 words (40): CHDA:LIT:0000901 evidence summary" in lines[2]

    first = run_copy.quotes.read_bytes()
    payload = json.loads(first)
    assert payload["format"] == 1 and len(payload["checks"]) == 11
    assert set(payload["checks"][0]) == {
        "finding", "origin", "field", "quote", "where", "found", "checked", "paper", "words",
        "parts"
    }  # fmt: skip
    assert all(c["checked"] is True for c in payload["checks"])
    assert [c["found"] for c in payload["checks"]].count(False) == 1
    assert runner.invoke(app, args).exit_code == 0
    assert run_copy.quotes.read_bytes() == first  # identical bytes on a second run


def test_the_command_counts_only_checked_quotes_and_names_the_unchecked(
    run_copy: RunPaths,
) -> None:
    lit = run_copy.root / "curation/literature/TBX5.yaml"
    lit.write_text(
        lit.read_text(encoding="utf-8").replace(
            "publication: PMID:90000002", "publication: PMID:90000099"
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["quotes", "TBX5", "--run", "r1", "--root", str(run_copy.root)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "5 quotes checked, 1 not found, 1 over 30 words"
    assert "6 quotes not checked: no text (PMID:90000099)" in lines
    payload = json.loads(run_copy.quotes.read_bytes())
    assert [c["checked"] for c in payload["checks"]].count(False) == 6


def test_a_malformed_run_exits_2_and_writes_nothing(run_copy: RunPaths) -> None:
    run_copy.texts.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["quotes", "TBX5", "--run", "r1", "--root", str(run_copy.root)])
    assert result.exit_code == 2
    assert "texts.json" in result.output
    assert not run_copy.quotes.exists()


@pytest.mark.parametrize("drop", ["id", "publication"])
def test_a_finding_with_no_id_or_publication_is_refused_not_skipped(
    run_copy: RunPaths, drop: str
) -> None:
    """Its quotes would silently go unchecked (every one 'not checked: no text')."""
    path = run_copy.root / "curation/literature/TBX5.yaml"
    text = path.read_text(encoding="utf-8")
    line = {"id": "  - id: CHDA:LIT:0000903\n", "publication": "    publication: PMID:90000003\n"}[
        drop
    ]
    assert text.count(line) == 1
    path.write_text(text.replace(line, "  - note: x\n" if drop == "id" else ""), encoding="utf-8")
    with pytest.raises(RunFormatError, match="no id or no publication"):
        check_quotes(run_copy)
