"""curator against the REAL atlas: ``CHD_ATLAS_CLI`` and a copy of ``CHD_ATLAS_ROOT``.

The unit tests use a stub atlas, so nothing there shows that curator's output is
what the atlas accepts. These tests run the atlas's own ``literature target`` and
``validate`` over a throwaway copy of the checkout. PubMed is still faked.
"""

from __future__ import annotations

import copy
import csv
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator import cli
from chd_curator import variantvalidator as vv
from chd_curator.cli import app
from fake_pubmed import FakePubMed, papers
from test_skills import ATLAS_COMMANDS, REPO, skill_examples

runner = CliRunner()
# .git, .venv, local, dist, .superpowers, CLAUDE.md, .claude and superpowers are
# gitignored local files or symlinks on a development machine and absent on CI.
IGNORED = (".git", ".venv", "local", "dist", ".superpowers", "CLAUDE.md", ".claude", "superpowers")


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """``text`` without ANSI escape sequences. With GITHUB_ACTIONS=true the atlas's
    typer/Rich help styles ``--against`` as two segments, ``\\x1b[1;36m-\\x1b[0m
    \\x1b[1;36m-against\\x1b[0m``, which no option-name match can see through."""
    return _ANSI.sub("", text)


def _atlas(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the atlas CLI with colour and wrapping off, and ANSI stripped from the output."""
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    run = subprocess.run(
        [os.environ["CHD_ATLAS_CLI"], *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    run.stdout = _plain(run.stdout)
    run.stderr = _plain(run.stderr)
    return run


@pytest.fixture
def atlas_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "atlas"
    shutil.copytree(
        os.environ["CHD_ATLAS_ROOT"],
        copy,
        symlinks=False,
        ignore=shutil.ignore_patterns(*IGNORED),
    )
    return copy


def _registry_targets(root: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """`(symbol, the real target's answer)` for each gene of ``mirrors/genes.tsv``, in file
    order, asking the atlas lazily so a caller stops at the first gene it wants."""
    with (root / "mirrors" / "genes.tsv").open(newline="", encoding="utf-8") as handle:
        symbols = [row["symbol"] for row in csv.DictReader(handle, delimiter="\t")]
    for symbol in symbols:
        run = _atlas("literature", "target", symbol, "--root", str(root))
        assert run.returncode == 0, run.stdout + run.stderr
        yield symbol, json.loads(run.stdout)


def _validate(root: Path) -> None:
    run = _atlas("validate", "--root", str(root))
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_real_target_answers_for_tbx5() -> None:
    run = _atlas("literature", "target", "TBX5", "--root", os.environ["CHD_ATLAS_ROOT"])
    assert run.returncode == 0, run.stdout + run.stderr
    answer = json.loads(run.stdout)
    assert answer["gene"] == "HGNC:11604"
    assert answer["published"] is True
    assert answer["query"].startswith('("TBX5"[tiab]')


def test_a_search_on_a_copy_of_the_atlas_validates(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = None
    for symbol, answer in _registry_targets(atlas_copy):
        file = atlas_copy / "curation" / "literature" / f"{answer['symbol']}.yaml"
        if answer["published"] is True and not file.exists():
            chosen = (symbol, answer, file)
            break
    assert chosen is not None, "no registered gene is published without a literature file"
    symbol, answer, file = chosen
    monkeypatch.setattr(cli, "_fetch", FakePubMed(papers("9", "25", "100")))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "search",
            symbol,
            "--run-on",
            "2026-11-11",
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 0, result.stdout
    written = YAML(typ="safe").load(file.read_text())
    assert written["gene"] == answer["gene"]
    assert written["searches"][0]["query"] == answer["query"]
    assert [t["pmid"] for t in written["triage"]] == ["PMID:9", "PMID:25", "PMID:100"]
    _validate(atlas_copy)


def test_a_search_on_a_registered_gene_outside_the_gate_is_refused_before_pubmed(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real registry holds genes the atlas does not publish (BRCA1 is not one of them:
    it is not registered), so the gate is asked of the real atlas, not a stub."""
    unpublished = next(
        ((s, a) for s, a in _registry_targets(atlas_copy) if a["published"] is False), None
    )
    assert unpublished is not None, "every registered gene is published, so the gate is untested"
    symbol, answer = unpublished
    literature = atlas_copy / "curation" / "literature"
    files_before = sorted(p.name for p in literature.iterdir())
    fake = FakePubMed(papers("9"))
    monkeypatch.setattr(cli, "_fetch", fake)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "search",
            symbol,
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 2, result.stdout
    assert result.stdout == (
        f"error: {answer['symbol']} ({answer['gene']}) is outside the publication gate; "
        "its literature would reach no page\n"
    )
    assert fake.urls == []
    assert sorted(p.name for p in literature.iterdir()) == files_before


def test_add_publication_on_a_copy_of_the_atlas_validates(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = atlas_copy / "curation" / "publications.yaml"
    pmid = "9"
    assert f"PMID:{pmid}\n" not in path.read_text()
    monkeypatch.setattr(cli, "_fetch", FakePubMed(papers(pmid)))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "add-publication",
            pmid,
            "--study-type",
            "case_report",
            "--reading",
            "A single-family report in the abstract.",
            "--root",
            str(atlas_copy),
        ],
    )

    assert result.exit_code == 0, result.stdout
    ids = [p["id"] for p in YAML(typ="safe").load(path.read_text())["publications"]]
    assert ids[-1] == "PMID:9"
    _validate(atlas_copy)


def _tbx5_target(text: str) -> tuple[int, str, str]:
    """`(line index, finding id, verifier)` of an approved, verified, unambiguous finding
    whose ``verified_by`` line directly follows ``extracted_by``."""
    data = YAML(typ="safe").load(text)
    keys = [
        (f["publication"], f["design"], tuple(sorted(f.get("cohorts") or [])))
        for f in data["findings"]
    ]
    unique = {
        f["id"]
        for f, k in zip(data["findings"], keys, strict=True)
        if keys.count(k) == 1 and f.get("approved_by") and f.get("verified_by")
    }
    lines = text.splitlines(keepends=True)
    current = ""
    for index, line in enumerate(lines):
        if line.startswith("  - id: "):
            current = line.removeprefix("  - id: ").strip()
        if (
            current in unique
            and line.startswith("    extracted_by: ")
            and lines[index + 1].startswith("    verified_by: ")
        ):
            return index + 1, current, lines[index + 1].removeprefix("    verified_by: ").strip()
    raise AssertionError("no approved, unambiguous finding has verified_by after extracted_by")


def test_mark_verified_restores_a_removed_mark_on_the_real_tbx5_byte_for_byte(
    atlas_copy: Path,
) -> None:
    """The phase's acceptance point: curator's one fact-writing field round-trips the real file.

    One approved finding loses its ``verified_by`` line; the unchanged findings are fed back
    as a blind pass; ``diff --mark-verified`` restores exactly that line. The pass holds
    only the findings that carried a verified_by originally: the others (those with none)
    agree with themselves too, and marking them would add lines the original does not have.
    """
    path = atlas_copy / "curation" / "literature" / "TBX5.yaml"
    original = path.read_text()
    index, finding_id, verifier = _tbx5_target(original)
    lines = original.splitlines(keepends=True)
    del lines[index]
    path.write_text("".join(lines))
    assert path.read_text() != original

    findings = [f for f in YAML(typ="safe").load(original)["findings"] if f.get("verified_by")]
    second = atlas_copy / "second-pass.yaml"
    with second.open("w") as handle:
        YAML(typ="safe").dump({"findings": findings}, handle)

    result = runner.invoke(
        app,
        [
            "diff",
            "TBX5",
            "--against",
            str(second),
            "--mark-verified",
            verifier,
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert f"marked verified (1): {finding_id}\n" in result.stdout
    assert path.read_text() == original
    run = _atlas("validate", "--root", str(atlas_copy))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "LIT015" not in run.stdout + run.stderr


_VALIDATE_SCRIPT = """
import json, sys
from chd_atlas.models.gene_literature import Finding, TriageEntry
data = json.load(sys.stdin)
for record in data["findings"]:
    Finding.model_validate(record)
for record in data["triage"]:
    TriageEntry.model_validate(record)
"""


def _through_the_atlas_models(
    findings: list[Any], triage: list[Any]
) -> subprocess.CompletedProcess[str]:
    """The atlas's own interpreter validates the records; curator imports nothing of it."""
    return subprocess.run(
        [os.environ["CHD_ATLAS_PYTHON"], "-c", _VALIDATE_SCRIPT],
        input=json.dumps({"findings": findings, "triage": triage}),
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_skill_examples_validate_through_the_atlas_models() -> None:
    findings, triage = skill_examples()
    assert findings and triage
    good = _through_the_atlas_models(findings, triage)
    assert good.returncode == 0, good.stdout + good.stderr
    # Without this, a script that validated nothing would also exit 0.
    broken = {**findings[0], "design": "case_report", "screened": 5}
    bad = _through_the_atlas_models([broken], [])
    assert bad.returncode != 0
    assert "ValidationError" in bad.stderr


def _names_option(text: str, option: str) -> bool:
    """Whether ``text`` names the whole option, not a prefix of a longer one."""
    return re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", text) is not None


def test_an_option_name_is_matched_whole() -> None:
    assert _names_option("  --on TEXT  Date.\n", "--on") is True
    assert _names_option("  --only TEXT\n  --not-on X\n  x--on\n", "--on") is False


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("\x1b[1;36m-\x1b[0m\x1b[1;36m-against\x1b[0m  TEXT\n", True),
        ("\x1b[1;36m-\x1b[0m\x1b[1;36m-against-all\x1b[0m  TEXT\n", False),
        ("--against\n", True),
    ],
)
def test_a_coloured_split_option_is_recognised_after_stripping(text: str, named: bool) -> None:
    if text.startswith("\x1b[1;36m-\x1b[0m\x1b[1;36m-against "):
        assert _names_option(text, "--against") is False  # unstripped, the split hides it
    assert _names_option(_plain(text), "--against") is named


def test_the_atlas_commands_the_skills_name_exist() -> None:
    review = _atlas("literature", "review", "--help")
    assert review.returncode == 0, review.stdout + review.stderr
    for option in sorted(ATLAS_COMMANDS["literature review"]):
        assert _names_option(review.stdout, option), option
    target = _atlas("literature", "target", "--help")
    assert target.returncode == 0, target.stdout + target.stderr
    frame = _atlas("literature", "frame", "--help")
    assert frame.returncode == 0, frame.stdout + frame.stderr
    for option in sorted(ATLAS_COMMANDS["literature frame"]):
        assert _names_option(frame.stdout, option), option
    questions = _atlas("literature", "questions", "--help")
    assert questions.returncode == 0, questions.stdout + questions.stderr
    for option in sorted(ATLAS_COMMANDS["literature questions"]):
        assert _names_option(questions.stdout, option), option
    validate = _atlas("validate", "--help")
    assert validate.returncode == 0, validate.stdout + validate.stderr


def test_normalise_on_a_copy_of_the_atlas_validates_without_variant_issues(
    atlas_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A placed variant, a refused one keeping the paper's residue (D18) and one whose names
    disagree, written by ``normalise`` into the real TBX5 file, raise no LIT016-LIT025.

    VariantValidator answers from two real fixtures; the atlas is the real atlas. The new
    finding is a copy of an existing one with its approval removed and three variants added.
    """
    file = atlas_copy / "curation" / "literature" / "TBX5.yaml"
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    registry = atlas_copy / "curation" / ".id_registry.yaml"
    counter = re.search(r"^  LIT: (\d+)$", registry.read_text(), re.M)
    assert counter is not None
    next_number = int(counter[1]) + 1
    registry.write_text(registry.read_text().replace(counter[0], f"  LIT: {next_number}"))
    new_id = f"CHDA:LIT:{next_number:07d}"
    assert new_id not in "".join(p.read_text() for p in file.parent.glob("*.yaml"))
    data = yaml.load(file.read_text())
    source = next(f for f in data["findings"] if f["id"] == "CHDA:LIT:0000027")
    new = copy.deepcopy(source)
    new["id"] = new_id
    for name in ("approved_by", "approved_on", "approved_digest"):
        del new[name]
    new["design"] = "cohort_screen"
    new["screened"] = 3
    new["carriers"] = {"de_novo": 3, "inherited": 0, "origin_unknown": 0}

    def named(number: int, c: str, p: str) -> dict[str, Any]:
        return {
            "id": number,
            "as_reported": f"{c} ({p})",
            "locator": {"kind": "in_text", "element": "Table 2"},
            "kind": "snv_indel",
            "placement": "mane",
            "reported": {"c": c, "p": p},
            "carriers": {"de_novo": 1, "inherited": 0, "origin_unknown": 0},
        }

    new["variants"] = [
        named(1, "c.835C>T", "p.R279X"),
        named(2, "c.998G>T", "p.D111Y"),
        named(3, "c.835C>T", "p.R279Q"),
    ]
    data["findings"].append(new)
    with file.open("w") as handle:
        yaml.dump(data, handle)

    fixtures = Path(__file__).parent / "fixtures" / "variantvalidator"
    answers = {
        vv.url("NM_181486.4:c.835C>T", "NM_181486.4"): (fixtures / "tbx5_c835.json").read_bytes(),
        vv.url("NM_181486.4:c.998G>T", "NM_181486.4"): (
            fixtures / "tbx5_c998_warning.json"
        ).read_bytes(),
    }
    monkeypatch.setattr(cli, "_vv_fetch", lambda u: answers[u])

    result = runner.invoke(
        app,
        [
            "normalise",
            "TBX5",
            "--root",
            str(atlas_copy),
            "--atlas-cli",
            os.environ["CHD_ATLAS_CLI"],
        ],
    )

    assert result.exit_code == 0, result.stdout
    run = _atlas("validate", "--root", str(atlas_copy))
    report = run.stdout + run.stderr
    assert run.returncode == 0, report
    assert not re.search(r"LIT0(1[6-9]|2[0-5])", report), report


def _append_findings(atlas_copy: Path, make: Any) -> None:
    """Append unapproved findings, built by ``make(source, new_id)``, to the copy's TBX5 file.

    ``source`` is an approved finding to copy; ``new_id(i)`` is the i-th unused id, with the
    registry counter moved past it, so validate sees a well-formed file and nothing else wrong.
    """
    file = atlas_copy / "curation" / "literature" / "TBX5.yaml"
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    registry = atlas_copy / "curation" / ".id_registry.yaml"
    counter = re.search(r"^  LIT: (\d+)$", registry.read_text(), re.M)
    assert counter is not None
    start = int(counter[1])
    data = yaml.load(file.read_text())
    source = next(f for f in data["findings"] if f["id"] == "CHDA:LIT:0000027")
    made = make(source, lambda i: f"CHDA:LIT:{start + i:07d}")
    data["findings"].extend(made)
    registry.write_text(registry.read_text().replace(counter[0], f"  LIT: {start + len(made)}"))
    with file.open("w") as handle:
        yaml.dump(data, handle)


def _errors(atlas_copy: Path) -> list[tuple[str, str]]:
    """``(code, message)`` of every error validate reports on the copy, with its place cut off."""
    run = _atlas("validate", "--root", str(atlas_copy))
    out = []
    for line in (run.stdout + run.stderr).splitlines():
        found = re.match(
            r"ERROR \[(\w+)\] (?:\S+:finding CHDA:LIT:\d+(?::variant \d+)?: )?(.*)$", line
        )
        if found:
            out.append((found[1], found[2]))
    return sorted(out)


def test_the_errors_validate_reports_before_normalise_are_those_extract_step_12_lists(
    atlas_copy: Path,
) -> None:
    """The skills' own example findings, written as an agent would before `normalise`.

    Measured against atlas 6bb9bee: every `mane` variant fails the three LIT022 messages, and
    LIT025 as well when the paper's `p.` names a residue. The first draft of step 12 listed the
    three and not LIT025, so an agent following it would have written an `hgvs_p` itself.
    """
    examples, _ = skill_examples()

    def make(source: Any, new_id: Any) -> list[Any]:
        out = []
        for number, example in enumerate(examples, start=1):
            finding = copy.deepcopy(example)
            finding["id"] = new_id(number)
            finding["publication"] = source["publication"]
            out.append(finding)
        return out

    _append_findings(atlas_copy, make)
    mane = [v for e in examples for v in e.get("variants", []) if v["placement"] == "mane"]
    assert len(mane) >= 2 and any("p" in v["reported"] for v in mane)
    expected = sorted(
        [
            ("LIT022", f"a mane variant needs {name}")
            for v in mane
            for name in ("grch38", "hgvs_c", "normalisation")
        ]
        + [
            ("LIT025", f"the paper names {v['reported']['p']} but the variant has no hgvs_p")
            for v in mane
            if "p" in v["reported"]
        ]
    )
    assert _errors(atlas_copy) == expected
    # Step 12 states every one of them, with the paper's own `p.` written `<p.>`.
    text = " ".join((REPO / "skills" / "literature-extract" / "SKILL.md").read_text().split())
    step = text[text.index("12. Run") : text.index("13. After")]
    for _code, message in sorted(set(expected)):
        shown = re.sub(r"the paper names \S+ but", "the paper names <p.> but", message)
        assert f'"{shown}"' in step, shown


def _variant(number: int, as_reported: str, placement: str, **fields: Any) -> dict[str, Any]:
    kind = fields.pop("kind", "snv_indel")
    return {
        "id": number,
        "as_reported": as_reported,
        "locator": {"kind": "in_text", "element": "Table 1"},
        "kind": kind,
        "placement": placement,
        "reported": fields.pop("reported", {}),
        "zygosity": "heterozygous",
        **fields,
    }


def test_the_placement_rules_extract_step_5_states_validate_clean_and_the_carrier_rule_refuses(
    atlas_copy: Path,
) -> None:
    """Each form step 5 teaches, written as it says, measured against atlas 6bb9bee.

    One finding per rule, so a refusal names its own. The last finding breaks the carrier rule
    step 5 states (two unlinked variants of one de novo carrier each, in a finding with one) and
    is the only one validate may refuse.
    """
    one = {"de_novo": 0, "inherited": 0, "origin_unknown": 1}
    de_novo = {"de_novo": 1, "inherited": 0, "origin_unknown": 0}
    mark = {"review_status": "pending_review", "review_note": "stated mismatch"}
    cases: list[tuple[list[dict[str, Any]], str, dict[str, Any]]] = [
        # protein: the three-letter form on the MANE protein, in parentheses.
        (
            [
                _variant(
                    1,
                    "p.R279X",
                    "protein",
                    reported={"p": "p.R279X"},
                    hgvs_p="NP_852259.1:p.(Arg279Ter)",
                    carriers=one,
                )
            ],
            "case_report",
            {"screened": 1, "carriers": one},
        ),
        # a protein variant whose residue fails: unplaced, no hgvs_p, settler authors.
        (
            [
                _variant(
                    1,
                    "p.D111Y",
                    "unplaced",
                    reported={"p": "p.D111Y"},
                    review_settled_by="authors",
                    carriers=one,
                    **mark,
                )
            ],
            "case_report",
            {"screened": 1, "carriers": one},
        ),
        # an rsID alone inside the exons, and a g. name alone: unplaced, settler atlas_model.
        (
            [
                _variant(
                    1,
                    "rs3825214",
                    "unplaced",
                    reported={"rsid": "rs3825214"},
                    review_settled_by="atlas_model",
                    carriers=one,
                    **mark,
                ),
                _variant(
                    2,
                    "g.114366312G>A",
                    "unplaced",
                    review_settled_by="atlas_model",
                    carriers=one,
                    **mark,
                ),
            ],
            "cohort_screen",
            {"screened": 10, "carriers": {"de_novo": 0, "inherited": 0, "origin_unknown": 2}},
        ),
        # a c. with an intronic offset beyond 50, and a CNV written in c. notation.
        (
            [
                _variant(
                    1,
                    "c.1234+51A>G",
                    "outside_region",
                    reported={"c": "c.1234+51A>G"},
                    carriers=one,
                ),
                _variant(
                    2,
                    "c.-200_*300del",
                    "structural",
                    kind="cnv",
                    reported={"c": "c.-200_*300del"},
                    carriers=one,
                ),
            ],
            "cohort_screen",
            {
                "screened": 10,
                "carriers": {"de_novo": 0, "inherited": 0, "origin_unknown": 2},
                "variant_classes": ["snv_indel", "cnv"],
            },
        ),
        # a common-variant finding names the variant it tested and carries no carriers.
        (
            [_variant(1, "rs3825214", "outside_region", reported={"rsid": "rs3825214"})],
            "common_variant_association",
            {"cases": 500, "controls": 5000, "p_value": 0.01, "odds_ratio": 1.3},
        ),
    ]
    broken = (
        [
            _variant(1, "c.835C>T", "outside_region", reported={"c": "c.835C>T"}, carriers=de_novo),
            _variant(2, "c.512A>G", "outside_region", reported={"c": "c.512A>G"}, carriers=de_novo),
        ],
        "cohort_screen",
        {"screened": 10, "carriers": {"de_novo": 1, "inherited": 0, "origin_unknown": 1}},
    )

    def make(source: Any, new_id: Any) -> list[Any]:
        out = []
        for number, (variants, design, extra) in enumerate([*cases, broken], start=1):
            finding = copy.deepcopy(source)
            for name in ("approved_by", "approved_on", "approved_digest", "verified_by"):
                finding.pop(name, None)
            for name in ("count_unit", "screened", "carriers", "cases", "controls", "comparator"):
                finding.pop(name, None)
            fields: dict[str, Any] = {"variant_classes": ["snv_indel"]}
            if design != "common_variant_association":
                fields["count_unit"] = "probands"
            fields.update(json.loads(json.dumps(extra)))
            # A round trip, so no dict is shared: ruamel would dump a shared one as an alias.
            variants = json.loads(json.dumps(variants))
            finding.update(id=new_id(number), design=design, variants=variants, **fields)
            out.append(finding)
        return out

    _append_findings(atlas_copy, make)
    assert _errors(atlas_copy) == [
        ("LIT023", "variants name at least 2 de_novo carrier(s), more than the finding's 1")
    ]
