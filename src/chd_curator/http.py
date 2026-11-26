"""The one opener behind curator's three HTTP clients, with a recorded-HTTP mode.

`pubmed.urllib_fetch`, `sources.urllib_get` and `variantvalidator.urllib_fetch` all open
their URL through `open_url`, which keeps `urllib.request.urlopen`'s contract: a reply
object (`.status`, `.url`, `.headers`, `.read()`, usable as a context manager) on success,
`urllib.error.HTTPError` with a readable body on an HTTP error status. Two environment
variables change where the answer comes from:

* `CHD_CURATOR_HTTP_CASSETTE=<dir>` answers from files and never touches the network. A
  request with no recorded answer raises `CassetteMiss`.
* `CHD_CURATOR_HTTP_RECORD=<dir>` performs the live request and records the reply, success
  and HTTP error alike.

Setting both is a usage error (`HttpConfigError`).

`CassetteMiss` and `HttpConfigError` derive from `Exception` only. The clients catch
`OSError`, `URLError`, `HTTPException` and `TimeoutError` and turn them into "not reachable"
or "not found"; a miss caught there would read as a paper with no text and drop its evidence
under a green run. Every other path in this module raises the same exceptions the live
opener does, so the clients' handlers are unchanged.

Entries are a pair per request, `<h>.json` (status, reason, content type, final URL, method)
and `<h>.bin` (the body), with `h` the sha256 of `method + "\\n" + url + "\\n" + body`. Any
`api_key` query parameter is removed before hashing and before writing, so no key reaches a
cassette and a replay finds its entry with any key or none. The key is only ever looked for
in the URL's query: a request body is hashed and never inspected (no curator client sends
one).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from chd_curator.fs import write_bytes_atomically

CASSETTE_ENV: Final = "CHD_CURATOR_HTTP_CASSETTE"
RECORD_ENV: Final = "CHD_CURATOR_HTTP_RECORD"


class CassetteMiss(Exception):
    """A request with no recorded answer. Never an `OSError`: see the module docstring."""


class HttpConfigError(Exception):
    """The recording environment is contradictory (both variables set)."""


class Reply:
    """A recorded success, shaped like what `urllib.request.urlopen` returns."""

    def __init__(self, status: int, url: str, headers: Message, body: bytes) -> None:
        self.status = status
        self.url = url
        self.headers = headers
        self._body = io.BytesIO(body)

    def read(self, *args: int) -> bytes:
        return self._body.read(*args)

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self._body.close()

    def __enter__(self) -> Reply:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def without_api_key(url: str) -> str:
    """`url` with every `api_key` query parameter removed, the rest byte for byte."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [p for p in parts.query.split("&") if p.partition("=")[0] != "api_key"]
    return urlunsplit(parts._replace(query="&".join(kept)))


def _key(request: urllib.request.Request) -> tuple[str, str]:
    url = without_api_key(request.full_url)
    body = request.data if isinstance(request.data, bytes) else b""
    material = f"{request.get_method()}\n{url}\n".encode() + body
    return hashlib.sha256(material).hexdigest(), url


def _headers(content_type: str) -> Message:
    headers = Message()
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _replay(directory: Path, request: urllib.request.Request) -> Any:
    digest, url = _key(request)
    try:
        meta = json.loads((directory / f"{digest}.json").read_text(encoding="utf-8"))
        body = (directory / f"{digest}.bin").read_bytes()
        status, reason = int(meta["status"]), str(meta["reason"])
        final, content_type = str(meta["url"]), str(meta["content_type"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Deliberately re-raised as a miss: an unreadable entry is not an empty answer.
        raise CassetteMiss(
            f"no usable recorded reply for {request.get_method()} {url} in {directory} "
            f"({type(exc).__name__}); record it with {RECORD_ENV}"
        ) from None
    headers = _headers(content_type)
    if status >= 400:
        raise urllib.error.HTTPError(final, status, reason, headers, io.BytesIO(body))
    return Reply(status, final, headers, body)


def _record(
    directory: Path,
    request: urllib.request.Request,
    status: int,
    reason: str,
    url: str,
    headers: Any,
    body: bytes,
) -> None:
    digest, _ = _key(request)
    meta = {
        "method": request.get_method(),
        "status": status,
        "reason": reason,
        "content_type": headers.get("Content-Type", "") if headers is not None else "",
        "url": without_api_key(url),
    }
    directory.mkdir(parents=True, exist_ok=True)
    write_bytes_atomically(directory / f"{digest}.bin", body)
    text = json.dumps(meta, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    write_bytes_atomically(directory / f"{digest}.json", text.encode("utf-8"))


def _live_and_record(directory: Path, request: urllib.request.Request, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310
            body: bytes = reply.read()
            status: int = reply.status
            url: str = reply.url
            headers = reply.headers
            _record(
                directory, request, status, getattr(reply, "reason", "") or "", url, headers, body
            )
            return Reply(status, url, headers, body)
    except urllib.error.HTTPError as exc:
        with exc:
            body = exc.read()
            _record(directory, request, exc.code, str(exc.reason), exc.geturl(), exc.headers, body)
            raise urllib.error.HTTPError(
                exc.geturl(), exc.code, str(exc.reason), exc.headers, io.BytesIO(body)
            ) from None


def open_url(req: urllib.request.Request | str, timeout: float) -> Any:
    """Open `req` as `urllib.request.urlopen` would, or from / into a cassette."""
    request = urllib.request.Request(req) if isinstance(req, str) else req  # noqa: S310
    cassette = os.environ.get(CASSETTE_ENV, "")
    record = os.environ.get(RECORD_ENV, "")
    if cassette and record:
        raise HttpConfigError(f"{CASSETTE_ENV} and {RECORD_ENV} are both set; set only one")
    if cassette:
        return _replay(Path(cassette), request)
    if record:
        return _live_and_record(Path(record), request, timeout)
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
