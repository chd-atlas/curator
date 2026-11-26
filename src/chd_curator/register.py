"""The qualification register: which model may fill which role.

``workflow/qualification.yaml`` ships with curator. One entry says that one backend
(``model_key``) was measured for one role against one gold set and got a verdict: ``pass``,
``fail`` or ``reference``. A ``reference`` is a model that produced the gold, so it is the
baseline and was not measured against it; its ``gold`` and ``gold_digest`` (and
``rules_digest``) may be empty, which a ``pass`` or ``fail`` may not. Today's gold was produced
by the current Claude models plus the owner's corrections, so it favours them; every entry's
``note`` says so.

An entry also carries the ``backend`` it was measured with (kind, model, settings), and the
loader recomputes ``model_key`` from it, so the key a site computes and the key in the register
cannot drift apart unnoticed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chd_curator.roles import ROLES, WORKFLOW
from chd_curator.site import KINDS, BackendSpec, model_key

REGISTER_PATH: Final = WORKFLOW / "qualification.yaml"
_KEYS: Final = frozenset(
    {
        "role",
        "model_key",
        "verdict",
        "gold",
        "gold_digest",
        "rules_digest",
        "date",
        "scores",
        "note",
        "backend",
    }
)
_TEST_KINDS: Final = ("replay", "fake")

Verdict = Literal["pass", "fail", "reference"]


class RegisterError(Exception):
    """The register is unreadable or malformed."""


@dataclass(frozen=True)
class Entry:
    role: str
    model_key: str
    verdict: Verdict
    gold: str
    gold_digest: str
    rules_digest: str
    date: str
    scores: Mapping[str, Any]
    note: str
    backend: Mapping[str, Any] = field(default_factory=dict)


def _entry(i: int, raw: object) -> Entry:
    where = f"entries[{i}]"
    if not isinstance(raw, Mapping):
        raise RegisterError(f"{where} must be a mapping")
    unknown = sorted(set(map(str, raw)) - _KEYS)
    if unknown:
        raise RegisterError(f"{where}: unknown key(s) {', '.join(unknown)}")
    missing = sorted(_KEYS - set(raw))
    if missing:
        raise RegisterError(f"{where}: missing {', '.join(missing)}")
    if raw["role"] not in ROLES:
        raise RegisterError(f"{where}.role {raw['role']!r} is not one of {', '.join(ROLES)}")
    if raw["verdict"] not in ("pass", "fail", "reference"):
        raise RegisterError(f"{where}.verdict {raw['verdict']!r} is not pass, fail or reference")
    when = raw["date"].isoformat() if isinstance(raw["date"], date) else raw["date"]
    texts = {k: raw[k] for k in ("model_key", "gold", "gold_digest", "rules_digest", "note")}
    for name, value in {**texts, "date": when}.items():
        if not isinstance(value, str):
            raise RegisterError(f"{where}.{name} must be a string")
    if not texts["model_key"] or not texts["note"] or not when:
        raise RegisterError(f"{where}: model_key, date and note must not be empty")
    if raw["verdict"] != "reference" and not (
        texts["gold"] and texts["gold_digest"] and texts["rules_digest"]
    ):
        raise RegisterError(
            f"{where}: only a reference entry may leave gold, gold_digest or rules_digest empty"
        )
    if not isinstance(raw["scores"], Mapping):
        raise RegisterError(f"{where}.scores must be a mapping")
    backend = raw["backend"]
    if (
        not isinstance(backend, Mapping)
        or set(backend) != {"kind", "model", "settings"}
        or backend["kind"] not in KINDS
        or not isinstance(backend["model"], str)
        or not isinstance(backend["settings"], Mapping)
    ):
        raise RegisterError(f"{where}.backend must be {{kind, model, settings}}")
    spec = BackendSpec(
        id=where,
        kind=backend["kind"],
        model=backend["model"],
        settings=backend["settings"],
        context_tokens=1,
        concurrency=1,
        key_env=None,
    )
    if model_key(spec) != texts["model_key"]:
        raise RegisterError(
            f"{where}: model_key {texts['model_key']!r} is not the key of its backend "
            f"({model_key(spec)!r})"
        )
    return Entry(
        role=raw["role"],
        model_key=texts["model_key"],
        verdict=cast(Verdict, raw["verdict"]),
        gold=texts["gold"],
        gold_digest=texts["gold_digest"],
        rules_digest=texts["rules_digest"],
        date=when,
        scores=dict(raw["scores"]),
        note=texts["note"],
        backend=dict(backend),
    )


def load_register(path: Path | None = None) -> list[Entry]:
    source = path if path is not None else REGISTER_PATH
    try:
        data = YAML(typ="safe").load(source.read_text(encoding="utf-8"))
    except (OSError, YAMLError, UnicodeDecodeError) as exc:
        raise RegisterError(f"cannot read register {source}: {exc}") from None
    if not isinstance(data, Mapping) or set(data) != {"entries"}:
        raise RegisterError("the register is a mapping with one key, entries")
    if not isinstance(data["entries"], list):
        raise RegisterError("entries must be a list")
    entries = [_entry(i, raw) for i, raw in enumerate(data["entries"])]
    seen: set[tuple[str, str]] = set()
    for e in entries:
        if (e.role, e.model_key) in seen:
            raise RegisterError(f"{e.model_key!r} is entered twice for role {e.role!r}")
        seen.add((e.role, e.model_key))
    return entries


def allowed(role: str, b: BackendSpec, register: list[Entry]) -> Entry | None:
    """The entry that lets ``b`` fill ``role`` (verdict pass or reference), else ``None``.

    A test backend (``replay``, ``fake``) answers from recorded files, not from a model, so it
    needs no entry; it gets a synthetic reference one.
    """
    key = model_key(b)
    if b.kind in _TEST_KINDS:
        return Entry(
            role=role,
            model_key=key,
            verdict="reference",
            gold="",
            gold_digest="",
            rules_digest="",
            date="",
            scores={},
            note="test backend: answers come from recorded files, not a model",
        )
    for e in register:
        if e.role == role and e.model_key == key and e.verdict in ("pass", "reference"):
            return e
    return None
