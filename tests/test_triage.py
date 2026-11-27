"""``triage-requests``, ``triage-combine`` and ``triage-apply``: the hybrid's decision rules.

The rule under test is one sentence: no exclusion in a risky class is final without the
capable check, an audit samples the trusted classes, and an answer that is missing, an error
or unparseable is never read as an exclusion. Every case below is a literal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from chd_curator import cli
from chd_curator.cli import app
from chd_curator.triage import (
    NO_ABSTRACT,
    NO_RECORD,
    RISKY,
    apply_decisions,
    audit_sample,
    awaiting,
    combine,
    escalated_classes,
    hit_block,
    to_check,
    valid_answer,
)
from fake_pubmed import FakePubMed, papers

runner = CliRunner()


def ans(key: str, decision: str, reason: str = "none", why: str = "because") -> dict[str, Any]:
    return {"pmid": key, "decision": decision, "reason": reason, "why": why}


def inc(key: str) -> dict[str, Any]:
    return ans(key, "included")


def exc(key: str, reason: str) -> dict[str, Any]:
    return ans(key, "excluded", reason)


# ---------------------------------------------------------------------------------- combine


def test_combine_includes_a_hit_either_tier_includes() -> None:
    screen = {
        "PMID:1": inc("PMID:1"),  # screen includes, check excludes
        "PMID:2": exc("PMID:2", "not_chd"),  # screen excludes (risky), check includes
        "PMID:3": exc("PMID:3", "not_chd"),  # both exclude, different reasons
        "PMID:4": exc("PMID:4", "review_article"),  # trusted class, not audited
    }
    check = {
        "PMID:1": exc("PMID:1", "not_chd"),
        "PMID:2": inc("PMID:2"),
        "PMID:3": exc("PMID:3", "no_finding_in_gene"),
    }

    got = combine(screen, check, RISKY, audit=set())

    assert {k: (v["decision"], v["reason"], v["tier"]) for k, v in got.items()} == {
        "PMID:1": ("included", "none", "screen"),
        "PMID:2": ("included", "none", "check"),
        "PMID:3": ("excluded", "no_finding_in_gene", "check"),
        "PMID:4": ("excluded", "review_article", "screen"),
    }


def test_a_hit_the_screen_did_not_answer_goes_to_the_check(tmp_path: Path) -> None:
    """Review Focus 1: a missing row, an error row and an unparseable answer are three ways
    of not answering, and none may become an exclusion (or an inclusion)."""
    requests = tmp_path / "req.jsonl"
    keys = ["PMID:1", "PMID:2", "PMID:3", "PMID:4"]
    requests.write_text(
        "".join(json.dumps({"key": k, "inputs": {"gene": "G", "hit": "h"}}) + "\n" for k in keys)
    )
    screen = tmp_path / "screen.jsonl"
    screen.write_text(
        json.dumps({"key": "PMID:1", "error": "gave up", "attempts": 3})
        + "\n"
        + json.dumps(
            {
                "key": "PMID:2",
                "parsed": {"decision": "excluded", "reason": "none", "pmid": "PMID:2", "why": "x"},
                "attempts": 1,
            }
        )
        + "\n"
        + json.dumps({"key": "PMID:3", "parsed": inc("PMID:3"), "attempts": 1})
        + "\n"
    )
    out, out_req = tmp_path / "d.json", tmp_path / "check-req.jsonl"

    result = runner.invoke(
        app,
        [
            "triage-combine",
            "--requests",
            str(requests),
            "--screen",
            str(screen),
            "--run",
            "R",
            "--out",
            str(out),
            "--out-requests",
            str(out_req),
        ],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(out.read_text())
    assert document["awaiting_check"] == ["PMID:1", "PMID:2", "PMID:4"]
    assert list(document["decisions"]) == ["PMID:3"]
    assert [json.loads(line)["key"] for line in out_req.read_text().splitlines()] == [
        "PMID:1",
        "PMID:2",
        "PMID:4",
    ]
    assert to_check({"PMID:1": {}, "PMID:3": inc("PMID:3")}, RISKY, set()) == {"PMID:1"}


# ----------------------------------------------------------------------------------- audit


def test_the_audit_takes_ten_percent_at_least_five_and_all_of_a_small_class() -> None:
    trusted = {
        "not_peer_reviewed": [f"PMID:{n}" for n in range(1000, 1182)],  # 182 -> ceil(18.2)
        "review_article": [f"PMID:{n}" for n in range(1, 14)],  # 13 -> the minimum, 5
        "other": [f"PMID:{n}" for n in range(90, 93)],  # 3 -> all 3
    }

    got = audit_sample(trusted, "RUN1")

    sizes = {name: len(got & set(keys)) for name, keys in trusted.items()}
    assert sizes == {"not_peer_reviewed": 19, "review_article": 5, "other": 3}


_PROBE = """
import json
from chd_curator.triage import audit_sample
t = {"review_article": [f"PMID:{n}" for n in range(1, 14)], "other": ["PMID:90", "PMID:91"]}
print(json.dumps({s: sorted(audit_sample(t, s)) for s in ("RUN1", "RUN2")}))
"""


@pytest.mark.parametrize("hashseed", ["0", "1", "4242"])
def test_the_audit_sample_is_fixed_by_the_seed(hashseed: str) -> None:
    """Literal samples, measured once; the same under three explicit PYTHONHASHSEED values
    (CLAUDE.md section 4.42), in a fresh process each."""
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    run = subprocess.run(
        [sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True, check=True
    )

    assert json.loads(run.stdout) == {
        "RUN1": ["PMID:11", "PMID:5", "PMID:7", "PMID:8", "PMID:9", "PMID:90", "PMID:91"],
        "RUN2": ["PMID:13", "PMID:4", "PMID:5", "PMID:6", "PMID:7", "PMID:90", "PMID:91"],
    }


# ------------------------------------------------------------- escalation, shadow, usage


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _combine(tmp_path: Path, *args: str) -> Any:
    return runner.invoke(
        app,
        [
            "triage-combine",
            "--requests",
            str(tmp_path / "req.jsonl"),
            "--run",
            "R",
            "--out",
            str(tmp_path / "d.json"),
            *args,
        ],
    )


def _setup(tmp_path: Path, reasons: dict[str, str], hits: dict[str, str] | None = None) -> None:
    _write(
        tmp_path / "req.jsonl",
        [
            {"key": k, "gene": "G", "inputs": {"gene": "G", "hit": (hits or {}).get(k, "h")}}
            for k in reasons
        ],
    )
    _write(tmp_path / "screen.jsonl", [{"key": k, "parsed": exc(k, r)} for k, r in reasons.items()])


def test_an_audit_miss_makes_its_class_risky_for_the_run(tmp_path: Path) -> None:
    keys = [
        f"PMID:{n}" for n in range(1, 7)
    ]  # six review_article exclusions: five audited, one not
    _setup(tmp_path, dict.fromkeys(keys, "review_article"))
    audit = sorted(audit_sample({"review_article": keys}, "R"))
    unaudited = sorted(set(keys) - set(audit))
    assert len(audit) == 5 and len(unaudited) == 1

    first = _combine(
        tmp_path,
        "--screen",
        str(tmp_path / "screen.jsonl"),
        "--out-requests",
        str(tmp_path / "r1.jsonl"),
    )
    assert first.exit_code == 0, first.output
    d1 = json.loads((tmp_path / "d.json").read_text())
    assert (
        d1["escalated"] == []
        and d1["awaiting_check"] == audit
        and list(d1["decisions"]) == unaudited
    )

    # The check includes one audited hit: a miss.
    c1 = _write(
        tmp_path / "c1.jsonl",
        [
            {"key": k, "parsed": inc(k) if k == audit[0] else exc(k, "review_article")}
            for k in audit
        ],
    )
    second = _combine(
        tmp_path,
        "--screen",
        str(tmp_path / "screen.jsonl"),
        "--check",
        str(c1),
        "--out-requests",
        str(tmp_path / "r2.jsonl"),
    )
    assert second.exit_code == 0, second.output
    d2 = json.loads((tmp_path / "d.json").read_text())
    assert d2["escalated"] == ["review_article"]
    assert d2["awaiting_check"] == unaudited and d2["decisions"][audit[0]]["decision"] == "included"
    assert [
        json.loads(x)["key"] for x in (tmp_path / "r2.jsonl").read_text().splitlines()
    ] == unaudited

    # A follow-up round answers the rest; both files are passed.
    c2 = _write(
        tmp_path / "c2.jsonl", [{"key": unaudited[0], "parsed": exc(unaudited[0], "other")}]
    )
    third = _combine(
        tmp_path, "--screen", str(tmp_path / "screen.jsonl"), "--check", str(c1), "--check", str(c2)
    )
    assert third.exit_code == 0, third.output
    d3 = json.loads((tmp_path / "d.json").read_text())
    assert d3["awaiting_check"] == []
    assert d3["decisions"][unaudited[0]] == {
        "decision": "excluded",
        "reason": "other",
        "why": "because",
        "tier": "check",
    }
    assert list(d3["decisions"]) == sorted(keys)  # keys sorted


def test_shadow_decides_by_the_check_and_records_the_hybrid(tmp_path: Path) -> None:
    _setup(tmp_path, {"PMID:1": "not_chd", "PMID:2": "review_article"})
    check = _write(
        tmp_path / "c.jsonl",
        [
            {"key": "PMID:1", "parsed": inc("PMID:1")},
            {"key": "PMID:2", "parsed": exc("PMID:2", "other")},
        ],
    )

    result = _combine(
        tmp_path, "--screen", str(tmp_path / "screen.jsonl"), "--check", str(check), "--shadow"
    )

    assert result.exit_code == 0, result.output
    document = json.loads((tmp_path / "d.json").read_text())
    assert document["shadow"] is True and document["awaiting_check"] == []
    one, two = document["decisions"]["PMID:1"], document["decisions"]["PMID:2"]
    assert (one["decision"], one["tier"], one["hybrid"]["decision"]) == (
        "included",
        "check",
        "included",
    )
    # The check decides (other); the hybrid (audited: both exclude) also takes the check's reason.
    assert (two["reason"], two["hybrid"]["reason"]) == ("other", "other")


def test_an_answer_for_a_key_never_requested_or_given_twice_exits_2_naming_it(
    tmp_path: Path,
) -> None:
    _setup(tmp_path, {"PMID:1": "not_chd"})
    stray = _write(tmp_path / "stray.jsonl", [{"key": "PMID:9", "parsed": inc("PMID:9")}])
    twice = _write(
        tmp_path / "twice.jsonl", [{"key": "PMID:1", "error": "e"}, {"key": "PMID:1", "error": "e"}]
    )

    for answers, key in ((stray, "PMID:9"), (twice, "PMID:1")):
        result = _combine(tmp_path, "--screen", str(answers))
        assert result.exit_code == 2 and key in result.output
    result = _combine(tmp_path, "--screen", str(tmp_path / "screen.jsonl"), "--check", str(stray))
    assert result.exit_code == 2 and "PMID:9" in result.output


# ------------------------------------------------------------------------- requests, apply

LITERATURE = (
    "gene: HGNC:11604\n"
    "searches:\n"
    "  - {id: 1, source: pubmed, query: q, run_on: 2026-11-02, hits: 4, new_hits: 4}\n"
    "triage:\n"
    "  - {pmid: 'PMID:5', search: 1, decision: pending}\n"
    "  - {pmid: 'PMID:6', search: 1, decision: included}\n"
    "  - {pmid: 'PMID:7', search: 1, decision: pending}\n"
    "  - {pmid: 'PMID:8', search: 1, decision: excluded, reason: not_chd}\n"
    "  - {pmid: 'PMID:9', search: 1, decision: pending}\n"
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "schemas").symlink_to(Path(os.environ["CHD_ATLAS_ROOT"]) / "schemas")
    path = tmp_path / "curation" / "literature" / "TBX5.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(LITERATURE)
    return tmp_path


def _decisions(
    path: Path,
    decisions: dict[str, Any],
    awaiting: list[str] | None = None,
    gene: str = "TBX5",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "gene": gene,
                "run": "R",
                "shadow": False,
                "escalated": [],
                "awaiting_check": awaiting or [],
                "decisions": decisions,
            }
        )
    )
    return path


def _apply(stub: Path, root: Path, decisions: Path) -> Any:
    return runner.invoke(
        app,
        [
            "triage-apply",
            "HOS",
            "--decisions",
            str(decisions),
            "--root",
            str(root),
            "--atlas-cli",
            str(stub),
        ],
    )


def test_triage_requests_writes_one_request_per_pending_hit(
    root: Path, stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    many = papers("5", "6", "7", "8", "9")
    many["7"]["abstract"] = ""
    monkeypatch.setattr(cli, "_fetch", FakePubMed(many))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    out = tmp_path / "req.jsonl"

    result = runner.invoke(
        app,
        [
            "triage-requests",
            "HOS",
            "--out",
            str(out),
            "--root",
            str(root),
            "--atlas-cli",
            str(stub_atlas),
        ],
    )
    shown = runner.invoke(
        app, ["abstracts", "TBX5", "--root", str(root), "--atlas-cli", str(stub_atlas)]
    )

    assert result.exit_code == 0, result.output
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["key"] for r in rows] == ["PMID:5", "PMID:7", "PMID:9"]  # file order, pending only
    assert all(r["inputs"]["gene"] == "TBX5" for r in rows)  # the approved symbol, typed as HOS
    # `hit` is exactly the block `abstracts` prints, which stays byte-identical to before.
    assert "\n\n".join(r["inputs"]["hit"] for r in rows) + "\n\n" == shown.stdout
    assert rows[1]["inputs"]["hit"] == (
        "PMID:7 · 2007 · Title of 7\nPublication types: (none in PubMed)\n(no abstract in PubMed)"
    )


def test_triage_apply_changes_only_pending_entries_and_refuses_a_changed_file(
    root: Path, stub_atlas: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = root / "curation" / "literature" / "TBX5.yaml"
    good = _decisions(
        tmp_path / "good.json",
        {
            "PMID:5": {"decision": "included", "reason": "none", "why": "w", "tier": "check"},
            "PMID:7": {
                "decision": "excluded",
                "reason": "other",
                "why": "an erratum",
                "tier": "check",
            },
            "PMID:9": {
                "decision": "excluded",
                "reason": "review_article",
                "why": "w",
                "tier": "screen",
            },
        },
    )

    # A file with a hit awaiting the check is refused whole.
    owed = _decisions(tmp_path / "owed.json", {}, awaiting=["PMID:1", "PMID:2"])
    refused = _apply(stub_atlas, root, owed)
    assert refused.exit_code == 1 and "2 hit(s) still await the check" in refused.output
    # A key that is not a pending entry (included, excluded, absent) is refused; nothing written.
    for key in ("PMID:6", "PMID:8", "PMID:99"):
        bad = _decisions(
            tmp_path / "bad.json",
            {key: {"decision": "included", "reason": "none", "why": "w", "tier": "screen"}},
        )
        result = _apply(stub_atlas, root, bad)
        assert result.exit_code == 1 and key in result.output
    assert path.read_text() == LITERATURE

    # The file changes between reading and writing: refused, the edit survives.
    real = cli.triage_module.with_triage

    def edit_meanwhile(raw: bytes, changes: Any) -> bytes:
        content = real(raw, changes)
        path.write_text(LITERATURE + "# an edit made meanwhile\n")
        return content

    monkeypatch.setattr(cli.triage_module, "with_triage", edit_meanwhile)
    stale = _apply(stub_atlas, root, good)
    assert stale.exit_code == 2 and "changed since it was read" in stale.output
    assert path.read_text() == LITERATURE + "# an edit made meanwhile\n"
    monkeypatch.undo()
    path.write_text(LITERATURE)

    ok = _apply(stub_atlas, root, good)

    assert ok.exit_code == 0, ok.output
    assert path.read_text() == (
        LITERATURE.replace(
            "{pmid: 'PMID:5', search: 1, decision: pending}",
            "{pmid: 'PMID:5', search: 1, decision: included}",
        )
        .replace(
            "{pmid: 'PMID:7', search: 1, decision: pending}",
            "{pmid: 'PMID:7', search: 1, decision: excluded, reason: other, note: an erratum}",
        )
        .replace(
            "{pmid: 'PMID:9', search: 1, decision: pending}",
            "{pmid: 'PMID:9', search: 1, decision: excluded, reason: review_article}",
        )
    )


# ------------------------------------------------------------------- fix round 1


_FIXTURES: list[tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str], set[str]]] = [
    # (screen, check, audit, thin)
    ({"A": {}, "B": inc("B"), "C": exc("C", "not_chd"), "D": exc("D", "other")}, {}, {"D"}, set()),
    ({"A": exc("A", "other"), "B": exc("B", "review_article")}, {}, {"A"}, {"B"}),
    (
        {"A": exc("A", "other"), "B": exc("B", "other"), "C": exc("C", "review_article")},
        {"A": inc("A")},
        {"A", "C"},
        set(),
    ),
    ({"A": inc("A"), "B": {}}, {"B": exc("B", "other")}, set(), {"A"}),
]


@pytest.mark.parametrize("screen,check,audit,thin", _FIXTURES)
def test_the_hits_awaiting_are_exactly_those_to_check_less_the_answered(
    screen: dict[str, dict[str, Any]],
    check: dict[str, dict[str, Any]],
    audit: set[str],
    thin: set[str],
) -> None:
    """`combine`, `awaiting` and `to_check` read one rule: what the check must still answer.
    Every hit is decided or awaiting, never both, never neither."""
    escalated = escalated_classes(screen, check, audit)
    wanted = to_check(screen, RISKY, audit, escalated, thin)
    owed = set(awaiting(screen, check, RISKY, audit, thin))
    decided = set(combine(screen, check, RISKY, audit, thin))

    assert owed == {k for k in wanted if not check.get(k)}
    assert owed | decided == set(screen) and not owed & decided


def test_a_hit_with_no_pubmed_record_or_no_abstract_never_stands_on_the_screens_word() -> None:
    assert NO_RECORD in hit_block("1", "2005", None)
    assert hit_block("1", "2005", ("T", "", [])).splitlines()[-1] == NO_ABSTRACT
    screen = {"PMID:1": exc("PMID:1", "other"), "PMID:2": exc("PMID:2", "other")}

    assert to_check(screen, RISKY, set()) == set()
    assert to_check(screen, RISKY, set(), thin={"PMID:2"}) == {"PMID:2"}
    assert awaiting(screen, {}, RISKY, set(), {"PMID:2"}) == ["PMID:2"]
    assert list(combine(screen, {}, RISKY, set(), {"PMID:2"})) == ["PMID:1"]


@pytest.mark.parametrize(
    "thin_block",
    [hit_block("0", "2005", None), hit_block("0", "2005", ("Title", "", ["Journal Article"]))],
    ids=["no-record", "no-abstract"],
)
def test_triage_combine_sends_a_thin_hit_to_the_check_whatever_the_screen_said(
    tmp_path: Path, thin_block: str
) -> None:
    keys = [f"PMID:{n}" for n in range(1, 8)]
    audit = audit_sample({"other": keys}, "R")
    unaudited = sorted(set(keys) - audit)
    assert len(unaudited) == 2
    _setup(tmp_path, dict.fromkeys(keys, "other"), {unaudited[0]: thin_block})
    out_req = tmp_path / "r.jsonl"

    result = _combine(
        tmp_path, "--screen", str(tmp_path / "screen.jsonl"), "--out-requests", str(out_req)
    )

    assert result.exit_code == 0, result.output
    owed = [json.loads(x)["key"] for x in out_req.read_text().splitlines()]
    assert owed == sorted([*audit, unaudited[0]])
    assert unaudited[1] in json.loads((tmp_path / "d.json").read_text())["decisions"]


def test_an_unhashable_reason_is_an_unparseable_answer_not_a_crash() -> None:
    bad = {"pmid": "PMID:1", "decision": "excluded", "reason": ["not_chd"], "why": "w"}
    assert valid_answer(bad, "PMID:1") is False


def test_a_check_error_row_leaves_an_audited_hit_and_a_risky_hit_awaiting(tmp_path: Path) -> None:
    keys = [f"PMID:{n}" for n in range(1, 7)]
    reasons = {**dict.fromkeys(keys, "review_article"), "PMID:50": "not_chd"}
    _setup(tmp_path, reasons)
    audit = sorted(audit_sample({"review_article": keys}, "R"))
    errors = _write(
        tmp_path / "c.jsonl",
        [{"key": audit[0], "error": "gave up", "attempts": 3}]
        + [{"key": "PMID:50", "error": "gave up", "attempts": 3}],
    )

    result = _combine(tmp_path, "--screen", str(tmp_path / "screen.jsonl"), "--check", str(errors))

    assert result.exit_code == 0, result.output
    document = json.loads((tmp_path / "d.json").read_text())
    assert {audit[0], "PMID:50"} <= set(document["awaiting_check"])
    assert not {audit[0], "PMID:50"} & set(document["decisions"])


def test_two_classes_can_escalate_in_one_run(tmp_path: Path) -> None:
    a = [f"PMID:{n}" for n in range(1, 7)]
    b = [f"PMID:{n}" for n in range(11, 17)]
    _setup(tmp_path, {**dict.fromkeys(a, "review_article"), **dict.fromkeys(b, "other")})
    audited = sorted(audit_sample({"review_article": a, "other": b}, "R"))
    hit_a = next(k for k in audited if k in a)
    hit_b = next(k for k in audited if k in b)
    check = _write(
        tmp_path / "c.jsonl",
        [{"key": k, "parsed": inc(k) if k in (hit_a, hit_b) else exc(k, "other")} for k in audited],
    )

    result = _combine(tmp_path, "--screen", str(tmp_path / "screen.jsonl"), "--check", str(check))

    assert result.exit_code == 0, result.output
    document = json.loads((tmp_path / "d.json").read_text())
    assert document["escalated"] == ["other", "review_article"]
    assert document["awaiting_check"] == sorted(set(a + b) - set(audited))


def test_shadow_with_a_hit_the_check_did_not_answer_leaves_it_awaiting(tmp_path: Path) -> None:
    _setup(tmp_path, {"PMID:1": "not_chd", "PMID:2": "review_article"})
    check = _write(tmp_path / "c.jsonl", [{"key": "PMID:1", "parsed": inc("PMID:1")}])
    out_req = tmp_path / "r.jsonl"

    result = _combine(
        tmp_path,
        "--screen",
        str(tmp_path / "screen.jsonl"),
        "--check",
        str(check),
        "--shadow",
        "--out-requests",
        str(out_req),
    )

    assert result.exit_code == 0, result.output
    document = json.loads((tmp_path / "d.json").read_text())
    assert document["awaiting_check"] == ["PMID:2"] and list(document["decisions"]) == ["PMID:1"]
    assert [json.loads(x)["key"] for x in out_req.read_text().splitlines()] == ["PMID:2"]
    assert document["gene"] == "G"


def _file(root: Path) -> Path:
    return root / "curation" / "literature" / "TBX5.yaml"


def test_apply_rechecks_what_it_can_and_refuses_the_rest(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    good = {"decision": "included", "reason": "none", "why": "w", "tier": "check"}
    cases = {
        "a risky exclusion on the screen's word": (
            {"PMID:5": {**good, "decision": "excluded", "reason": "not_chd", "tier": "screen"}},
            "TBX5",
            "PMID:5",
        ),
        "another gene's file": ({"PMID:5": good}, "GATA4", "GATA4"),
        "no gene": ({"PMID:5": good}, "", "gene"),
        "an other exclusion with no why": (
            {"PMID:5": {**good, "decision": "excluded", "reason": "other", "why": ""}},
            "TBX5",
            "PMID:5",
        ),
        "a good key beside a bad one": (
            {"PMID:5": good, "PMID:6": good},  # PMID:6 is not pending
            "TBX5",
            "PMID:6",
        ),
    }
    for what, (decisions, gene, named) in cases.items():
        result = _apply(stub_atlas, root, _decisions(tmp_path / "d.json", decisions, gene=gene))
        assert result.exit_code == 1 and named in result.output, what
        assert _file(root).read_text() == LITERATURE, what


def test_apply_takes_a_shadow_file_and_its_check_decisions(
    root: Path, stub_atlas: Path, tmp_path: Path
) -> None:
    decision = {"decision": "included", "reason": "none", "why": "w", "tier": "check"}
    shadow = {"PMID:5": {**decision, "hybrid": {**decision, "tier": "screen"}}}
    path = _decisions(tmp_path / "d.json", shadow)
    document = json.loads(path.read_text())
    document["shadow"] = True
    path.write_text(json.dumps(document))

    result = _apply(stub_atlas, root, path)

    assert result.exit_code == 0, result.output
    assert "{pmid: 'PMID:5', search: 1, decision: included}" in _file(root).read_text()


def test_a_schema_refusal_and_a_changed_file_are_told_apart(root: Path) -> None:
    from jsonschema import Draft202012Validator

    from chd_curator.triage import TriageError

    decisions = {"PMID:5": {"decision": "included", "reason": "none", "why": "w", "tier": "check"}}
    with pytest.raises(TriageError) as schema:
        apply_decisions(root, "TBX5", decisions, Draft202012Validator({"not": {}}))
    assert "does not satisfy its schema" in str(schema.value)
    assert "changed since it was read" not in str(schema.value)
    assert _file(root).read_text() == LITERATURE
