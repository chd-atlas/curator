"""Removing a reviewer from the review archive on request (CP-D16).

A clinician asks to be removed. :func:`plan_redact` lists what that touches without changing
anything; :func:`apply_redact` does it and commits it. A reviewer is found by key, the name
stripped and casefolded, so "Dr X" and " dr x" are one clinician; the owner (key ``owner``) is
never removed.

The plan:

* ``delete``: the reviewer's decisions files, their own pages and those pages' ``.artifact``
  files (a file counts when its ``reviewer`` is not the owner and its key matches);
* ``rewrite``: owner pages whose data names the reviewer in a *structural field*, that is an
  entry of ``second_opinions[*][*]``, ``triage_earlier.clinicians[*]``,
  ``triage_earlier.flips[*]`` or ``not_on_page[*]`` whose ``name`` has that key (in
  ``triage_earlier.flips`` and ``not_on_page`` only an entry whose ``role`` is
  ``second_opinion``). In each one ``name`` becomes :data:`REMOVED` and ``note`` becomes ``""``;
  every other value stays. The data is re-serialised with :func:`review_page.inline_json` and every
  byte outside the data element is left alone (:func:`redacted_page`);
* ``republish`` / ``unpublish``: the claude.ai URLs the owner must republish or take down;
* ``elsewhere``: every file under the archive (``.git`` excluded, README included) that
  :func:`name_pattern` still matches once the deletes and rewrites are done, and every
  surviving file whose lowercased path holds the name's file-name slug (``review_page.slug``) as
  a whole token (``-`` is a boundary, so ``dr-x`` matches ``dr-x-smith`` but not ``dr-xavier``),
  so a clinician spelled "Dr. X" is found by the ``--dr-x`` in their file names. Redaction never
  edits the owner's own notes or any other prose, so a name found there stops ``--apply`` until a
  curator edits it.

An owner page whose data cannot be read, and any file the scan cannot read, is refused
(:class:`ArchiveError`), never skipped. So is an archive in which a path carries a
skip-worktree or assume-unchanged bit (``git ls-files -v``: ``S`` or a lowercase tag): git then
hides a working-tree change from ``git status``, so the plan could list nothing for a file
``HEAD`` still holds. :func:`apply_redact` needs a clean checkout, deletes, rewrites, adds a line
under README's ``## Removals``, scans again (:func:`_rescan`), and commits with the archive's own
identity through :func:`archive.commit`. When the scan finds the name, a write fails, or the
commit fails, it restores every touched path with ``git checkout HEAD -- <paths>`` and raises
:class:`GitError`.

History still holds the name and the notes after that commit. ``apply_redact`` leaves
``.git/redact-expressions.txt`` and ``.git/redact-paths.txt`` (the deleted files and the rewritten
owner pages, whose original blobs hold the clinician's notes) for a history rewrite, and
:func:`procedure` is the text the command prints for the owner to run. That procedure has not been
run: it is unmeasured as of 2026-11-25, and ``git filter-repo`` is not installed where it was
written.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from chd_curator.archive import (
    ArchiveError,
    GitError,
    _git,
    check_archive,
    commit,
    require_identity,
)
from chd_curator.archive_read import open_archive
from chd_curator.decisions import _DATA_OPEN, ExportError, page_data_from_text
from chd_curator.fs import write_bytes_atomically
from chd_curator.review_page import inline_json, slug

REMOVED: Final = "a removed reviewer"
MESSAGE: Final = "redact a removed reviewer"
_README: Final = "README.md"
_HEADING: Final = "## Removals"


@dataclass(frozen=True)
class RedactPlan:
    delete: tuple[str, ...]  # their decisions files, their own pages and those pages' .artifact
    rewrite: tuple[str, ...]  # owner pages whose data names them in a structural field
    republish: tuple[str, ...]  # the .artifact URLs of the rewritten owner pages
    unpublish: tuple[str, ...]  # the .artifact URLs of their own pages
    # surviving files the name pattern matches after the plan, or whose path holds the name's slug
    elsewhere: tuple[str, ...]


def _key(name: str) -> str:
    return name.strip().casefold()


def name_pattern(name: str) -> re.Pattern[str]:
    """The name as a whole word (or words), any case: initials never match inside a word."""
    return re.compile(r"(?<!\w)" + re.escape(name.strip()) + r"(?!\w)", re.IGNORECASE)


def _hit(entry: object, key: str, *, second_opinion_only: bool) -> bool:
    if not isinstance(entry, dict):
        return False
    name = entry.get("name")
    if not isinstance(name, str) or _key(name) != key:
        return False
    return not second_opinion_only or entry.get("role") == "second_opinion"


def _structural(data: Mapping[str, Any], key: str) -> Iterator[dict[str, Any]]:
    """Every structural entry of an owner page's data that names ``key``."""
    opinions = data.get("second_opinions")
    if isinstance(opinions, dict):
        for entries in opinions.values():
            for entry in entries if isinstance(entries, list) else ():
                if _hit(entry, key, second_opinion_only=False):
                    yield entry
    earlier = data.get("triage_earlier")
    if isinstance(earlier, dict):
        for entry in earlier.get("clinicians") or ():
            if _hit(entry, key, second_opinion_only=False):
                yield entry
        for entry in earlier.get("flips") or ():
            if _hit(entry, key, second_opinion_only=True):
                yield entry
    for entry in data.get("not_on_page") or ():
        if _hit(entry, key, second_opinion_only=True):
            yield entry


def _redact_data(data: dict[str, Any], key: str) -> int:
    """Redact ``data`` in place; how many entries changed."""
    found = list(_structural(data, key))
    for entry in found:
        entry["name"] = REMOVED
        entry["note"] = ""
    return len(found)


def redacted_page(text: str, key: str) -> str:
    """``text`` with ``key``'s structural entries redacted, only the data element changed.

    A page that names the key nowhere structurally is returned unchanged.
    """
    data = page_data_from_text(text, "the page")
    if _redact_data(data, key) == 0:
        return text
    start = text.index(_DATA_OPEN) + len(_DATA_OPEN)
    end = text.index("</script>", start)
    return text[:start] + inline_json(data) + text[end:]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ArchiveError(f"{path}: cannot be read: {exc}") from exc


def _files(top: Path) -> list[Path]:
    return sorted(
        p for p in top.rglob("*") if p.is_file() and ".git" not in p.relative_to(top).parts[:1]
    )


def _reviewer(doc: object, label: str) -> tuple[str, str]:
    try:
        who = doc["reviewer"]  # type: ignore[index]
        return str(who["name"]), str(who["role"])
    except (KeyError, TypeError) as exc:
        raise ArchiveError(f"{label}: no reviewer name and role ({exc})") from exc


def _url(path: Path) -> str | None:
    return _read_text(path).strip() if path.is_file() else None


def _slug_token(name: str) -> re.Pattern[str] | None:
    """The name's file-name slug as a whole token of a lowercased path, None when it has none.

    Any character that is not a letter or digit bounds the token, ``-`` included, so ``dr-x``
    matches ``dr-x-smith`` and ``--dr-x.json`` but not ``dr-xavier``.
    """
    word = slug(name)
    if not word:
        return None
    return re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])")


def _matches(
    top: Path,
    name: str,
    gone: frozenset[str],
    rewritten: Mapping[str, str],
) -> tuple[str, ...]:
    """The surviving files that still name ``name``: by content, or by a slug in the path."""
    pattern = name_pattern(name)
    token = _slug_token(name)
    found: list[str] = []
    for path in _files(top):
        rel = path.relative_to(top).as_posix()
        if rel in gone:
            continue
        if token is not None and token.search(rel.lower()):
            found.append(rel)
            continue
        text = rewritten.get(rel)
        if text is None:
            try:
                text = path.read_bytes().decode("utf-8", errors="replace")
            except OSError as exc:
                raise ArchiveError(f"{rel}: cannot be read: {exc}") from exc
        if pattern.search(text):
            found.append(rel)
            continue
        if rel.endswith(".json"):  # a name written as \uXXXX escapes still reads as the name
            try:
                plain = json.dumps(json.loads(text), ensure_ascii=False)
            except ValueError:
                continue
            if pattern.search(plain):
                found.append(rel)
    return tuple(found)


def _refuse_hidden(top: Path) -> None:
    """Refuse an archive in which git has been told to hide a path's working-tree state."""
    listed = _git(top, "ls-files", "-v", "-z")
    if listed.returncode != 0:
        raise GitError(f"git ls-files failed: {listed.stderr.strip()}")
    # `ls-files -v` tags: S skip-worktree, s both bits, any other lowercase letter assume-unchanged
    # (measured on git 2.34.1); `--no-skip-worktree --no-assume-unchanged` in one command clears
    # only the assume-unchanged bit, so each bit gets its own command.
    commands: list[str] = []
    hidden: list[str] = []
    for entry in sorted(e for e in listed.stdout.split("\0") if len(e) > 2):
        tag, path = entry[0], entry[2:]
        bits = [
            flag
            for flag, set_ in (
                ("--no-skip-worktree", tag in "Ss"),
                ("--no-assume-unchanged", tag.islower()),
            )
            if set_
        ]
        if bits:
            hidden.append(path)
            commands += [
                f"  git -C {shlex.quote(str(top))} update-index {flag} -- {shlex.quote(path)}"
                for flag in bits
            ]
    if hidden:
        raise ArchiveError(
            "git hides changes to these paths (skip-worktree or assume-unchanged), so the plan "
            f"cannot be trusted: {', '.join(hidden)}; clear each bit with these commands and run "
            "again:\n" + "\n".join(commands)
        )


def _rescan(top: Path, name: str) -> tuple[str, ...]:
    """The files on disk that still name ``name`` (after the changes were made)."""
    return _matches(top, name, frozenset(), {})


def plan_redact(archive: Path, name: str) -> RedactPlan:
    """What removing ``name`` would delete, rewrite, republish, unpublish and leave behind."""
    top = check_archive(archive)
    _refuse_hidden(top)
    key = _key(name)
    if not key:
        raise ArchiveError("--name is empty")
    if key == "owner":
        raise ArchiveError("the owner is not a reviewer that can be removed")
    delete: list[str] = []
    unpublish: list[str] = []
    rewrite: list[str] = []
    republish: list[str] = []
    rewritten: dict[str, str] = {}
    for path in sorted(top.glob("*/*/decisions/*.json")):
        rel = path.relative_to(top)
        if rel.parts[0].startswith("."):
            continue
        try:
            doc = json.loads(_read_text(path))
        except ValueError as exc:
            raise ArchiveError(f"{rel}: is not valid JSON: {exc}") from exc
        who, role = _reviewer(doc, str(rel))
        if role != "owner" and _key(who) == key:
            delete.append(rel.as_posix())
    for path in sorted(top.glob("*/*/pages/*.html")):
        rel = path.relative_to(top)
        if rel.parts[0].startswith("."):
            continue
        text = _read_text(path)
        try:
            data = page_data_from_text(text, str(rel))
        except ExportError as exc:
            raise ArchiveError(str(exc)) from exc
        who, role = _reviewer(data, str(rel))
        artifact = path.with_suffix(".artifact")
        if role == "owner":
            new = redacted_page(text, key)
            if new != text:
                rewrite.append(rel.as_posix())
                rewritten[rel.as_posix()] = new
                url = _url(artifact)
                if url:
                    republish.append(url)
        elif _key(who) == key:
            delete.append(rel.as_posix())
            if artifact.is_file():
                delete.append(artifact.relative_to(top).as_posix())
                url = _url(artifact)
                if url:
                    unpublish.append(url)
    gone = frozenset(delete)
    return RedactPlan(
        delete=tuple(sorted(delete)),
        rewrite=tuple(rewrite),
        republish=tuple(republish),
        unpublish=tuple(unpublish),
        elsewhere=_matches(top, name, gone, rewritten),
    )


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _readme_with_removal(text: str, today: date, deleted: int, owner_pages: int) -> str:
    lines = text.split("\n")
    heads = [i for i, line in enumerate(lines) if line == _HEADING]
    if len(heads) != 1:
        raise ArchiveError(
            f"{_README} must hold exactly one '{_HEADING}' line, it holds {len(heads)}"
        )
    start = heads[0] + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    entry = (
        f"- {today.isoformat()}: one reviewer removed: {_plural(deleted, 'file')} deleted, their "
        f"name and notes redacted from {_plural(owner_pages, 'owner page')}."
    )
    placeholder = [i for i in range(start, end) if lines[i].strip() == "None yet."]
    if placeholder:
        lines[placeholder[0]] = entry
    else:
        last = max((i for i in range(start, end) if lines[i].strip()), default=heads[0])
        lines.insert(last + 1, entry)
    return "\n".join(lines)


def _restore(top: Path, paths: list[str]) -> None:
    done = _git(top, "checkout", "HEAD", "--", *paths)
    if done.returncode != 0:
        raise GitError(f"git checkout HEAD failed to restore {paths}: {done.stderr.strip()}")


def apply_redact(archive: Path, name: str, plan: RedactPlan, today: date) -> str:
    """Carry ``plan`` out and commit it; the short sha.

    Refuses (:class:`ArchiveError`, nothing changed) an archive that is unclean or has no
    identity, a plan that is not what the archive now yields, a name still found elsewhere, a
    removal that touches nothing, a missing README or one without exactly one ``## Removals``
    line, a path git hides (skip-worktree or assume-unchanged), and a ``.git/redact-*.txt`` left
    by an earlier removal (finish its history rewrite first). A write that fails, a scan that
    still finds the name, or a commit that fails restores every touched path (``git checkout
    HEAD``, index included) and raises :class:`GitError`.
    """
    top, _ = open_archive(archive)
    require_identity(top)
    done = _git(top, "rev-parse", "--absolute-git-dir")
    if done.returncode != 0:
        raise GitError(f"git rev-parse failed: {done.stderr.strip()}")
    gitdir = Path(done.stdout.strip())
    leftover = [f.name for f in sorted(gitdir.glob("redact-*.txt"))]
    if leftover:
        raise ArchiveError(
            f"{gitdir} still holds {', '.join(leftover)} from an earlier removal: finish that "
            "history rewrite first (the printed procedure removes them at its end)"
        )
    if plan_redact(top, name) != plan:
        raise ArchiveError("the plan is stale: the archive changed since it was made")
    if plan.elsewhere:
        raise ArchiveError(
            "the name is still found in files redaction does not edit: "
            + ", ".join(plan.elsewhere)
            + "; check each: one that names this reviewer (written another way such as "
            '"Dr. X", or in the owner\'s own note) is yours to edit or delete, then commit and '
            'run again; one that names another reviewer whose name contains this one (such as "Dr '
            'X Smith") must not be changed here: this command cannot remove one without the other, '
            "so do that removal by hand"
        )
    if not plan.delete and not plan.rewrite:
        raise ArchiveError("no file names this reviewer: nothing to remove")
    key = _key(name)
    readme = _read_text(top / _README)
    new_readme = _readme_with_removal(readme, today, len(plan.delete), len(plan.rewrite)).encode(
        "utf-8"
    )
    pages = {rel: redacted_page(_read_text(top / rel), key).encode("utf-8") for rel in plan.rewrite}
    touched = [*plan.delete, *plan.rewrite, _README]
    pattern = name_pattern(name)
    try:
        for rel in plan.delete:
            (top / rel).unlink()
        for rel, content in pages.items():
            write_bytes_atomically(top / rel, content)
        write_bytes_atomically(top / _README, new_readme)
        left = _rescan(top, name)
    except (OSError, ArchiveError) as exc:
        _restore(top, touched)
        raise GitError(f"cannot change or rescan the archive: {exc}") from exc
    if left:
        _restore(top, touched)
        raise GitError(f"the name is still found after redaction in {', '.join(left)}; restored")
    try:
        sha = commit(
            top,
            [Path(p) for p in touched],
            MESSAGE,
            [*([None] * len(plan.delete)), *pages.values(), new_readme],
        )
    except (ArchiveError, GitError) as exc:
        _restore(top, touched)
        raise GitError(f"the commit failed, every touched path was restored: {exc}") from exc
    try:
        (gitdir / "redact-expressions.txt").write_text(
            f"regex:(?i){pattern.pattern}==>{REMOVED}\n", encoding="utf-8"
        )
        (gitdir / "redact-paths.txt").write_text(
            "".join(f"{p}\n" for p in [*plan.delete, *plan.rewrite]), encoding="utf-8"
        )
    except OSError as exc:
        raise GitError(
            f"committed {sha}, but cannot write the history-rewrite files: {exc}"
        ) from exc
    return sha


def procedure(archive: Path, name: str, plan: RedactPlan) -> list[str]:
    """The lines printed after a removal: the history rewrite and the copies to take down.

    The rewrite deletes the clinician's files and the rewritten owner pages from all of history
    (their original blobs hold the clinician's notes), then re-adds the redacted pages in one
    commit made with the checkout's own identity. Afterwards the checkout's reflog is expired,
    because it still holds the name. The copies outside the archive and GitHub's retention of
    force-pushed-over commits are named, not handled. The last lines say which claude.ai pages to
    republish (each rewritten owner page's ``.artifact`` URL, or that none is recorded) and which
    to unpublish (the clinician's own pages). Never run, so unmeasured as of 2026-11-25.
    """
    d = str(archive.resolve())
    rw = f"{d}-rewrite"
    pattern = shlex.quote(name_pattern(name).pattern)
    lines = ["History still holds the name and the notes. On the owner's word, in a fresh clone:"]
    if shutil.which("git-filter-repo") is None:
        lines.append("git filter-repo is not on this PATH: uv tool install git-filter-repo")
    lines += [
        f"  git clone --no-local {d} {rw}",
        f"  git -C {rw} filter-repo --replace-text {d}/.git/redact-expressions.txt "
        f"--replace-message {d}/.git/redact-expressions.txt --invert-paths "
        f"--paths-from-file {d}/.git/redact-paths.txt",
    ]
    if plan.rewrite:
        for rel in plan.rewrite:
            lines.append(f"  mkdir -p {rw}/{Path(rel).parent} && cp {d}/{rel} {rw}/{rel}")
        lines += [
            f"  git -C {rw} add -- {' '.join(plan.rewrite)}",
            f'  git -C {rw} -c user.name="$(git -C {d} config --get user.name)" '
            f'-c user.email="$(git -C {d} config --get user.email)" '
            "commit -m 'file the redacted owner pages again'",
        ]
    lines += [
        f"  git -C {rw} log --all -p | grep -ciP {pattern}      must print 0",
        f"  git -C {rw} push --force <origin url> main",
        f"  git -C {d} fetch origin && git -C {d} reset --hard origin/main "
        f"&& rm {d}/.git/redact-*.txt",
        f"  git -C {d} reflog expire --expire=now --all && git -C {d} gc --prune=now",
        f"  git -C {d} log --all --reflog -p | grep -ciP {pattern}      must print 0",
        "Delete by hand the copies outside the archive, on this machine: the clinician's page "
        f"(for example the atlas's local/literature/runs/<SYMBOL>/<run>/review-{slug(name)}.html) "
        "and the file they sent back, wherever it was saved; the owner's own review pages built "
        "while their verdicts were in the archive (the run directories' review*.html, e.g. "
        "local/literature/runs/<SYMBOL>/<run>/review.html); and any store-export directories "
        "made from those pages.",
        "GitHub may keep the force-pushed-over commits reachable by id until GitHub Support "
        "removes them (unmeasured).",
    ]
    for rel in plan.rewrite:
        url = _url((Path(d) / rel).with_suffix(".artifact"))
        if url:
            lines.append(f"republish redacted: {url} from {rel}")
        else:
            lines.append(f"rewritten, no claude.ai copy recorded: {rel}")
    lines.extend(f"unpublish: {url}" for url in plan.unpublish)
    return lines
