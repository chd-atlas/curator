"""The Claude Code backend, against a stub `claude` on PATH. No model is ever called."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from chd_curator.backends import BackendError, Request, TransientError, make_backend
from chd_curator.backends.claude_code import CLUSTER_VARIABLES, REFUSAL
from chd_curator.site import BackendSpec

FAKES = Path(__file__).parent / "fakes"
REPLY = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "the answer",
    "duration_ms": 1234,
    "usage": {
        "input_tokens": 11,
        "output_tokens": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "service_tier": "standard",
    },
    "modelUsage": {"claude-test-1": {"inputTokens": 11}},
}
SYSTEM = "SYSTEM-TEXT-QQ"
USER = "USER-TEXT-ZZ"


def spec(settings: dict[str, Any] | None = None) -> BackendSpec:
    return BackendSpec("cc", "claude_code", "opus-x", settings or {}, 1000, 1, None)  # type: ignore[arg-type]


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "log.json"
    monkeypatch.setenv("PATH", f"{FAKES}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_STUB_PYTHON", sys.executable)
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_REPLY", json.dumps(REPLY))
    for name in CLUSTER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return log


def ask(settings: dict[str, Any] | None = None) -> Any:
    ticks = iter([1.0, 3.5])
    b = make_backend(spec(settings), clock=lambda: next(ticks))
    return b.ask(Request("k:1", SYSTEM, USER, None, 500))


def test_flags_and_input(stub: Path) -> None:
    ans = ask()
    log = json.loads(stub.read_text())
    argv = log["argv"]
    # a literal, never FLAGS: removing a flag from the module must fail this test
    assert argv == [
        "-p",
        "--output-format",
        "json",
        "--tools",
        "",
        "--strict-mcp-config",
        "--safe-mode",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--permission-prompts",
        "none",
        "--system-prompt-file",
        log["system_path"],
        "--model",
        "opus-x",
    ]
    assert argv[argv.index("--model") + 1] == "opus-x"
    assert log["system_path"].startswith("/") and log["system_mode"] == "0o600"
    assert not any(SYSTEM in a or USER in a for a in argv)
    assert log["system"] == SYSTEM
    assert log["stdin"] == USER
    assert not Path(log["system_path"]).exists()
    assert not Path(log["system_path"]).is_relative_to(Path(log["cwd"]))
    assert ans.text == "the answer" and ans.key == "k:1" and ans.seconds == 2.5
    assert ans.usage == {
        "input_tokens": 11,
        "output_tokens": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
    }
    assert ans.meta == {
        "temperature": None,
        "claude_code_version": "9.9.9 (Claude Code)",
        "adds_context": True,
        "model": "claude-test-1",
        "claude_duration_ms": 1234,
    }


def test_the_version_is_asked_once_per_backend(stub: Path) -> None:
    ticks = iter(range(100))
    b = make_backend(spec(), clock=lambda: float(next(ticks)))
    r = Request("k:1", SYSTEM, USER, None, 500)
    assert b.ask(r).meta["claude_code_version"] == b.ask(r).meta["claude_code_version"]
    assert Path(f"{stub}.version").read_text() == "x"


def test_runs_in_a_fresh_empty_directory_that_is_removed(stub: Path, tmp_path: Path) -> None:
    ask()
    log = json.loads(stub.read_text())
    cwd = Path(log["cwd"])
    assert log["listing"] == []
    assert not cwd.exists()
    assert cwd.resolve() != Path.cwd().resolve()


@pytest.mark.parametrize("variable", ["SLURM_JOB_ID", "LSB_JOBID", "PBS_JOBID"])
def test_refuses_on_a_cluster_node_before_any_process(
    stub: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Slurm, LSF and PBS each set their job variable inside a job; the message names it."""
    monkeypatch.setenv(variable, "123")
    with pytest.raises(BackendError, match=f"{variable} is set") as exc:
        ask()
    assert REFUSAL in str(exc.value)
    assert not stub.exists() and not Path(f"{stub}.version").exists()


def test_the_claude_process_gets_no_api_key_or_auth_token(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M9: with ANTHROPIC_API_KEY in its environment Claude Code silently bills the API
    instead of the OAuth login. The stub logs its environment's names only."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "y")
    ask()
    for names in (json.loads(stub.read_text())["env_names"],
                  json.loads(Path(f"{stub}.version_env").read_text())):  # fmt: skip
        assert "ANTHROPIC_API_KEY" not in names and "ANTHROPIC_AUTH_TOKEN" not in names
        assert "STUB_LOG" in names  # the rest of the environment still reaches it
    assert os.environ["ANTHROPIC_API_KEY"] == "x"  # the caller's own environment is untouched


def test_a_nonzero_exit_names_the_last_stderr_line(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_EXIT", "3")
    monkeypatch.setenv("STUB_STDERR", "first\nnot logged in\n\n")
    with pytest.raises(BackendError, match="not logged in") as exc:
        ask()
    assert "first" not in str(exc.value)


def test_an_error_result_a_bad_reply_and_a_missing_binary(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_REPLY", json.dumps({**REPLY, "is_error": True, "result": "boom"}))
    with pytest.raises(BackendError, match="boom"):
        ask()
    monkeypatch.setenv("STUB_REPLY", "not json")
    with pytest.raises(BackendError, match="not JSON"):
        ask()
    monkeypatch.setenv("STUB_REPLY", json.dumps({**REPLY, "result": 7}))
    with pytest.raises(BackendError, match="no result text"):
        ask()
    monkeypatch.setenv("PATH", str(stub.parent))
    with pytest.raises(BackendError, match="not found"):
        ask()


def test_a_timeout_is_transient_and_removes_the_directory(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_SLEEP", "5")
    with pytest.raises(TransientError, match="timed out"):
        ask({"timeout_seconds": 1})
    cwd = Path(json.loads(stub.read_text())["cwd"])
    assert not cwd.exists()
