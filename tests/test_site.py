"""The site config: roles to backends, and the one thing it must never hold, a key."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chd_curator.cli import app
from chd_curator.site import TRANSPORT_KEYS, BackendSpec, SiteError, load_site, model_key

runner = CliRunner()

README = Path(__file__).resolve().parents[1] / "README.md"


def _backend(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "b1",
        "kind": "claude_code",
        "model": "claude-sonnet-5-5",
        "context_tokens": 200000,
        "concurrency": 2,
    }
    base.update(over)
    return base


def _write(tmp_path: Path, roles: dict[str, dict[str, object]]) -> Path:
    from ruamel.yaml import YAML

    path = tmp_path / "site.yaml"
    with path.open("w", encoding="utf-8") as handle:
        YAML(typ="safe").dump({"roles": roles}, handle)
    return path


def _spec(**over: object) -> BackendSpec:
    b = _backend(**over)
    return BackendSpec(
        id="b1",
        kind="openai_compat",
        model=str(b["model"]),
        settings=dict(b.get("settings") or {}),  # type: ignore[call-overload]
        context_tokens=1,
        concurrency=1,
        key_env=None,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"api_key": "abc"},
        {"key": "abc"},
        {"token": "abc"},
        {"settings": {"password": "hunter2"}},
        {"settings": {"auth": {"client_secret": "x"}}},
        {"settings": {"header": "sk-live-123"}},
        {"key_env": "sk-live-123"},
        {"key_env": "lowercase_value"},
        {"surprise": 1},
        {"settings": {"Authorization": "Bearer abc123"}},
        {"settings": {"headers": ["Bearer abc123"]}},
        {"settings": {"base_url": "http://user:pw@host"}},
        {"settings": {"base_url": "http://user@host"}},
        {"settings": {"pass": "abc"}},
        {"settings": {"api_key": 123456}},
        {"settings": {"extra": {"deeper": [{"cookie": "c"}]}}},
        {"settings": {"note": "bearer abc"}},
    ],
)
def test_a_site_config_with_a_literal_key_is_refused(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    path = _write(tmp_path, {"extract": _backend(**bad)})
    with pytest.raises(SiteError):
        load_site(path)


def test_a_site_config_loads_and_refuses_the_malformed(tmp_path: Path) -> None:
    ok = load_site(
        _write(
            tmp_path,
            {"extract": _backend(key_env="ANTHROPIC_API_KEY", settings={"temperature": 0})},
        )
    )
    spec = ok.roles["extract"]
    assert spec.key_env == "ANTHROPIC_API_KEY" and spec.settings == {"temperature": 0}
    assert spec.concurrency == 2 and spec.context_tokens == 200000
    for roles in (
        {"summarise": _backend()},
        {"extract": _backend(concurrency=0)},
        {"extract": _backend(context_tokens=0)},
        {"extract": _backend(kind="telepathy")},
        {"extract": {k: v for k, v in _backend().items() if k != "model"}},
    ):
        with pytest.raises(SiteError):
            load_site(_write(tmp_path, roles))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("reader", "blind"),
    [
        ("claude-opus-5-5", "Claude-Opus-5-5"),
        ("claude-opus-5-5", "claude-opus-4-1"),
        ("claude-sonnet-5-5", "anthropic.sonnet-x"),
    ],
)
def test_the_same_model_in_both_reader_roles_is_refused(
    tmp_path: Path, reader: str, blind: str
) -> None:
    path = _write(
        tmp_path,
        {"extract": _backend(model=reader), "blind-read": _backend(id="b2", model=blind)},
    )
    with pytest.raises(SiteError, match="extract.*blind-read"):
        load_site(path)
    different = _write(
        tmp_path,
        {
            "extract": _backend(model="claude-sonnet-5-5"),
            "blind-read": _backend(id="b2", model="claude-opus-5-5"),
        },
    )
    assert set(load_site(different).roles) == {"extract", "blind-read"}


def test_a_model_key_changes_with_any_setting() -> None:
    base = model_key(_spec(settings={}))
    assert base == "openai_compat:claude-sonnet-5-5"
    seen = {base}
    for settings in ({"temperature": 0}, {"temperature": 1}, {"temperature": 0, "seed": 0}):
        seen.add(model_key(_spec(settings=settings)))
    assert len(seen) == 4
    # Order of keys is not a setting.
    assert model_key(_spec(settings={"a": 1, "b": 2})) == model_key(
        _spec(settings={"b": 2, "a": 1})
    )
    # Transport is not a setting of the model.
    assert {"base_url", "timeout_seconds", "dir"} == TRANSPORT_KEYS
    moved = model_key(_spec(settings={"temperature": 0, "base_url": "http://elsewhere"}))
    assert moved == model_key(_spec(settings={"temperature": 0}))
    assert re.fullmatch(r"openai_compat:claude-sonnet-5-5@[0-9a-f]{12}", moved)


def _example() -> str:
    text = README.read_text(encoding="utf-8")
    match = re.search(r"## Site config.*?```yaml site-config\n(.*?)```", text, re.S)
    assert match, "README.md has no yaml block under '## Site config'"
    return match.group(1)


def test_the_readme_site_example_loads(tmp_path: Path) -> None:
    example = _example()
    path = tmp_path / "site.yaml"
    path.write_text(example, encoding="utf-8")
    site = load_site(path)
    assert set(site.roles) == {"triage-screen", "triage-check", "extract", "blind-read", "judge"}
    assert not re.search(r"https?://(?!example\.invalid)", example)
    # The example is a real site: its capable roles are registered, its screen runs shadowed.
    plain = runner.invoke(app, ["check-site", "--site", str(path)])
    assert plain.exit_code == 2 and "triage-screen" in plain.output
    assert "extract" not in plain.output and "judge" not in plain.output
    shadowed = runner.invoke(
        app, ["check-site", "--site", str(path), "--shadow-role", "triage-screen"]
    )
    assert shadowed.exit_code == 0, shadowed.output


def _check(tmp_path: Path, roles: dict[str, dict[str, object]], *extra: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["check-site", "--site", str(_write(tmp_path, roles)), *extra])


def test_check_site_exit_codes(tmp_path: Path) -> None:
    ref = {"kind": "claude_code", "settings": {}, "context_tokens": 1000, "concurrency": 1}
    everything = {
        "triage-screen": {
            "id": "s",
            "kind": "fake",
            "model": "stub",
            "context_tokens": 1000,
            "concurrency": 1,
        },
        "triage-check": {"id": "c", "model": "claude-haiku-5-5", **ref},
        "extract": {"id": "e", "model": "claude-sonnet-5-5", **ref},
        "blind-read": {"id": "b", "model": "claude-opus-5-5", **ref},
        "judge": {"id": "j", "model": "claude-opus-5-5", **ref},
    }
    ok = _check(tmp_path, everything)
    assert ok.exit_code == 0, ok.output
    # An unqualified screen model is named by role and key; shadowing clears it.
    unq = {
        **everything,
        "triage-screen": {
            **everything["triage-screen"],
            "kind": "openai_compat",
            "model": "mystery",
        },
    }
    bad = _check(tmp_path, unq)
    assert bad.exit_code == 2
    assert "triage-screen" in bad.output and "openai_compat:mystery" in bad.output
    shadowed = _check(tmp_path, unq, "--shadow-role", "triage-screen")
    assert shadowed.exit_code == 0, shadowed.output
    # A role the site omits is a failure, not a pass.
    missing = {k: v for k, v in everything.items() if k != "judge"}
    gone = _check(tmp_path, missing)
    assert gone.exit_code == 2 and "judge" in gone.output
    # Only the screen may be shadowed (R-SWA-41): the check decides, so it must be qualified.
    for role in ("judge", "triage-check"):
        usage = _check(tmp_path, everything, "--shadow-role", role)
        assert usage.exit_code == 2 and "shadow" in usage.output, role
    # A site that does not load names why.
    broken = _check(tmp_path, {"extract": {**everything["extract"], "api_key": "x"}})
    assert broken.exit_code == 2 and "api_key" in broken.output
