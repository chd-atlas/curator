"""Backends over HTTP, replay and fake. No network: every HTTP call goes through `FakePost`."""

from __future__ import annotations

import http.client
import http.server
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from chd_curator.backends import (
    BackendError,
    Request,
    TransientError,
    make_backend,
)
from chd_curator.site import BackendSpec
from fake_http import FakePost

KEY = "zz-distinctive-secret-9f3"


def spec(
    kind: str, settings: dict[str, Any] | None = None, key_env: str | None = None
) -> BackendSpec:
    return BackendSpec("m1", kind, "model-x", settings or {}, 1000, 1, key_env)  # type: ignore[arg-type]


def req(schema: dict[str, Any] | None = None) -> Request:
    return Request("k:1", "SYS", "USER", schema, 500)


def ok(payload: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
    return 200, {}, json.dumps(payload).encode()


def clock() -> Any:
    ticks = iter([10.0, 12.5])
    return lambda: next(ticks)


OPENAI_REPLY = {
    "choices": [
        {
            "message": {"content": "hello", "reasoning_content": "thinking"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "extra": 1},
}


def test_openai_sends_the_literal_body_and_reads_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", KEY)
    post = FakePost(ok(OPENAI_REPLY))
    b = make_backend(
        spec("openai_compat", {"base_url": "http://h:8000/v1/"}, "MY_KEY"), post=post, clock=clock()
    )
    ans = b.ask(req())
    url, headers, body, timeout = post.calls[0]
    assert url == "http://h:8000/v1/chat/completions"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert timeout == 600.0
    sent = json.loads(body)
    assert type(sent["temperature"]) is int and type(sent["seed"]) is int
    assert sent == {
        "model": "model-x",
        "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}],
        "temperature": 0,
        "seed": 0,
        "max_tokens": 500,
    }
    assert ans.key == "k:1" and ans.text == "hello" and ans.seconds == 2.5
    assert ans.usage == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert ans.meta == {"reasoning_content": "thinking", "finish_reason": "stop"}


def test_openai_response_format_only_when_the_setting_is_true() -> None:
    schema = {"type": "object"}
    base = {"base_url": "http://h/v1", "timeout_seconds": 5, "max_tokens": 100}
    on = FakePost(ok(OPENAI_REPLY))
    make_backend(spec("openai_compat", {**base, "json_schema": True}), post=on).ask(req(schema))
    body = json.loads(on.calls[0][2])
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "strict": True, "schema": schema},
    }
    assert body["max_tokens"] == 100 and on.calls[0][1].get("authorization") is None
    assert on.calls[0][3] == 5.0
    for settings, r in (
        ({**base, "json_schema": False}, req(schema)),
        ({**base, "json_schema": True}, req()),
    ):
        off = FakePost(ok(OPENAI_REPLY))
        make_backend(spec("openai_compat", settings), post=off).ask(r)
        assert "response_format" not in json.loads(off.calls[0][2])


def test_anthropic_sends_the_literal_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANT_KEY", KEY)
    post = FakePost(
        ok(
            {
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "tool_use", "id": "x"},
                    {"type": "text", "text": "b"},
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2},
                "stop_reason": "end_turn",
            }
        )
    )
    ans = make_backend(spec("anthropic", key_env="ANT_KEY"), post=post, clock=clock()).ask(req())
    url, headers, body, _ = post.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers == {
        "x-api-key": KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    sent = json.loads(body)
    assert type(sent["temperature"]) is int
    assert sent == {
        "model": "model-x",
        "max_tokens": 500,
        "system": "SYS",
        "messages": [{"role": "user", "content": "USER"}],
        "temperature": 0,
    }
    assert (ans.text, ans.usage, ans.meta, ans.seconds) == (
        "ab",
        {"input_tokens": 4, "output_tokens": 2},
        {"stop_reason": "end_turn"},
        2.5,
    )


@pytest.mark.parametrize("value", [None, ""])
def test_anthropic_without_a_key_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.delenv("ANT_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("ANT_KEY", value)
    post = FakePost()
    with pytest.raises(BackendError, match="ANT_KEY"):
        make_backend(spec("anthropic", key_env="ANT_KEY"), post=post).ask(req())
    assert post.calls == []


@pytest.mark.parametrize(
    ("kind", "settings"),
    [("openai_compat", {"base_url": "http://h/v1"}), ("anthropic", {})],
)
@pytest.mark.parametrize(
    ("reply", "transient"),
    [
        ((400, {}, b'{"error": {"message": "bad %KEY%"}}'), False),
        ((401, {}, b'{"error": "invalid key %KEY%"}'), False),
        ((429, {}, b"slow down %KEY%"), True),
        ((500, {}, b"boom %KEY%"), True),
        ((503, {}, b"boom"), True),
        (TimeoutError(f"timed out {KEY}"), True),
        (ConnectionError(f"refused {KEY}"), True),
    ],
)
def test_errors_split_transient_from_final_and_never_carry_the_key(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    settings: dict[str, Any],
    reply: Any,
    transient: bool,
) -> None:
    monkeypatch.setenv("K", KEY)
    if isinstance(reply, tuple):
        reply = (reply[0], reply[1], reply[2].replace(b"%KEY%", KEY.encode()))
    b = make_backend(spec(kind, settings, "K"), post=FakePost(reply))
    with pytest.raises(TransientError if transient else BackendError) as info:
        b.ask(req())
    shown = f"{info.value} {info.value!r} {info.value.args}"
    assert KEY not in shown
    if not transient:
        assert f"HTTP {reply[0]}" in shown


def test_a_final_error_carries_the_servers_message_truncated() -> None:
    long = json.dumps({"error": {"message": "x" * 1000}}).encode()
    b = make_backend(
        spec("openai_compat", {"base_url": "http://h"}), post=FakePost((422, {}, long))
    )
    with pytest.raises(BackendError) as info:
        b.ask(req())
    assert str(info.value).endswith(": " + "x" * 300)


def test_openai_requires_base_url() -> None:
    with pytest.raises(BackendError, match="base_url"):
        make_backend(spec("openai_compat"))


def test_replay_hit_miss_and_collision(tmp_path: Path) -> None:
    (tmp_path / "a_b_1.json").write_text(
        json.dumps({"text": "T", "usage": {"input_tokens": 1}, "meta": {"m": 1}}), encoding="utf-8"
    )
    b = make_backend(spec("replay", {"dir": str(tmp_path)}))
    ans = b.ask(Request("a/b:1", "s", "u", None, 1))
    assert (ans.key, ans.text, ans.usage, ans.meta) == ("a/b:1", "T", {"input_tokens": 1}, {"m": 1})
    with pytest.raises(BackendError, match="a/b 1.*a/b:1|a/b:1.*a/b 1"):
        b.ask(Request("a/b 1", "s", "u", None, 1))
    with pytest.raises(BackendError) as info:
        b.ask(Request("nope", "s", "u", None, 1))
    assert "nope" in str(info.value) and str(tmp_path) in str(info.value)


def test_fake_scripts_outcomes_in_order_and_records_requests() -> None:
    answers = {"k": ["one", {"transient": True}, {"error": "bad"}]}
    b = make_backend(spec("fake", {"answers": answers}))
    r = Request("k", "s", "u", None, 1)
    assert b.ask(r).text == "one"
    with pytest.raises(TransientError):
        b.ask(r)
    with pytest.raises(BackendError, match="bad"):
        b.ask(r)
    with pytest.raises(BackendError, match="no scripted answer"):
        b.ask(r)
    assert b.requests == [r] * 4  # type: ignore[attr-defined]


def test_fake_records_the_peak_of_concurrent_calls() -> None:
    answers = {str(i): ["x"] for i in range(4)}
    b = make_backend(spec("fake", {"answers": answers, "delay": 0.2}))
    threads = [
        threading.Thread(target=b.ask, args=(Request(str(i), "s", "u", None, 1),)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.peak == 4  # type: ignore[attr-defined]


def test_claude_code_waits_for_a_later_task() -> None:
    with pytest.raises(BackendError, match="later task"):
        make_backend(spec("claude_code"))


# ---- fix round 1 ----

HTTP_KINDS = [("openai_compat", {"base_url": "http://h/v1"}), ("anthropic", {})]


@pytest.mark.parametrize(("kind", "settings"), HTTP_KINDS)
@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1, 2]", b"{}"],
)
def test_a_malformed_200_is_a_backend_error(
    monkeypatch: pytest.MonkeyPatch, kind: str, settings: dict[str, Any], raw: bytes
) -> None:
    monkeypatch.setenv("K", KEY)
    b = make_backend(spec(kind, settings, "K"), post=FakePost((200, {}, raw)))
    with pytest.raises(BackendError, match="m1"):
        b.ask(req())


@pytest.mark.parametrize("raw", [{"choices": []}, {"choices": [{"message": {"content": None}}]}])
def test_an_openai_reply_without_text_is_a_backend_error(raw: dict[str, Any]) -> None:
    b = make_backend(spec("openai_compat", {"base_url": "http://h"}), post=FakePost(ok(raw)))
    with pytest.raises(BackendError, match="m1"):
        b.ask(req())


@pytest.mark.parametrize(
    "content",
    [[], [{"type": "tool_use", "id": "x"}], [{"type": "thinking", "thinking": "hm"}]],
)
def test_an_anthropic_reply_without_a_text_block_is_a_backend_error(
    monkeypatch: pytest.MonkeyPatch, content: list[Any]
) -> None:
    monkeypatch.setenv("K", KEY)
    b = make_backend(spec("anthropic", key_env="K"), post=FakePost(ok({"content": content})))
    with pytest.raises(BackendError, match="m1.*no text block"):
        b.ask(req())


@pytest.mark.parametrize(("kind", "settings"), HTTP_KINDS)
def test_a_redirect_is_a_backend_error_and_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, kind: str, settings: dict[str, Any]
) -> None:
    monkeypatch.setenv("K", KEY)
    post = FakePost(
        (302, {"Location": "http://elsewhere/"}, b'{"choices": []}'),
        pytest.fail,  # type: ignore[arg-type]
    )
    with pytest.raises(BackendError, match="HTTP 302") as info:
        make_backend(spec(kind, settings, "K"), post=post).ask(req())
    assert len(post.calls) == 1 and KEY not in str(info.value)


def test_the_default_transport_refuses_to_follow_a_redirect() -> None:
    from urllib import request

    from chd_curator.backends.base import NoRedirect

    r = request.Request("http://a/", headers={"x-api-key": KEY})
    assert NoRedirect().redirect_request(r, None, 302, "Found", {}, "http://b/") is None  # type: ignore[arg-type]


@pytest.mark.parametrize("exc", [http.client.IncompleteRead(b"x"), http.client.BadStatusLine("x")])
def test_http_client_failures_are_transient(exc: Exception) -> None:
    b = make_backend(spec("openai_compat", {"base_url": "http://h"}), post=FakePost(exc))
    with pytest.raises(TransientError):
        b.ask(req())


@pytest.mark.parametrize("usage", [5, "x", ["a"], {"a": "1"}, {"a": True}])
def test_replay_refuses_a_usage_that_is_not_a_str_to_int_mapping(
    tmp_path: Path, usage: Any
) -> None:
    (tmp_path / "k.json").write_text(json.dumps({"text": "T", "usage": usage}), encoding="utf-8")
    b = make_backend(spec("replay", {"dir": str(tmp_path)}))
    with pytest.raises(BackendError, match=r"k\.json"):
        b.ask(Request("k", "s", "u", None, 1))


@pytest.mark.parametrize("kind", ["openai_compat", "anthropic"])
@pytest.mark.parametrize("cap", [True, 0, -5, "9"])
def test_a_bad_max_tokens_setting_is_a_backend_error(
    monkeypatch: pytest.MonkeyPatch, kind: str, cap: Any
) -> None:
    monkeypatch.setenv("K", KEY)
    post = FakePost()
    b = make_backend(spec(kind, {"base_url": "http://h", "max_tokens": cap}, "K"), post=post)
    with pytest.raises(BackendError, match="max_tokens"):
        b.ask(req())
    assert post.calls == []


@pytest.mark.parametrize("script", ["abc", 5, {"a": 1}])
def test_fake_refuses_a_script_that_is_not_a_list(script: Any) -> None:
    with pytest.raises(BackendError, match="list"):
        make_backend(spec("fake", {"answers": {"k": script}}))


class _Hits(http.server.BaseHTTPRequestHandler):
    hits: list[str]
    location: str | None = None

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.hits.append(self.headers.get("x-api-key") or self.headers.get("authorization") or "")
        self.send_response(302 if self.location else 200)
        if self.location:
            self.send_header("Location", self.location)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def two_servers() -> Any:
    """Loopback servers A (answers 302 to B) and B (counts what reaches it)."""
    servers = []
    handlers = []
    for _ in range(2):
        handler = type("H", (_Hits,), {"hits": []})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        handlers.append(handler)
    a, b = servers
    handlers[0].location = f"http://127.0.0.1:{b.server_port}/stolen"
    yield f"http://127.0.0.1:{a.server_port}", handlers[0], handlers[1]
    for s in servers:
        s.shutdown()
        s.server_close()


def test_the_default_transport_never_sends_a_key_to_a_redirect_target(
    monkeypatch: pytest.MonkeyPatch, two_servers: Any
) -> None:
    from chd_curator.backends.base import urllib_post

    url, a, b = two_servers
    monkeypatch.setenv("K", KEY)
    backend = make_backend(spec("openai_compat", {"base_url": url, "timeout_seconds": 5}, "K"))
    with pytest.raises(BackendError, match="HTTP 302") as info:
        backend.ask(req())
    # The Anthropic URL is a constant, so its default transport is called directly.
    status, _, _ = urllib_post(url + "/v1/messages", {"x-api-key": KEY}, b"{}", 5.0)
    assert status == 302
    assert a.hits == [f"Bearer {KEY}", KEY]
    assert b.hits == [] and KEY not in str(info.value)
