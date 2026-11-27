"""A scratch root for every step that writes curated files, and the state carried between steps.

The pipeline never writes the atlas checkout. A process that changes curated files runs in a
*scratch root*: a tree of symlinks to the (read-only) atlas, with the gene's three mutable files
copied in as real files and, when ``state`` folders are given, everything the earlier steps wrote
laid over it (several states, as a fan-out leaves them, are laid over as their union; a file two
states hold with different bytes is refused). ``export_state`` copies that state out again for
the next process, and refuses a file a step wrote that it would not carry.

Why the directories are real. A symlinked directory would let a new file land inside the atlas,
and an atomic replace (``fs.py``) swaps a file link, never its target. So ``curation`` and
``local/literature`` are mirrored as real directories all the way down with every file a link
(nothing else of ``local`` enters the root), and every other top-level entry is one link to its
resolved path. A scratch root is therefore only as safe
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
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from chd_curator.fs import write_bytes_atomically
from chd_curator.run import RunFormatError, RunPaths, load_texts

MUTABLE: Final = (
    "curation/literature/{symbol}.yaml",
    "curation/publications.yaml",
    "curation/.id_registry.yaml",
)
LITERATURE: Final = "literature"  # the one entry of the atlas's local/ a scratch root mirrors
TEXTS_DIR: Final = "local/literature/texts"
_WRITABLE: Final = ("curation", "local")


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


def _lay_over(files: dict[str, Path], dest_local: Path) -> None:
    """Copy each ``{path relative to local/: source}`` under `dest_local` as a real file."""
    for rel, source in sorted(files.items()):
        target = dest_local / rel
        for parent in reversed(target.relative_to(dest_local).parents):
            part = dest_local / parent
            if part.is_symlink():  # never write through a link
                part.unlink()
            part.mkdir(exist_ok=True)
        copy_file(source, target)


def _bytes_or_none(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _check_states(atlas: Path, symbol: str, states: Sequence[Path]) -> dict[str, Path]:
    """Refuse states that cannot be laid over together; return the union of their ``local/``
    files (path relative to ``local/`` -> one source). Exit 1 naming each file: a mutable file
    the atlas has and a state lacks (M6), a mutable file the states do not hold byte for byte
    alike (present in some and absent in others counts), and a ``local/`` path two states hold
    with different bytes."""
    problems: list[str] = []
    for rel in mutable_paths(symbol):
        held = [_bytes_or_none(state / rel) for state in states]
        if (atlas / rel).is_file():
            problems += [
                f"{rel}: the atlas has it and the state {state} lacks it"
                for state, data in zip(states, held, strict=True)
                if data is None
            ]
        if any(data != held[0] for data in held[1:]):
            problems.append(f"{rel}: the states hold it with different bytes")
    union: dict[str, tuple[Path, Path]] = {}
    for state in states:
        local = state / "local"
        for source in sorted(local.rglob("*")) if local.is_dir() else []:
            if not source.is_file():
                continue
            rel = source.relative_to(local).as_posix()
            first_state, first = union.setdefault(rel, (state, source))
            if first is not source and first.read_bytes() != source.read_bytes():
                problems.append(
                    f"local/{rel}: the states {first_state} and {state} hold it with "
                    "different bytes"
                )
    if problems:
        raise ScratchError("refused, no root made:\n" + "\n".join(problems), 1)
    return {rel: source for rel, (_, source) in union.items()}


def scratch_root(atlas: Path, dest: Path, symbol: str, states: Sequence[Path] = ()) -> None:
    """Build a scratch root at `dest` (which must not exist). See the module docstring."""
    if not atlas.is_dir():
        raise ScratchError(f"--atlas {atlas} is not a directory", 2)
    for state in states:
        if not state.is_dir():
            raise ScratchError(f"--state {state} is not a directory", 2)
    if os.path.lexists(dest):
        raise ScratchError(f"--dest {dest} already exists", 2)
    union = _check_states(atlas, symbol, states) if states else {}
    dest.mkdir(parents=True)
    for entry in sorted(atlas.iterdir()):
        if entry.name == "curation" and entry.is_dir():  # a link to a directory too
            _mirror(entry, dest / entry.name)
        elif entry.name == "local" and entry.is_dir():
            (dest / "local").mkdir()
            if (entry / LITERATURE).is_dir():
                _mirror(entry / LITERATURE, dest / "local" / LITERATURE)
        else:
            (dest / entry.name).symlink_to(entry.resolve())
    for rel in mutable_paths(symbol):
        link = dest / rel
        source = (states[0] if states else atlas) / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        if source.is_file():
            copy_file(source, link)
    if union:
        (dest / "local").mkdir(exist_ok=True)
        _lay_over(union, dest / "local")


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


def _written_files(root: Path) -> list[Path]:
    """Every real (not linked) file under the root's ``curation/`` and ``local/``, relative to
    the root. Links are not followed: a mirrored directory is real, a file link is the atlas's."""
    found: list[Path] = []
    for top in _WRITABLE:
        for here, dirs, files in os.walk(root / top):
            base = Path(here)
            dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
            found += [(base / f).relative_to(root) for f in files if not (base / f).is_symlink()]
    return sorted(found)


def export_state(root: Path, symbol: str, run_id: str, dest: Path) -> None:
    """Copy a scratch root's state into `dest` (which must not exist), as real files at the same
    relative paths: the three files (each when present), the run folder when it exists, and
    the texts the run lists when it has a ``texts.json`` (R-SWA-42: the steps before TEXTS have
    no run folder, or one without ``texts.json``). Refused (exit 1, naming every file, nothing
    written): a real file a step wrote under ``curation/`` or ``local/`` that is none of these,
    since the next step would silently lose it."""
    if os.path.lexists(dest):
        raise ScratchError(f"--dest {dest} already exists", 2)
    run = RunPaths(root, symbol, run_id)
    texts = listed_texts(root, symbol, run_id) if run.texts.is_file() else []
    carried = {Path(rel) for rel in mutable_paths(symbol)} | set(texts)
    run_rel = run.dir.relative_to(root)
    stray = [
        rel for rel in _written_files(root) if rel not in carried and run_rel not in rel.parents
    ]
    if stray:
        raise ScratchError(
            "refused, nothing exported: written file(s) the state would not carry:\n"
            + "\n".join(str(rel) for rel in stray),
            1,
        )
    dest.mkdir(parents=True)
    for rel in mutable_paths(symbol):
        if (root / rel).is_file():
            copy_file(root / rel, dest / rel)
    for file in sorted(run.dir.rglob("*")) if run.dir.is_dir() else []:
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
