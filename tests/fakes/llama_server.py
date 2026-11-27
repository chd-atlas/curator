"""A stand-in for ``llama-server``: stdlib HTTP, no model, no network beyond localhost.

Run as ``llama_server.py --host 127.0.0.1 --port N [any other llama-server flag, ignored]``.
The tests reach it through a wrapper named ``llama-server`` that ``exec``s the test interpreter
(PATH's ``python3`` is 3.9), so the process the script under test starts IS this process.

Serves ``GET /health`` (503 for ``STUB_HEALTH_503`` polls, then 200) and
``POST /v1/chat/completions`` (one scripted triage answer per request, for the PMID it finds in
the user message; nothing else is echoed). Environment:

- ``STUB_MODE``: ``ok`` (default); ``exit_at_once`` (prints the load error and exits 3 before it
  listens: the benchmark's ``--no-mmap`` spelling error); ``die_while_loading`` (listens, answers
  503, then exits 3 after 0.4 s: a server that dies while loading); ``ignore_term`` (``ok`` but
  SIGTERM is ignored, so only SIGKILL stops it).
- ``STUB_HEALTH_503``: how many ``/health`` polls answer 503 first (default 0).
- ``STUB_DELAY``: seconds each chat request waits before it answers (default 0).
- The stub exits by itself after ``STUB_LIFETIME`` seconds (default 120), so a test that dies
  before stopping it leaves no server behind for long.
- ``STUB_PIDFILE``: where to write this process's pid; ``STUB_ARGS_FILE``: where to write argv;
  ``STUB_REQUEST_LOG``: one line per chat request is appended there.

A zombie (an exited child nobody has reaped) cannot be produced from inside the child: its own
parent, the script's bash, reaps it. The zombie case is therefore built by the test, in Python,
with a ``Popen`` it does not ``wait`` on; see ``test_jobs.py``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODE = os.environ.get("STUB_MODE", "ok")
HEALTH_503 = int(os.environ.get("STUB_HEALTH_503", "0"))
_polls = 0
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        global _polls
        if self.path != "/health":
            self._send(404, {"error": "no such path"})
            return
        with _lock:
            _polls += 1
            loading = MODE == "die_while_loading" or _polls <= HEALTH_503
        self._send(503 if loading else 200, {"status": "loading model" if loading else "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "no such path"})
            return
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        user = body["messages"][-1]["content"]
        found = re.search(r"PMID:\d+", user)
        answer = {
            "pmid": found.group(0) if found else "PMID:0",
            "decision": "included",
            "reason": "none",
            "why": "stub answer",
        }
        log = os.environ.get("STUB_REQUEST_LOG")
        if log:
            with _lock, open(log, "a", encoding="utf-8") as handle:
                handle.write(f"{answer['pmid']}\n")
        time.sleep(float(os.environ.get("STUB_DELAY", "0")))  # after the log: "in flight"
        self._send(
            200,
            {
                "choices": [
                    {
                        "message": {"content": json.dumps(answer)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
            },
        )


def _arg(name: str, default: str) -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main() -> None:
    for var, value in (("STUB_PIDFILE", str(os.getpid())), ("STUB_ARGS_FILE", " ".join(sys.argv))):
        path = os.environ.get(var)
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(value)
    if MODE == "exit_at_once":
        sys.stderr.write("error: invalid argument: --no-mmap\n")
        raise SystemExit(3)
    if MODE == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    threading.Timer(float(os.environ.get("STUB_LIFETIME", "120")), lambda: os._exit(0)).start()
    server = ThreadingHTTPServer((_arg("--host", "127.0.0.1"), int(_arg("--port", "0"))), Handler)
    if MODE == "die_while_loading":

        def die() -> None:
            time.sleep(0.4)
            sys.stderr.write("error: failed to load model\n")
            os._exit(3)

        threading.Thread(target=die, daemon=True).start()
    signal.signal(
        signal.SIGTERM, signal.SIG_IGN if MODE == "ignore_term" else lambda *_: os._exit(0)
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
