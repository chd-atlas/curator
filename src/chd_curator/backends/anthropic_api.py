"""The Anthropic Messages API. The key comes from the environment variable ``key_env`` names."""

from __future__ import annotations

import os
from typing import Any, Final

from chd_curator.backends.base import (
    Answer,
    BackendError,
    Clock,
    Post,
    Request,
    call,
    int_usage,
    now,
    timeout_of,
    urllib_post,
)
from chd_curator.site import BackendSpec

URL: Final = "https://api.anthropic.com/v1/messages"
VERSION: Final = "2023-06-01"


class AnthropicAPI:
    def __init__(self, spec: BackendSpec, post: Post = urllib_post, clock: Clock = now) -> None:
        self.spec = spec
        self._post = post
        self._clock = clock
        self._timeout = timeout_of(spec)

    def _key(self) -> str:
        name = self.spec.key_env
        if name is None:
            raise BackendError(f"{self.spec.id}: key_env is required for the anthropic backend")
        value = os.environ.get(name, "")
        if not value:
            raise BackendError(f"{self.spec.id}: environment variable {name} is not set")
        return value

    def body(self, req: Request) -> dict[str, Any]:
        cap = self.spec.settings.get("max_tokens")
        limit = min(req.max_tokens, cap) if isinstance(cap, int) else req.max_tokens
        return {
            "model": self.spec.model,
            "max_tokens": limit,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
            "temperature": 0,
        }

    def ask(self, req: Request) -> Answer:
        secret = self._key()
        headers = {
            "x-api-key": secret,
            "anthropic-version": VERSION,
            "content-type": "application/json",
        }
        t0 = self._clock()
        data = call(self._post, URL, headers, self.body(req), self._timeout, secret, self.spec.id)
        seconds = self._clock() - t0
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise BackendError(f"{self.spec.id}: the reply has no content blocks")
        text = "".join(
            b["text"]
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
        meta = {"stop_reason": data["stop_reason"]} if data.get("stop_reason") is not None else {}
        usage = int_usage(data.get("usage"), ("input_tokens", "output_tokens"))
        return Answer(req.key, text, usage, seconds, meta)
