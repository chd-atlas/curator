"""A fake web for the source-client tests: canned `Response`s by URL, every URL recorded."""

from __future__ import annotations

import pytest

from chd_curator.sources import Response


class FakeWeb:
    """Serves `routes` (exact URL first, then the longest matching prefix).

    Every URL asked for is recorded in `urls`; a URL no route covers fails the test. A route
    whose value is an exception raises it, as the transport raises `SourceError`.
    """

    def __init__(self, routes: dict[str, Response | Exception]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    def __call__(self, url: str) -> Response:
        self.urls.append(url)
        if url in self.routes:
            answer = self.routes[url]
        else:
            prefixes = [p for p in self.routes if url.startswith(p)]
            if not prefixes:
                pytest.fail(f"unexpected request: {url}")
            answer = self.routes[max(prefixes, key=len)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(url: str, body: bytes, content_type: str = "application/json") -> Response:
    return Response(200, url, content_type, body)
