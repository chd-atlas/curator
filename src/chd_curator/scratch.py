"""A scratch root for every step that writes curated files, and the state carried between steps.

The pipeline never writes the atlas checkout. A process that changes curated files runs in a
*scratch root*: a tree of symlinks to the (read-only) atlas, with the gene's three mutable files
copied in as real files and, when a ``state`` folder is given, everything the earlier steps wrote
laid over it. ``export_state`` copies that state out again for the next process.

Why the directories are real. A symlinked directory would let a new file land inside the atlas,
and an atomic replace (``fs.py``) swaps a file link, never its target. So ``curation`` and
``local`` are mirrored as real directories all the way down with every file a link, and every
other top-level entry is one link to its resolved path. A scratch root is therefore only as safe
as the rule that a writer replaces files and never edits one in place. Every curator write that
can run in a scratch root goes through ``fs.write_bytes_atomically`` (it replaces a link, never
writes through it); ``tests/test_scratch.py`` exercises ``merge``, ``write_batches``,
``write_evidence`` and ``review-page`` on a root whose files are links. A directory that is
itself a link (``local`` in an atlas worktree is one) is mirrored from its resolved target, so
a root built over it still holds only real directories; a link loop is refused.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Final

from chd_curator.fs import write_bytes_atomically
from chd_curator.run import RunFormatError, RunPaths, load_texts

MUTABLE: Final = (
    "curation/literature/{symbol}.yaml",
    "curation/publications.yaml",
    "curation/.id_registry.yaml",
)
_MIRRORED: Final = ("curation", "local")


class ScratchError(Exception):
    """A refusal. ``code`` is the CLI exit code: 1 for content, 2 for usage."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def mutable_paths(symbol: str) -> tuple[str, ...]:
    return tuple(m.format(symbol=symbol) for m in MUTABLE)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_file(source: Path, target: Path) -> None:
    """Copy bytes to ``target`` as a real file, replacing a link or file there."""
    target.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomically(target, source.read_bytes())


def _mirror(src: Path, dst: Path, active: tuple[Path, ...] = ()) -> None:
    """Real directories all the way down, every file a link to its resolved target.

    A directory link is followed and mirrored from its resolved target, never linked whole (a
    link to a directory would let a new file land in its target). `active` holds the resolved
    directories being mirrored above this one: reaching one again is a loop and is refused."""
    resolved = src.resolve()
    if resolved in active:
        raise ScratchError(f"{src}: a directory link loop (it resolves to {resolved} again)", 2)
    dst.mkdir()
    for entry in sorted(resolved.iterdir()):
        if entry.is_dir():
            _mirror(entry, dst / entry.name, (*active, resolved))
        else:
            (dst / entry.name).symlink_to(entry.resolve())


def _lay_over(state_local: Path, dest_local: Path) -> None:
    for source in sorted(state_local.rglob("*")):
        if not source.is_file():
            continue
        target = dest_local / source.relative_to(state_local)
        for parent in reversed(target.relative_to(dest_local).parents):
            part = dest_local / parent
            if part.is_symlink():  # never write through a link
                part.unlink()
            part.mkdir(exist_ok=True)
        copy_file(source, target)


def scratch_root(atlas: Path, dest: Path, symbol: str, state: Path | None) -> None:
    """Build a scratch root at `dest` (which must not exist). See the module docstring."""
    if not atlas.is_dir():
        raise ScratchError(f"--atlas {atlas} is not a directory", 2)
    if state is not None and not state.is_dir():
        raise ScratchError(f"--state {state} is not a directory", 2)
    if os.path.lexists(dest):
        raise ScratchError(f"--dest {dest} already exists", 2)
    dest.mkdir(parents=True)
    for entry in sorted(atlas.iterdir()):
        if entry.name in _MIRRORED and entry.is_dir():  # a link to a directory too
            _mirror(entry, dest / entry.name)
        else:
            (dest / entry.name).symlink_to(entry.resolve())
    for rel in mutable_paths(symbol):
        link = dest / rel
        source = (state if state is not None else atlas) / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        if source.is_file():
            copy_file(source, link)
    if state is not None and (state / "local").is_dir():
        (dest / "local").mkdir(exist_ok=True)
        _lay_over(state / "local", dest / "local")


def listed_texts(root: Path, symbol: str, run_id: str) -> list[Path]:
    """Every file, relative to `root`, of the texts the run's ``texts.json`` lists: each listed
    paper's ``local/literature/texts/<digits>/`` directory whole and its supplied PDF. A path
    ``texts.json`` names that is missing is refused, naming it."""
    run = RunPaths(root, symbol, run_id)
    try:
        papers = load_texts(run)
    except RunFormatError as exc:
        raise ScratchError(str(exc), 1) from None
    found: set[Path] = set()
    for paper in papers:
        digits = paper.pmid.removeprefix("PMID:")
        named = [paper.text, paper.abstract, *paper.supplements]
        for path in named:
            if path is not None and not path.is_file():
                raise ScratchError(f"{paper.pmid}: texts.json names {path}, which is missing", 1)
        directory = root / "local/literature/texts" / digits
        for file in sorted(directory.rglob("*")) if directory.is_dir() else []:
            if file.is_file():
                found.add(file.relative_to(root))
        supplied = root / "local/literature/supplied" / f"{digits}.pdf"
        if supplied.is_file():
            found.add(supplied.relative_to(root))
    return sorted(found)


def export_state(root: Path, symbol: str, run_id: str, dest: Path) -> None:
    """Copy the three files, the run folder and the listed texts of a scratch root into `dest`
    (which must not exist), as real files at the same relative paths."""
    if os.path.lexists(dest):
        raise ScratchError(f"--dest {dest} already exists", 2)
    run = RunPaths(root, symbol, run_id)
    if not run.dir.is_dir():
        raise ScratchError(f"{run.dir}: the run folder does not exist", 1)
    texts = listed_texts(root, symbol, run_id)
    dest.mkdir(parents=True)
    for rel in mutable_paths(symbol):
        if (root / rel).is_file():
            copy_file(root / rel, dest / rel)
    for file in sorted(run.dir.rglob("*")):
        if file.is_file():
            copy_file(file, dest / file.relative_to(root))
    for text in texts:
        copy_file(root / text, dest / text)


def base_digests(atlas: Path, symbol: str) -> dict[str, str | None]:
    """sha256 of each of the three mutable files in the checkout, or None when absent."""
    return {
        rel: sha256_of(atlas / rel) if (atlas / rel).is_file() else None
        for rel in mutable_paths(symbol)
    }


def atlas_head(atlas: Path) -> str | None:
    """The checkout's full HEAD commit, or None when it is not a git checkout."""
    try:
        done = subprocess.run(
            ["git", "-C", str(atlas), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None
