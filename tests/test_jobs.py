"""The local-model job scripts, run against a stub ``llama-server``: no model, no cluster.

``serve-and-ask.sh`` is run for real (bash, the stub, a ``chd-curator`` wrapper over the test
interpreter). The tests assert what the benchmark's ledger says went wrong: a server that died
while loading (the client waited 20 minutes), a zombie counted as alive, a server left running,
and server flags that would have page-mapped the model from GPFS or silently turned thinking on.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from chd_curator.cli import app
from chd_curator.jobs import JOB_SCRIPTS, local_site_text
from chd_curator.site import load_site, model_key

HERE = Path(__file__).resolve().parent
JOBS = HERE.parent / "src" / "chd_curator" / "workflow" / "jobs"
SERVE = JOBS / "serve-and-ask.sh"
RULES_ROOT = HERE / "fixtures" / "roles"
STUB = HERE / "fakes" / "llama_server.py"
runner = CliRunner()
GOOD_ARGS = "--load-mode none --reasoning off --jinja"


def _sh(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def kit(tmp_path: Path) -> dict[str, str]:
    """An environment with the stub as ``llama-server`` and a ``chd-curator`` wrapper."""
    py = sys.executable
    server = _sh(tmp_path / "llama-server", f'exec "{py}" "{STUB}" "$@"\n')
    cli = _sh(tmp_path / "chd-curator", f'exec "{py}" -m chd_curator "$@"\n')
    return {
        **os.environ,
        "LLAMA_SERVER": str(server),
        "CHD_CURATOR": str(cli),
        "CHD_CURATOR_PYTHON": py,
        "SERVE_POLL_SECONDS": "0.1",
        "SERVE_WAIT_SECONDS": "20",
        "SERVE_GRACE_SECONDS": "1",
        "SERVE_WORKDIR": str(tmp_path / "work"),
        "STUB_PIDFILE": str(tmp_path / "stub.pid"),
        "STUB_REQUEST_LOG": str(tmp_path / "requests.log"),
        "STUB_ARGS_FILE": str(tmp_path / "stub.args"),
    }


def _site(tmp_path: Path) -> Path:
    path = tmp_path / "site.yaml"
    path.write_text(
        "roles:\n"
        "  triage-screen:\n    id: local\n    kind: openai_compat\n    model: stub-model\n"
        "    context_tokens: 100000\n    concurrency: 2\n"
        "    settings:\n      base_url: http://placeholder.invalid/v1\n      json_schema: true\n"
        "  judge:\n    id: other\n    kind: fake\n    model: m\n"
        "    context_tokens: 1000\n    concurrency: 1\n"
    )
    return path


def _requests(tmp_path: Path, n: int) -> Path:
    path = tmp_path / "req.jsonl"
    keys = [f"PMID:9100000{i}" for i in range(n)]
    path.write_text(
        "".join(
            json.dumps({"key": k, "inputs": {"gene": "TBX5", "hit": f"{k}\nA title"}}) + "\n"
            for k in keys
        )
    )
    return path


def _run(
    tmp_path: Path,
    env: dict[str, str],
    *,
    server_args: str = GOOD_ARGS,
    requests: int = 3,
) -> subprocess.CompletedProcess[str]:
    answers = tmp_path / "answers.jsonl"
    return subprocess.run(
        ["bash", str(SERVE), "--site", str(_site(tmp_path)), "--role", "triage-screen",
         "--model-file", str(tmp_path / "m.gguf"), "--server-args", server_args, "--",
         "--requests", str(_requests(tmp_path, requests)), "--answers", str(answers),
         "--root", str(RULES_ROOT), "--shadow"],
        env=env, capture_output=True, text=True, timeout=90,
    )  # fmt: skip


def _gone(pid: int) -> bool:
    """True when no live process has this pid (a zombie counts as gone)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return True
    return state == "Z"


def test_serve_and_ask_answers_every_request_and_stops_the_server(
    tmp_path: Path, kit: dict[str, str]
) -> None:
    kit["STUB_HEALTH_503"] = "3"  # the script waits through three loading polls
    done = _run(tmp_path, kit)
    assert done.returncode == 0, done.stdout + done.stderr
    rows = [json.loads(x) for x in (tmp_path / "answers.jsonl").read_text().splitlines()]
    assert [r["key"] for r in rows] == [f"PMID:9100000{i}" for i in range(3)]
    assert all(r["parsed"]["decision"] == "included" for r in rows)
    # Only the base URL changed, so the register's key is the original site's.
    original = load_site(_site(tmp_path)).roles["triage-screen"]
    assert {r["model_key"] for r in rows} == {model_key(original)}
    assert len((tmp_path / "requests.log").read_text().split()) == 3
    pid = int((tmp_path / "stub.pid").read_text())
    assert _gone(pid), "the server outlived the script"
    flags = (tmp_path / "stub.args").read_text()
    assert "--load-mode none" in flags and "--reasoning off" in flags
    assert "--host 127.0.0.1" in flags and "-m " + str(tmp_path / "m.gguf") in flags


def test_ask_failure_is_the_scripts_exit_code_and_the_server_still_stops(
    tmp_path: Path, kit: dict[str, str]
) -> None:
    answers = tmp_path / "answers.jsonl"
    done = subprocess.run(
        ["bash", str(SERVE), "--site", str(_site(tmp_path)), "--role", "triage-screen",
         "--model-file", "m.gguf", "--server-args", GOOD_ARGS, "--",
         "--requests", str(tmp_path / "missing.jsonl"), "--answers", str(answers),
         "--root", str(RULES_ROOT), "--shadow"],
        env=kit, capture_output=True, text=True, timeout=90,
    )  # fmt: skip
    assert done.returncode == 2, done.stdout + done.stderr  # ask's usage exit, passed through
    assert _gone(int((tmp_path / "stub.pid").read_text()))


@pytest.mark.parametrize("mode", ["exit_at_once", "die_while_loading"])
def test_a_server_that_dies_while_loading_fails_at_once(
    tmp_path: Path, kit: dict[str, str], mode: str
) -> None:
    kit["STUB_MODE"] = mode
    kit["SERVE_WAIT_SECONDS"] = "600"  # the benchmark's client waited its whole 1,200 s here
    started = time.monotonic()
    done = _run(tmp_path, kit)
    assert time.monotonic() - started < 30
    assert done.returncode == 1
    assert "exited before it became healthy" in done.stderr
    assert "failed to load model" in done.stderr or "--no-mmap" in done.stderr  # the log's tail
    assert not (tmp_path / "answers.jsonl").exists()


def test_a_server_that_never_becomes_healthy_is_stopped_after_the_wait(
    tmp_path: Path, kit: dict[str, str]
) -> None:
    kit["STUB_HEALTH_503"] = "100000"
    kit["SERVE_WAIT_SECONDS"] = "1"
    done = _run(tmp_path, kit)
    assert done.returncode == 1 and "not healthy after 1 s" in done.stderr
    assert _gone(int((tmp_path / "stub.pid").read_text()))


def test_a_server_that_ignores_term_is_killed_after_the_grace_period(
    tmp_path: Path, kit: dict[str, str]
) -> None:
    kit["STUB_MODE"] = "ignore_term"
    done = _run(tmp_path, kit, requests=1)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _gone(int((tmp_path / "stub.pid").read_text()))


def test_a_zombie_counts_as_a_dead_server(tmp_path: Path) -> None:
    """``kill -0`` succeeds on an unreaped exit, which is how the benchmark's client waited
    forever; the script reads the state from /proc. The zombie is built here, from a child
    this process never waits on (the script's own bash would reap its child at once)."""
    child = subprocess.Popen(["true"])
    live = subprocess.Popen(["sleep", "30"])
    try:
        deadline = time.monotonic() + 10
        while not _gone(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Path(f"/proc/{child.pid}/stat").exists(), "the child was reaped; no zombie"
        os.kill(child.pid, 0)  # kill -0 alone would say alive

        def alive(pid: int) -> int:
            return subprocess.run(
                ["bash", "-c", f'source "{SERVE}"; server_alive {pid}'], timeout=20
            ).returncode

        assert alive(child.pid) != 0  # zombie: dead
        assert alive(live.pid) == 0
        assert alive(2**22 + 12345) != 0  # no such process: dead
    finally:
        live.send_signal(signal.SIGKILL)
        live.wait()
        child.wait()


@pytest.mark.parametrize(
    "server_args",
    [
        "--reasoning off",  # no --load-mode none: the model would be page-mapped from the network
        "--load-mode mmap --reasoning off",
        "--load-mode none",  # no explicit --reasoning: the server's `auto` can turn thinking on
        "--load-mode none --reasoning auto",
        "--no-mmap --reasoning off",  # the spelling the benchmark's llama.cpp rejected
        "",
    ],
)
def test_the_script_refuses_server_args_without_load_mode_or_reasoning(
    tmp_path: Path, kit: dict[str, str], server_args: str
) -> None:
    done = _run(tmp_path, kit, server_args=server_args)
    assert done.returncode == 2
    assert "--load-mode none" in done.stderr or "--reasoning" in done.stderr
    assert not (tmp_path / "stub.pid").exists(), "the server was started"
    assert not (tmp_path / "answers.jsonl").exists()


def test_usage_errors_exit_2_before_starting_anything(tmp_path: Path, kit: dict[str, str]) -> None:
    done = subprocess.run(
        ["bash", str(SERVE), "--site", str(_site(tmp_path)), "--role", "judge"],
        env=kit, capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert done.returncode == 2 and "--model-file" in done.stderr
    assert not (tmp_path / "stub.pid").exists()


def test_only_the_base_url_changes_in_the_generated_site(tmp_path: Path) -> None:
    text = local_site_text(_site(tmp_path), "triage-screen", "http://127.0.0.1:9/v1")
    data = YAML(typ="safe").load(text)
    assert set(data["roles"]) == {"triage-screen"}
    entry = data["roles"]["triage-screen"]
    assert entry["settings"] == {"base_url": "http://127.0.0.1:9/v1", "json_schema": True}
    out = tmp_path / "gen.yaml"
    out.write_text(text)
    old = load_site(_site(tmp_path)).roles["triage-screen"]
    new = load_site(out).roles["triage-screen"]
    assert model_key(new) == model_key(old) and new.concurrency == old.concurrency


def test_job_script_prints_the_path_of_each_packaged_script_and_refuses_others() -> None:
    for name in JOB_SCRIPTS:
        done = runner.invoke(app, ["job-script", name])
        assert done.exit_code == 0 and Path(done.output.strip()) == JOBS / name
        assert Path(done.output.strip()).is_absolute()
        assert os.access(JOBS / name, os.X_OK)
    bad = runner.invoke(app, ["job-script", "nope.sh"])
    assert bad.exit_code == 2
    assert all(name in bad.output for name in JOB_SCRIPTS)
    assert set(JOB_SCRIPTS) == {p.name for p in JOBS.iterdir()}


@pytest.mark.parametrize("name", JOB_SCRIPTS)
def test_every_job_script_parses(name: str) -> None:
    done = subprocess.run(["bash", "-n", str(JOBS / name)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_scripts_commit_no_site_detail() -> None:
    for name in JOB_SCRIPTS:
        text = (JOBS / name).read_text()
        assert not re.search(r"#SBATCH\s+--(partition|account|qos|mail|nodelist)", text), name
        assert (
            not re.search(r"\$WORK\b", text) and "/home/" not in text and "/scratch" not in text
        ), name


def test_the_build_job_needs_its_inputs_and_pins_the_web_ui_version(tmp_path: Path) -> None:
    text = (JOBS / "llama-build.sbatch").read_text()
    assert "HF_UI_VERSION" in text and "GGML_BACKEND_DL=ON" in text
    done = subprocess.run(
        ["bash", str(JOBS / "llama-build.sbatch")],
        env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, cwd=tmp_path, timeout=30,
    )  # fmt: skip
    assert done.returncode != 0 and "LLAMA_SRC" in done.stderr


def test_the_cluster_job_runs_the_serve_script_with_its_environment(
    tmp_path: Path, kit: dict[str, str]
) -> None:
    kit.update(
        ASK_SITE=str(_site(tmp_path)), ASK_ROLE="triage-screen", MODEL_FILE="m.gguf",
        SERVER_ARGS=GOOD_ARGS,
    )  # fmt: skip
    done = subprocess.run(
        ["bash", str(JOBS / "cluster-ask.sbatch"),
         "--requests", str(_requests(tmp_path, 2)), "--answers", str(tmp_path / "answers.jsonl"),
         "--root", str(RULES_ROOT), "--shadow"],
        env=kit, capture_output=True, text=True, timeout=90,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    assert len((tmp_path / "answers.jsonl").read_text().splitlines()) == 2
    assert _gone(int((tmp_path / "stub.pid").read_text()))
    kit.pop("ASK_SITE")
    missing = subprocess.run(
        ["bash", str(JOBS / "cluster-ask.sbatch")], env=kit, capture_output=True, text=True,
        timeout=30,
    )  # fmt: skip
    assert missing.returncode != 0 and "ASK_SITE" in missing.stderr
