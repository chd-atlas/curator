"""The review archive: a private git repository holding each review page as it was sent.

An archive is a git checkout with ``.review-archive`` committed at its top. Every command
that takes ``--archive`` refuses (:class:`ArchiveError`, exit 2, nothing written) a
directory that is not such a checkout, including a subdirectory of one. A page is filed at
``<SYMBOL>/<run>/pages/<built_on, compact UTC>--<reviewer slug>.html`` byte for byte, and
its claude.ai artifact URL beside it as ``.artifact``.

Filing is append-only, judged against what is committed: when ``HEAD`` holds the target, a
payload with different bytes is refused (whatever the working tree now holds there) and the
same bytes are reported as ``already filed``, the working-tree file being restored from
``HEAD`` if it is missing or differs. A target absent from ``HEAD`` but present in the working
tree is refused when its bytes differ, and reported as ``already filed`` when they are the
same. A target that git ignores is refused before anything is written. Whatever is not yet in
``HEAD`` is committed with ``git add`` and ``git commit`` limited to exactly those paths, under
the checkout's own ``user.name`` and ``user.email`` (read with ``--local`` and passed to the
commit as ``-c`` values; every ``GIT_AUTHOR_*``, ``GIT_COMMITTER_*``, ``GIT_CONFIG_COUNT``,
``GIT_CONFIG_KEY_*``, ``GIT_CONFIG_VALUE_*`` and ``GIT_CONFIG_PARAMETERS`` variable is removed
from the commit's environment, so dates and identity are git's own); the commit message holds no
reviewer name and no path. After the commit every committed path is read back from ``HEAD``
and must hold the filed bytes, or :class:`GitError` is raised. A missing identity is refused
before anything is written. Nothing here pushes.

The checks a decisions file must pass are one function, :func:`check_decisions`, which
``file_decisions`` calls before writing and the reader calls on reading, so the archive does
not accept a file its reader refuses. The one cross-file rule, one gene per symbol, is checked
at filing against the decisions files already filed for the symbol.

:class:`ArchiveError` is a refusal (the CLI exits 2); :class:`GitError` is a git command
failing for another reason (exit 1).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator

from chd_curator.atlas import AtlasError, schema_validator
from chd_curator.decisions import ExportError, page_data
from chd_curator.fs import write_bytes_atomically
from chd_curator.review_page import slug

MARKER: Final = ".review-archive"
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ArchiveError(Exception):
    """A refusal: a bad argument, or an archive or file that must not be written."""


class GitError(Exception):
    """A git command failed for a reason that is not a refusal."""


def _git(
    archive: Path, *args: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(archive), *args],
            capture_output=True,
            text=True,
            check=False,
            env=None if env is None else dict(env),
        )
    except OSError as exc:
        raise GitError(f"git cannot be run: {exc}") from exc


def head_bytes(archive: Path, rel: Path) -> bytes | None:
    """The bytes ``HEAD`` holds at ``rel``, or None when ``HEAD`` has no such file."""
    try:
        done = subprocess.run(
            ["git", "-C", str(archive), "show", f"HEAD:{rel.as_posix()}"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise GitError(f"git cannot be run: {exc}") from exc
    return done.stdout if done.returncode == 0 else None


_ENV_PREFIXES: Final = (
    "GIT_AUTHOR_",
    "GIT_COMMITTER_",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_CONFIG_PARAMETERS",
)


def check_reviewer_key(label: str, name: str, role: str) -> None:
    """Refuse a second opinion whose name would be keyed as the owner (``owner`` once stripped)."""
    if role != "owner" and name.strip().casefold() == "owner":
        raise ArchiveError(
            f"{label}: the second opinion {name!r} would be keyed as the owner: "
            "a clinician's name must not read as 'owner'"
        )


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
    if _git(top, "cat-file", "-e", f"HEAD:{MARKER}").returncode != 0:
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
    except KeyError as exc:
        raise ArchiveError(f"{page}: the page's data lacks {exc}") from exc
    except TypeError as exc:
        raise ArchiveError(f"{page}: the page's data has an unexpected shape: {exc}") from exc
    check_reviewer_key(str(page), identity.reviewer, identity.role)
    if not slug(identity.reviewer):
        raise ArchiveError(
            f"{page}: the reviewer name {identity.reviewer!r} has no ASCII letter or digit "
            "for a file name"
        )
    return identity


def _local(archive: Path, key: str) -> str:
    """The checkout's own value of ``key`` (``--local``), refusing when it is unset or empty."""
    got = _git(archive, "config", "--local", "--get", key)
    if got.returncode != 0 or not got.stdout.strip():
        raise ArchiveError(
            f"the archive checkout has no {key} of its own: "
            f"set it with `git -C <archive> config {key} <value>`"
        )
    return got.stdout.strip()


def require_identity(archive: Path) -> None:
    """Refuse unless the checkout's own config sets ``user.name`` and ``user.email``."""
    for key in ("user.name", "user.email"):
        _local(archive, key)


def commit(
    archive: Path, paths: Sequence[Path], message: str, contents: Sequence[bytes | None]
) -> str:
    """Commit exactly ``paths`` (relative to ``archive``); the short sha.

    ``contents`` are the bytes each path must hold in ``HEAD`` afterwards; a path that does
    not is a :class:`GitError` (git can exit 0 having committed nothing for a path). A content
    of None means the path must be absent from ``HEAD`` (a deletion).
    """
    names = [str(p) for p in paths]
    added = _git(archive, "add", "--", *names)
    if added.returncode != 0:
        raise GitError(f"git add failed: {added.stderr.strip()}")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(_ENV_PREFIXES)}
    who = [_local(archive, key) for key in ("user.name", "user.email")]
    done = _git(
        archive,
        "-c",
        f"user.name={who[0]}",
        "-c",
        f"user.email={who[1]}",
        "commit",
        "-m",
        message,
        "--",
        *names,
        env=clean,
    )
    if done.returncode != 0:
        raise GitError(f"git commit failed: {(done.stderr or done.stdout).strip()}")
    for rel, content in zip(paths, contents, strict=True):
        if head_bytes(archive, rel) != content:
            raise GitError(f"git commit left {rel} out of HEAD or changed its bytes")
    sha = _git(archive, "rev-parse", "--short", "HEAD")
    if sha.returncode != 0:
        raise GitError(f"git rev-parse failed: {sha.stderr.strip()}")
    return sha.stdout.strip()


@dataclass(frozen=True)
class Filed:
    path: Path
    state: Literal["filed", "already filed"]


def _plan_target(top: Path, rel: Path, content: bytes) -> tuple[bool, bool]:
    """(is new, is in HEAD) for filing ``content`` at ``rel``; refuse a different file.

    ``HEAD`` decides when it holds ``rel``; otherwise the working tree does. A git-ignored
    target is refused.
    """
    if _git(top, "check-ignore", "-q", "--", rel.as_posix()).returncode == 0:
        raise ArchiveError(f"{rel} is ignored by git: a filing there could not be committed")
    held = head_bytes(top, rel)
    if held is not None:
        if held != content:
            raise ArchiveError(
                f"the archive already holds {rel} with different content: filing never overwrites"
            )
        return False, True
    target = top / rel
    if target.exists() or target.is_symlink():
        try:
            same = target.is_file() and target.read_bytes() == content
        except OSError as exc:
            raise GitError(f"{rel}: cannot be read: {exc}") from exc
        if not same:
            raise ArchiveError(
                f"the archive already holds {rel} with different content: filing never overwrites"
            )
        return False, False
    return True, False


def _holds(target: Path, content: bytes) -> bool:
    try:
        return target.is_file() and not target.is_symlink() and target.read_bytes() == content
    except OSError:
        return False


def _write_targets(top: Path, plan: Sequence[tuple[Path, bytes, bool, bool]]) -> None:
    """Write each new target, and restore one ``HEAD`` holds whose working copy differs."""
    try:
        for rel, content, new, in_head in plan:
            target = top / rel
            if new or (in_head and not _holds(target, content)):
                target.parent.mkdir(parents=True, exist_ok=True)
                write_bytes_atomically(target, content)
    except OSError as exc:
        raise GitError(f"cannot write into the archive: {exc}") from exc


def file_page(
    archive: Path, symbol: str, run: str, page: Path, artifact: str | None
) -> tuple[list[Filed], str | None]:
    """File ``page`` (and ``artifact``) under ``symbol``/``run``; the paths and the commit sha.

    Every refusal comes before the first write. The sha is None when ``HEAD`` already held
    everything.
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

    plan: list[tuple[Path, bytes, bool, bool]] = []
    for rel, content in wanted:
        new, in_head = _plan_target(top, rel, content)
        plan.append((rel, content, new, in_head))

    _write_targets(top, plan)
    filed = [Filed(rel, "filed" if new else "already filed") for rel, _, new, _ in plan]
    todo = [(rel, content) for rel, content, _, in_head in plan if not in_head]
    sha = (
        commit(top, [r for r, _ in todo], f"file {symbol} {run}: page", [c for _, c in todo])
        if todo
        else None
    )
    return filed, sha


def filed_page_for(archive: Path, symbol: str, run: str, doc: Mapping[str, Any]) -> Path | None:
    """The page filed under ``symbol``/``run`` that ``doc`` was made on, relative to ``archive``.

    A page matches when its own ``built_on``, ``literature_sha256``, reviewer name and reviewer
    role equal the decisions file's ``page.built_on``, ``page.literature_sha256`` and
    ``reviewer.name`` / ``reviewer.role`` as strings (both come from the page's data, so no
    normalisation is applied). None when no filed page matches; an unreadable page read before a
    match is refused rather than skipped. With several matches the first by name is returned.
    """
    try:
        info = doc["page"]
        who = doc["reviewer"]
        wanted = (
            str(info["built_on"]),
            str(info["literature_sha256"]),
            str(who["name"]),
            str(who["role"]),
        )
    except (KeyError, TypeError) as exc:
        raise ArchiveError(f"the decisions file lacks {exc}") from exc
    pages = archive / symbol / run / "pages"
    if not pages.is_dir():
        return None
    for page in sorted(pages.glob("*.html")):
        found = page_identity(page)
        if (found.built_on, found.literature_sha256, found.reviewer, found.role) == wanted:
            return page.relative_to(archive)
    return None


def check_decisions(doc: object, label: str, symbol: str, validator: Draft202012Validator) -> str:
    """Refuse a decisions file the archive could not read back; the file name it must carry.

    ``label`` names the file in each message. The checks, in order: a JSON object, the atlas's
    schema, a finished session, the right symbol, a second opinion not keyed as the owner, no
    finding decided twice, and a reviewer name with an ASCII letter or digit for the file
    name. The returned name is ``<finished_at, compact UTC>--<reviewer slug>.json``.
    """
    if not isinstance(doc, dict):
        raise ArchiveError(f"{label}: is not a JSON object")
    errors = sorted(
        f"{'/'.join(map(str, e.path)) or '(file)'}: {e.message}" for e in validator.iter_errors(doc)
    )
    if errors:
        raise ArchiveError(f"{label} fails the schema: {errors[0]}")
    finished = doc["session"]["finished_at"]
    if not finished:
        raise ArchiveError(f"{label}: the session is not finished (session.finished_at is empty)")
    if doc["symbol"] != symbol:
        raise ArchiveError(f"{label} is for {doc['symbol']}, not {symbol}")
    name = str(doc["reviewer"]["name"])
    check_reviewer_key(label, name, doc["reviewer"]["role"])
    ids = [f["id"] for f in doc["findings"]]
    again = sorted({i for i in ids if ids.count(i) > 1})
    if again:
        raise ArchiveError(f"{label}: decides {again[0]} more than once")
    if not slug(name):
        raise ArchiveError(
            f"{label}: the reviewer name {name!r} has no ASCII letter or digit for a file name"
        )
    return f"{compact_utc(finished)}--{slug(name)}.json"


def _filed_genes(top: Path, symbol: str) -> set[str]:
    """The genes named by the decisions files ``HEAD`` holds under ``symbol``."""
    genes: set[str] = set()
    listed = _git(top, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", symbol)
    if listed.returncode != 0:
        raise GitError(f"git ls-tree failed: {listed.stderr.strip()}")
    for name in sorted(listed.stdout.split("\0")):
        parts = name.split("/")
        if len(parts) != 4 or parts[2] != "decisions" or not name.endswith(".json"):
            continue
        try:
            gene = json.loads(head_bytes(top, Path(name)) or b"").get("gene")
        except (ValueError, AttributeError) as exc:
            raise ArchiveError(f"{name}: cannot be read: {exc}") from exc
        if isinstance(gene, str):
            genes.add(gene)
    return genes


def file_decisions(
    archive: Path, symbol: str, run: str, decisions: Path, schema_root: Path
) -> tuple[Filed, str | None]:
    """File the finished decisions file ``decisions`` beside the page it was made on.

    The file is copied byte for byte to
    ``<symbol>/<run>/decisions/<finished_at, compact UTC>--<reviewer slug>.json``. Every refusal
    comes before the first write and before any commit: ids, archive, identity, readable JSON,
    everything :func:`check_decisions` refuses, a filed page it was made on, the page's gene, a
    gene differing from one already filed for the symbol, a git-ignored target, and a target
    that holds different bytes (in ``HEAD``, else in the working tree). The message for a
    different file at that name names it. Returns the filed path, the commit sha (None when
    ``HEAD`` already held the file).
    """
    check_id("symbol", symbol)
    check_id("run id", run)
    top = check_archive(archive)
    require_identity(top)
    try:
        payload = decisions.read_bytes()
    except OSError as exc:
        raise ArchiveError(f"{decisions}: cannot be read: {exc}") from exc
    try:
        doc = json.loads(payload)
    except ValueError as exc:
        raise ArchiveError(f"{decisions}: is not valid JSON: {exc}") from exc
    try:
        validator = schema_validator(schema_root, "review_decisions")
    except AtlasError as exc:
        raise ArchiveError(str(exc)) from exc
    name = check_decisions(doc, str(decisions), symbol, validator)
    page = filed_page_for(top, symbol, run, doc)
    if page is None:
        raise ArchiveError(
            f"no page filed under {symbol}/{run} for {decisions}: file the page it was made on "
            "with archive-page first"
        )
    filed_gene = page_identity(top / page).gene
    if doc["gene"] != filed_gene:
        raise ArchiveError(
            f"{decisions}: gene {doc['gene']} differs from gene {filed_gene} on the filed "
            f"page {page}"
        )
    others = _filed_genes(top, symbol) - {doc["gene"]}
    if others:
        raise ArchiveError(
            f"{decisions}: gene {doc['gene']} differs from the gene already filed for "
            f"{symbol}: {sorted(others)}; the archive may hold one gene per symbol"
        )
    rel = Path(symbol) / run / "decisions" / name
    new, in_head = _plan_target(top, rel, payload)
    _write_targets(top, [(rel, payload, new, in_head)])
    sha = None if in_head else commit(top, [rel], f"file {symbol} {run}: decisions", [payload])
    return Filed(rel, "filed" if new else "already filed"), sha
