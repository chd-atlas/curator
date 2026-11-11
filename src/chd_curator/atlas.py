"""curator's only link to the atlas.

curator never imports ``chd_atlas``. It asks the atlas about a gene by running
``chd-atlas literature target`` and it checks what it writes against the JSON Schemas the
atlas exports into ``<root>/schemas/``.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator, FormatChecker

TARGET_FORMAT: Final = 1
_TARGET_KEYS: Final = frozenset({"format", "gene", "published", "query", "symbol"})
_TIMEOUT_SECONDS: Final = 300
_FORMAT_CHECKER: Final[FormatChecker] = Draft202012Validator.FORMAT_CHECKER


class AtlasError(Exception):
    """The atlas could not be asked, or its answer was not usable (exit 2 at the CLI)."""


@dataclass(frozen=True)
class Target:
    gene: str
    symbol: str
    published: bool
    query: str


def default_atlas_cli(root: Path) -> Path:
    return root / ".venv" / "bin" / "chd-atlas"


def target(root: Path, symbol: str, atlas_cli: Path) -> Target:
    """Run the atlas's ``literature target`` command and validate its one-line answer."""
    command = [str(atlas_cli), "literature", "target", symbol, "--root", str(root)]
    try:
        run = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        # The OS raises this for a missing file and also for an existing script whose
        # shebang interpreter is missing; only the first is "does not exist".
        if atlas_cli.exists():
            raise AtlasError(
                f"the atlas CLI {atlas_cli} exists but could not be run: {exc}"
            ) from exc
        raise AtlasError(
            f"the atlas CLI {atlas_cli} does not exist; set CHD_ATLAS_CLI or pass --atlas-cli"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AtlasError(f"the atlas CLI {atlas_cli} could not be run: {exc}") from exc
    if run.returncode != 0:
        message = run.stdout.strip() or run.stderr.strip() or f"exit {run.returncode}"
        message = message.removeprefix("error: ")
        raise AtlasError(f"the atlas refused {symbol!r}: {message}")
    try:
        answer = json.loads(run.stdout)
    except ValueError as exc:
        raise AtlasError(f"the atlas's answer is not JSON: {run.stdout[:200]!r}") from exc
    if not isinstance(answer, dict):
        raise AtlasError(f"the atlas's answer is not a JSON object: {run.stdout[:200]!r}")
    keys = set(answer)
    if keys != _TARGET_KEYS:
        raise AtlasError(
            f"the atlas's answer has unexpected keys: extra {sorted(keys - _TARGET_KEYS)}, "
            f"missing {sorted(_TARGET_KEYS - keys)}"
        )
    if answer["format"] != TARGET_FORMAT or type(answer["format"]) is not int:
        raise AtlasError(
            f"the atlas answered format {answer['format']!r}; curator reads format {TARGET_FORMAT}"
        )
    if type(answer["published"]) is not bool:
        raise AtlasError(f"the atlas's published is not a boolean: {answer['published']!r}")
    for key in ("gene", "symbol", "query"):
        if not isinstance(answer[key], str):
            raise AtlasError(f"the atlas's {key} is not a string: {answer[key]!r}")
    return Target(answer["gene"], answer["symbol"], answer["published"], answer["query"])


def schema_validator(root: Path, name: str) -> Draft202012Validator:
    """The validator for ``<root>/schemas/<name>.schema.json``, with formats checked.

    The exported schemas carry no ``$schema`` key, so the draft is chosen here; without
    the format checker an impossible date such as 2026-13-45 would pass.
    """
    path = root / "schemas" / f"{name}.schema.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AtlasError(f"cannot read the atlas's exported schema {path}: {exc}") from exc
    return Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)


def plain(data: Any) -> Any:
    """YAML-loaded data as plain ``dict``/``list`` values, dates as ISO strings."""
    if isinstance(data, Mapping):
        return {str(k): plain(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [plain(v) for v in data]
    if isinstance(data, datetime.date):
        return data.isoformat()
    return data
