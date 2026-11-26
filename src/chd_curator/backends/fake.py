"""A scripted backend for tests. ``settings.answers`` maps a request key to outcomes consumed in
order: a string is the answer text, ``{"transient": true}`` raises ``TransientError`` and
``{"error": "..."}`` raises ``BackendError``. It records the requests it received and the peak
number of calls in flight (thread-safe); ``settings.delay`` sleeps inside each call."""

from __future__ import annotations

import threading
import time
from typing import Any

from chd_curator.backends.base import Answer, BackendError, Request, TransientError
from chd_curator.site import BackendSpec


class Fake:
    def __init__(self, spec: BackendSpec) -> None:
        self.spec = spec
        answers = spec.settings.get("answers", {})
        if not isinstance(answers, dict):
            raise BackendError(f"{spec.id}: settings.answers must be a mapping")
        for k, v in answers.items():
            if not isinstance(v, list):
                raise BackendError(f"{spec.id}: the script for {k!r} must be a list")
        self._script: dict[str, list[Any]] = {k: list(v) for k, v in answers.items()}
        self._delay = float(spec.settings.get("delay", 0))
        self._lock = threading.Lock()
        self._active = 0
        self.requests: list[Request] = []
        self.peak = 0

    def ask(self, req: Request) -> Answer:
        with self._lock:
            self.requests.append(req)
            self._active += 1
            self.peak = max(self.peak, self._active)
            queue = self._script.get(req.key)
            outcome = queue.pop(0) if queue else None
        try:
            if self._delay:
                time.sleep(self._delay)
        finally:
            with self._lock:
                self._active -= 1
        if outcome is None:
            raise BackendError(f"{self.spec.id}: no scripted answer left for key {req.key!r}")
        if isinstance(outcome, str):
            # seconds is the configured delay, not a measured time
            return Answer(req.key, outcome, {}, self._delay, {})
        if isinstance(outcome, dict) and outcome.get("transient"):
            raise TransientError(f"{self.spec.id}: scripted transient error")
        if isinstance(outcome, dict) and isinstance(outcome.get("error"), str):
            raise BackendError(outcome["error"])
        raise BackendError(f"{self.spec.id}: unusable scripted outcome for key {req.key!r}")
