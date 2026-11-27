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

from chd_curator import qualify as qualify_mod
from chd_curator.atlas import Digests, FindingDigests
from chd_curator.cli import app
from chd_curator.qualify import QualifyError, score_triage, triage_gold
from chd_curator.register import load_register
from chd_curator.triage import NO_ABSTRACT, RISKY, audit_sample, read_answers, trusted_exclusions

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


# ------------------------------------------------------------------ thin hits


def test_a_thin_hit_is_checked_whatever_class_the_screen_gave_it() -> None:
    """`triage-combine` never lets the screen's word be final on a hit PubMed had no abstract
    for; the scorer passes the same `thin` set, so the same hit is owed to the check."""
    gold = gold_of(**{str(n): 0 for n in range(100, 140)}, **{"1": 3})
    screen = {k: exc(k, "not_human_genetics") for k in gold}
    seed = next(
        s
        for s in map(str, range(50))
        if "PMID:1" not in audit_sample(trusted_exclusions(screen, RISKY), s)
    )
    audit = audit_sample(trusted_exclusions(screen, RISKY), seed)
    check = {k: exc(k, "not_human_genetics") if k in audit else inc(k) for k in gold}
    assert score_triage(gold, screen, check, seed).verdict == "FAIL"
    found = score_triage(gold, screen, check, seed, thin={"PMID:1"})
    assert (found.verdict, found.missed, found.sent_to_check) == ("PASS", [], 1)
    unanswered = {k: v for k, v in check.items() if k != "PMID:1"}
    owed = score_triage(gold, screen, unanswered, seed, thin={"PMID:1"})
    assert (owed.verdict, owed.missed, owed.owed) == ("INCOMPLETE", [], ["PMID:1"])


# ------------------------------------------------------------------ the gold and the CLI


def _digests(*void: str) -> Digests:
    ids = ["CHDA:LIT:0000001", "CHDA:LIT:0000002", "CHDA:LIT:0000003"]
    rows = tuple(FindingDigests(i, "f", "s", i != ids[2], False, i in void) for i in ids)
    return Digests("HGNC:1", "ABC1", "x", rows)


@pytest.fixture(autouse=True)
def _no_void(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qualify_mod, "digests", lambda root, symbol, cli: _digests())


def _atlas(tmp_path: Path, triage: list[Any] | None = None) -> Path:
    lit = tmp_path / "curation" / "literature"
    lit.mkdir(parents=True)
    record = {
        "gene": "HGNC:1",
        "triage": triage
        or [
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
    gold = triage_gold(_atlas(tmp_path), "ABC1", Path("atlas-cli"))
    assert gold == {
        "PMID:1": {"decision": "included", "reason": "none", "findings": 2},
        "PMID:2": {"decision": "included", "reason": "none", "findings": 0},
        "PMID:3": {"decision": "excluded", "reason": "not_chd", "findings": 0},
    }  # a pending hit is not gold


def test_a_void_approval_is_not_an_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One valid and one void approval (the atlas's own `approval_void`, LIT015) for one PMID."""
    monkeypatch.setattr(
        qualify_mod, "digests", lambda root, symbol, cli: _digests("CHDA:LIT:0000002")
    )
    gold = triage_gold(_atlas(tmp_path), "ABC1", Path("atlas-cli"))
    assert gold["PMID:1"]["findings"] == 1


@pytest.mark.parametrize(
    ("entry", "named"),
    [({"search": 1, "decision": "included"}, "triage[1]"), ("PMID:9", "triage[1]")],
)
def test_a_malformed_triage_entry_is_named_by_its_index(
    tmp_path: Path, entry: Any, named: str
) -> None:
    root = _atlas(tmp_path, [{"pmid": "PMID:1", "decision": "included"}, entry])
    with pytest.raises(QualifyError, match=r"triage\[1\]"):
        triage_gold(root, "ABC1", Path("atlas-cli"))


def test_a_proposed_entry_states_the_bias_the_spec_requires(tmp_path: Path) -> None:
    result, _ = _run(tmp_path)
    entry = _entry(result)
    assert (
        "Bias: today's gold was produced by the current Claude models plus the owner's "
        "corrections, so it favours them." in entry["note"]
    )


def _rows(path: Path, answers: Mapping[str, dict[str, Any]], **extra: str) -> None:
    path.write_text(
        "".join(json.dumps({"key": k, "parsed": v, **extra}) + "\n" for k, v in answers.items()),
        encoding="utf-8",
    )


SITE = "roles:\n  triage-screen:\n    id: s\n    kind: fake\n    model: m1\n"
SITE += "    settings: {temperature: 0, base_url: http://host.example}\n"
SITE += "    context_tokens: 100\n    concurrency: 1\n"
RULES = "sha256:" + "a" * 64
KEY = "fake:m1@" + __import__("hashlib").sha256(b'{"temperature":0}').hexdigest()[:12]


def _run(
    tmp_path: Path,
    screen_rows: Mapping[str, dict[str, Any]] | None = None,
    check_rows: Mapping[str, dict[str, Any]] | None = None,
    extra: list[str] | None = None,
    **meta: str,
) -> tuple[Any, Path]:
    root = _atlas(tmp_path / "atlas")
    screen, check, site = (
        tmp_path / "screen.jsonl",
        tmp_path / "check.jsonl",
        tmp_path / "site.yaml",
    )
    site.write_text(SITE, encoding="utf-8")
    meta = {"model_key": KEY, "rules_digest": RULES, **meta}
    _rows(
        screen,
        screen_rows
        or {"PMID:1": inc("PMID:1"), "PMID:2": inc("PMID:2"), "PMID:3": exc("PMID:3", "not_chd")},
        **meta,
    )
    _rows(check, check_rows or {"PMID:3": exc("PMID:3", "not_chd")})
    args = ["qualify", "triage", "ABC1", "--screen", str(screen), "--check", str(check)]
    args += ["--run", "r1", "--gold-root", str(root), "--site", str(site), *(extra or [])]
    return runner.invoke(app, args), tmp_path


def _entry(result: Any) -> dict[str, Any]:
    entry: dict[str, Any] = _yaml.load(result.output[result.output.index("entries:") :])["entries"][
        0
    ]
    return entry


def test_the_cli_prints_a_register_entry_the_loader_accepts_and_writes_nothing(
    tmp_path: Path,
) -> None:
    result, _ = _run(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output and "thin-hit routing was not applied" in result.output
    entry = _entry(result)
    assert (entry["role"], entry["verdict"], entry["gold"]) == ("triage-screen", "pass", "ABC1")
    assert entry["scores"]["missed"] == 0 and entry["scores"]["with_findings"] == 1
    assert entry["model_key"] == KEY and entry["rules_digest"] == RULES
    assert entry["backend"] == {"kind": "fake", "model": "m1", "settings": {"temperature": 0}}
    path = tmp_path / "register.yaml"
    path.write_text(result.output[result.output.index("entries:") :], encoding="utf-8")
    assert [e.model_key for e in load_register(path)] == [KEY]  # no transport key, no error
    assert sorted(p.name for p in tmp_path.rglob("*")) == sorted([*before, "register.yaml"])


def test_a_screen_not_made_by_this_sites_model_is_warned_about(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, model_key="fake:other")
    assert result.exit_code == 0
    assert "fake:other" in result.stderr and KEY in result.stderr


def test_screen_answers_that_disagree_on_the_rules_exit_2(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, rules_digest="")
    assert result.exit_code == 2 and "rules_digest" in result.output
    screen = tmp_path / "screen.jsonl"
    rows = screen.read_text(encoding="utf-8").splitlines()
    rows[0] = rows[0].replace(RULES, "sha256:" + "b" * 64)
    screen.write_text("\n".join(rows) + "\n", encoding="utf-8")
    again = runner.invoke(app, _args(tmp_path))
    assert again.exit_code == 2 and "rules_digest" in again.output


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "qualify", "triage", "ABC1", "--screen", str(tmp_path / "screen.jsonl"),
        "--check", str(tmp_path / "check.jsonl"), "--run", "r1",
        "--gold-root", str(tmp_path / "atlas"), "--site", str(tmp_path / "site.yaml"), *extra,
    ]  # fmt: skip


def test_an_incomplete_run_exits_1_and_proposes_no_entry(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, check_rows={"PMID:1": inc("PMID:1")})  # PMID:3 is owed
    assert result.exit_code == 1
    assert "INCOMPLETE" in result.output and "PMID:3" in result.output
    assert "entries:" not in result.output


def test_a_screen_answer_outside_the_genes_hits_exits_2(tmp_path: Path) -> None:
    rows = {"PMID:1": inc("PMID:1"), "PMID:99": inc("PMID:99")}
    result, _ = _run(tmp_path, screen_rows=rows)
    assert result.exit_code == 2 and "PMID:99" in result.output


def test_a_screen_that_did_not_answer_every_gold_hit_exits_2_naming_them(tmp_path: Path) -> None:
    """R-SWA-52: `ask` writes a row per request, error rows included, so a gold key with no row
    means the gene was not fully asked; filling it with {} scored a 1-of-3 screen PASS."""
    result, _ = _run(tmp_path, screen_rows={"PMID:1": inc("PMID:1")})
    assert result.exit_code == 2, result.output
    assert "PMID:2" in result.output and "PMID:3" in result.output
    assert "entries:" not in result.output
    # An error row is an answer of sorts: the hit goes to the check, the gene was asked.
    screen = tmp_path / "screen.jsonl"
    with screen.open("a", encoding="utf-8") as handle:
        for key in ("PMID:2", "PMID:3"):
            row = {"key": key, "error": "gave up", "attempts": 3}
            handle.write(json.dumps({**row, "model_key": KEY, "rules_digest": RULES}) + "\n")
    check = {"PMID:2": inc("PMID:2"), "PMID:3": exc("PMID:3", "not_chd")}
    _rows(tmp_path / "check.jsonl", check)
    asked = runner.invoke(app, _args(tmp_path))
    assert asked.exit_code == 0, asked.output


def test_a_missing_check_file_is_a_usage_error(tmp_path: Path) -> None:
    _run(tmp_path)
    (tmp_path / "check.jsonl").unlink()
    assert runner.invoke(app, _args(tmp_path)).exit_code == 2


def test_requests_are_read_for_thin_hits_and_their_absence_is_said(tmp_path: Path) -> None:
    """The routing itself is pinned by the scorer test above; here the CLI reads the file, says
    nothing about routing being skipped when it has one, and refuses an unreadable one."""
    thin = {
        "key": "PMID:1",
        "gene": "ABC1",
        "inputs": {
            "gene": "ABC1",
            "hit": f"PMID:1 · 2020 · T\nPublication types: x\n{NO_ABSTRACT}",
        },
    }
    requests = tmp_path / "req.jsonl"
    without, _ = _run(tmp_path)
    requests.write_text(json.dumps(thin) + "\n", encoding="utf-8")
    given = runner.invoke(app, _args(tmp_path, "--requests", str(requests)))
    assert given.exit_code == 0 and "not applied" not in given.output
    assert "thin-hit routing was not applied" in without.output
    missing = runner.invoke(app, _args(tmp_path, "--requests", str(tmp_path / "no.jsonl")))
    assert missing.exit_code == 2
