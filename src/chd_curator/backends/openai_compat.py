"""Any server that speaks ``POST {base_url}/chat/completions`` (vLLM, llama.cpp, a hosted API).

Temperature 0 and seed 0. ``response_format`` is sent only when ``settings.json_schema`` is true
and the request carries a schema. ``settings.max_tokens``, when set, caps the request's own.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

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


class OpenAICompat:
    def __init__(self, spec: BackendSpec, post: Post = urllib_post, clock: Clock = now) -> None:
        self.spec = spec
        self._post = post
        self._clock = clock
        base = spec.settings.get("base_url")
        if not isinstance(base, str) or not base:
            raise BackendError(f"{spec.id}: settings.base_url is required")
        self._url = base.rstrip("/") + "/chat/completions"
        self._timeout = timeout_of(spec)

    def _key(self) -> str | None:
        name = self.spec.key_env
        if name is None:
            return None
        value = os.environ.get(name, "")
        if not value:
            raise BackendError(f"{self.spec.id}: environment variable {name} is not set")
        return value

    def body(self, req: Request) -> dict[str, Any]:
        cap = self.spec.settings.get("max_tokens")
        limit = min(req.max_tokens, cap) if isinstance(cap, int) else req.max_tokens
        body: dict[str, Any] = {
            "model": self.spec.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "temperature": 0,
            "seed": 0,
            "max_tokens": limit,
        }
        if self.spec.settings.get("json_schema") is True and req.schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": req.schema},
            }
        return body

    def ask(self, req: Request) -> Answer:
        secret = self._key()
        headers = {"content-type": "application/json"}
        if secret:
            headers["authorization"] = f"Bearer {secret}"
        t0 = self._clock()
        data = call(
            self._post, self._url, headers, self.body(req), self._timeout, secret, self.spec.id
        )
        seconds = self._clock() - t0
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise BackendError(
                f"{self.spec.id}: the reply has no choices[0].message.content"
            ) from None
        if not isinstance(text, str):
            raise BackendError(f"{self.spec.id}: the reply's content is not text")
        meta: Mapping[str, Any] = {
            k: v
            for k, v in (
                ("reasoning_content", choice["message"].get("reasoning_content")),
                ("finish_reason", choice.get("finish_reason")),
            )
            if v is not None
        }
        usage = int_usage(data.get("usage"), ("prompt_tokens", "completion_tokens", "total_tokens"))
        return Answer(req.key, text, usage, seconds, meta)
