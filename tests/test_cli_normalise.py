"""``chd-curator normalise``, with a stub atlas, the atlas's real exported schemas and
VariantValidator answered from fixtures (taken 2026-11-17): TBX5 c.835C>T places, c.998G>T
is refused for a reference mismatch. The network is never reached."""

from __future__ import annotations

import datetime
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner, Result

from chd_curator import cli
from chd_curator.atlas import Frame
from chd_curator.cli import app
from chd_curator.variantvalidator import VariantValidatorError, url

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures" / "variantvalidator"
TBX5 = "NM_181486.4"

HEAD = """\
# Curator's head comment -- must survive every write.
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
findings:
  - id: CHDA:LIT:0000001
    publication: PMID:8988165
    design: cohort_screen
    methods:
      - exome
    count_unit: probands
    screened: 7
    carriers:
      de_novo: 2
      inherited: 1
      origin_unknown: 0
    variant_classes:
      - snv_indel
    variants:
"""
TAIL = """\
    phenotypes:
      - HP:0001631
    lesion_groups:
      - septal
    syndromic: both
    shares_probands_with: unknown
    locators:
      - kind: in_text
        element: Table 2
    summary: Three of seven probands carry a variant.
    extracted_by: agent:extract/test
    verified_by: agent:verify/test
    approved_by: E.A.M
    approved_on: 2026-11-03
    approved_digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
"""


def variant(number: int, c: str | None, p: str | None, **more: str) -> str:
    """One `mane` variant as the YAML a curator writes, naming `c` and `p` as the paper did."""
    reported = [f"          c: {c}"] if c else []
    reported += [f"          p: {p}"] if p else []
    reported += [f"          {k}: {v}" for k, v in more.items()]
    return (
        f"      - id: {number}\n"
        f"        as_reported: variant {number}\n"
        "        locator:\n          kind: in_text\n          element: Table 2\n"
        "        kind: snv_indel\n"
        "        placement: mane\n"
        "        reported:\n" + "\n".join(reported) + "\n"
        "        carriers: {de_novo: 1, inherited: 0, origin_unknown: 0}\n"
    )


def literature(*variants: str) -> str:
    return HEAD + "".join(variants) + TAIL


def fixture_for(descriptions: dict[str, str]) -> Callable[[str], bytes]:
    """A fetch answering each description from the named fixture, and recording what it saw."""
    by_url = {url(d, TBX5): (FIXTURES / name).read_bytes() for d, name in descriptions.items()}
    asked: list[str] = []

    def fetch(request_url: str) -> bytes:
        asked.append(request_url)
        if request_url not in by_url:
            raise AssertionError(f"unexpected request {request_url}")
        return by_url[request_url]

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


C835 = "tbx5_c835.json"
C998 = "tbx5_c998_warning.json"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "curation" / "literature").mkdir(parents=True)
    (tmp_path / "schemas").symlink_to(Path(os.environ["CHD_ATLAS_ROOT"]) / "schemas")
    return tmp_path


def _write(root: Path, text: str) -> Path:
    path = root / "curation" / "literature" / "TBX5.yaml"
    path.write_text(text)
    return path


def _normalise(stub: Path, root: Path) -> Result:
    return runner.invoke(app, ["normalise", "TBX5", "--root", str(root), "--atlas-cli", str(stub)])


def _variants(path: Path) -> dict[int, dict[str, Any]]:
    data = YAML(typ="safe").load(path.read_text())
    return {v["id"]: v for v in data["findings"][0]["variants"]}


def test_normalise_places_one_variant_and_marks_another_unplaced_with_the_measured_note(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        root,
        literature(
            variant(1, "c.835C>T", "p.R279X"),
            variant(2, "c.998G>T", "p.D111Y"),
        ),
    )
    fetch = fixture_for({f"{TBX5}:c.835C>T": C835, f"{TBX5}:c.998G>T": C998})
    monkeypatch.setattr(cli, "_vv_fetch", fetch)

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    data = YAML(typ="safe").load(path.read_text())
    one, two = _variants(path)[1], _variants(path)[2]
    assert one["placement"] == "mane"
    assert one["hgvs_c"] == "NM_181486.4:c.835C>T"
    assert one["hgvs_p"] == "NP_852259.1:p.(Arg279Ter)"
    assert one["grch38"] == "12:114366312:G:A"
    assert one["normalisation"] == 1
    assert "review_status" not in one
    # D18: refused, so unplaced and pending review, quoting VariantValidator's first warning;
    # the paper's residue is kept in the three-letter form the atlas checks (LIT019).
    assert two["placement"] == "unplaced"
    assert two["review_status"] == "pending_review"
    assert two["review_note"] == (
        "VariantValidator refused NM_181486.4:c.998G>T: ReferenceMismatchError: "
        "NM_181486.4:c.998G>T: Variant reference (G) does not agree with reference sequence (C)"
    )
    assert two["hgvs_p"] == "NP_852259.1:p.(Asp111Tyr)"
    assert "hgvs_c" not in two and "grch38" not in two
    assert two["normalisation"] == 1
    assert data["normalisations"] == [
        {
            "id": 1,
            "tool": "variantvalidator",
            "version": "4.0.1.dev7+gbdab9c72f",
            "hgvs_version": "4.0.0",
            "vvta": "vvta_2025_02",
            "vvdb": "vvdb_2025_3",
            "seqrepo": "VV_SR_2025_02/master",
            "run_on": datetime.date.today(),
        }
    ]
    assert list(data) == ["gene", "searches", "normalisations", "triage", "findings"]
    assert path.read_text().startswith("# Curator's head comment")
    assert "variant 2 (variant 2): unplaced: ReferenceMismatchError" in result.stdout
    assert "kept the paper's NP_852259.1:p.(Asp111Tyr), run chd-atlas validate" in result.stdout
    assert len(fetch.asked) == 2  # type: ignore[attr-defined]


def _other_accession() -> Callable[[str], bytes]:
    answer = json.loads((FIXTURES / C835).read_text())
    entry = answer[f"{TBX5}:c.835C>T"]["hgvs_predicted_protein_consequence"]
    entry["tlr"] = "NP_852259.2:p.(Arg279Ter)"
    return lambda u: json.dumps(answer).encode()


@pytest.mark.parametrize(
    ("c", "p", "fetch"),
    [
        # Refused by VariantValidator.
        ("c.998G>T", "p.D111Y", fixture_for({f"{TBX5}:c.998G>T": C998})),
        # Placed, but the predicted p. disagrees with the paper's.
        ("c.835C>T", "p.R279Q", fixture_for({f"{TBX5}:c.835C>T": C835})),
        # Placed, but on another protein accession than the pinned MANE one.
        ("c.835C>T", "p.R279X", _other_accession()),
    ],
    ids=["refused", "p_disagrees", "other_accession"],
)
def test_normalise_names_an_expert_as_the_settler_of_every_mark_it_writes(
    stub_atlas: Path,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    c: str,
    p: str,
    fetch: Callable[[str], bytes],
) -> None:
    """R-U6: the tool cannot tell a paper error from a tool or frame limit, so it names the
    one settler that is always true. The atlas refuses a pending_review without one; these are
    the three places `_decide` writes a mark. A placed variant carries no mark and no settler."""
    path = _write(root, literature(variant(1, c, p)))
    monkeypatch.setattr(cli, "_vv_fetch", fetch)

    assert _normalise(stub_atlas, root).exit_code == 0

    one = _variants(path)[1]
    assert one["review_status"] == "pending_review"
    assert one["review_settled_by"] == "expert"


def test_a_placed_variant_carries_no_settler(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(root, literature(variant(1, "c.835C>T", "p.R279X")))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({f"{TBX5}:c.835C>T": C835}))

    assert _normalise(stub_atlas, root).exit_code == 0

    assert "review_settled_by" not in _variants(path)[1]


def test_normalise_keeps_a_settler_a_curator_already_wrote(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = literature(variant(1, "c.998G>T", "p.D111Y")).replace(
        "        placement: mane\n",
        "        placement: mane\n"
        "        review_status: pending_review\n"
        "        review_note: the curator's own question\n"
        "        review_settled_by: authors\n",
    )
    path = _write(root, text)
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({f"{TBX5}:c.998G>T": C998}))

    assert _normalise(stub_atlas, root).exit_code == 0

    one = _variants(path)[1]
    assert one["review_status"] == "pending_review"
    assert one["review_settled_by"] == "authors"


def test_a_placed_variant_whose_p_disagrees_is_unplaced_naming_both_forms(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(root, literature(variant(1, "c.835C>T", "p.R279Q")))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({f"{TBX5}:c.835C>T": C835}))

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    one = _variants(path)[1]
    assert one["placement"] == "unplaced"
    assert one["review_status"] == "pending_review"
    assert "p.R279Q" in one["review_note"] and "NP_852259.1:p.(Arg279Ter)" in one["review_note"]
    assert not {"hgvs_c", "hgvs_p", "grch38"} & set(one)
    assert one["normalisation"] == 1


@pytest.mark.parametrize(
    ("reported_p", "extra", "tlr", "placement", "hgvs_p"),
    [
        ("p.R279X", {}, None, "mane", "NP_852259.1:p.(Arg279Ter)"),
        (None, {}, None, "mane", "NP_852259.1:p.(Arg279Ter)"),
        # D6: a VariantValidator p. naming no residue has nothing to compare, so it is exempt ...
        ("p.R279X", {}, "NP_852259.1:p.?", "mane", "NP_852259.1:p.?"),
        ("p.R279X", {}, "NP_852259.1:p.(=)", "mane", "NP_852259.1:p.(=)"),
        # D6': the same on the paper's side: a p. naming no residue has nothing to compare.
        ("p.?", {}, None, "mane", "NP_852259.1:p.(Arg279Ter)"),
        ("p.=", {}, None, "mane", "NP_852259.1:p.(Arg279Ter)"),
        ("p.0", {}, None, "mane", "NP_852259.1:p.(Arg279Ter)"),
        ("p.(?)", {}, None, "mane", "NP_852259.1:p.(Arg279Ter)"),
        ("p.R279Q", {}, None, "unplaced", None),
        # An absent VariantValidator p. is no exemption when the paper names a residue.
        ("p.R279X", {}, "", "unplaced", None),
        ("p.?", {}, "", "mane", None),
        # D5': a frameshift naming no new residue still differs from a stop gain.
        ("p.R279fs", {}, None, "unplaced", None),
    ],
)
def test_the_agree_rule_exempts_what_the_atlas_exempts(
    stub_atlas: Path,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_p: str | None,
    extra: dict[str, str],
    tlr: str | None,
    placement: str,
    hgvs_p: str | None,
) -> None:
    path = _write(root, literature(variant(1, "c.835C>T", reported_p, **extra)))
    description = f"{extra.get('transcript') or TBX5}:c.835C>T"
    answer = json.loads((FIXTURES / C835).read_text())
    if tlr is not None:
        answer[f"{TBX5}:c.835C>T"]["hgvs_predicted_protein_consequence"]["tlr"] = tlr
    monkeypatch.setattr(
        cli,
        "_vv_fetch",
        lambda u: json.dumps(answer).encode() if u == url(description, TBX5) else b"",
    )

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    one = _variants(path)[1]
    assert one["placement"] == placement
    assert one.get("hgvs_p") == hgvs_p


def test_a_refused_variant_on_another_transcript_keeps_no_hgvs_p(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D18's gate: the paper's residue number is kept only where the paper numbered on MANE."""
    path = _write(
        root,
        literature(
            variant(1, "c.998G>T", "p.D111Y", transcript="NM_000192.3"),
            variant(2, "c.998G>T", "p.D111Y", transcript="NM_181486.5"),
            variant(3, "c.998G>T", "p.?"),
        ),
    )
    answer = (FIXTURES / C998).read_bytes()
    monkeypatch.setattr(cli, "_vv_fetch", lambda u: answer)

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    got = _variants(path)
    assert [got[i]["placement"] for i in (1, 2, 3)] == ["unplaced"] * 3
    assert "hgvs_p" not in got[1]
    assert got[2]["hgvs_p"] == "NP_852259.1:p.(Asp111Tyr)"  # a version-less match counts
    assert "hgvs_p" not in got[3]  # names no residue: nothing to keep


def test_two_variants_normalising_to_one_key_write_nothing_and_exit_2(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D19: the atlas would refuse this file (LIT022), so normalise must not write it."""
    text = literature(variant(1, "c.835C>T", "p.R279X"), variant(2, "c.835C>T", "p.R279X"))
    path = _write(root, text)
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({f"{TBX5}:c.835C>T": C835}))

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 2
    assert "variants 1 and 2 both name 12:114366312:G:A" in result.stdout
    assert path.read_text() == text


def test_the_same_key_with_different_partners_is_allowed(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D16': a repeated key is fine when the `with` partner sets differ."""
    second = variant(2, "c.835C>T", "p.R279X").replace(
        "        carriers:", "        with: [3]\n        carriers:"
    )
    third = variant(3, "c.998G>T", "p.D111Y").replace(
        "        carriers:", "        with: [2]\n        carriers:"
    )
    path = _write(root, literature(variant(1, "c.835C>T", "p.R279X"), second, third))
    monkeypatch.setattr(
        cli, "_vv_fetch", fixture_for({f"{TBX5}:c.835C>T": C835, f"{TBX5}:c.998G>T": C998})
    )

    assert _normalise(stub_atlas, root).exit_code == 0
    got = _variants(path)
    assert got[1]["grch38"] == got[2]["grch38"] == "12:114366312:G:A"


def test_normalise_leaves_approval_and_verified_by_untouched(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = literature(variant(1, "c.835C>T", "p.R279X"))
    path = _write(root, before)
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({f"{TBX5}:c.835C>T": C835}))

    assert _normalise(stub_atlas, root).exit_code == 0

    finding = YAML(typ="safe").load(path.read_text())["findings"][0]
    assert finding["approved_by"] == "E.A.M"
    assert finding["approved_on"] == datetime.date(2026, 11, 3)
    assert finding["approved_digest"] == "sha256:" + "0" * 64
    assert finding["verified_by"] == "agent:verify/test"
    lines = path.read_text().splitlines()
    assert [ln for ln in lines if "approved_" in ln or "verified_by" in ln] == [
        ln for ln in before.splitlines() if "approved_" in ln or "verified_by" in ln
    ]


def test_a_network_failure_writes_nothing_and_exits_1(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = literature(variant(1, "c.835C>T", "p.R279X"), variant(2, "c.998G>T", "p.D111Y"))
    path = _write(root, text)
    good = (FIXTURES / C835).read_bytes()

    def fetch(request_url: str) -> bytes:
        if "c.998" in request_url or "c.998" in request_url.replace("%3A", ":"):
            raise VariantValidatorError("could not reach VariantValidator: URLError")
        return good

    monkeypatch.setattr(cli, "_vv_fetch", fetch)

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 1
    assert "could not reach VariantValidator" in result.stdout
    assert path.read_text() == text


def test_nothing_to_normalise_makes_no_request_and_writes_nothing(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = variant(1, "c.835C>T", "p.R279X").replace(
        "        carriers:", "        normalisation: 1\n        carriers:"
    )
    no_c = variant(2, None, "p.R279X")
    text = literature(done, no_c)
    path = _write(root, text)

    def fetch(_url: str) -> bytes:
        raise AssertionError("no request expected")

    monkeypatch.setattr(cli, "_vv_fetch", fetch)

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 0
    assert path.read_text() == text


def test_a_changed_file_is_refused_with_exit_2(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = literature(variant(1, "c.835C>T", "p.R279X"))
    path = _write(root, text)
    edited = text.replace("Table 2\n    summary", "Table 3\n    summary")
    good = (FIXTURES / C835).read_bytes()

    def fetch(_url: str) -> bytes:
        path.write_text(edited)  # an edit lands while VariantValidator is being asked
        return good

    monkeypatch.setattr(cli, "_vv_fetch", fetch)

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 2
    assert "changed since it was read" in result.stdout
    assert path.read_text() == edited


def test_bad_arguments_exit_2(stub_atlas: Path, root: Path) -> None:
    assert _normalise(stub_atlas, root).exit_code == 2  # no file
    result = runner.invoke(
        app, ["normalise", "NOPE", "--root", str(root), "--atlas-cli", str(stub_atlas)]
    )
    assert result.exit_code == 2
    assert "not a known gene symbol" in result.stdout
    result = runner.invoke(
        app, ["normalise", "BRCA1", "--root", str(root), "--atlas-cli", str(stub_atlas)]
    )
    assert result.exit_code == 2  # the stub's frame refuses a gene with no MANE Select


def test_a_second_run_appends_its_own_normalisation_and_keeps_the_first(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = literature(variant(1, "c.835C>T", "p.R279X"))
    text = text.replace(
        "triage:",
        "normalisations:\n  - id: 4\n    tool: variantvalidator\n    version: v0\n"
        "    hgvs_version: h0\n    vvta: t0\n    vvdb: d0\n    seqrepo: s0\n"
        "    run_on: 2026-11-01\n"
        "triage:",
    )
    path = _write(root, text)
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({f"{TBX5}:c.835C>T": C835}))

    assert _normalise(stub_atlas, root).exit_code == 0

    data = YAML(typ="safe").load(path.read_text())
    assert [n["id"] for n in data["normalisations"]] == [4, 5]
    assert data["normalisations"][0]["version"] == "v0"
    assert _variants(path)[1]["normalisation"] == 5


def test_an_approved_finding_it_changes_is_reported_void_in_one_literal_line(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(root, literature(variant(1, "c.835C>T", "p.R279X")))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({f"{TBX5}:c.835C>T": C835}))

    # Typed as the gene's previous symbol, the command must still name the approved one.
    result = runner.invoke(
        app, ["normalise", "HOS", "--root", str(root), "--atlas-cli", str(stub_atlas)]
    )

    assert result.exit_code == 0
    assert (
        "CHDA:LIT:0000001: its approval is now void, because its variants changed; re-approve "
        "it with: chd-atlas literature review TBX5 --approve CHDA:LIT:0000001 --by <name>\n"
    ) in result.stdout
    # An unapproved finding draws no such line.
    path.write_text(
        literature(variant(1, "c.835C>T", "p.R279X"))
        .replace("    approved_by: E.A.M\n", "")
        .replace("    approved_on: 2026-11-03\n", "")
        .replace("    approved_digest: sha256:" + "0" * 64 + "\n", "")
    )
    again = _normalise(stub_atlas, root)
    assert again.exit_code == 0 and "approval is now void" not in again.stdout


def linked(
    number: int,
    c: str,
    *,
    as_reported: str | None = None,
    p: str | None = None,
    with_: list[int] | None = None,
    phase: str = "trans",
    kind: str = "snv_indel",
) -> str:
    """A variant written compactly, optionally linked to partners by `with`."""
    reported = f"{{c: '{c}'" + (f", p: '{p}'" if p else "") + "}"
    text = (
        f"      - id: {number}\n        as_reported: '{as_reported or c}'\n"
        "        locator: {kind: in_text, element: Table 2}\n"
        f"        kind: {kind}\n        placement: mane\n        reported: {reported}\n"
    )
    if with_ is not None:
        text += f"        zygosity: heterozygous\n        with: {with_}\n        phase: {phase}\n"
    return text + "        carriers: {de_novo: 1, inherited: 0, origin_unknown: 0}\n"


BOTH = {f"{TBX5}:c.835C>T": C835, f"{TBX5}:c.998G>T": C998}


@pytest.mark.parametrize(
    "variants",
    [
        # S1: one trans pair written twice under different ids; the partners are named alike.
        [
            linked(1, "c.835C>T", with_=[3]),
            linked(2, "c.835C>T", with_=[4]),
            linked(3, "c.998G>T", with_=[1]),
            linked(4, "c.998G>T", with_=[2]),
        ],
        # S2: one change twice inside one `with` pair, which is in effect homozygous.
        [
            linked(1, "c.835C>T", as_reported="a", with_=[2]),
            linked(2, "c.835C>T", as_reported="b", with_=[1]),
        ],
        # One change twice in one group whose contexts differ (a chain): only the group rule
        # catches it, because 1's partners are {x} and 3's are {x, y}.
        [
            linked(1, "c.835C>T", as_reported="a", with_=[2]),
            linked(2, "c.998G>T", as_reported="x", with_=[1, 3]),
            linked(3, "c.835C>T", as_reported="b", with_=[2, 4]),
            linked(4, "c.998G>T", as_reported="y", with_=[3]),
        ],
        # The same key twice with no partner at all.
        [linked(1, "c.835C>T", as_reported="a"), linked(2, "c.835C>T", as_reported="b")],
    ],
    ids=["same-pair-twice", "one-change-twice-in-a-pair", "chain-in-one-group", "plain-twice"],
)
def test_the_atlas_d16_prime_duplicates_write_nothing_and_exit_2(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch, variants: list[str]
) -> None:
    text = literature(*variants)
    path = _write(root, text)
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for(BOTH))

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 2, result.stdout
    assert "one genotype context" in result.stdout
    assert path.read_text() == text


@pytest.mark.parametrize(
    "variants",
    [
        # Partners named differently (as_reported of an unplaced partner) are different contexts.
        [
            linked(1, "c.835C>T", with_=[3]),
            linked(2, "c.835C>T", with_=[4]),
            linked(3, "c.998G>T", as_reported="A", with_=[1]),
            linked(4, "c.998G>T", as_reported="B", with_=[2]),
        ],
        # So are different phases.
        [
            linked(1, "c.835C>T", with_=[3], phase="trans"),
            linked(2, "c.835C>T", with_=[4], phase="cis"),
            linked(3, "c.998G>T", with_=[1], phase="trans"),
            linked(4, "c.998G>T", with_=[2], phase="cis"),
        ],
    ],
    ids=["different-partner-names", "different-phase"],
)
def test_distinct_contexts_are_written(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch, variants: list[str]
) -> None:
    path = _write(root, literature(*variants))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for(BOTH))

    assert _normalise(stub_atlas, root).exit_code == 0
    assert _variants(path)[1]["grch38"] == _variants(path)[2]["grch38"]


def test_a_bare_question_mark_is_a_p_naming_a_residue_like_the_atlas_reads_it(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6: `?` is not a `p.` description, the atlas's parser rejects it and reads that as
    "names a residue", so it disagrees with Arg279Ter and the variant is unplaced."""
    path = _write(root, literature(linked(1, "c.835C>T", p="?")))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for(BOTH))

    assert _normalise(stub_atlas, root).exit_code == 0
    assert _variants(path)[1]["placement"] == "unplaced"


def test_a_count_less_frameshift_is_kept_in_a_form_the_atlas_reads(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(root, literature(linked(1, "c.998G>T", p="p.D111fsX")))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for(BOTH))

    assert _normalise(stub_atlas, root).exit_code == 0
    assert _variants(path)[1]["hgvs_p"] == "NP_852259.1:p.(Asp111fs)"


def test_a_protein_on_another_accession_is_unplaced_naming_both(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(root, literature(linked(1, "c.835C>T", p="p.R279X")))
    answer = json.loads((FIXTURES / C835).read_text())
    entry = answer[f"{TBX5}:c.835C>T"]["hgvs_predicted_protein_consequence"]
    entry["tlr"] = "NP_852259.2:p.(Arg279Ter)"
    monkeypatch.setattr(cli, "_vv_fetch", lambda u: json.dumps(answer).encode())

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 0
    one = _variants(path)[1]
    assert one["placement"] == "unplaced" and one["review_status"] == "pending_review"
    assert "NP_852259.2" in one["review_note"] and "NP_852259.1" in one["review_note"]
    assert not {"hgvs_c", "hgvs_p", "grch38"} & set(one)
    assert "unplaced: VariantValidator's protein is NP_852259.2, not NP_852259.1" in result.stdout


def test_only_snv_indel_variants_are_asked_and_the_rest_are_reported_skipped(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = literature(linked(1, "c.835C>T"), linked(2, "c.998G>T", kind="cnv"))
    path = _write(root, text)
    fetch = fixture_for({f"{TBX5}:c.835C>T": C835})
    monkeypatch.setattr(cli, "_vv_fetch", fetch)

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 0, result.stdout
    assert "variant 2 (c.998G>T): skipped, kind cnv is not snv_indel" in result.stdout
    assert "asked about 1 variant(s)" in result.stdout
    assert "normalisation" not in _variants(path)[2]
    assert len(fetch.asked) == 1  # type: ignore[attr-defined]


def test_a_padded_partner_name_is_the_same_context_as_the_stripped_one(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S8: the atlas strips every string on load, so a partner named `c.998G>T ` and one named
    `c.998G>T` are one context and the pair written twice is refused (exit 2)."""
    text = literature(
        linked(1, "c.835C>T", with_=[3]),
        linked(2, "c.835C>T", with_=[4]),
        linked(3, "c.998G>T", as_reported="c.998G>T ", with_=[1]),
        linked(4, "c.998G>T", with_=[2]),
    )
    path = _write(root, text)
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for(BOTH))

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 2, result.stdout
    assert path.read_text() == text


@pytest.mark.parametrize("padded", [" p.?", "p.? ", " p.R279X ", "\tp.(?)"])
def test_a_padded_paper_p_is_read_stripped_and_untouched_text_stays_as_written(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch, padded: str
) -> None:
    text = literature(linked(1, "c.835C>T", p=padded))
    path = _write(root, text)
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for(BOTH))

    assert _normalise(stub_atlas, root).exit_code == 0
    got = _variants(path)[1]
    assert got["placement"] == "mane" and got["hgvs_p"] == "NP_852259.1:p.(Arg279Ter)"
    assert got["reported"]["p"] == padded  # the paper's own text is not rewritten


def test_a_count_less_stop_loss_is_kept_in_variantvalidators_form(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S9: the atlas reads `ext*?` but rejects a bare `extTer`."""
    path = _write(root, literature(linked(1, "c.998G>T", p="p.*519Qext*")))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for(BOTH))

    assert _normalise(stub_atlas, root).exit_code == 0
    assert _variants(path)[1]["hgvs_p"] == "NP_852259.1:p.(Ter519GlnextTer?)"


def test_when_only_a_skipped_variant_exists_none_was_asked_and_the_skip_is_shown(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S10."""
    path = _write(root, literature(linked(1, "c.835C>T", kind="cnv")))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({}))

    result = _normalise(stub_atlas, root)

    assert result.exit_code == 0
    assert "variant 1 (c.835C>T): skipped, kind cnv is not snv_indel" in result.stdout
    assert "no variant was asked about" in result.stdout
    assert "is placed `mane`" not in result.stdout
    assert "normalisation" not in path.read_text()


def test_a_padded_reported_c_is_asked_stripped_and_the_agree_rule_strips_both_sides(
    stub_atlas: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chd_curator.atlas import Frame

    mane = Frame("HGNC:11604", "TBX5", TBX5, "NP_852259.1")
    assert cli._agrees({"p": " p.? "}, "NP_852259.1:p.(Arg279Ter)", mane)
    assert cli._agrees({"p": "p.R279X"}, " NP_852259.1:p.(Arg279Ter) ", mane)
    assert not cli._agrees({"p": " p.R279Q"}, "NP_852259.1:p.(Arg279Ter)", mane)
    # R-F9: a variant on the paper's own transcript is placed on MANE, so its p. is compared too.
    assert not cli._agrees({"p": "p.R279Q", "transcript": " NM_000192.3 "}, "NP_852259.1:p.X", mane)

    path = _write(root, literature(linked(1, " c.835C>T ")))
    monkeypatch.setattr(cli, "_vv_fetch", fixture_for({f"{TBX5}:c.835C>T": C835}))  # strict URL
    assert _normalise(stub_atlas, root).exit_code == 0
    assert _variants(path)[1]["grch38"] == "12:114366312:G:A"


NR2F2_FRAME = Frame("HGNC:7976", "NR2F2", "NM_021005.4", "NP_066285.1")


def _two_step_fetch(step1: str = "nr2f2_v3_c288.json", step2: str = "nr2f2_genomic_on_mane.json"):  # type: ignore[no-untyped-def]
    paper = "NM_021005.3:c.288C>A"
    by_url = {
        url(paper, "NM_021005.3"): (FIXTURES / step1).read_bytes(),
        url("15-96332393-C-A", NR2F2_FRAME.transcript): (FIXTURES / step2).read_bytes(),
    }
    asked: list[str] = []

    def fetch(request_url: str) -> bytes:
        asked.append(request_url)
        if request_url not in by_url:
            raise AssertionError(f"unexpected request {request_url}")
        return by_url[request_url]

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


def _nr2f2(root: Path, monkeypatch: pytest.MonkeyPatch, p: str | None, fetch: Any) -> Path:
    path = root / "curation" / "literature" / "NR2F2.yaml"
    path.write_text(
        literature(variant(1, "c.288C>A", p, transcript="NM_021005.3")).replace(
            "gene: HGNC:11604", "gene: HGNC:7976"
        )
    )
    monkeypatch.setattr(cli, "frame", lambda *_: NR2F2_FRAME)
    monkeypatch.setattr(cli, "_vv_fetch", fetch)
    result = runner.invoke(
        app, ["normalise", "NR2F2", "--root", str(root), "--atlas-cli", "unused"]
    )
    assert result.exit_code == 0, result.stdout
    return path


def _first_variant(path: Path) -> dict[str, Any]:
    return YAML(typ="safe").load(path.read_text())["findings"][0]["variants"][0]


def test_a_variant_on_the_papers_older_transcript_is_placed_on_mane_and_the_run_recorded(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = _two_step_fetch()
    path = _nr2f2(root, monkeypatch, "p.C96X", fetch)

    one = _first_variant(path)
    assert one["placement"] == "mane"
    assert one["hgvs_c"] == "NM_021005.4:c.288C>A"
    assert one["hgvs_p"] == "NP_066285.1:p.(Cys96Ter)"
    assert one["grch38"] == "15:96332393:C:A"
    assert one["normalisation"] == 1
    assert len(YAML(typ="safe").load(path.read_text())["normalisations"]) == 1
    assert len(fetch.asked) == 2


def test_a_paper_c_naming_another_residue_on_its_older_transcript_is_unplaced(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _nr2f2(root, monkeypatch, "p.C96Y", _two_step_fetch())

    one = _first_variant(path)
    assert one["placement"] == "unplaced" and one["review_status"] == "pending_review"
    assert "p.C96Y" in one["review_note"] and "NP_066285.1:p.(Cys96Ter)" in one["review_note"]
    assert not {"hgvs_c", "hgvs_p", "grch38"} & set(one)
