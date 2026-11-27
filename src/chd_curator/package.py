"""``package`` and ``integrate``: a run's proposal, and bringing it into the atlas.

A package is a directory: ``proposal/`` (the three mutable files as they stood in the work root,
an absent one staying absent), ``run/`` (the run folder), ``texts/`` (the texts the run lists,
laid out as under ``local/literature/texts/`` plus ``supplied/`` for supplied PDFs),
``review.html`` when the run has one, and ``package.json`` written last.

``integrate`` writes nothing until every check has passed: (1) each of the three files in the
checkout must still have the digest the run started from (every differing file is named), and
each file the checkout holds must have a proposal; (2) the run folder must not exist; (3) a
text already present must be byte-identical (it is then skipped). Only then does it copy.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from chd_curator.fs import write_bytes_atomically
from chd_curator.run import RunPaths
from chd_curator.scratch import (
    ScratchError,
    base_digests,
    copy_file,
    listed_texts,
    mutable_paths,
)

PACKAGE_FORMAT: Final = 1
_TEXTS: Final = "local/literature/texts/"
_SUPPLIED: Final = "local/literature/supplied/"
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write_package(
    work_root: Path,
    symbol: str,
    run_id: str,
    base: Mapping[str, str | None],
    out: Path,
    models: Sequence[Mapping[str, str]],
    atlas_commit: str | None = None,
) -> None:
    """Write the package at `out` (which must not exist), or nothing."""
    if os.path.lexists(out):
        raise ScratchError(f"--out {out} already exists", 2)
    run = RunPaths(work_root, symbol, run_id)
    if not run.dir.is_dir():
        raise ScratchError(f"{run.dir}: the run folder does not exist", 1)
    texts = listed_texts(work_root, symbol, run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    try:
        for rel in mutable_paths(symbol):
            if (work_root / rel).is_file():
                copy_file(work_root / rel, staging / "proposal" / rel)
        for file in sorted(run.dir.rglob("*")):
            if file.is_file():
                copy_file(file, staging / "run" / file.relative_to(run.dir))
        for listed in texts:
            text = str(listed)
            inner = (
                text.removeprefix(_TEXTS)
                if text.startswith(_TEXTS)
                else text.removeprefix("local/literature/")
            )
            copy_file(work_root / listed, staging / "texts" / inner)
        has_review = run.review_html.is_file()
        if has_review:
            copy_file(run.review_html, staging / "review.html")
        write_bytes_atomically(
            staging / "package.json",
            _json(
                {
                    "format": PACKAGE_FORMAT,
                    "symbol": symbol,
                    "run_id": run_id,
                    "base": dict(base),
                    "atlas_commit": atlas_commit,
                    "models": [dict(m) for m in models],
                    "review": "review.html" if has_review else None,
                }
            ),
        )
        staging.chmod(0o755)
        os.rename(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load(package: Path) -> dict[str, Any]:
    path = package / "package.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScratchError(f"{path}: cannot be read as a package ({exc})", 2) from None
    if not isinstance(loaded, dict) or loaded.get("format") != PACKAGE_FORMAT:
        raise ScratchError(f"{path}: not a format {PACKAGE_FORMAT} package", 2)
    for key in ("symbol", "run_id"):
        value = loaded.get(key)
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            raise ScratchError(f"{path}: {key} is {value!r}, not a plain name", 2)
    base = loaded.get("base")
    if not isinstance(base, dict) or set(base) != set(mutable_paths(loaded["symbol"])):
        raise ScratchError(f"{path}: base does not name exactly the three mutable files", 2)
    return loaded


def _texts_in(package: Path) -> list[tuple[Path, str]]:
    """(source, path relative to the atlas) for every text file of the package."""
    root = package / "texts"
    found: list[tuple[Path, str]] = []
    for file in sorted(root.rglob("*")) if root.is_dir() else []:
        if file.is_file():
            rel = file.relative_to(root).as_posix()
            prefix = "supplied/"
            target = _SUPPLIED + rel[len(prefix) :] if rel.startswith(prefix) else _TEXTS + rel
            found.append((file, target))
    return found


def integrate(package: Path, atlas: Path) -> int:
    """Bring `package` into `atlas`. Returns the exit code (0 ok, 1 refused, 2 usage); every
    check runs before the first write, and a refusal names what it found."""
    try:
        return _integrate(package, atlas)
    except ScratchError as exc:
        print(f"error: {exc}")
        return exc.code


def _integrate(package: Path, atlas: Path) -> int:
    if not atlas.is_dir():
        raise ScratchError(f"--atlas {atlas} is not a directory", 2)
    meta = _load(package)
    symbol, run_id = meta["symbol"], meta["run_id"]
    current = base_digests(atlas, symbol)
    problems = [
        f"{rel}: changed since the run started (base {meta['base'][rel]}, now {current[rel]})"
        for rel in mutable_paths(symbol)
        if current[rel] != meta["base"][rel]
    ]
    proposal = package / "proposal"
    problems += [
        f"{rel}: the checkout has it and the package proposes none"
        for rel in mutable_paths(symbol)
        if current[rel] is not None and not (proposal / rel).is_file()
    ]
    if problems:
        raise ScratchError("refused, nothing copied:\n" + "\n".join(problems), 1)
    run_target = RunPaths(atlas, symbol, run_id).dir
    if os.path.lexists(run_target):
        raise ScratchError(f"refused, nothing copied: {run_target} already exists", 1)
    texts = _texts_in(package)
    new: list[tuple[Path, str]] = []
    differing: list[str] = []
    for source, rel in texts:
        target = atlas / rel
        if not target.is_file():
            new.append((source, rel))
        elif target.read_bytes() != source.read_bytes():
            differing.append(f"{rel}: already present with different bytes")
    if differing:
        raise ScratchError("refused, nothing copied:\n" + "\n".join(differing), 1)
    for source, rel in new:
        copy_file(source, atlas / rel)
    run_files = [f for f in sorted((package / "run").rglob("*")) if f.is_file()]
    for file in run_files:
        copy_file(file, run_target / file.relative_to(package / "run"))
    copied = 0
    for rel in mutable_paths(symbol):
        if (proposal / rel).is_file():
            copy_file(proposal / rel, atlas / rel)
            copied += 1
    print(
        f"integrated {symbol}/{run_id}: {copied} curated file(s), {len(run_files)} run file(s), "
        f"{len(new)} new text file(s) ({len(texts) - len(new)} already present)"
    )
    return 0
