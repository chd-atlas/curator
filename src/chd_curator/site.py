"""The site config: which backend fills each model role at this site.

A site (a laptop, a cluster) keeps one YAML outside every repository::

    roles:
      <role>:
        id: <label>              kind: openai_compat | anthropic | claude_code | replay | fake
        model: <model id>        settings: {...}       (optional)
        context_tokens: <int>    concurrency: <int>    key_env: <ENV VAR NAME>  (optional)

The file never holds a secret. Keys arrive in the environment (a Nextflow secret becomes an
environment variable), and ``key_env`` names the variable. Three guards make a literal key
impossible to load: an unknown key is refused, so ``api_key:`` has nowhere to go; a string under a
key whose name says it is a secret is refused; and a string that starts ``sk-`` is refused wherever
it sits. ``key_env`` must look like a variable name, so a pasted key does not pass as one.

``model_key`` is what the register is keyed on: kind and model, plus a hash of every setting that
changes what the model answers. Transport settings (where the server is, how long to wait) do not.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator.roles import ROLES

KINDS: Final = ("openai_compat", "anthropic", "claude_code", "replay", "fake")
TRANSPORT_KEYS: Final = frozenset({"base_url", "timeout_seconds", "dir"})
FAMILIES: Final = ("fable", "opus", "sonnet", "haiku")

_BACKEND_KEYS: Final = frozenset(
    {"id", "kind", "model", "settings", "context_tokens", "concurrency", "key_env"}
)
_SECRET_NAME: Final = re.compile(r"key|token|secret|password", re.I)
_ENV_NAME: Final = re.compile(r"[A-Z_][A-Z0-9_]*")

Kind = Literal["openai_compat", "anthropic", "claude_code", "replay", "fake"]


class SiteError(Exception):
    """The site config is unreadable, malformed, or holds something it must not."""


@dataclass(frozen=True)
class BackendSpec:
    id: str
    kind: Kind
    model: str
    settings: Mapping[str, Any]
    context_tokens: int
    concurrency: int
    key_env: str | None


@dataclass(frozen=True)
class Site:
    roles: Mapping[str, BackendSpec]


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def model_key(b: BackendSpec) -> str:
    """``kind:model``, plus ``@`` and 12 hex digits of the settings that are not transport."""
    key = f"{b.kind}:{b.model}"
    own = {k: v for k, v in b.settings.items() if k not in TRANSPORT_KEYS}
    if not own:
        return key
    try:
        text = canonical_json(own)
    except (TypeError, ValueError) as exc:
        raise SiteError(f"settings of {b.id!r} are not plain data: {exc}") from None
    return f"{key}@{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


def _scan_for_secrets(node: object, where: str) -> None:
    if isinstance(node, str):
        if node.startswith("sk-"):
            raise SiteError(f"{where}: a value that looks like a key; keep keys in the environment")
    elif isinstance(node, Mapping):
        for name, value in node.items():
            here = f"{where}.{name}"
            if (
                name != "key_env"
                and _SECRET_NAME.search(str(name))
                and isinstance(value, str)
                and value
            ):
                raise SiteError(f"{here}: a secret has no place in the site config; use key_env")
            _scan_for_secrets(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _scan_for_secrets(value, f"{where}[{i}]")


def _int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SiteError(f"{where} must be an integer of at least 1")
    return value


def _backend(role: str, raw: object) -> BackendSpec:
    where = f"roles.{role}"
    if not isinstance(raw, Mapping):
        raise SiteError(f"{where} must be a mapping")
    unknown = sorted(set(map(str, raw)) - _BACKEND_KEYS)
    if unknown:
        raise SiteError(f"{where}: unknown key(s) {', '.join(unknown)}")
    _scan_for_secrets(raw, where)
    for need in ("id", "kind", "model", "context_tokens", "concurrency"):
        if need not in raw:
            raise SiteError(f"{where}: {need} is required")
    for text in ("id", "model"):
        if not isinstance(raw[text], str) or not raw[text].strip():
            raise SiteError(f"{where}.{text} must be a non-empty string")
    if raw["kind"] not in KINDS:
        raise SiteError(f"{where}.kind {raw['kind']!r} is not one of {', '.join(KINDS)}")
    settings = raw.get("settings") or {}
    if not isinstance(settings, Mapping):
        raise SiteError(f"{where}.settings must be a mapping")
    key_env = raw.get("key_env")
    if key_env is not None and not (isinstance(key_env, str) and _ENV_NAME.fullmatch(key_env)):
        raise SiteError(f"{where}.key_env must be the NAME of an environment variable")
    spec = BackendSpec(
        id=raw["id"],
        kind=cast(Kind, raw["kind"]),
        model=raw["model"],
        settings=dict(settings),
        context_tokens=_int(raw["context_tokens"], f"{where}.context_tokens"),
        concurrency=_int(raw["concurrency"], f"{where}.concurrency"),
        key_env=key_env,
    )
    model_key(spec)  # settings must serialise
    return spec


def _family(model: str) -> str | None:
    lower = model.lower()
    found = [(lower.index(w), w) for w in FAMILIES if w in lower]
    return min(found)[1] if found else None


def same_model(a: str, b: str) -> bool:
    """Equal ignoring case, or the same Claude family word in both."""
    if a.lower() == b.lower():
        return True
    fa = _family(a)
    return fa is not None and fa == _family(b)


def load_site(path: Path) -> Site:
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError, UnicodeDecodeError) as exc:
        raise SiteError(f"cannot read site config {path}: {exc}") from None
    if not isinstance(data, Mapping) or set(data) != {"roles"}:
        raise SiteError("a site config is a mapping with one key, roles")
    roles = data["roles"]
    if not isinstance(roles, Mapping) or not roles:
        raise SiteError("roles must map role names to backends")
    out: dict[str, BackendSpec] = {}
    for name, raw in roles.items():
        if name not in ROLES:
            raise SiteError(f"roles.{name}: not a role (one of {', '.join(ROLES)})")
        out[name] = _backend(name, raw)
    if (
        "extract" in out
        and "blind-read" in out
        and same_model(out["extract"].model, out["blind-read"].model)
    ):
        raise SiteError(
            "extract and blind-read must be different models: "
            f"{out['extract'].model!r} and {out['blind-read'].model!r} are the same model"
        )
    return Site(roles=out)
