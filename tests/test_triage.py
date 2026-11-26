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
from chd_curator.triage import RISKY, audit_sample, combine, to_check
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


def _setup(tmp_path: Path, reasons: dict[str, str]) -> None:
    _write(
        tmp_path / "req.jsonl", [{"key": k, "inputs": {"gene": "G", "hit": "h"}} for k in reasons]
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


def _decisions(path: Path, decisions: dict[str, Any], awaiting: list[str] | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "format": 1,
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
