"""Model backends: one per ``BackendSpec.kind``."""

from __future__ import annotations

from chd_curator.backends.anthropic_api import AnthropicAPI
from chd_curator.backends.base import (
    Answer,
    Backend,
    BackendError,
    Clock,
    Post,
    Request,
    TransientError,
    now,
    urllib_post,
)
from chd_curator.backends.claude_code import ClaudeCode
from chd_curator.backends.fake import Fake
from chd_curator.backends.openai_compat import OpenAICompat
from chd_curator.backends.replay import Replay
from chd_curator.site import BackendSpec

__all__ = [
    "Answer",
    "Backend",
    "BackendError",
    "Request",
    "TransientError",
    "make_backend",
]


def make_backend(spec: BackendSpec, *, post: Post = urllib_post, clock: Clock = now) -> Backend:
    if spec.kind == "openai_compat":
        return OpenAICompat(spec, post, clock)
    if spec.kind == "anthropic":
        return AnthropicAPI(spec, post, clock)
    if spec.kind == "replay":
        return Replay(spec)
    if spec.kind == "fake":
        return Fake(spec)
    if spec.kind == "claude_code":
        return ClaudeCode(spec, post, clock)
    raise BackendError(f"{spec.id}: unknown backend kind {spec.kind!r}")
