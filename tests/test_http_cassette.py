"""The recorded-HTTP mode: every curator HTTP call answered from files, never the network.

The property that matters is that a cassette miss is not mistaken for a missing paper: each
client's real entry point is driven into a miss and `CassetteMiss` must come out unconverted.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from chd_curator import http as curator_http
from chd_curator import pubmed, sources
from chd_curator import variantvalidator as vv
from chd_curator.http import CassetteMiss, HttpConfigError, open_url

CASSETTE = "CHD_CURATOR_HTTP_CASSETTE"
RECORD = "CHD_CURATOR_HTTP_RECORD"
URL = "https://example.invalid/esearch?term=tbx5"


class FakeLive:
    """Stands in for `urllib.request.urlopen`; counts calls."""

    def __init__(self, status: int = 200, body: bytes = b"live body") -> None:
        self.calls = 0
        self.status = status
        self.body = body

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.calls += 1
        headers = Message()
        headers["Content-Type"] = "text/x-test"
        url = request.full_url
        if self.status >= 400:
            raise urllib.error.HTTPError(url, self.status, "Teapot", headers, io.BytesIO(self.body))
        return _Live(self.status, url, headers, self.body)


class _Live(io.BytesIO):
    def __init__(self, status: int, url: str, headers: Message, body: bytes) -> None:
        super().__init__(body)
        self.status = status
        self.url = url
        self.headers = headers


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> FakeLive:
    fake = FakeLive()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.delenv(CASSETTE, raising=False)
    monkeypatch.delenv(RECORD, raising=False)
    return fake


def _record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str = URL) -> Path:
    cassette = tmp_path / "cassette"
    monkeypatch.setenv(RECORD, str(cassette))
    with open_url(urllib.request.Request(url), 5.0) as reply:
        reply.read()
    monkeypatch.delenv(RECORD)
    return cassette


def test_no_mode_set_is_the_live_opener_unchanged(live: FakeLive) -> None:
    with open_url(urllib.request.Request(URL), 5.0) as reply:
        assert reply.read() == b"live body"
    assert live.calls == 1


def test_record_then_replay_round_trips_the_reply_without_the_network(
    live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cassette = _record(monkeypatch, tmp_path)
    assert live.calls == 1
    monkeypatch.setenv(CASSETTE, str(cassette))
    with open_url(urllib.request.Request(URL), 5.0) as reply:
        assert reply.status == 200
        assert reply.url == URL
        assert reply.headers.get("Content-Type", "") == "text/x-test"
        assert reply.read() == b"live body"
    assert live.calls == 1  # replay never touched the opener
    assert len(list(cassette.glob("*.json"))) == len(list(cassette.glob("*.bin"))) == 1


def test_an_unrecorded_request_raises_a_miss_naming_the_url_without_its_key(
    live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(CASSETTE, str(tmp_path))
    with pytest.raises(CassetteMiss) as caught:
        open_url(urllib.request.Request(URL + "&api_key=SECRETVALUE"), 5.0)
    assert URL in str(caught.value)
    assert "SECRETVALUE" not in str(caught.value)
    assert live.calls == 0


def test_a_miss_is_no_kind_of_network_or_http_failure() -> None:
    """Every client's `except` clause catches these; a miss caught by one reads as 'not found'."""
    assert not issubclass(CassetteMiss, (OSError, urllib.error.URLError, urllib.error.HTTPError))
    assert not issubclass(CassetteMiss, (TimeoutError, ValueError))


def test_the_api_key_is_written_to_no_file_and_replay_ignores_it(
    live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cassette = _record(monkeypatch, tmp_path, URL + "&api_key=SECRETVALUE")
    for path in cassette.iterdir():
        assert b"SECRETVALUE" not in path.read_bytes(), path.name
        assert "SECRETVALUE" not in path.name
    monkeypatch.setenv(CASSETTE, str(cassette))
    for asked in (URL + "&api_key=OTHER", URL + "&api_key=SECRETVALUE", URL):
        with open_url(urllib.request.Request(asked), 5.0) as reply:
            assert reply.read() == b"live body"
    assert live.calls == 1


def test_the_key_is_stripped_from_a_url_with_other_params_in_any_position(
    live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cassette = _record(monkeypatch, tmp_path, "https://example.invalid/e?api_key=K1&a=1&b=2")
    monkeypatch.setenv(CASSETTE, str(cassette))
    with open_url(urllib.request.Request("https://example.invalid/e?a=1&api_key=K2&b=2"), 5.0):
        pass
    with pytest.raises(CassetteMiss):  # a different non-key param is a different request
        open_url(urllib.request.Request("https://example.invalid/e?a=9&b=2"), 5.0)


def test_method_and_body_are_part_of_the_key(
    live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cassette = tmp_path / "c"
    monkeypatch.setenv(RECORD, str(cassette))
    with open_url(urllib.request.Request(URL, data=b"one", method="POST"), 5.0):
        pass
    monkeypatch.delenv(RECORD)
    monkeypatch.setenv(CASSETTE, str(cassette))
    with open_url(urllib.request.Request(URL, data=b"one", method="POST"), 5.0):
        pass
    for other in (
        urllib.request.Request(URL),
        urllib.request.Request(URL, data=b"two", method="POST"),
    ):
        with pytest.raises(CassetteMiss):
            open_url(other, 5.0)


def test_an_http_error_is_recorded_and_replayed_as_the_same_http_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeLive(status=418, body=b"short and stout")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.delenv(CASSETTE, raising=False)
    monkeypatch.setenv(RECORD, str(tmp_path / "c"))
    with pytest.raises(urllib.error.HTTPError) as live_err:
        open_url(urllib.request.Request(URL), 5.0)
    assert live_err.value.read() == b"short and stout"  # the recording read the body first
    monkeypatch.delenv(RECORD)
    monkeypatch.setenv(CASSETTE, str(tmp_path / "c"))
    with pytest.raises(urllib.error.HTTPError) as replayed:
        open_url(urllib.request.Request(URL), 5.0)
    err = replayed.value
    assert (err.code, err.reason, err.geturl()) == (418, "Teapot", URL)
    assert err.headers.get("Content-Type", "") == "text/x-test"
    assert err.read() == b"short and stout"
    assert fake.calls == 1


def test_both_modes_at_once_is_a_usage_error_naming_both(
    live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(CASSETTE, str(tmp_path))
    monkeypatch.setenv(RECORD, str(tmp_path))
    with pytest.raises(HttpConfigError) as caught:
        open_url(urllib.request.Request(URL), 5.0)
    assert CASSETTE in str(caught.value) and RECORD in str(caught.value)
    assert not isinstance(caught.value, OSError)
    assert live.calls == 0


def test_an_unreadable_entry_is_a_miss_not_an_empty_answer(
    live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cassette = _record(monkeypatch, tmp_path)
    next(cassette.glob("*.json")).write_text("{not json")
    monkeypatch.setenv(CASSETTE, str(cassette))
    with pytest.raises(CassetteMiss):
        open_url(urllib.request.Request(URL), 5.0)
    next(cassette.glob("*.json")).unlink()
    with pytest.raises(CassetteMiss):
        open_url(urllib.request.Request(URL), 5.0)


# -- every client, driven through its real entry point ---------------------------------------


def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pubmed, "_throttle", pubmed._Throttle(lambda: 0.0, lambda s: None))
    monkeypatch.setattr(sources, "_throttle", sources._HostThrottle(lambda: 0.0, lambda s: None))
    monkeypatch.setattr(vv, "_throttle", vv._Throttle(lambda: 0.0, lambda s: None))


CLIENTS = {
    "pubmed": (pubmed.urllib_fetch, "https://eutils.ncbi.nlm.nih.gov/x?api_key=SECRETVALUE"),
    "sources": (sources.urllib_get, "https://europepmc.org/x"),
    "variantvalidator": (vv.urllib_fetch, "https://rest.variantvalidator.org/x"),
}


@pytest.mark.parametrize("client", sorted(CLIENTS))
def test_a_cassette_miss_escapes_every_client_unconverted(
    client: str, live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_throttle(monkeypatch)
    monkeypatch.setenv(CASSETTE, str(tmp_path))
    fetch, url = CLIENTS[client]
    with pytest.raises(CassetteMiss) as caught:
        fetch(url)
    assert "SECRETVALUE" not in str(caught.value)
    assert url.split("?")[0] in str(caught.value)
    assert live.calls == 0


@pytest.mark.parametrize("client", sorted(CLIENTS))
def test_every_client_replays_what_it_recorded_with_no_network_call(
    client: str, live: FakeLive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_throttle(monkeypatch)
    fetch, url = CLIENTS[client]
    monkeypatch.setenv(RECORD, str(tmp_path))
    first = fetch(url)
    monkeypatch.delenv(RECORD)
    monkeypatch.setenv(CASSETTE, str(tmp_path))
    second = fetch(url)
    assert live.calls == 1
    if client == "sources":
        assert (second.status, second.body, second.content_type) == (  # type: ignore[union-attr]
            200,
            b"live body",
            "text/x-test",
        )
        assert first == second
    else:
        assert second == first == b"live body"


def test_a_recorded_404_reads_as_each_client_reads_a_live_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replay changes nothing about what a recorded error means to a client."""
    _no_throttle(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", FakeLive(status=404, body=b"gone"))
    monkeypatch.setenv(RECORD, str(tmp_path))
    answers: dict[str, object] = {}
    for name, (fetch, url) in CLIENTS.items():
        try:
            answers[name] = fetch(url)
        except Exception as exc:  # noqa: BLE001
            answers[name] = type(exc).__name__
    monkeypatch.delenv(RECORD)
    monkeypatch.setenv(CASSETTE, str(tmp_path))
    replayed: dict[str, object] = {}
    for name, (fetch, url) in CLIENTS.items():
        try:
            replayed[name] = fetch(url)
        except Exception as exc:  # noqa: BLE001
            replayed[name] = type(exc).__name__
    assert replayed == answers
    assert answers["variantvalidator"] == "NotFound"
    assert answers["pubmed"] == "PubMedError"
    assert answers["sources"].status == 404  # type: ignore[attr-defined]


def test_the_clients_no_longer_call_the_stdlib_opener_directly() -> None:
    for module in (pubmed, sources, vv):
        text = Path(module.__file__ or "").read_text()
        assert "urllib.request.urlopen" not in text, module.__name__
    assert curator_http.open_url is open_url
