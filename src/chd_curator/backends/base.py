"""What a model backend is: a request in, an answer out, and the two ways a call can fail.

``TransientError`` is worth another attempt after a pause (rate limit, server error, timeout,
connection failure). ``BackendError`` is not (a refused request, a missing key, an unknown replay
key). No message here carries a key; ``scrub`` removes the value of the key from any text a server
sent back, because some servers echo the credential they refused.
"""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib import error, request

from chd_curator.site import BackendSpec

DEFAULT_TIMEOUT: Final = 600.0
_ERROR_CHARS: Final = 300

# (url, headers, body, timeout seconds) -> (status, response headers, response body)
Post = Callable[[str, Mapping[str, str], bytes, float], tuple[int, Mapping[str, str], bytes]]
Clock = Callable[[], float]


class BackendError(Exception):
    """The call failed in a way another attempt will not fix."""


class TransientError(Exception):
    """The call failed in a way another attempt may fix."""


@dataclass(frozen=True)
class Request:
    key: str
    system: str
    user: str
    schema: Mapping[str, Any] | None
    max_tokens: int


@dataclass(frozen=True)
class Answer:
    key: str
    text: str
    usage: Mapping[str, int]
    seconds: float
    meta: Mapping[str, Any]


class Backend(Protocol):
    spec: BackendSpec

    def ask(self, req: Request) -> Answer: ...


class NoRedirect(request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would copy the credential headers to the new host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER: Final = request.build_opener(NoRedirect)


def urllib_post(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    """The default transport. An HTTP error status is returned, not raised."""
    req = request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def timeout_of(spec: BackendSpec) -> float:
    value = spec.settings.get("timeout_seconds", DEFAULT_TIMEOUT)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise BackendError(f"{spec.id}: settings.timeout_seconds must be a positive number")
    return float(value)


def capped(spec: BackendSpec, requested: int) -> int:
    """The request's ``max_tokens``, lowered to ``settings.max_tokens`` when that is set."""
    cap = spec.settings.get("max_tokens")
    if cap is None:
        return requested
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise BackendError(f"{spec.id}: settings.max_tokens must be an integer of at least 1")
    return min(requested, cap)


def scrub(text: str, secret: str | None) -> str:
    if secret:
        text = text.replace(secret, "[redacted]")
    return text


def call(
    post: Post,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    timeout: float,
    secret: str | None,
    who: str,
) -> dict[str, Any]:
    """POST ``body`` as JSON and return the parsed 2xx reply, or raise the right error."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        status, _headers, raw = post(url, headers, payload, timeout)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise TransientError(f"{who}: {type(exc).__name__} reaching the server") from None
    if status == 429 or status >= 500:
        raise TransientError(f"{who}: HTTP {status}")
    if status >= 300 or status < 200:
        raise BackendError(f"{who}: HTTP {status}: {_message(raw, secret)}")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise BackendError(f"{who}: the reply is not JSON") from None
    if not isinstance(parsed, dict):
        raise BackendError(f"{who}: the reply is not a JSON object")
    return parsed


def _message(raw: bytes, secret: str | None) -> str:
    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            text = err["message"]
        elif isinstance(err, str):
            text = err
    except ValueError:
        pass
    return scrub(text, secret)[:_ERROR_CHARS]


def int_usage(raw: object, names: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        n: raw[n] for n in names if isinstance(raw.get(n), int) and not isinstance(raw[n], bool)
    }


def now() -> float:
    return time.monotonic()
