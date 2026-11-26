"""Claude Code in headless print mode, given the role's input and nothing else.

Pinned against Claude Code 2.1.332 (``claude --version`` on the workstation). Each flag below is
one that version's ``--help`` lists; none was run against a model while writing this module.

* ``-p``: print the answer and exit (non-interactive).
* ``--output-format json``: one result object carrying the text, usage and the model used.
* ``--tools ""``: no built-in tool is available.
* ``--strict-mcp-config``: with no ``--mcp-config`` given, no MCP server is loaded.
* ``--safe-mode``: CLAUDE.md, skills, plugins, hooks, MCP servers and custom commands are off
  while the login still works (``--bare`` would not do: it reads no OAuth login).
* ``--setting-sources ""``: no user, project or local settings file is read.
* ``--disable-slash-commands``, ``--no-session-persistence``, ``--permission-prompts none``.
* ``--system-prompt``: a fixed neutral line, not the role's system text. This version lists no
  system-prompt *file* flag, so the role's system text and user text both travel on stdin under
  markers, never in argv.

Claude Code cannot set a temperature or a token cap, so ``meta`` records ``temperature: null``.
The call runs in a fresh empty directory, removed afterwards, so no checkout's CLAUDE.md or
project settings are found. It never runs on a cluster node.
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
REFUSAL: Final = "refusing to run Claude Code on a cluster node (SLURM_JOB_ID is set)"
NEUTRAL_SYSTEM: Final = "Follow the SYSTEM section of the input. Answer only what it asks."
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
    "--system-prompt",
    NEUTRAL_SYSTEM,
)
USAGE_NAMES: Final = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def stdin_text(req: Request) -> str:
    return f"=== SYSTEM ===\n{req.system}\n=== USER ===\n{req.user}\n"


class ClaudeCode:
    def __init__(self, spec: BackendSpec, _post: Post = urllib_post, clock: Clock = now) -> None:
        self.spec = spec
        self._clock = clock
        self._timeout = timeout_of(spec) if "timeout_seconds" in spec.settings else DEFAULT_TIMEOUT

    def ask(self, req: Request) -> Answer:
        who = self.spec.id
        if os.environ.get("SLURM_JOB_ID"):
            raise BackendError(f"{who}: {REFUSAL}")
        argv = ["claude", *FLAGS, "--model", self.spec.model]
        t0 = self._clock()
        with tempfile.TemporaryDirectory(prefix="chd-curator-claude-") as cwd:
            try:
                proc = subprocess.run(
                    argv,
                    input=stdin_text(req),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=cwd,
                    timeout=self._timeout,
                    check=False,
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
        meta: dict[str, Any] = {"temperature": None}
        used = data.get("modelUsage")
        if isinstance(used, dict) and used:
            meta["model"] = sorted(used)[0]
        if isinstance(data.get("duration_ms"), int):
            meta["claude_duration_ms"] = data["duration_ms"]
        return Answer(req.key, text, int_usage(data.get("usage"), USAGE_NAMES), seconds, meta)
