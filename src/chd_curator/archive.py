"""The review archive: a private git repository holding each review page as it was sent.

An archive is a git checkout with ``.review-archive`` committed at its top. Every command
that takes ``--archive`` refuses (:class:`ArchiveError`, exit 2, nothing written) a
directory that is not such a checkout, including a subdirectory of one. A page is filed at
``<SYMBOL>/<run>/pages/<built_on, compact UTC>--<reviewer slug>.html`` byte for byte, and
its claude.ai artifact URL beside it as ``.artifact``.

Filing is append-only: a target holding different bytes is refused, and one holding the same
bytes is reported as ``already filed``. Whatever was filed but is uncommitted (untracked or
modified, by ``git status --porcelain``) is committed with ``git add`` and ``git commit``
limited to exactly those paths, under the checkout's own ``user.name`` and ``user.email``
(``--local``); the commit message holds no reviewer name and no path. A missing identity is
refused before anything is written. Nothing here pushes.

:class:`ArchiveError` is a refusal (the CLI exits 2); :class:`GitError` is a git command
failing for another reason (exit 1).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from chd_curator.decisions import ExportError, page_data
from chd_curator.fs import write_bytes_atomically
from chd_curator.review_page import slug

MARKER: Final = ".review-archive"
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ArchiveError(Exception):
    """A refusal: a bad argument, or an archive or file that must not be written."""


class GitError(Exception):
    """A git command failed for a reason that is not a refusal."""


def _git(archive: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(archive), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise GitError(f"git cannot be run: {exc}") from exc


def check_archive(archive: Path) -> Path:
    """The resolved ``archive``, once it is the top of a checkout holding a committed marker."""
    top = archive.resolve()
    refusal = f"{archive} is not a review archive: it must be the top of a git checkout"
    if not top.is_dir():
        raise ArchiveError(f"{refusal} with {MARKER} committed")
    found = _git(top, "rev-parse", "--show-toplevel")
    if found.returncode != 0:
        raise ArchiveError(f"{refusal} with {MARKER} committed")
    if Path(found.stdout.strip()).resolve() != top:
        raise ArchiveError(f"{archive} is not the top of its git checkout ({found.stdout.strip()})")
    if _git(top, "ls-files", "--error-unmatch", MARKER).returncode != 0:
        raise ArchiveError(f"{refusal} with {MARKER} committed")
    return top


def check_id(kind: str, value: str) -> str:
    """``value`` when usable as a directory name: ``[A-Za-z0-9][A-Za-z0-9._-]*``."""
    if not _ID.fullmatch(value):
        raise ArchiveError(
            f"{kind} {value!r} is not allowed: letters, digits, '.', '_' and '-', starting with "
            "a letter or digit"
        )
    return value


def compact_utc(value: str) -> str:
    """``%Y%m%dT%H%M%SZ`` of the instant ``value`` names, in UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ArchiveError(f"{value!r} is not a date-time") from exc
    if parsed.tzinfo is None:
        raise ArchiveError(f"{value!r} carries no UTC offset")
    return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class PageIdentity:
    symbol: str
    gene: str
    built_on: str
    literature_sha256: str
    reviewer: str
    role: str


def page_identity(page: Path) -> PageIdentity:
    """Who and what a review page is, read from its inline data."""
    try:
        data = page_data(page)
    except ExportError as exc:
        raise ArchiveError(str(exc)) from exc
    try:
        info: dict[str, Any] = data["page"]
        who: dict[str, Any] = data["reviewer"]
        identity = PageIdentity(
            symbol=str(info["symbol"]),
            gene=str(info["gene"]),
            built_on=str(info["built_on"]),
            literature_sha256=str(info["literature_sha256"]),
            reviewer=str(who["name"]),
            role=str(who["role"]),
        )
    except (KeyError, TypeError) as exc:
        raise ArchiveError(f"{page}: the page's data lacks {exc}") from exc
    if not slug(identity.reviewer):
        raise ArchiveError(f"{page}: the reviewer name {identity.reviewer!r} has no letters")
    return identity


def require_identity(archive: Path) -> None:
    """Refuse unless the checkout's own config sets ``user.name`` and ``user.email``."""
    for key in ("user.name", "user.email"):
        got = _git(archive, "config", "--local", "--get", key)
        if got.returncode != 0 or not got.stdout.strip():
            raise ArchiveError(
                f"the archive checkout has no {key} of its own: "
                f"set it with `git -C <archive> config {key} <value>`"
            )


def commit(archive: Path, paths: Sequence[Path], message: str) -> str:
    """Commit exactly ``paths`` (relative to ``archive``); the short sha."""
    names = [str(p) for p in paths]
    added = _git(archive, "add", "--", *names)
    if added.returncode != 0:
        raise GitError(f"git add failed: {added.stderr.strip()}")
    done = _git(archive, "commit", "-m", message, "--", *names)
    if done.returncode != 0:
        raise GitError(f"git commit failed: {(done.stderr or done.stdout).strip()}")
    sha = _git(archive, "rev-parse", "--short", "HEAD")
    if sha.returncode != 0:
        raise GitError(f"git rev-parse failed: {sha.stderr.strip()}")
    return sha.stdout.strip()


@dataclass(frozen=True)
class Filed:
    path: Path
    state: Literal["filed", "already filed"]


def _uncommitted(archive: Path, path: Path) -> bool:
    status = _git(archive, "status", "--porcelain", "--", str(path))
    if status.returncode != 0:
        raise GitError(f"git status failed: {status.stderr.strip()}")
    return bool(status.stdout.strip())


def file_page(
    archive: Path, symbol: str, run: str, page: Path, artifact: str | None
) -> tuple[list[Filed], str | None]:
    """File ``page`` (and ``artifact``) under ``symbol``/``run``; the paths and the commit sha.

    Every refusal comes before the first write. The sha is None when nothing was uncommitted.
    """
    check_id("symbol", symbol)
    check_id("run id", run)
    if artifact is not None and not artifact.startswith("https://claude.ai/"):
        raise ArchiveError(f"--artifact {artifact!r} must start with https://claude.ai/")
    top = check_archive(archive)
    require_identity(top)
    identity = page_identity(page)
    if identity.symbol != symbol:
        raise ArchiveError(f"{page} is for {identity.symbol}, not {symbol}")
    try:
        payload = page.read_bytes()
    except OSError as exc:
        raise ArchiveError(f"{page}: cannot be read: {exc}") from exc
    stem = (
        Path(symbol)
        / run
        / "pages"
        / f"{compact_utc(identity.built_on)}--{slug(identity.reviewer)}"
    )
    wanted = [(stem.with_suffix(".html"), payload)]
    if artifact is not None:
        wanted.append((stem.with_suffix(".artifact"), (artifact + "\n").encode("utf-8")))

    plan: list[tuple[Path, bytes, bool]] = []
    for rel, content in wanted:
        target = top / rel
        if target.exists() or target.is_symlink():
            try:
                same = target.is_file() and target.read_bytes() == content
            except OSError as exc:
                raise GitError(f"{rel}: cannot be read: {exc}") from exc
            if not same:
                raise ArchiveError(
                    f"the archive already holds {rel} with different content: "
                    "filing never overwrites"
                )
            plan.append((rel, content, False))
        else:
            plan.append((rel, content, True))

    try:
        for rel, content, new in plan:
            if new:
                (top / rel).parent.mkdir(parents=True, exist_ok=True)
                write_bytes_atomically(top / rel, content)
    except OSError as exc:
        raise GitError(f"cannot write into the archive: {exc}") from exc
    filed = [Filed(rel, "filed" if new else "already filed") for rel, _, new in plan]
    pending = [f.path for f in filed if _uncommitted(top, f.path)]
    sha = commit(top, pending, f"file {symbol} {run}: page") if pending else None
    return filed, sha
