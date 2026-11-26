"""Recorded answers: ``<dir>/<key>.json`` holds ``text`` and optionally ``usage`` and ``meta``.

The file name is the request key with every character outside ``[A-Za-z0-9._-]`` replaced by
``_``. Two keys that map to one name are refused at lookup. Never touches the network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from chd_curator.backends.base import Answer, BackendError, Request, int_usage
from chd_curator.site import BackendSpec

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def file_name(key: str) -> str:
    return _UNSAFE.sub("_", key) + ".json"


class Replay:
    def __init__(self, spec: BackendSpec) -> None:
        self.spec = spec
        directory = spec.settings.get("dir")
        if not isinstance(directory, str) or not directory:
            raise BackendError(f"{spec.id}: settings.dir is required")
        self._dir = Path(directory)
        self._seen: dict[str, str] = {}

    def ask(self, req: Request) -> Answer:
        name = file_name(req.key)
        other = self._seen.setdefault(name, req.key)
        if other != req.key:
            raise BackendError(
                f"{self.spec.id}: keys {other!r} and {req.key!r} share the file {name}"
            )
        path = self._dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise BackendError(
                f"{self.spec.id}: no recorded answer for key {req.key!r} in {self._dir}"
            ) from None
        except (OSError, ValueError) as exc:
            raise BackendError(f"{self.spec.id}: cannot read {path}: {exc}") from None
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise BackendError(f"{self.spec.id}: {path} has no text")
        meta = data.get("meta")
        return Answer(
            req.key,
            data["text"],
            int_usage(data.get("usage"), tuple(data.get("usage") or ())),
            0.0,
            meta if isinstance(meta, dict) else {},
        )
