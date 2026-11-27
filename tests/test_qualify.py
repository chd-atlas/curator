"""``qualify triage``: score a triage configuration against a gene's approved curation.

The pass rule is the spec's: zero papers with findings missed by the hybrid. The scorer reuses
``triage.py``'s functions; nothing here re-implements the hybrid. ``tests/fixtures/triage-444``
holds the benchmark's decisions and counts only (its README says where from).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator.cli import app
from chd_curator.qualify import score_triage, triage_gold
from chd_curator.triage import RISKY, audit_sample, read_answers, trusted_exclusions

FIXTURE = Path(__file__).parent / "fixtures" / "triage-444"
runner = CliRunner()
_yaml = YAML(typ="safe")


def ans(key: str, decision: str, reason: str = "none") -> dict[str, Any]:
    return {"pmid": key, "decision": decision, "reason": reason, "why": "x"}


def inc(key: str) -> dict[str, Any]:
    return ans(key, "included")


def exc(key: str, reason: str) -> dict[str, Any]:
    return ans(key, "excluded", reason)


def gold_of(**findings: int) -> dict[str, dict[str, Any]]:
    return {
        f"PMID:{k}": {"decision": "included" if n else "excluded", "reason": "none", "findings": n}
        for k, n in findings.items()
    }


# ------------------------------------------------------------------ the scorer can fail


def test_the_scorer_can_fail() -> None:
    gold = gold_of(**{"1": 2, "2": 0, "3": 1})
    screen = {"PMID:1": inc("PMID:1"), "PMID:2": exc("PMID:2", "not_chd"), "PMID:3": {}}
    check = {"PMID:2": exc("PMID:2", "not_chd"), "PMID:3": inc("PMID:3")}
    ok = score_triage(gold, screen, check, "s")
    assert (ok.verdict, ok.missed, ok.with_findings, ok.screen_alone) == ("PASS", [], 2, 1)

    # one excluded paper with findings (the unanswered hit, the check excludes it) -> FAIL
    bad = score_triage(gold, screen, {**check, "PMID:3": exc("PMID:3", "not_chd")}, "s")
    assert (bad.verdict, bad.missed) == ("FAIL", ["PMID:3"])
    # a risky exclusion of a paper with findings, which the check excludes too -> FAIL
    screen2 = {**screen, "PMID:1": exc("PMID:1", "no_finding_in_gene"), "PMID:3": inc("PMID:3")}
    bad2 = score_triage(gold, screen2, {**check, "PMID:1": exc("PMID:1", "not_chd")}, "s")
    assert (bad2.verdict, bad2.missed) == ("FAIL", ["PMID:1"])
    # a check with no answer for a hit sent to it: the run is partial, never a PASS
    partial = score_triage(gold, screen, {"PMID:2": check["PMID:2"]}, "s")
    assert (partial.verdict, partial.missed) == ("INCOMPLETE", [])
    # a miss beats a partial run
    both = score_triage(gold, screen2, {}, "s")
    assert both.verdict == "INCOMPLETE"
    both = score_triage(gold, screen2, {"PMID:1": exc("PMID:1", "not_chd")}, "s")
    assert (both.verdict, both.missed) == ("FAIL", ["PMID:1"])
    # a gold hit the screen was never asked about is a FAIL naming it
    absent = score_triage(gold, {k: v for k, v in screen.items() if k != "PMID:1"}, check, "s")
    assert (absent.verdict, absent.missed) == ("FAIL", ["PMID:1"])
    # a missed list is sorted
    two = score_triage(
        gold_of(**{"9": 1, "10": 1}),
        {"PMID:9": exc("PMID:9", "not_chd"), "PMID:10": exc("PMID:10", "not_chd")},
        {"PMID:9": exc("PMID:9", "not_chd"), "PMID:10": exc("PMID:10", "not_chd")},
        "s",
    )
    assert two.missed == ["PMID:10", "PMID:9"]


def test_a_trusted_exclusion_outside_the_audit_is_a_miss() -> None:
    """The hybrid trusts a class it does not audit: a paper with findings in it is lost, and the
    scorer says so (the seed is chosen so the hit is not in the sample, and that is asserted)."""
    gold = gold_of(**{str(n): 0 for n in range(100, 140)}, **{"1": 3})
    screen = {k: exc(k, "not_human_genetics") for k in gold}
    seed = next(
        s
        for s in map(str, range(50))
        if "PMID:1" not in audit_sample(trusted_exclusions(screen, RISKY), s)
    )
    audit = audit_sample(trusted_exclusions(screen, RISKY), seed)
    # The check agrees with every audited exclusion, so the class is not escalated, and it
    # would include every other hit if it were asked: a hit it is not owed is not rescued.
    check = {k: exc(k, "not_human_genetics") if k in audit else inc(k) for k in gold}
    score = score_triage(gold, screen, check, seed)
    assert (score.verdict, score.missed, score.sent_to_check) == ("FAIL", ["PMID:1"], 0)
    assert score.audit == 5  # 41 hits in one class: max(5, ceil(4.1)) = 5
    assert score.audit == len(audit_sample(trusted_exclusions(screen, RISKY), seed))


# ------------------------------------------------------------------ the benchmark


def _fixture_gold() -> list[dict[str, Any]]:
    return json.loads((FIXTURE / "gold.json").read_text(encoding="utf-8"))


def _score_fixture(seed: str, screen_name: str = "screen") -> dict[str, int | list[str]]:
    total: dict[str, Any] = {"hits": 0, "with_findings": 0, "screen_alone": 0, "sent": 0}
    missed: list[str] = []
    for gene in ("MMP21", "NR2F2", "TBX5"):
        gold = {
            g["pmid"]: {k: g[k] for k in ("decision", "reason", "findings")}
            for g in _fixture_gold()
            if g["gene"] == gene
        }
        keys = set(gold)
        screen = read_answers(FIXTURE / f"{screen_name}-{gene}.jsonl", keys)
        check = read_answers(FIXTURE / f"check-{gene}.jsonl", keys)
        assert len(screen) == len(gold) == len(check)
        score = score_triage(gold, {k: screen.get(k, {}) for k in keys}, check, f"{seed}:{gene}")
        assert score.verdict == "PASS", (gene, score)
        total["hits"] += len(gold)
        total["with_findings"] += score.with_findings
        total["screen_alone"] += score.screen_alone
        total["sent"] += score.sent_to_check
        missed += score.missed
    return {**total, "missed": missed}


@pytest.mark.parametrize("seed", ["run-a", "run-b", "run-c"])
def test_score_triage_reproduces_benchmark_run_3(seed: str) -> None:
    """The benchmark's run 3 (DeepSeek V4 Flash, thinking on): 82 of 86 papers with findings
    included by the screen alone, 93 of 444 hits sent to the check before the audit, 86 of 86
    with the hybrid when the check is the abstract-stage decisions."""
    found = _score_fixture(seed)
    assert (found["hits"], found["with_findings"]) == (444, 86)
    assert found["screen_alone"] == 82
    assert found["sent"] == 93
    assert found["missed"] == []


def test_the_benchmark_fixture_fails_without_the_check_over_the_risky_classes() -> None:
    gold = {
        g["pmid"]: {k: g[k] for k in ("decision", "reason", "findings")}
        for g in _fixture_gold()
        if g["gene"] == "TBX5"
    }
    keys = set(gold)
    screen = {k: v for k, v in read_answers(FIXTURE / "screen-TBX5.jsonl", keys).items()}
    nothing = score_triage(gold, screen, {}, "run-a")
    assert nothing.verdict == "INCOMPLETE"  # the risky exclusions are owed to the check
    # a check that includes nothing: the screen's own four misses stay lost
    excl = {k: exc(k, "other") for k in keys}
    assert score_triage(gold, screen, excl, "run-a").verdict == "FAIL"


# ------------------------------------------------------------------ the gold and the CLI


def _atlas(tmp_path: Path) -> Path:
    lit = tmp_path / "curation" / "literature"
    lit.mkdir(parents=True)
    record = {
        "gene": "HGNC:1",
        "triage": [
            {"pmid": "PMID:1", "search": 1, "decision": "included"},
            {"pmid": "PMID:2", "search": 1, "decision": "included"},
            {"pmid": "PMID:3", "search": 1, "decision": "excluded", "reason": "not_chd"},
            {"pmid": "PMID:4", "search": 1, "decision": "pending"},
        ],
        "findings": [
            {"id": "CHDA:LIT:0000001", "publication": "PMID:1", "approved_by": "E.A.M"},
            {"id": "CHDA:LIT:0000002", "publication": "PMID:1", "approved_by": "E.A.M"},
            {"id": "CHDA:LIT:0000003", "publication": "PMID:2"},  # drafted, never approved
        ],
    }
    with (lit / "ABC1.yaml").open("w", encoding="utf-8") as handle:
        _yaml.dump(record, handle)
    return tmp_path


def test_triage_gold_counts_approved_findings_per_hit(tmp_path: Path) -> None:
    gold = triage_gold(_atlas(tmp_path), "ABC1")
    assert gold == {
        "PMID:1": {"decision": "included", "reason": "none", "findings": 2},
        "PMID:2": {"decision": "included", "reason": "none", "findings": 0},
        "PMID:3": {"decision": "excluded", "reason": "not_chd", "findings": 0},
    }  # a pending hit is not gold


def _rows(path: Path, answers: Mapping[str, dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps({"key": k, "parsed": v}) + "\n" for k, v in answers.items()),
        encoding="utf-8",
    )


def test_the_cli_prints_the_score_and_a_register_entry_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root = _atlas(tmp_path / "atlas")
    screen, check = tmp_path / "screen.jsonl", tmp_path / "check.jsonl"
    _rows(
        screen,
        {"PMID:1": inc("PMID:1"), "PMID:2": inc("PMID:2"), "PMID:3": exc("PMID:3", "not_chd")},
    )
    _rows(check, {"PMID:3": exc("PMID:3", "not_chd")})
    before = sorted(p.name for p in tmp_path.rglob("*"))
    result = runner.invoke(
        app,
        ["qualify", "triage", "ABC1", "--screen", str(screen), "--check", str(check),
         "--run", "r1", "--gold-root", str(root)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
    entry = _yaml.load(result.output[result.output.index("entries:") :])["entries"][0]
    assert (entry["role"], entry["verdict"], entry["gold"]) == ("triage-screen", "pass", "ABC1")
    assert entry["scores"]["missed"] == 0 and entry["scores"]["with_findings"] == 1

    # a missing check file is a usage error (exit 2), not a partial run
    bad = runner.invoke(
        app,
        ["qualify", "triage", "ABC1", "--screen", str(screen),
         "--check", str(tmp_path / "no.jsonl"), "--run", "r1", "--gold-root", str(root)],
    )  # fmt: skip
    assert bad.exit_code == 2  # the check file does not exist: usage
