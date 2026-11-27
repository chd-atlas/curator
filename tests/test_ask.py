"""``chd-curator ask``: the validated structured call, against the fake backend only.

No test calls a model or the network. Triage cases assemble against the small rules file in
``tests/fixtures/roles``; the ``check-batch`` cases use the shared run fixture and a stub atlas
CLI (the same ones ``test_check.py`` uses), so the contract files and their removal are measured
on real paths.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator import ask as ask_module
from chd_curator.ask import AskSummary, AskUsageError, ask
from chd_curator.backends import Answer, Request
from chd_curator.backends.fake import Fake
from chd_curator.cli import app
from chd_curator.site import BackendSpec, Site
from test_check import OK_LINE, _fake, _stdout  # noqa: F401
from test_merge import _fresh

HERE = Path(__file__).resolve().parent
RULES_ROOT = HERE / "fixtures" / "roles"
YAML_SAFE = YAML(typ="safe")
runner = CliRunner()
PMID = "PMID:91000001"


def triage(
    pmid: str = PMID, decision: str = "included", reason: str = "none", why: str = "ok"
) -> str:
    return json.dumps({"pmid": pmid, "decision": decision, "reason": reason, "why": why})


def spec(
    answers: dict[str, list[Any]] | None = None,
    *,
    concurrency: int = 1,
    context: int = 100_000,
    **settings: Any,
) -> BackendSpec:
    return BackendSpec(
        "t", "fake", "m", {"answers": answers or {}, **settings}, context, concurrency, None
    )


def site_of(b: BackendSpec, role: str = "triage-screen") -> Site:
    return Site(roles={role: b})


def requests_file(tmp_path: Path, keys: list[str], **extra: Any) -> Path:
    path = tmp_path / "req.jsonl"
    rows = [{"key": k, "inputs": {"gene": "TBX5", "hit": f"{k}\nA title"}, **extra} for k in keys]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def run_ask(
    tmp_path: Path,
    b: BackendSpec,
    keys: list[str],
    *,
    role: str = "triage-screen",
    cache: Path | None = None,
    shadow: bool = False,
    fake: Fake | None = None,
    sleep: Callable[[float], None] = lambda _s: None,
) -> tuple[AskSummary, list[dict[str, Any]], Fake]:
    backend = fake or Fake(b)
    out = tmp_path / "answers.jsonl"
    summary = ask(
        role,
        requests_file(tmp_path, keys),
        out,
        site_of(b, role),
        [],
        RULES_ROOT,
        cache,
        "schema",
        None,
        shadow,
        make=lambda _spec: backend,
        sleep=sleep,
    )
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    return summary, rows, backend


# ---- the plan's ten ----------------------------------------------------------------------------


def test_a_cached_answer_is_returned_without_asking(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    b = spec({PMID: [triage()]})
    first, rows1, _ = run_ask(tmp_path, b, [PMID], cache=cache)
    second, rows2, fake2 = run_ask(tmp_path, b, [PMID], cache=cache)
    assert (first.asked, first.cached) == (1, 0) and (second.asked, second.cached) == (0, 1)
    assert fake2.requests == []  # the second backend had no script left and was never called
    assert rows2[0]["attempts"] == 0 and rows2[0]["parsed"] == rows1[0]["parsed"]
    assert set(rows2[0]) == {
        "key", "parsed", "attempts", "usage", "seconds", "model_key", "input_digest",
        "rules_digest", "meta",
    }  # fmt: skip
    path = next((cache / "triage-screen").iterdir())
    assert path.name == (
        hashlib.sha256(f"{rows1[0]['model_key']}\0{rows1[0]['input_digest']}".encode()).hexdigest()
        + ".json"
    )


def test_a_settings_change_misses_the_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    run_ask(tmp_path, spec({PMID: [triage()]}, seed=1), [PMID], cache=cache)
    second, _, fake = run_ask(tmp_path, spec({PMID: [triage()]}, seed=2), [PMID], cache=cache)
    assert second.cached == 0 and len(fake.requests) == 1
    assert len(list((cache / "triage-screen").iterdir())) == 2


def test_two_invalid_then_a_valid_answer_succeeds_on_the_third_attempt(tmp_path: Path) -> None:
    b = spec({PMID: ["not json", "{}", triage()]})
    summary, rows, fake = run_ask(tmp_path, b, [PMID])
    assert rows[0]["attempts"] == 3 and summary.failed == []
    assert len(fake.requests) == 3


def test_three_invalid_answers_write_an_error_row_and_exit_1(tmp_path: Path) -> None:
    b = spec({PMID: ["x", "y", "z", triage()]})
    summary, rows, fake = run_ask(tmp_path, b, [PMID])
    assert summary.failed == [PMID] and len(fake.requests) == 3
    assert set(rows[0]) == {"key", "error", "attempts", "usage", "seconds"}
    assert rows[0]["attempts"] == 3
    out = tmp_path / "answers.jsonl"
    site = tmp_path / "site.yaml"
    site.write_text(
        "roles:\n  triage-screen:\n    id: t\n    kind: fake\n    model: m\n"
        "    context_tokens: 100000\n    concurrency: 1\n    settings:\n      answers:\n"
        f"        '{PMID}': [x, y, z]\n"
    )
    args = ["--role", "triage-screen", "--requests", str(requests_file(tmp_path, [PMID])),
            "--answers", str(out), "--site", str(site), "--root", str(RULES_ROOT)]  # fmt: skip
    bad = runner.invoke(app, ["ask", *args])
    assert bad.exit_code == 1 and PMID in bad.output
    ok = runner.invoke(app, ["ask", *args, "--allow-errors"])
    assert ok.exit_code == 0 and "failed 1 (PMID:91000001)" in ok.output


def test_the_retry_carries_the_validators_errors_verbatim(tmp_path: Path) -> None:
    bad = triage(decision="excluded", reason="none")
    b = spec({PMID: [bad, triage()]})
    _, rows, fake = run_ask(tmp_path, b, [PMID])
    first, second = fake.requests
    assert second.user.startswith(first.user)
    assert "reason: an excluded hit needs a reason other than none" in second.user
    assert second.user != first.user and rows[0]["attempts"] == 2
    # the cache key is the original input's, not the retry's
    assert (
        rows[0]["input_digest"]
        == ask_module.assemble(
            "triage-screen", RULES_ROOT, {"gene": "TBX5", "hit": f"{PMID}\nA title"}
        ).digest
    )


def test_an_input_larger_than_the_context_is_refused_before_any_call(tmp_path: Path) -> None:
    b = spec({PMID: [triage()]}, context=2000, max_tokens=100)
    big = tmp_path / "req.jsonl"
    big.write_text(json.dumps({"key": PMID, "inputs": {"gene": "TBX5", "hit": "x" * 9000}}) + "\n")
    fake = Fake(b)
    ask("triage-screen", big, tmp_path / "a.jsonl", site_of(b), [], RULES_ROOT, None, "schema",
        None, False, make=lambda _s: fake)  # fmt: skip
    row = json.loads((tmp_path / "a.jsonl").read_text())
    assert fake.requests == [] and row["attempts"] == 0
    assert "over the backend's context of 2000" in row["error"]


def test_transient_errors_do_not_spend_validation_retries(tmp_path: Path) -> None:
    t = {"transient": True}
    b = spec({PMID: [t, "bad", t, "bad", t, triage()]})
    slept: list[float] = []
    _, rows, _ = run_ask(tmp_path, b, [PMID], sleep=slept.append)
    assert rows[0]["attempts"] == 3 and "parsed" in rows[0]
    assert slept == [5, 20, 60]


def test_a_fourth_transient_error_is_an_error_row(tmp_path: Path) -> None:
    t = {"transient": True}
    slept: list[float] = []
    summary, rows, _ = run_ask(tmp_path, spec({PMID: [t, t, t, t]}), [PMID], sleep=slept.append)
    assert slept == [5, 20, 60] and summary.failed == [PMID]
    assert "transient" in rows[0]["error"]


def test_a_backend_error_is_an_error_row_at_once(tmp_path: Path) -> None:
    summary, rows, fake = run_ask(tmp_path, spec({PMID: [{"error": "HTTP 400: nope"}]}), [PMID])
    assert rows[0]["error"] == "HTTP 400: nope" and len(fake.requests) == 1
    assert summary.failed == [PMID]


def test_a_rerun_asks_only_the_keys_without_a_cached_answer(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    keys = ["PMID:1", "PMID:2", "PMID:3"]
    script = {k: [triage(k)] for k in keys}
    b = spec(script)  # one spec: the script is part of the model key, so it must not change
    run_ask(tmp_path, b, keys[:2], cache=cache)
    summary, rows, fake = run_ask(tmp_path, b, keys, cache=cache)
    assert [r.key for r in fake.requests] == ["PMID:3"]
    assert (summary.asked, summary.cached) == (1, 2)
    assert [r["key"] for r in rows] == keys and [r["attempts"] for r in rows] == [0, 0, 1]


def test_an_answer_for_an_unrequested_or_repeated_key_is_refused(tmp_path: Path) -> None:
    class Wrong(Fake):
        def ask(self, req: Request) -> Answer:
            return Answer("PMID:999", triage(), {}, 0.0, {})

    cache = tmp_path / "cache"
    b = spec()
    summary, rows, _ = run_ask(tmp_path, b, [PMID], fake=Wrong(b), cache=cache)
    assert summary.failed == [PMID] and "PMID:999" in rows[0]["error"]
    assert not (cache / "triage-screen").exists()  # nothing refused is cached
    path = tmp_path / "dup.jsonl"
    row = json.dumps({"key": PMID, "inputs": {"gene": "TBX5", "hit": "h"}}) + "\n"
    path.write_text(row + row)
    with pytest.raises(AskUsageError, match="PMID:91000001.*twice"):
        ask("triage-screen", path, tmp_path / "a.jsonl", site_of(b), [], RULES_ROOT, None,
            "schema", None, False, make=lambda _s: Fake(b))  # fmt: skip
    assert not (tmp_path / "a.jsonl").exists()


def test_an_unqualified_backend_exits_2_unless_shadow(tmp_path: Path) -> None:
    unqualified = BackendSpec("t", "openai_compat", "m", {}, 100_000, 1, None)
    fake = Fake(spec({PMID: [triage()]}))
    kwargs: dict[str, Any] = {"make": lambda _s: fake}
    args = ("triage-screen", requests_file(tmp_path, [PMID]), tmp_path / "a.jsonl",
            site_of(unqualified), [], RULES_ROOT, None, "schema", None)  # fmt: skip
    with pytest.raises(AskUsageError, match=r"triage-screen.*openai_compat:m.*not qualified"):
        ask(*args, False, **kwargs)
    assert fake.requests == []
    assert ask(*args, True, **kwargs).failed == []
    site = tmp_path / "site.yaml"
    site.write_text(
        "roles:\n  triage-screen:\n    id: t\n    kind: openai_compat\n    model: m\n"
        "    context_tokens: 100000\n    concurrency: 1\n"
    )
    cli = ["ask", "--role", "triage-screen", "--requests", str(requests_file(tmp_path, [PMID])),
           "--answers", str(tmp_path / "b.jsonl"), "--site", str(site),
           "--root", str(RULES_ROOT)]  # fmt: skip
    refused = runner.invoke(app, cli)
    assert refused.exit_code == 2 and "not qualified" in refused.output


@pytest.mark.parametrize("role", ["triage-check", "extract", "blind-read", "judge"])
def test_shadow_is_for_the_screen_only_and_any_other_role_exits_2_before_any_call(
    tmp_path: Path, role: str
) -> None:
    """R-SWA-41: in shadow only the screen is recorded and scored; every other role's answers
    decide something (the check's exclusions, the readers' and judge's findings), so a model
    the register does not qualify may not fill it, shadow or not. Even a test backend is
    refused, so the refusal comes before the register is read."""
    fake = Fake(spec({PMID: [triage()]}))
    with pytest.raises(AskUsageError, match=rf"--shadow.*triage-screen.*{role}|{role}.*shadow"):
        ask(role, requests_file(tmp_path, [PMID]), tmp_path / "a.jsonl", site_of(spec(), role),
            [], RULES_ROOT, None, "schema", None, True, make=lambda _s: fake)  # fmt: skip
    assert fake.requests == [] and not (tmp_path / "a.jsonl").exists()
    site = tmp_path / "site.yaml"
    site.write_text(
        f"roles:\n  {role}:\n    id: t\n    kind: fake\n    model: m\n"
        "    context_tokens: 100000\n    concurrency: 1\n"
    )
    cli = ["ask", "--role", role, "--requests", str(requests_file(tmp_path, [PMID])),
           "--answers", str(tmp_path / "b.jsonl"), "--site", str(site),
           "--root", str(RULES_ROOT), "--shadow"]  # fmt: skip
    refused = runner.invoke(app, cli)
    assert refused.exit_code == 2 and "shadow" in refused.output
    assert not (tmp_path / "b.jsonl").exists()


def test_no_more_than_concurrency_calls_in_flight(tmp_path: Path) -> None:
    keys = [f"PMID:{n}" for n in range(1, 7)]
    b = spec({k: [triage(k)] for k in keys}, concurrency=2, delay=0.05)
    summary, rows, fake = run_ask(tmp_path, b, keys)
    assert fake.peak == 2 and summary.asked == 6
    assert [r["key"] for r in rows] == keys  # the requests' order, whatever finished first


# ---- parse and validate ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "good"),
    [
        (triage(), True),
        (f"```json\n{triage()}\n```", True),
        (f"```\n{triage()}\n```\n", True),
        (f"Here you go: {triage()}", False),
        (f"```json\n{triage()}\n```\nand more", False),
        (f"{triage()}\n{triage()}", False),
        ("[1]", False),
        ('{"pmid": "PMID:91000001", "decision": "included", "reason": "none", "why": NaN}', False),
        (triage("PMID:5"), False),
        (triage(decision="excluded", reason="none"), False),
        (triage(decision="included", reason="not_chd"), True),
    ],
)
def test_what_parses_and_validates(tmp_path: Path, text: str, good: bool) -> None:
    summary, rows, _ = run_ask(tmp_path, spec({PMID: [text, text, text]}), [PMID])
    assert (summary.failed == []) is good
    if good:
        assert rows[0]["attempts"] == 1 and rows[0]["parsed"]["pmid"] == PMID


@pytest.mark.parametrize(
    ("why", "good"),
    [
        ("see the contact someone@example.org", False),
        ("the paper's corresponding author is quoted", True),
    ],
)
def test_an_address_the_input_lacks_is_refused_and_one_it_has_is_not(
    tmp_path: Path, why: str, good: bool
) -> None:
    text = triage(why=why)
    summary, rows, fake = run_ask(tmp_path, spec({PMID: [text, text, text]}), [PMID])
    assert (summary.failed == []) is good
    if not good:
        assert "someone@example.org" not in rows[0]["error"]
        assert "not in its input" in rows[0]["error"]
    path = tmp_path / "req2.jsonl"
    path.write_text(json.dumps({"key": PMID, "inputs": {
        "gene": "TBX5", "hit": "Contact: someone@example.org"}}) + "\n")  # fmt: skip
    b = spec({PMID: [triage(why="quoted someone@example.org")]})
    again = ask("triage-screen", path, tmp_path / "a2.jsonl", site_of(b), [], RULES_ROOT, None,
                "schema", None, False, make=lambda _s: Fake(b))  # fmt: skip
    assert again.failed == []  # an address quoted from the paper's own text passes


def test_usage_and_seconds_sum_every_attempt(tmp_path: Path) -> None:
    class Metered(Fake):
        def ask(self, req: Request) -> Answer:
            got = super().ask(req)
            return Answer(got.key, got.text, {"input_tokens": 10, "output_tokens": 3}, 1.5, {})

    b = spec({PMID: ["bad", triage()]})
    summary, rows, _ = run_ask(tmp_path, b, [PMID], fake=Metered(b))
    assert rows[0]["usage"] == {"input_tokens": 20, "output_tokens": 6}
    assert rows[0]["seconds"] == 3.0 and summary.usage == rows[0]["usage"]


def test_a_request_that_cannot_be_assembled_exits_2_before_any_call(tmp_path: Path) -> None:
    b = spec({PMID: [triage()]})
    fake = Fake(b)
    path = tmp_path / "req.jsonl"
    path.write_text(json.dumps({"key": PMID, "inputs": {"gene": "TBX5"}}) + "\n")
    with pytest.raises(AskUsageError, match="inputs missing"):
        ask("triage-screen", path, tmp_path / "a.jsonl", site_of(b), [], RULES_ROOT, None,
            "schema", None, False, make=lambda _s: fake)  # fmt: skip
    renamed = tmp_path / "rules"
    shutil.copytree(RULES_ROOT, renamed)
    rules = renamed / "curator" / "curator-rules.md"
    rules.write_text(rules.read_text().replace("## Triage", "## Sorting"))
    with pytest.raises(AskUsageError, match="Triage"):
        ask("triage-screen", requests_file(tmp_path, [PMID]), tmp_path / "a.jsonl", site_of(b),
            [], renamed, None, "schema", None, False, make=lambda _s: fake)  # fmt: skip
    assert fake.requests == []


@pytest.mark.parametrize(
    "line",
    ["not json", "[1]", '{"inputs": {}}', '{"key": "k"}', '{"key": "k", "inputs": {"a": 1}}',
     '{"key": "k", "inputs": {}, "stray": 1}'],
)  # fmt: skip
def test_a_malformed_request_row_exits_2_naming_its_line(tmp_path: Path, line: str) -> None:
    b = spec()
    path = tmp_path / "req.jsonl"
    path.write_text(line + "\n")
    with pytest.raises(AskUsageError, match=r"req\.jsonl:1"):
        ask("triage-screen", path, tmp_path / "a.jsonl", site_of(b), [], RULES_ROOT, None,
            "schema", None, False, make=lambda _s: Fake(b))  # fmt: skip


def test_the_cli_prints_one_summary_line_and_records_shadow(tmp_path: Path) -> None:
    site = tmp_path / "site.yaml"
    site.write_text(
        "roles:\n  triage-screen:\n    id: t\n    kind: fake\n    model: m\n"
        "    context_tokens: 100000\n    concurrency: 1\n    settings:\n      answers:\n"
        f"        '{PMID}': ['{triage()}']\n"
    )
    out = runner.invoke(
        app,
        [
            "ask",
            "--role",
            "triage-screen",
            "--requests",
            str(requests_file(tmp_path, [PMID])),
            "--answers",
            str(tmp_path / "a.jsonl"),
            "--site",
            str(site),
            "--root",
            str(RULES_ROOT),
            "--shadow",
            "--run",
            "r1",
        ],  # fmt: skip
    )
    assert out.exit_code == 0
    assert out.output == "answered 1, from cache 0, failed 0, tokens 0, shadow: true, run: r1\n"


# ---- check-batch -------------------------------------------------------------------------------


def _reader_root(tmp_path: Path, atlas_root: Path) -> Path:
    p = _fresh(tmp_path, atlas_root)
    shutil.copytree(RULES_ROOT / "curator", p.root / "curator")
    return p.root


def _answer_from(root: Path, folder: str) -> str:
    run = root / "local/literature/runs/TBX5/r1" / folder
    return json.dumps(
        {
            "findings": YAML_SAFE.load((run / "batch-01.findings.yaml").read_text())["findings"],
            "evidence": YAML_SAFE.load((run / "batch-01.evidence.yaml").read_text()),
            "papers": json.loads((run / "batch-01.papers.json").read_text()),
            "report": (run / "batch-01.report.md").read_text(),
        }
    )


def _reader_request(tmp_path: Path, **extra: Any) -> Path:
    path = tmp_path / "rreq.jsonl"
    row = {"key": "TBX5:01", "inputs": {"manifest": "m", "texts": "t"}, "gene": "TBX5",
           "batch": "01", **extra}  # fmt: skip
    path.write_text(json.dumps(row) + "\n")
    return path


def _ask_batch(
    tmp_path: Path,
    root: Path,
    cli: Path,
    answers: list[str],
    role: str = "extract",
    **extra: Any,
) -> tuple[AskSummary, list[dict[str, Any]], Fake]:
    b = BackendSpec("t", "fake", "m", {"answers": {"TBX5:01": answers}}, 10_000_000, 1, None)
    fake = Fake(b)
    out = tmp_path / "ra.jsonl"
    summary = ask(role, _reader_request(tmp_path, **extra), out, site_of(b, role), [], root, None,
                  "check-batch", "r1", False, atlas_cli=cli, make=lambda _s: fake)  # fmt: skip
    return summary, [json.loads(x) for x in out.read_text().splitlines()], fake


@pytest.mark.parametrize(("role", "folder"), [("extract", "extract"), ("blind-read", "verify")])
def test_a_passing_batch_leaves_its_contract_files_where_merge_reads_them(
    tmp_path: Path, atlas_root: Path, role: str, folder: str
) -> None:
    root = _reader_root(tmp_path, atlas_root)
    answer = _answer_from(root, folder)
    run = root / "local/literature/runs/TBX5/r1" / folder
    originals = {p.name: p.read_bytes() for p in run.glob("batch-01.*")}
    for p in run.glob("batch-01.*"):
        p.unlink()
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    summary, rows, _ = _ask_batch(tmp_path, root, cli, [answer], role)
    assert summary.failed == [] and rows[0]["attempts"] == 1
    assert sorted(p.name for p in run.glob("batch-01.*")) == sorted(originals)
    for p in run.glob("batch-01.*"):
        if p.suffix == ".yaml" and "findings" in p.name:
            assert YAML_SAFE.load(p.read_text()) == YAML_SAFE.load(originals[p.name].decode())
    assert json.loads((run / "batch-01.papers.json").read_text()) == json.loads(
        originals["batch-01.papers.json"]
    )


def test_a_refused_batch_is_retried_with_the_refusal_and_leaves_no_file_behind(
    tmp_path: Path, atlas_root: Path
) -> None:
    root = _reader_root(tmp_path, atlas_root)
    answer = json.loads(_answer_from(root, "extract"))
    answer["findings"][0]["screened"] = "many"  # the atlas's schema refuses this
    run = root / "local/literature/runs/TBX5/r1/extract"
    for p in run.glob("batch-01.*"):
        p.unlink()
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    summary, rows, fake = _ask_batch(tmp_path, root, cli, [json.dumps(answer)] * 3)
    assert summary.failed == ["TBX5:01"] and rows[0]["attempts"] == 3
    assert list(run.glob("batch-01.*")) == []
    assert "screened" in fake.requests[1].user and "refused" in fake.requests[1].user


def test_a_refused_batch_puts_back_the_file_that_was_there(
    tmp_path: Path, atlas_root: Path
) -> None:
    root = _reader_root(tmp_path, atlas_root)
    answer = json.loads(_answer_from(root, "extract"))
    run = root / "local/literature/runs/TBX5/r1/extract"
    before = {p.name: p.read_bytes() for p in run.glob("batch-01.*")}
    cli = _fake(tmp_path, _stdout("ERROR [LIT999] x: a refusal"), code=1)
    summary, _, _ = _ask_batch(tmp_path, root, cli, [json.dumps(answer)] * 3)
    assert summary.failed == ["TBX5:01"]
    assert {p.name: p.read_bytes() for p in run.glob("batch-01.*")} == before


def test_a_mode_that_is_not_the_runs_exits_2_naming_the_key(
    tmp_path: Path, atlas_root: Path
) -> None:
    root = _reader_root(tmp_path, atlas_root)
    cli = _fake(tmp_path)
    with pytest.raises(AskUsageError, match=r"TBX5:01.*backfill.*new"):
        _ask_batch(tmp_path, root, cli, ["{}"], mode="backfill")


@pytest.mark.parametrize(
    ("extra", "match"),
    [({"batch": "07"}, "batch 07 is not in"), ({"gene": None}, "needs `gene`")],
)
def test_a_check_batch_request_must_fit_its_run(
    tmp_path: Path, atlas_root: Path, extra: dict[str, Any], match: str
) -> None:
    root = _reader_root(tmp_path, atlas_root)
    with pytest.raises(AskUsageError, match=match):
        _ask_batch(tmp_path, root, _fake(tmp_path), ["{}"], **extra)


def test_check_batch_is_refused_for_triage_and_without_a_run(tmp_path: Path) -> None:
    b = spec({PMID: [triage()]})
    req = requests_file(tmp_path, [PMID], gene="TBX5", batch="01")
    with pytest.raises(AskUsageError, match="readers and the judge"):
        ask("triage-screen", req, tmp_path / "a.jsonl", site_of(b), [], RULES_ROOT, None,
            "check-batch", "r1", False, make=lambda _s: Fake(b))  # fmt: skip
    with pytest.raises(AskUsageError, match="needs --run"):
        ask("extract", req, tmp_path / "a.jsonl", site_of(b, "extract"), [], RULES_ROOT, None,
            "check-batch", None, False, make=lambda _s: Fake(b))  # fmt: skip


def test_a_backfill_answer_is_written_as_the_backfill_pass_file(
    tmp_path: Path, atlas_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_check import _BACKFILL_PASS, _backfill_run, _pubmed

    _pubmed(monkeypatch)
    p = _backfill_run(tmp_path, atlas_root, _BACKFILL_PASS)
    shutil.copytree(RULES_ROOT / "curator", p.root / "curator")
    written = (p.extract / "batch-01.findings.yaml").read_bytes()
    for f in p.extract.glob("batch-01.*"):
        f.unlink()
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    answer = {"findings": _BACKFILL_PASS, "evidence": {}, "report": "r"}
    summary, _, _ = _ask_batch(tmp_path, p.root, cli, [json.dumps(answer)], mode="backfill")
    assert summary.failed == []
    assert YAML_SAFE.load((p.extract / "batch-01.findings.yaml").read_text()) == YAML_SAFE.load(
        written.decode()
    )
    assert sorted(f.name for f in p.extract.glob("batch-01.*")) == [
        "batch-01.evidence.yaml", "batch-01.findings.yaml", "batch-01.report.md"
    ]  # fmt: skip


# ---- fix round 1 -------------------------------------------------------------------------------


def _two_batch_requests(tmp_path: Path) -> Path:
    path = tmp_path / "two.jsonl"
    rows = [
        {"key": f"TBX5:{n}", "inputs": {"manifest": "m", "texts": "t"}, "gene": "TBX5", "batch": n}
        for n in ("01", "02")
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _batch_answer(root: Path, n: str, **over: Any) -> dict[str, Any]:
    run = root / "local/literature/runs/TBX5/r1/extract"
    return {
        "findings": YAML_SAFE.load((run / f"batch-{n}.findings.yaml").read_text())["findings"],
        "evidence": YAML_SAFE.load((run / f"batch-{n}.evidence.yaml").read_text()),
        "papers": json.loads((run / f"batch-{n}.papers.json").read_text()),
        "report": (run / f"batch-{n}.report.md").read_text(),
        **over,
    }


def test_a_contract_write_that_fails_costs_one_row_and_restores_its_files(
    tmp_path: Path, atlas_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _reader_root(tmp_path, atlas_root)
    run = root / "local/literature/runs/TBX5/r1/extract"
    before = {p.name: p.read_bytes() for p in run.glob("batch-02.*")}
    answers = {
        "TBX5:01": [json.dumps(_batch_answer(root, "01"))],
        "TBX5:02": [json.dumps(_batch_answer(root, "02", report="a changed report"))],
    }
    b = BackendSpec("t", "fake", "m", {"answers": answers}, 10_000_000, 1, None)
    real, fired = ask_module.write_bytes_atomically, []

    def failing(path: Path, payload: bytes) -> None:
        if path.name == "batch-02.papers.json" and not fired:
            fired.append(path)
            raise OSError("disk full")
        real(path, payload)

    monkeypatch.setattr(ask_module, "write_bytes_atomically", failing)
    out = tmp_path / "two-answers.jsonl"
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    requests = _two_batch_requests(tmp_path)
    summary = ask("extract", requests, out, site_of(b, "extract"), [], root, None, "check-batch",
                  "r1", False, atlas_cli=cli, make=lambda _s: Fake(b))  # fmt: skip
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["key"] for r in rows] == ["TBX5:01", "TBX5:02"] and "parsed" in rows[0]
    assert "OSError" in rows[1]["error"] and "disk full" in rows[1]["error"]
    assert summary.failed == ["TBX5:02"]
    assert {p.name: p.read_bytes() for p in run.glob("batch-02.*")} == before


def test_a_cache_write_that_fails_costs_one_row_and_the_run_still_writes_its_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = ["PMID:1", "PMID:2", "PMID:3"]
    real = ask_module.write_bytes_atomically

    def failing(path: Path, payload: bytes) -> None:
        if path.parent.name == "triage-screen" and b"PMID:2" in payload:
            raise OSError("read-only cache")
        real(path, payload)

    monkeypatch.setattr(ask_module, "write_bytes_atomically", failing)
    summary, rows, _ = run_ask(tmp_path, spec({k: [triage(k)] for k in keys}), keys,
                               cache=tmp_path / "cache")  # fmt: skip
    assert [("parsed" in r) for r in rows] == [True, False, True]
    assert "OSError: read-only cache" in rows[1]["error"] and summary.failed == ["PMID:2"]


def test_a_retry_that_succeeds_replaces_the_file_and_nothing_of_the_first_attempt_survives(
    tmp_path: Path, atlas_root: Path
) -> None:
    root = _reader_root(tmp_path, atlas_root)
    run = root / "local/literature/runs/TBX5/r1"
    good = _batch_answer(root, "01")
    bad = json.loads(json.dumps(good))
    bad["findings"][0]["screened"] = "FIRST-ATTEMPT-MARK"
    good["findings"][0]["summary"] = "The second attempt's own summary of the case."
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    summary, rows, _ = _ask_batch(tmp_path, root, cli, [json.dumps(bad), json.dumps(good)])
    assert summary.failed == [] and rows[0]["attempts"] == 2
    kept = YAML_SAFE.load((run / "extract/batch-01.findings.yaml").read_text())["findings"]
    assert kept == good["findings"]
    assert not list(run.rglob("*.tmp"))
    assert not [
        p for p in run.rglob("*") if p.is_file() and b"FIRST-ATTEMPT-MARK" in p.read_bytes()
    ]


def test_a_cache_hit_rewrites_the_contract_files_that_were_removed(
    tmp_path: Path, atlas_root: Path
) -> None:
    root = _reader_root(tmp_path, atlas_root)
    run = root / "local/literature/runs/TBX5/r1/extract"
    cache = tmp_path / "cache"
    answer = json.dumps(_batch_answer(root, "01"))
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    spec_ = BackendSpec("t", "fake", "m", {"answers": {"TBX5:01": [answer]}}, 10_000_000, 1, None)

    def once() -> tuple[AskSummary, Fake]:
        fake = Fake(spec_)
        summary = ask("extract", _reader_request(tmp_path), tmp_path / "c.jsonl",
                      site_of(spec_, "extract"), [], root, cache, "check-batch", "r1", False,
                      atlas_cli=cli, make=lambda _s: fake)  # fmt: skip
        return summary, fake

    once()
    names = sorted(p.name for p in run.glob("batch-01.*"))
    for p in run.glob("batch-01.*"):
        p.unlink()
    summary, fake = once()
    row = json.loads((tmp_path / "c.jsonl").read_text())
    assert fake.requests == [] and row["attempts"] == 0 and summary.cached == 1
    assert sorted(p.name for p in run.glob("batch-01.*")) == names


def test_a_judge_answer_lands_where_check_and_compare_read_it(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # like test_check.py's judge cases this uses the stub atlas CLI, not the real validator
    from chd_curator.run import RunPaths, load_judge
    from test_assemble import _ready

    p = _ready(tmp_path, atlas_root, stub_atlas, monkeypatch)
    shutil.copytree(RULES_ROOT / "curator", p.root / "curator")
    findings = p.judging / "judge-01.findings.yaml"
    decisions = p.judge / "judge-01.decisions.yaml"
    answer = json.dumps(
        {
            "findings": YAML_SAFE.load(findings.read_text())["findings"],
            "decisions": YAML_SAFE.load(decisions.read_text()),
        }
    )
    expected = load_judge(RunPaths(p.root, "TBX5", "r1"), only="01")
    findings_before, decisions_before = YAML_SAFE.load(findings.read_text()), None
    decisions_before = YAML_SAFE.load(decisions.read_text())
    findings.unlink()
    decisions.unlink()
    cli = _fake(tmp_path, _stdout(summary="0 error(s), 3 warning(s)"))
    summary, rows, _ = _ask_batch(tmp_path, p.root, cli, [answer], "judge")
    assert summary.failed == [] and rows[0]["attempts"] == 1
    assert YAML_SAFE.load(findings.read_text())["findings"] == findings_before["findings"]
    assert YAML_SAFE.load(decisions.read_text()) == decisions_before
    assert load_judge(RunPaths(p.root, "TBX5", "r1"), only="01") == expected
    assert findings.is_file() and decisions.is_file()


def test_the_context_boundary_passes_at_equality_and_refuses_one_token_over(
    tmp_path: Path,
) -> None:
    inputs = {"gene": "TBX5", "hit": f"{PMID}\nA title"}
    ri = ask_module.assemble("triage-screen", RULES_ROOT, inputs)
    exact = ask_module._estimate(ri.system, ri.user) + 100
    ok = spec({PMID: [triage()]}, context=exact, max_tokens=100)
    assert run_ask(tmp_path, ok, [PMID])[0].failed == []
    over = spec({PMID: [triage()]}, context=exact - 1, max_tokens=100)
    summary, rows, fake = run_ask(tmp_path, over, [PMID])
    assert summary.failed == [PMID] and fake.requests == []


# ---- the fix wave: configuration exits 2, what a row records ------------------------------------


class _Metered(Fake):
    """The scripted fake, reporting usage, seconds and meta as a real backend does."""

    def ask(self, req: Request) -> Answer:
        got = super().ask(req)
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return Answer(got.key, got.text, usage, 1.5, {"finish_reason": "stop", "n": len(got.text)})


def test_an_answers_meta_reaches_its_row_and_the_cache(tmp_path: Path) -> None:
    """M1: claude_code_version, adds_context, the model used and finish_reason are in `meta`."""
    cache = tmp_path / "cache"
    b = spec({PMID: [triage()]})
    _, rows, _ = run_ask(tmp_path, b, [PMID], cache=cache, fake=_Metered(b))
    meta = {"finish_reason": "stop", "n": len(triage())}
    assert rows[0]["meta"] == meta
    body = json.loads(next((cache / "triage-screen").iterdir()).read_text())
    assert body["meta"] == meta
    _, again, _ = run_ask(tmp_path, b, [PMID], cache=cache, fake=Fake(b))
    assert again[0]["attempts"] == 0 and again[0]["meta"] == meta  # the answer's, from the cache


def test_an_error_row_carries_the_usage_and_seconds_its_attempts_spent(tmp_path: Path) -> None:
    """M2: a refused answer still cost tokens; the error row says how many."""
    b = spec({PMID: ["x", "y", "z"]})
    summary, rows, _ = run_ask(tmp_path, b, [PMID], fake=_Metered(b))
    assert summary.failed == [PMID] and rows[0]["attempts"] == 3
    usage = {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45}
    assert rows[0]["usage"] == usage and rows[0]["seconds"] == 4.5


def test_the_summary_counts_tokens_as_the_report_does(tmp_path: Path) -> None:
    """M2 (sentences): openai_compat reports prompt, completion AND total; summing every count
    doubled the figure. The summary and the report share one rule."""
    from chd_curator import report
    from chd_curator.backends.base import token_count

    assert report.token_count is token_count  # one rule, shared
    b = spec({PMID: [triage()]})
    summary, rows, _ = run_ask(tmp_path, b, [PMID], fake=_Metered(b))
    assert summary.tokens == 15 == token_count(rows[0]["usage"])
    replies = tmp_path / "replies"
    replies.mkdir()
    (replies / "PMID_91000001.json").write_text(
        json.dumps({"text": triage(), "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                                "total_tokens": 15}})
    )  # fmt: skip
    site = tmp_path / "site.yaml"
    site.write_text(
        "roles:\n  triage-screen:\n    id: t\n    kind: replay\n    model: m\n"
        f"    context_tokens: 100000\n    concurrency: 1\n    settings:\n      dir: {replies}\n"
    )
    out = runner.invoke(app, ["ask", "--role", "triage-screen", "--requests",
                              str(requests_file(tmp_path, [PMID])), "--answers",
                              str(tmp_path / "c.jsonl"), "--site", str(site),
                              "--root", str(RULES_ROOT)])  # fmt: skip
    assert out.exit_code == 0, out.output
    assert out.output == "answered 1, from cache 0, failed 0, tokens 15\n"


@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_key_variable_exits_2_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """R-SWA-44: a misconfigured site must not "fail every gene on content"."""
    if value is None:
        monkeypatch.delenv("CHD_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("CHD_TEST_KEY", value)
    b = BackendSpec("t", "fake", "m", {"answers": {PMID: [triage()]}}, 100_000, 1, "CHD_TEST_KEY")
    fake = Fake(b)
    with pytest.raises(AskUsageError, match="CHD_TEST_KEY is not set"):
        ask("triage-screen", requests_file(tmp_path, [PMID]), tmp_path / "a.jsonl", site_of(b),
            [], RULES_ROOT, None, "schema", None, False, make=lambda _s: fake)  # fmt: skip
    assert fake.requests == [] and not (tmp_path / "a.jsonl").exists()


def test_a_replay_directory_that_does_not_exist_exits_2(tmp_path: Path) -> None:
    b = BackendSpec("t", "replay", "m", {"dir": str(tmp_path / "nowhere")}, 100_000, 1, None)
    with pytest.raises(AskUsageError, match="nowhere"):
        ask("triage-screen", requests_file(tmp_path, [PMID]), tmp_path / "a.jsonl", site_of(b),
            [], RULES_ROOT, None, "schema", None, False)  # fmt: skip
    assert not (tmp_path / "a.jsonl").exists()


@pytest.mark.parametrize("problem", ["SLURM_JOB_ID", "LSB_JOBID", "no claude", "version fails"])
def test_claude_code_that_cannot_run_here_exits_2_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    import os
    import sys

    from chd_curator.register import load_register

    fakes = HERE / "fakes"
    log = tmp_path / "log.json"
    path = (
        os.environ["PATH"] if problem == "no claude" else f"{fakes}{os.pathsep}{os.environ['PATH']}"
    )
    if problem == "no claude":
        path = os.pathsep.join(
            p for p in path.split(os.pathsep) if not (Path(p) / "claude").exists()
        )
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("CLAUDE_STUB_PYTHON", sys.executable)
    monkeypatch.setenv("STUB_LOG", str(log))
    for name in ("SLURM_JOB_ID", "LSB_JOBID", "PBS_JOBID"):
        monkeypatch.delenv(name, raising=False)
    if problem.isupper():
        monkeypatch.setenv(problem, "7")
    if problem == "version fails":
        monkeypatch.setenv("STUB_VERSION_EXIT", "1")
    b = BackendSpec("cc", "claude_code", "claude-haiku-5-5", {}, 100_000, 1, None)
    with pytest.raises(AskUsageError) as info:
        ask("triage-check", requests_file(tmp_path, [PMID]), tmp_path / "a.jsonl",
            site_of(b, "triage-check"), load_register(), RULES_ROOT, None, "schema", None,
            False)  # fmt: skip
    expected = {"no claude": "not found", "version fails": "--version failed"}.get(problem, problem)
    assert expected in str(info.value)
    assert not log.exists() and not (tmp_path / "a.jsonl").exists()


def test_a_refused_credential_stops_the_run_and_exits_2(tmp_path: Path) -> None:
    """R-SWA-44: HTTP 401/403 stop the run: no further call, the answers file holds the rows
    so far and an error row for each request not asked."""
    from chd_curator.backends.openai_compat import OpenAICompat
    from fake_http import FakePost

    b = BackendSpec("o", "openai_compat", "m", {"base_url": "http://h"}, 100_000, 1, None)
    post = FakePost((401, {}, b'{"error": "invalid key"}'))
    keys = ["PMID:1", "PMID:2", "PMID:3"]
    with pytest.raises(AskUsageError, match="HTTP 401") as info:
        ask("triage-screen", requests_file(tmp_path, keys), tmp_path / "a.jsonl", site_of(b),
            [], RULES_ROOT, None, "schema", None, True,
            make=lambda s: OpenAICompat(s, post))  # fmt: skip
    assert len(post.calls) == 1 and "stopped" in str(info.value)
    rows = [json.loads(x) for x in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert [r["key"] for r in rows] == keys and all("error" in r for r in rows)
    assert "HTTP 401" in rows[0]["error"]
    assert all(r["error"].startswith("not asked") and r["attempts"] == 0 for r in rows[1:])
