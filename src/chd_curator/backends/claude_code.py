"""Claude Code in headless print mode, with no tools, no MCP and no settings of any checkout.

Pinned against Claude Code 2.1.332 (``claude --version`` on the workstation). Flags, each listed
by that version's ``--help`` unless noted:

* ``-p``: print the answer and exit (non-interactive).
* ``--output-format json``: one result object carrying the text, usage and the model used.
* ``--tools ""``: no built-in tool is available.
* ``--strict-mcp-config``: with no ``--mcp-config`` given, no MCP server is loaded.
* ``--safe-mode``: CLAUDE.md, skills, plugins, hooks, MCP servers and custom commands are off
  while the login still works (``--bare`` would not do: it reads no OAuth login).
* ``--setting-sources ""``: no user, project or local settings file is read.
* ``--disable-slash-commands``, ``--no-session-persistence``, ``--permission-prompts none``.
* ``--system-prompt-file <path>``: ``--help`` omits it, but ``--bare``'s help names it
  ("--system-prompt[-file]") and the controller measured it working on 2026-11-27 (a secret word
  in the file was answered). The role's system text goes there, in a private temporary file
  outside the working directory, removed after the call; only the user text is on stdin. Neither
  is ever in argv.

What the model receives is NOT only that input. The controller measured (2026-11-27, three real
calls, 2.1.332) that Claude Code adds to a custom system prompt an ``# Environment`` section (the
temporary working directory's name, platform, shell, OS version), a model-identity line and
today's date, and puts a ``<system-reminder>`` with account context, including the logged-in
account's email address, before the user text. No flag in this version removes them:
``--exclude-dynamic-system-prompt-sections`` is ignored with a custom system prompt and ``--bare``
never reads the OAuth login. So a ``claude_code`` call is not byte-identical across runs, nor to
an API provider's input; ``meta`` says so (``adds_context``). The guard against the account context
reaching an answer is Task A6's refusal of any answer containing an email address its input lacks.

Claude Code cannot set a temperature or a token cap, so ``meta`` records ``temperature: null``.
The call runs in a fresh empty directory, removed afterwards, so no checkout's CLAUDE.md or
project settings are found, and without ``ANTHROPIC_API_KEY`` or ``ANTHROPIC_AUTH_TOKEN`` in its
environment, so it uses the login rather than billing the API. It never runs on a cluster node:
it refuses when ``SLURM_JOB_ID``, ``LSB_JOBID`` or ``PBS_JOBID`` is set.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any, Final

from chd_curator.backends.base import (
    Answer,
    BackendError,
    Clock,
    Post,
    Request,
    TransientError,
    int_usage,
    now,
    timeout_of,
    urllib_post,
)
from chd_curator.site import BackendSpec

DEFAULT_TIMEOUT: Final = 900.0
REFUSAL: Final = "refusing to run Claude Code on a cluster node"
# The job variable each scheduler sets inside a job: Slurm, LSF, PBS.
CLUSTER_VARIABLES: Final = ("SLURM_JOB_ID", "LSB_JOBID", "PBS_JOBID")
# With either set, Claude Code bills the API instead of the OAuth login it is meant to use.
DROPPED_VARIABLES: Final = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
FLAGS: Final = (
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
)
SYSTEM_FILE_FLAG: Final = "--system-prompt-file"
USAGE_NAMES: Final = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _environment() -> dict[str, str]:
    """The caller's environment less ``DROPPED_VARIABLES``; the caller's own is untouched."""
    return {k: v for k, v in os.environ.items() if k not in DROPPED_VARIABLES}


class ClaudeCode:
    def __init__(self, spec: BackendSpec, _post: Post = urllib_post, clock: Clock = now) -> None:
        # `_post` is accepted only so make_backend can build every kind the same way.
        self.spec = spec
        self._clock = clock
        self._timeout = timeout_of(spec) if "timeout_seconds" in spec.settings else DEFAULT_TIMEOUT
        self._version: str | None = None

    def _refuse_on_a_cluster_node(self) -> None:
        for name in CLUSTER_VARIABLES:
            if os.environ.get(name):
                raise BackendError(f"{self.spec.id}: {REFUSAL} ({name} is set)")

    def preflight(self) -> None:
        """What ``ask`` checks before its first call: not on a cluster node, and ``claude
        --version`` succeeds. Raises ``BackendError`` (or ``TransientError`` on a timeout)."""
        self._refuse_on_a_cluster_node()
        self.version()

    def version(self) -> str:
        """``claude --version``, run once per instance, on first use."""
        if self._version is None:
            try:
                proc = subprocess.run(
                    ["claude", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    env=_environment(),
                )
            except FileNotFoundError:
                raise BackendError(f"{self.spec.id}: the claude executable was not found") from None
            except subprocess.TimeoutExpired:
                raise TransientError(f"{self.spec.id}: claude --version timed out") from None
            out = proc.stdout.strip()
            if proc.returncode != 0 or not out:
                raise BackendError(f"{self.spec.id}: claude --version failed")
            self._version = out
        return self._version

    def ask(self, req: Request) -> Answer:
        who = self.spec.id
        self._refuse_on_a_cluster_node()
        version = self.version()
        t0 = self._clock()
        with (
            tempfile.TemporaryDirectory(prefix="chd-curator-claude-") as cwd,
            tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", prefix="chd-curator-system-", suffix=".txt"
            ) as sysfile,
        ):
            sysfile.write(req.system)
            sysfile.flush()
            argv = ["claude", *FLAGS, SYSTEM_FILE_FLAG, sysfile.name, "--model", self.spec.model]
            try:
                proc = subprocess.run(
                    argv,
                    input=req.user,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=cwd,
                    timeout=self._timeout,
                    check=False,
                    env=_environment(),
                )
            except FileNotFoundError:
                raise BackendError(f"{who}: the claude executable was not found") from None
            except subprocess.TimeoutExpired:
                raise TransientError(f"{who}: claude timed out after {self._timeout:g} s") from None
        seconds = self._clock() - t0
        if proc.returncode != 0:
            lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
            last = lines[-1][:300] if lines else "no stderr"
            raise BackendError(f"{who}: claude exited {proc.returncode}: {last}")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise BackendError(f"{who}: the claude output is not JSON") from None
        if not isinstance(data, dict):
            raise BackendError(f"{who}: the claude output is not a JSON object")
        text = data.get("result")
        if data.get("is_error") is True:
            detail = text if isinstance(text, str) else str(data.get("subtype", "unknown"))
            raise BackendError(f"{who}: claude reported an error: {detail[:300]}")
        if not isinstance(text, str):
            raise BackendError(f"{who}: the claude output has no result text")
        meta: dict[str, Any] = {
            "temperature": None,
            "claude_code_version": version,
            "adds_context": True,
        }
        used = data.get("modelUsage")
        if isinstance(used, dict) and used:
            meta["model"] = sorted(used)[0]
        if isinstance(data.get("duration_ms"), int):
            meta["claude_duration_ms"] = data["duration_ms"]
        return Answer(req.key, text, int_usage(data.get("usage"), USAGE_NAMES), seconds, meta)
