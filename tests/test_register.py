"""The qualification register: which model may fill which role."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chd_curator.register import Entry, RegisterError, allowed, load_register
from chd_curator.roles import ROLES
from chd_curator.site import BackendSpec, model_key


def _b(kind: str = "openai_compat", model: str = "m", **settings: object) -> BackendSpec:
    return BackendSpec(
        id="b",
        kind=kind,
        model=model,
        settings=settings,  # type: ignore[arg-type]
        context_tokens=1,
        concurrency=1,
        key_env=None,
    )


def _entry(role: str, b: BackendSpec, verdict: str) -> Entry:
    return Entry(
        role=role,
        model_key=model_key(b),
        verdict=verdict,  # type: ignore[arg-type]
        gold="g",
        gold_digest="d",
        rules_digest="r",
        date="2026-11-26",
        scores={},
        note="n",
    )


def test_allowed_accepts_pass_and_reference_and_refuses_fail_and_absent() -> None:
    b = _b(temperature=0)
    for verdict, expected in (("pass", True), ("reference", True), ("fail", False)):
        got = allowed("extract", b, [_entry("extract", b, verdict)])
        assert (got is not None) == expected
        if got is not None:
            assert got.verdict == verdict
    reg = [_entry("extract", b, "pass")]
    assert allowed("judge", b, reg) is None  # another role
    assert allowed("extract", _b(temperature=1), reg) is None  # another setting
    assert allowed("extract", _b(), []) is None
    # Test backends need no entry, and say so.
    for kind in ("replay", "fake"):
        got = allowed("extract", _b(kind=kind), [])
        assert got is not None and got.verdict == "reference"
        assert "answers are recorded or scripted" in got.note


def test_two_entries_for_one_role_and_key_are_refused(tmp_path: Path) -> None:
    row = (
        "- {role: extract, model_key: 'fake:x', verdict: reference, gold: '', gold_digest: '',"
        " rules_digest: '', date: '2026-11-26', scores: {}, note: n,"
        " backend: {kind: fake, model: x, settings: {}}}\n"
    )
    path = tmp_path / "q.yaml"
    path.write_text("entries:\n" + row + row, encoding="utf-8")
    with pytest.raises(RegisterError, match="twice"):
        load_register(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "verdict: maybe",
        "role: summarise",
        "model_key: 'fake:other'",
        "gold: ''",  # a pass needs its gold
        "surprise: 1",
    ],
)
def test_a_malformed_register_is_refused(tmp_path: Path, mutation: str) -> None:
    base = {
        "role": "extract",
        "model_key": "'fake:x'",
        "verdict": "pass",
        "gold": "g",
        "gold_digest": "d",
        "rules_digest": "r",
        "date": "'2026-11-26'",
        "scores": "{}",
        "note": "n",
        "backend": "{kind: fake, model: x, settings: {}}",
    }
    key, _, value = mutation.partition(": ")
    base[key] = value
    row = "- {" + ", ".join(f"{k}: {v}" for k, v in base.items()) + "}\n"
    path = tmp_path / "q.yaml"
    path.write_text("entries:\n" + row, encoding="utf-8")
    with pytest.raises(RegisterError):
        load_register(path)


def test_the_packaged_register_records_the_three_benchmark_failures() -> None:
    register = load_register()
    fails = [e for e in register if e.verdict == "fail"]
    assert {e.role for e in fails} == {"triage-screen"}
    assert sorted(e.scores["with_findings_included"] for e in fails) == [76, 79, 82]
    assert {e.scores["with_findings"] for e in fails} == {86}
    assert {e.gold for e in fails} == {"triage-444-2026-11-26"}
    assert {e.gold_digest for e in fails} == {
        "1b6892213c319e43ed4c7d4c14606502f0a1df97c80d5879980bf07891bcd2b7"
    }
    assert all(e.model_key.startswith("openai_compat:") and e.date == "2026-11-26" for e in fails)
    assert len({e.model_key for e in fails}) == 3
    assert all("94b1d8d" in e.note for e in fails)
    # The three keys are the keys a site configured with exactly these settings would compute.
    for e in fails:
        spec = BackendSpec(
            id="x",
            kind=e.backend["kind"],
            model=e.backend["model"],
            settings=e.backend["settings"],
            context_tokens=1,
            concurrency=1,
            key_env=None,
        )
        assert model_key(spec) == e.model_key
        assert allowed(e.role, spec, register) is None


def test_the_packaged_register_references_and_states_its_bias() -> None:
    register = load_register()
    refs = {e.role: e.model_key for e in register if e.verdict == "reference"}
    assert refs == {
        "triage-check": "claude_code:claude-haiku-5-5",
        "extract": "claude_code:claude-sonnet-5-5",
        "blind-read": "claude_code:claude-opus-5-5",
        "judge": "claude_code:claude-opus-5-5",
    }
    assert set(refs) <= set(ROLES)
    assert all("favours" in e.note for e in register)
    assert all(e.note.startswith("produced the gold") for e in register if e.verdict == "reference")
    # Each reference is allowed on its own role, with no settings.
    for e in register:
        if e.verdict == "reference":
            spec = BackendSpec(
                id="x",
                kind="claude_code",
                model=e.backend["model"],
                settings={},
                context_tokens=1,
                concurrency=1,
                key_env=None,
            )
            assert allowed(e.role, spec, register) is not None


def test_the_failures_were_measured_under_one_rules_text() -> None:
    # The benchmark's rules text is local to the owner, so the digest cannot be re-derived
    # here; pin its shape and that all three failures share it.
    fails = [e for e in load_register() if e.verdict == "fail"]
    assert len({e.rules_digest for e in fails}) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", fails[0].rules_digest)
