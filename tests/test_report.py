"""``chd-curator report``: tiers, cost and how each finding was settled.

Every count is pinned on a run folder where the counts differ from one another (CLAUDE.md
section 4.36): a report that swaps two of them, or reads one count from another's file, fails.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from chd_curator.cli import app
from chd_curator.register import Entry
from chd_curator.report import ReportError, write_report
from chd_curator.run import AssembleRecord, Comparison, RunPaths, write_assemble, write_comparison
from chd_curator.site import BackendSpec, model_key

runner = CliRunner()
SHA = "a" * 64


def _fid(n: int) -> str:
    return f"CHDA:LIT:{n:07d}"


def make_run(
    root: Path,
    *,
    verified: Sequence[int] = (),
    judged: Sequence[int] = (),
    fixed: Sequence[int] = (),
    placed: Sequence[int] = (),
    unplaced: Sequence[int] = (),
    assemble: bool = True,
) -> Path:
    p = RunPaths(root, "TBX5", "r1")
    p.dir.mkdir(parents=True)
    write_comparison(
        p, Comparison({}, tuple(_fid(n) for n in verified), (), {_fid(n): ("x",) for n in judged})
    )
    if assemble:
        write_assemble(
            p,
            AssembleRecord(
                replaced=tuple(_fid(n) for n in judged),
                added={},
                notation=(),
                verified=tuple(_fid(n) for n in verified),
                verifier="agent:blind",
                judged={_fid(n): SHA for n in judged},
                approvals={_fid(n): None for n in judged},
                blind_only={},
            ),
        )
    entries = [
        {"finding": _fid(n), "variant": 1, "outcome": "placed", "line": "l", "reading": None}
        for n in placed
    ] + [
        {"finding": _fid(n), "variant": 1, "outcome": "unplaced", "line": "l", "reading": None}
        for n in unplaced
    ]
    if entries:
        (p.dir / "normalise.json").write_text(
            json.dumps({"format": 1, "normalisation": None, "variants": entries}), "utf-8"
        )
    if fixed:
        p.decisions.mkdir()
        rows = [{"id": _fid(n), "decision": "fix"} for n in fixed] + [
            {"id": _fid(900 + n), "decision": "approve"} for n in range(3)
        ]
        (p.decisions / "review.json").write_text(
            json.dumps({"format": 1, "findings": rows}), "utf-8"
        )
    return p.dir


def answers_file(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    return path


def row(
    key: str,
    usage: Mapping[str, int],
    seconds: float,
    *,
    model_key_: str = "fake:m",
    rules: str = "r1",
) -> dict[str, Any]:
    return {
        "key": key,
        "parsed": {},
        "attempts": 1,
        "usage": dict(usage),
        "seconds": seconds,
        "model_key": model_key_,
        "input_digest": "d",
        "rules_digest": rules,
    }


def _report(run_dir: Path, answers: list[tuple[str, Path]], out: Path, **kw: Any) -> dict[str, Any]:
    write_report(run_dir, answers, kw.pop("trace", None), out, register=kw.pop("register", []))
    return json.loads((out / "report.json").read_text("utf-8"))


def test_the_summary_line_counts_each_way_a_finding_was_settled(tmp_path: Path) -> None:
    """Agreed 2, judged 3, corrected 1, normalised 4 (placed only; one unplaced is not):
    all different, so a report reading one count from another's file fails. Local tokens are
    100 (``total_tokens``, not prompt + completion + total) of 800: 12.5%, rounded half up."""
    run = make_run(
        tmp_path / "root",
        verified=[1, 2],
        judged=[3, 4, 5],
        fixed=[3],
        placed=[1, 2, 3, 4],
        unplaced=[5],
    )
    screen = answers_file(
        tmp_path / "s.jsonl",
        [row("a", {"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100}, 2.0)],
    )
    extract = answers_file(
        tmp_path / "e.jsonl",
        [row("b", {"input_tokens": 200, "output_tokens": 500}, 10.5)],
    )
    report = _report(run, [("triage-screen", screen), ("extract", extract)], tmp_path / "out")
    assert report["findings"] == {
        "total": 5,
        "agreed": 2,
        "judged": 3,
        "corrected_by_owner": 1,
        "normalised": 4,
    }
    assert (
        report["summary"]
        == "5 findings: 2 agreed, 3 judged, 1 corrected by the owner; 13% of tokens local"
    )
    by_role = {p["role"]: p for p in report["processes"]}
    assert by_role["triage-screen"]["tier"] == "local"
    assert by_role["extract"]["tier"] == "capable"
    assert by_role["triage-screen"]["tokens"] == 100
    assert by_role["extract"]["tokens"] == 700
    assert by_role["extract"]["seconds"] == 10.5
    md = (tmp_path / "out" / "report.md").read_text("utf-8")
    assert report["summary"] in md


@pytest.mark.parametrize(
    ("kwargs", "line"),
    [
        (
            {"verified": [1, 2, 3]},
            "3 findings: 3 agreed, 0 judged, 0 corrected by the owner",
        ),
        (
            {"judged": [1, 2, 3, 4], "fixed": [2]},
            "4 findings: 0 agreed, 4 judged, 1 corrected by the owner",
        ),
    ],
)
def test_a_fixture_where_all_findings_agree_and_one_where_none_do(
    tmp_path: Path, kwargs: dict[str, Any], line: str
) -> None:
    run = make_run(tmp_path / "root", **kwargs)
    report = _report(run, [], tmp_path / "out")
    assert report["summary"] == line + "; no tokens recorded"


def test_no_local_tokens_is_zero_percent_and_no_tokens_is_said(tmp_path: Path) -> None:
    run = make_run(tmp_path / "root", verified=[1])
    extract = answers_file(
        tmp_path / "e.jsonl", [row("b", {"input_tokens": 5, "output_tokens": 5}, 1)]
    )
    assert _report(run, [("extract", extract)], tmp_path / "o1")["summary"].endswith(
        "; 0% of tokens local"
    )
    empty = answers_file(tmp_path / "z.jsonl", [row("b", {}, 0)])
    assert _report(run, [("extract", empty)], tmp_path / "o2")["summary"].endswith(
        "; no tokens recorded"
    )


def test_a_run_with_no_assemble_counts_no_findings_and_says_so(tmp_path: Path) -> None:
    run = make_run(tmp_path / "root", assemble=False)
    report = _report(run, [], tmp_path / "out")
    assert report["findings"]["total"] == 0
    assert any("assemble.json" in w for w in report["warnings"])


def test_a_later_decisions_file_overrides_an_earlier_one_per_finding(tmp_path: Path) -> None:
    run = make_run(tmp_path / "root", judged=[1, 2], fixed=[1, 2])
    (run / "decisions" / "review.z.json").write_text(
        json.dumps({"format": 1, "findings": [{"id": _fid(2), "decision": "approve"}]}), "utf-8"
    )
    assert _report(run, [], tmp_path / "out")["findings"]["corrected_by_owner"] == 1


def test_failed_and_cached_answers_are_counted_apart(tmp_path: Path) -> None:
    run = make_run(tmp_path / "root", verified=[1])
    cached = row("c", {}, 0)
    cached["attempts"] = 0
    rows = [
        row("a", {"input_tokens": 1, "output_tokens": 2}, 1.25),
        cached,
        {"key": "x", "error": "e", "attempts": 3},
    ]
    f = answers_file(tmp_path / "e.jsonl", rows)
    (proc,) = _report(run, [("extract", f)], tmp_path / "out")["processes"]
    assert (proc["answers"], proc["cached"], proc["failed"], proc["tokens"]) == (3, 1, 1, 3)


TRACE_HEAD = "task_id\tname\tstatus\tprocess\tcpus\trealtime\n"


def test_core_hours_come_from_cpus_times_realtime_of_the_matching_process(tmp_path: Path) -> None:
    """BLIND_READ: 4 cpus x 1h 30m = 6 h, plus 2 cpus x 30m 36s = 1.02 h: 7.02. EXTRACT: 1 x
    36 s = 0.01. A CACHED task spent none; a process no role carries is named, not summed in."""
    trace = tmp_path / "trace.txt"
    trace.write_text(
        TRACE_HEAD
        + "1\tBLIND_READ (1)\tCOMPLETED\tBLIND_READ\t4\t1h 30m\n"
        + "2\tBLIND_READ (2)\tCOMPLETED\twf:BLIND_READ\t2\t30m 36s\n"
        + "3\tEXTRACT (1)\tCOMPLETED\tEXTRACT\t1\t36s\n"
        + "4\tEXTRACT (2)\tCACHED\tEXTRACT\t8\t-\n"
        + "5\tFETCH (1)\tCOMPLETED\tFETCH_TEXTS\t1\t350ms\n",
        "utf-8",
    )
    run = make_run(tmp_path / "root", verified=[1])
    blind = answers_file(tmp_path / "b.jsonl", [row("a", {"input_tokens": 1}, 1)])
    report = _report(run, [("blind-read", blind)], tmp_path / "out", trace=trace)
    by_role = {p["role"]: p for p in report["processes"]}
    assert by_role["blind-read"]["core_hours"] == pytest.approx(7.02)
    assert by_role["extract"]["core_hours"] == pytest.approx(0.01)
    assert by_role["extract"]["answers"] == 0
    assert report["trace_unmatched_processes"] == ["FETCH_TEXTS"]


def test_a_trace_missing_columns_or_with_an_unreadable_realtime_is_refused(tmp_path: Path) -> None:
    run = make_run(tmp_path / "root", verified=[1])
    bad = tmp_path / "t1.txt"
    bad.write_text("process\tstatus\nEXTRACT\tCOMPLETED\n", "utf-8")
    with pytest.raises(ReportError, match="cpus, realtime"):
        write_report(run, [], bad, tmp_path / "o1", register=[])
    worse = tmp_path / "t2.txt"
    worse.write_text(TRACE_HEAD + "1\tn\tCOMPLETED\tEXTRACT\t1\tsoon\n", "utf-8")
    with pytest.raises(ReportError, match="realtime"):
        write_report(run, [], worse, tmp_path / "o2", register=[])


def _spec(model: str) -> BackendSpec:
    return BackendSpec(
        id="b",
        kind="anthropic",
        model=model,
        settings={},
        context_tokens=1,
        concurrency=1,
        key_env=None,
    )


def _entry(role: str, model: str, rules: str) -> Entry:
    return Entry(
        role=role,
        model_key=model_key(_spec(model)),
        verdict="pass",
        gold="g",
        gold_digest="d",
        rules_digest=rules,
        date="2026-11-26",
        scores={},
        note="n",
    )


def test_a_role_that_ran_on_other_rules_than_it_was_qualified_on_is_warned_about(
    tmp_path: Path,
) -> None:
    key = model_key(_spec("m"))
    register = [_entry("extract", "m", "qualified"), _entry("judge", "m", "same")]
    run = make_run(tmp_path / "root", verified=[1])
    extract = answers_file(
        tmp_path / "e.jsonl",
        [
            row("a", {}, 1, model_key_=key, rules="changed"),
            row("b", {}, 1, model_key_=key, rules="changed"),
            row("c", {}, 1, model_key_=key, rules="qualified"),
        ],
    )
    judge = answers_file(tmp_path / "j.jsonl", [row("a", {}, 1, model_key_=key, rules="same")])
    synthetic = answers_file(
        tmp_path / "f.jsonl", [row("a", {}, 1, model_key_="fake:x", rules="whatever")]
    )
    report = _report(
        run,
        [("extract", extract), ("judge", judge), ("blind-read", synthetic)],
        tmp_path / "out",
        register=register,
    )
    assert report["warnings"] == [
        "warning: extract ran on rules that changed since its qualification (2 answers)"
    ]
    assert report["warnings"][0] in (tmp_path / "out" / "report.md").read_text("utf-8")


def test_an_unknown_role_or_a_run_dir_that_is_not_a_run_is_refused(tmp_path: Path) -> None:
    run = make_run(tmp_path / "root", verified=[1])
    f = answers_file(tmp_path / "e.jsonl", [row("a", {}, 1)])
    with pytest.raises(ReportError, match="role"):
        write_report(run, [("nonsense", f)], None, tmp_path / "o", register=[])
    with pytest.raises(ReportError, match="local/literature/runs"):
        write_report(tmp_path, [], None, tmp_path / "o", register=[])


def test_the_command_writes_the_two_files_and_prints_the_summary(tmp_path: Path) -> None:
    run = make_run(tmp_path / "root", verified=[1, 2], judged=[3])
    f = answers_file(tmp_path / "e.jsonl", [row("a", {"input_tokens": 4}, 1)])
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        ["report", "--run-dir", str(run), "--answers", f"extract={f}", "--out-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert (
        "3 findings: 2 agreed, 1 judged, 0 corrected by the owner; 0% of tokens local"
        in result.output
    )
    assert sorted(p.name for p in out.iterdir()) == ["report.json", "report.md"]
    text = (out / "report.json").read_text("utf-8")
    assert text == json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    bad = runner.invoke(
        app, ["report", "--run-dir", str(run), "--answers", "nonsense", "--out-dir", str(out)]
    )
    assert bad.exit_code == 2
    assert "<role>=<path>" in bad.output


def test_a_decision_for_a_finding_outside_the_run_is_listed_and_not_counted(tmp_path: Path) -> None:
    run = make_run(tmp_path / "root", judged=[1])
    (run / "decisions").mkdir()
    (run / "decisions" / "review.json").write_text(
        json.dumps(
            {
                "format": 1,
                "findings": [
                    {"id": _fid(1), "decision": "fix"},
                    {"id": _fid(777), "decision": "fix"},
                ],
            }
        ),
        "utf-8",
    )
    report = _report(run, [], tmp_path / "out")
    assert report["findings"]["corrected_by_owner"] == 1
    assert report["decisions_outside_run"] == [_fid(777)]
    (warning,) = report["warnings"]
    assert _fid(777) in warning
    assert warning in (tmp_path / "out" / "report.md").read_text("utf-8")


def test_a_role_on_a_model_with_no_register_entry_is_noted_but_a_test_backend_is_not(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path / "root", verified=[1])
    key = model_key(_spec("m"))
    a = answers_file(
        tmp_path / "a.jsonl", [row("a", {}, 1, model_key_=key), row("b", {}, 1, model_key_=key)]
    )
    b = answers_file(tmp_path / "b.jsonl", [row("a", {}, 1, model_key_="replay:x")])
    c = answers_file(tmp_path / "c.jsonl", [row("a", {}, 1, model_key_="fake:x")])
    report = _report(
        run, [("extract", a), ("blind-read", b), ("judge", c)], tmp_path / "out", register=[]
    )
    note = (
        f"note: extract ran a model with no register entry ({key}); "
        "its answers were recorded in shadow or unqualified"
    )
    assert report["notes"] == [note]
    assert report["unqualified_models"] == [{"role": "extract", "model_key": key, "answers": 2}]
    assert note in (tmp_path / "out" / "report.md").read_text("utf-8")


@pytest.mark.parametrize(("role", "percent"), [("triage-screen", "100"), ("extract", "0")])
def test_the_summary_says_100_percent_local_and_0_percent_local(
    tmp_path: Path, role: str, percent: str
) -> None:
    run = make_run(tmp_path / "root", verified=[1])
    f = answers_file(tmp_path / "a.jsonl", [row("a", {"total_tokens": 9}, 1)])
    summary = _report(run, [(role, f)], tmp_path / "out")["summary"]
    assert summary == (
        f"1 findings: 1 agreed, 0 judged, 0 corrected by the owner; {percent}% of tokens local"
    )
