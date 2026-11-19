"""The review page's data: what a card shows, what it refuses to show, and that it is stable.

Fixture: tests/fixtures/review (run r1; findings 0901 verified with one variant, 0902 judged
with two variants in a ``with`` group, 0903 ``not_accessible``). The stub atlas answers
approved/sound for 0901, unapproved for 0902, approved-but-void for 0903.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from chd_curator.atlas import digests
from chd_curator.review_data import (
    APPROVAL_KEYS,
    ReviewDataError,
    Reviewer,
    build_review_data,
)
from chd_curator.run import RunPaths

FIXTURE = Path(__file__).parent / "fixtures" / "review"
BUILT_ON = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
OWNER = Reviewer("E.A.M", "owner")
LIT = "curation/literature/TBX5.yaml"
J, A, G = "CHDA:LIT:0000902", "CHDA:LIT:0000903", "CHDA:LIT:0000901"


@pytest.fixture
def run(tmp_path: Path) -> RunPaths:
    shutil.copytree(FIXTURE, tmp_path / "root")
    return RunPaths(tmp_path / "root", "TBX5", "r1")


def build(run: RunPaths, stub: Path, reviewer: Reviewer = OWNER, **kw: Any) -> dict[str, Any]:
    return build_review_data(
        run.root,
        run,
        digests(run.root, "TBX5", stub),
        reviewer,
        BUILT_ON,
        "abc1234",
        "0 error(s), 3 warning(s)",
        **kw,
    )


def card(data: dict[str, Any], fid: str) -> dict[str, Any]:
    return next(c for c in data["findings"] if c["id"] == fid)


def row(c: dict[str, Any], name: str) -> dict[str, Any]:
    return next(r for r in c["fields"] if r["name"] == name)


def test_cards_are_ordered_judged_abstract_agreed_and_carry_the_digests_answer(
    run: RunPaths, stub_atlas: Path
) -> None:
    data = build(run, stub_atlas)
    assert [c["id"] for c in data["findings"]] == [J, A, G]
    assert [c["group"] for c in data["findings"]] == ["judged", "abstract", "agreed"]
    assert [g["key"] for g in data["groups"]] == ["judged", "abstract", "agreed"]
    assert {
        c["id"]: (c["shown_digest"][7], c["approval"], c["verified"]) for c in data["findings"]
    } == {
        G: ("2", "approved", True),
        J: ("4", "unapproved", False),
        A: ("6", "void", False),
    }
    assert data["page"] == {
        "gene": "HGNC:11604",
        "symbol": "TBX5",
        "built_on": "2026-11-19T12:00:00+00:00",
        "atlas_commit": "abc1234",
        "literature_sha256": "sha256:" + "a" * 64,
        "validate": "0 error(s), 3 warning(s)",
    }
    assert data["reviewer"] == {"name": "E.A.M", "role": "owner"}


def test_judged_rows_and_variant_decisions_sit_where_they_were_made(
    run: RunPaths, stub_atlas: Path
) -> None:
    c = card(build(run, stub_atlas), J)
    ph = row(c, "phenotypes")
    assert ph["state"] == "judged"
    assert ph["first"] == "HP:0001629 Ventricular septal defect"
    assert ph["verify"].endswith("HP:0001631 Atrial septal defect")
    assert "Phenotypes are the carrier's own lesions" in ph["judge"]
    v1, v2 = c["variants"]
    assert [d["field"] for d in v1["diffs"]] == ["variant c.100C>T, p.(Arg34Ter) phase"]
    assert v1["state"] == "judged" and v2["state"] == "agreed" and v2["diffs"] == []
    # the decision naming a variant the finding lacks is on the finding, not dropped
    absent = row(c, "variant c.999A>G, p.(Lys333Glu) carriers")
    assert absent["state"] == "judged" and absent["value"] == "1 origin unknown"
    # as_reported and whitespace decisions are settled by rule: not shown anywhere
    shown = [r["name"] for r in c["fields"]] + [
        d["field"] for v in c["variants"] for d in v["diffs"]
    ]
    assert not [f for f in shown if f.endswith("as_reported") or "reported.p" in f]
    assert "Whitespace inside a name" not in json.dumps(c)
    assert c["n_judged"] == 3  # phenotypes row, the absent-variant row, variant 1
    assert v1["phase"] == "trans with c.250G>A, p.(Gly84Arg)"


def test_a_quote_reports_the_checkers_own_answer_and_unchecked_is_not_a_miss(
    run: RunPaths, stub_atlas: Path
) -> None:
    data = build(run, stub_atlas)
    g = card(data, G)
    v1 = g["variants"][0]
    miss = next(q for q in v1["quotes"] if q["quote"].startswith("this sentence"))
    assert (miss["checked"], miss["found"]) == (True, False)
    assert (g["n_unchecked"], g["n_not_found"]) == (0, 1)
    # the same card with no source text: unchecked, and not counted as a miss
    t = run.texts
    texts = json.loads(t.read_text())
    texts["papers"][0].update(source="none", text=None, abstract=None, supplements=[])
    t.write_text(json.dumps(texts))
    g2 = card(build(run, stub_atlas), G)
    assert (g2["n_unchecked"], g2["n_not_found"]) == (5, 0)
    assert all(q["checked"] is False and q["found"] is False for q in g2["variants"][0]["quotes"])


def test_a_quote_over_30_words_reaches_the_page_as_its_first_30_words(
    run: RunPaths, stub_atlas: Path
) -> None:
    data = build(run, stub_atlas)
    summary = row(card(data, G), "summary")
    long_q = next(q for q in summary["quotes"] if q["over_limit"])
    full = (
        "The variant was found by exome sequencing in the proband and then confirmed in the "
        "father; the variant was absent from the unaffected mother and from the unaffected "
        "brother, who were both tested because the family history described several relatives"
    )
    words = full.split()
    assert len(words) == 40 and long_q["words"] == 40
    assert long_q["quote"] == " ".join(words[:30]) + " …"
    assert long_q["found"] is True
    blob = json.dumps(data)
    # the 31st word is "were", which occurs elsewhere; its run with the next words does not
    assert words[30:33] == ["were", "both", "tested"] and "were both tested" not in blob
    assert "described several relatives" not in blob


def test_abstract_group_follows_the_papers_source_or_the_not_accessible_mark(
    run: RunPaths, stub_atlas: Path
) -> None:
    data = build(run, stub_atlas)
    assert card(data, A)["source"] == "abstract"  # texts.json says abstract and the mark
    assert card(data, J)["source"] == "abstract"  # texts.json alone says it
    assert card(data, G)["source"] == "full"
    t = run.texts
    texts = json.loads(t.read_text())
    texts["papers"][2]["source"] = "full_text"  # the mark alone still groups it
    t.write_text(json.dumps(texts))
    assert card(build(run, stub_atlas), A)["group"] == "abstract"
    lit = run.root / LIT
    lit.write_text(
        lit.read_text().replace("review_status: not_accessible", "review_status: pending_review")
    )
    assert card(build(run, stub_atlas), A)["group"] == "agreed"


def test_papers_needed_triage_and_labels(run: RunPaths, stub_atlas: Path) -> None:
    data = build(run, stub_atlas)
    assert data["papers_needed"] == [
        {
            "pmid": "PMID:90000002",
            "title": "Two TBX5 missense variants in a compound heterozygous proband.",
            "year": "2021",
            "why": "the proband's table is in the full text, which is not open access",
            "doi_url": None,
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/90000002/",
        }
    ]
    assert [t["pmid"] for t in data["triage"]] == [
        "PMID:90000001",
        "PMID:90000002",
        "PMID:90000003",
    ]
    assert data["triage"][0]["year"] == "2019"
    assert "Atrial septal defect (HP:0001631)" in row(card(data, G), "phenotypes")["value"]
    assert card(data, G)["label"] == "Alpha et al. 2019"


def test_triage_order_and_the_fallbacks_for_a_paper_not_in_publications(
    run: RunPaths, stub_atlas: Path
) -> None:
    (run.root / "curation/publications.yaml").unlink()
    lit = run.root / LIT
    doc = YAML(typ="safe").load(lit.read_text())
    doc["triage"] = [
        {
            "pmid": "PMID:90000004",
            "search": 1,
            "decision": "included",
            "title": "T4",
            "year": 2020,
            "doi": "10.1/x",
        },
        {
            "pmid": "PMID:90000003",
            "search": 1,
            "decision": "excluded",
            "reason": "case_series_without_variants",
            "note": "n",
        },
        {"pmid": "PMID:90000002", "search": 1, "decision": "pending"},
        {
            "pmid": "PMID:90000001",
            "search": 1,
            "decision": "excluded",
            "reason": "not_human_genetics",
        },
    ]
    out = YAML(typ="safe")
    out.default_flow_style = False
    with lit.open("w") as fh:
        out.dump(doc, fh)
    data = build(run, stub_atlas)
    assert [(t["pmid"], t["decision"]) for t in data["triage"]] == [
        ("PMID:90000002", "pending"),
        ("PMID:90000003", "excluded"),  # reasons order the excluded: case_series < not_human
        ("PMID:90000001", "excluded"),
        ("PMID:90000004", "included"),
    ]
    last = data["triage"][-1]
    assert (last["title"], last["year"]) == ("T4", "2020")
    assert data["triage"][1]["reason"] == "case series without variants"
    assert data["triage"][1]["note"] == "n"
    assert card(data, G)["title"] == "" and card(data, G)["label"] == "PMID:90000001"


def test_a_doi_makes_a_link(run: RunPaths, stub_atlas: Path) -> None:
    pubs = run.root / "curation/publications.yaml"
    pubs.write_text(
        pubs.read_text().replace("    year: 2021\n", "    year: 2021\n    doi: 10.1000/syn.2\n")
    )
    (need,) = build(run, stub_atlas)["papers_needed"]
    assert need["doi_url"] == "https://doi.org/10.1000/syn.2"


@pytest.mark.parametrize("files", [[], ["hp-a.obo", "hp-b.obo"]])
def test_zero_or_two_hpo_files_is_an_error_naming_the_directory(
    run: RunPaths, stub_atlas: Path, files: list[str]
) -> None:
    d = run.root / "ontologies"
    src = next(d.glob("hp-*.obo"))
    src.unlink()
    for name in files:
        shutil.copy(FIXTURE / "ontologies/hp-2026-06-23.obo", d / name)
    with pytest.raises(ReviewDataError, match=str(d)):
        build(run, stub_atlas)


def test_a_term_missing_from_the_obo_is_shown_as_its_bare_id(
    run: RunPaths, stub_atlas: Path
) -> None:
    lit = run.root / LIT
    lit.write_text(
        lit.read_text().replace(
            "phenotypes: [HP:0001631]\n    lesion", "phenotypes: [HP:0009999]\n    lesion", 1
        )
    )
    assert row(card(build(run, stub_atlas), G), "phenotypes")["value"] == "HP:0009999"


def _second(
    name: str, fid: str, decision: str, note: str, role: str = "second_opinion"
) -> dict[str, Any]:
    return {
        "format": 1,
        "gene": "HGNC:11604",
        "reviewer": {"name": name, "role": role},
        "findings": [{"id": fid, "decision": decision, "note": note}],
    }


def test_second_opinions_reach_the_owner_sorted_and_refuse_a_second_opinion_reviewer(
    run: RunPaths, stub_atlas: Path
) -> None:
    opinions = [_second("Zed", J, "fix", "why"), _second("Ann", J, "approve", "")]
    data = build(run, stub_atlas, second_opinions=opinions)
    assert data["second_opinions"] == {
        J: [
            {"name": "Ann", "decision": "approve", "note": ""},
            {"name": "Zed", "decision": "fix", "note": "why"},
        ]
    }
    assert "second_opinions" not in build(run, stub_atlas, Reviewer("Dr X", "second_opinion"))
    with pytest.raises(ValueError, match="second-opinion"):
        build(run, stub_atlas, Reviewer("Dr X", "second_opinion"), second_opinions=opinions)
    with pytest.raises(ValueError, match="second_opinion reviewer"):
        build(run, stub_atlas, second_opinions=[_second("Ann", J, "approve", "", "owner")])
    with pytest.raises(ValueError, match="which the page lacks"):
        build(run, stub_atlas, second_opinions=[_second("Ann", "CHDA:LIT:0009999", "approve", "")])


def test_two_builds_are_identical(run: RunPaths, stub_atlas: Path) -> None:
    a, b = build(run, stub_atlas), build(run, stub_atlas)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@pytest.fixture
def rich(run: RunPaths) -> RunPaths:
    """The fixture with extra keys on 0901 and its variant, which the plain fixture lacks."""
    path = run.root / LIT
    doc = YAML(typ="safe").load(path.read_text())
    f = next(f for f in doc["findings"] if f["id"] == G)
    f.update(
        comparator="case_control",
        cohorts=["gnomad_controls"],
        review_settled_by="E.A.M",
        cases=12,
        p_value=0.003,
        odds_ratio=2.5,
        burden_study="PMID:90000001",
    )
    f["variants"][0].update(
        hgvs_c="c.1085delC",
        hgvs_p="p.(Pro362Argfs*8)",
        grch38={"chrom": "12", "pos": 1, "ref": "A", "alt": "T"},
        review_status="pending_review",
        review_note="n",
        review_settled_by="E.A.M",
        unclear=True,
    )
    out = YAML(typ="safe")
    out.default_flow_style = False
    with path.open("w") as fh:
        out.dump(doc, fh)
    return run


def _record(run: RunPaths) -> dict[str, Any]:
    doc = YAML(typ="safe").load((run.root / LIT).read_text())
    return {f["id"]: f for f in doc["findings"]}


@pytest.mark.parametrize("which", ["run", "rich"])
def test_every_key_the_approval_binds_is_shown_on_the_card_and_each_variant(
    which: str, request: pytest.FixtureRequest, stub_atlas: Path
) -> None:
    run: RunPaths = request.getfixturevalue(which)
    data = build(run, stub_atlas)
    for fid, rec in _record(run).items():
        c = card(data, fid)
        assert c["keys_shown"] == sorted(set(rec) - APPROVAL_KEYS), fid
        for v, vrec in zip(c["variants"], rec["variants"], strict=True):
            assert v["keys_shown"] == sorted(vrec), (fid, vrec["id"])
    # an approved finding's approval keys are not among them
    assert "approved_by" not in card(data, G)["keys_shown"]
    # fields the pilot's page omitted reach the page as readable rows
    g = card(data, G)
    if which == "rich":
        assert row(g, "p_value")["value"] == "0.003"
        assert row(g, "design")["value"] == "family segregation · case_control"
        assert "GRCh38 12-1-A-T" in g["variants"][0]["placed"]
    assert row(g, "locators")["value"] == '[{"element": "Results - Patients", "kind": "in_text"}]'
    assert row(g, "verified_by")["value"].startswith("agent:literature-verify")
    assert {"locator", "reported"} <= {o["name"] for o in g["variants"][0]["others"]}


def _perturbed(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, list):
        return [*value, 99 if value and isinstance(value[0], int) else "zz"]
    if isinstance(value, dict):
        return {**value, "zz": 1}
    return f"{value}-changed"


def _change(run: RunPaths, fid: str, key: str, variant: int | None) -> None:
    path = run.root / LIT
    doc = YAML(typ="safe").load(path.read_text())
    for f in doc["findings"]:
        if f["id"] == fid:
            target = f if variant is None else next(v for v in f["variants"] if v["id"] == variant)
            target[key] = _perturbed(target[key])
    out = YAML(typ="safe")
    out.default_flow_style = False
    with path.open("w") as fh:
        out.dump(doc, fh)


def _content(obj: Any) -> str:
    """The card as JSON without its own claims about which keys it shows."""

    def strip(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in ("keys", "keys_shown")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x

    return json.dumps(strip(obj), sort_keys=True)


def test_changing_any_key_changes_what_the_card_shows(rich: RunPaths, stub_atlas: Path) -> None:
    """keys_shown is declared by the rows; this checks each declared key's VALUE is rendered.

    Deleting a key would not do: a row with a blank value still differs from no row.
    """
    run = rich
    base = build(run, stub_atlas)
    records = _record(run)
    for fid, rec in records.items():
        for variant, keys in [
            (None, sorted(set(rec) - APPROVAL_KEYS - {"id", "publication", "variants"}))
        ] + [(v["id"], sorted(set(v) - {"id"})) for v in rec["variants"]]:
            for key in keys:
                shutil.copy(run.root / LIT, run.root / "backup.yaml")
                _change(run, fid, key, variant)
                changed = build(run, stub_atlas)
                shutil.copy(run.root / "backup.yaml", run.root / LIT)
                assert _content(card(changed, fid)) != _content(card(base, fid)), (
                    fid,
                    variant,
                    key,
                )


def test_a_finding_with_no_digests_is_refused_not_given_a_blank_card(
    run: RunPaths, stub_atlas: Path
) -> None:
    d = digests(run.root, "TBX5", stub_atlas)
    short = type(d)(d.gene, d.symbol, d.literature_sha256, d.findings[:2])
    with pytest.raises(ReviewDataError, match="CHDA:LIT:0000903"):
        build_review_data(run.root, run, short, OWNER, BUILT_ON, None, "x")


def test_a_quote_whose_field_names_no_row_or_variant_is_kept_on_the_card(
    run: RunPaths, stub_atlas: Path
) -> None:
    ev = run.evidence / "batch-01.yaml"
    ev.write_text(
        ev.read_text()
        + '  - {field: "variant 7 zygosity", quote: "a heterozygous frameshift", where: "A"}\n'
        + '  - {field: "stray note", quote: "a proband with a ventricular septal", where: "A"}\n'
    )
    c = card(build(run, stub_atlas), J)
    assert [q["quote"] for q in c["other_quotes"]] == [
        "a heterozygous frameshift",
        "a proband with a ventricular septal",
    ]
    assert (
        c["n_unchecked"] == 0 and c["n_not_found"] == 1
    )  # only the frameshift is not in the abstract


def test_judged_cards_sort_by_how_many_rows_were_judged_then_id(
    run: RunPaths, stub_atlas: Path
) -> None:
    extra = run.judge / "judge-02.decisions.yaml"
    extra.write_text(
        f"{G}:\n  - {{field: design, first: a, second: b, decision: first, value: a,"
        ' quote: "", where: "", rule: r}\n'
    )
    data = build(run, stub_atlas)
    assert [(c["id"], c["n_judged"]) for c in data["findings"][:2]] == [(J, 3), (G, 1)]
