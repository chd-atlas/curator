"""``package`` and ``integrate``: a run's proposal, and bringing it into the atlas.

A package is a directory: ``proposal/`` (the three mutable files as they stood in the work root,
an absent one staying absent), ``run/`` (the run folder), ``texts/`` (the texts the run lists,
laid out as under ``local/literature/texts/`` plus ``supplied/`` for supplied PDFs),
``review.html`` when the run has one, and ``package.json`` written last.

``package.json`` is ``{format: 1, symbol, run_id, base, atlas_commit, models, review}``: ``base``
is the ``digests`` object ``base`` printed (the three mutable files' sha256, null when absent, and
each text directory's ``scratch.tree_digest`` keyed ``local/literature/texts/<name>``),
``atlas_commit`` the full HEAD commit it printed (null outside a git checkout), ``models`` one
``{role, model_key, verdict, rules_digest}`` per distinct (role, model_key, rules_digest) of the
``--answers`` rows (``verdict`` the register's ``pass``, ``fail`` or ``reference``,
``unqualified`` with no entry, ``test-backend`` for a replay or fake backend; [] without
``--answers``) and ``review`` is ``"review.html"`` when the run folder holds a review page, else
null.

``integrate`` writes nothing until every check has passed: (1) each of the three files in the
checkout must still have the digest the run started from (every differing file is named), and
each file the checkout holds must have a proposal; (2) the run folder must not exist; (3) each
text directory of the package is checked against the base (R-SWA-47): one the checkout holds
with its base digest is replaced by the package's (a run that re-fetched it), one changed since
the base, one absent at the base and present now, and one present at the base and gone now are
refused; one absent from both is copied; (4) a supplied PDF already present must be
byte-identical (it is then skipped). Only then does it copy.
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
from chd_curator.register import Entry
from chd_curator.roles import ROLES
from chd_curator.run import RunPaths
from chd_curator.scratch import (
    ScratchError,
    base_digests,
    copy_file,
    listed_texts,
    mutable_paths,
    tree_digest,
)

PACKAGE_FORMAT: Final = 1
_TEXTS: Final = "local/literature/texts/"
_SUPPLIED: Final = "local/literature/supplied/"
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_TEXT_KEY: Final = re.compile(re.escape(_TEXTS) + r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_TEST_KINDS: Final = ("replay", "fake")


def models_from_answers(
    answers: Sequence[tuple[str, Path]], register: Sequence[Entry]
) -> list[dict[str, str]]:
    """One ``{role, model_key, verdict, rules_digest}`` per distinct (role, model_key,
    rules_digest) of the answer rows (error rows carry no model and are skipped), sorted. The
    verdict is the register entry's for (role, model_key); ``unqualified`` when there is none;
    ``test-backend`` for a replay or fake backend. Raises ``ScratchError`` (exit 2) for an
    unknown role or an unreadable answers file."""
    seen: set[tuple[str, str, str]] = set()
    for role, path in answers:
        if role not in ROLES:
            raise ScratchError(f"--answers {role}={path}: {role!r} is not a role", 2)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            rows = [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError) as exc:
            raise ScratchError(f"--answers {role}={path}: cannot be read ({exc})", 2) from None
        for row in rows:
            if not isinstance(row, dict) or "error" in row:
                continue
            key, rules = row.get("model_key"), row.get("rules_digest")
            if not isinstance(key, str) or not isinstance(rules, str):
                raise ScratchError(f"--answers {role}={path}: a row lacks model_key or rules", 2)
            seen.add((role, key, rules))
    verdicts = {(e.role, e.model_key): e.verdict for e in register}
    return [
        {
            "role": role,
            "model_key": key,
            "verdict": (
                "test-backend"
                if key.split(":", 1)[0] in _TEST_KINDS
                else verdicts.get((role, key), "unqualified")
            ),
            "rules_digest": rules,
        }
        for role, key, rules in sorted(seen)
    ]


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
    files = set(mutable_paths(loaded["symbol"]))
    if not isinstance(base, dict) or not files <= set(base):
        raise ScratchError(f"{path}: base does not name the three mutable files", 2)
    for key, value in base.items():
        if key not in files and not (
            _TEXT_KEY.fullmatch(key) and isinstance(value, str) and _HEX64.fullmatch(value)
        ):
            raise ScratchError(f"{path}: base names {key!r}, not a mutable file or text folder", 2)
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
    check runs before the first write, and a refusal names what it found. The copy itself is
    not atomic across files: an I/O error part-way leaves a partial run folder (and possibly
    texts or curated files), and the message says so and names the run folder."""
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
    not_files = [
        f"{rel}: exists and is not a file"
        for rel in (*mutable_paths(symbol), *(t for _, t in _texts_in(package)))
        if os.path.lexists(atlas / rel) and not (atlas / rel).is_file()
    ]
    if not_files:
        raise ScratchError("refused, nothing copied:\n" + "\n".join(not_files), 1)
    run_target = RunPaths(atlas, symbol, run_id).dir
    if os.path.lexists(run_target):
        raise ScratchError(f"refused, nothing copied: {run_target} already exists", 1)
    texts = _texts_in(package)
    new, replaced, unchanged, differing = _plan_texts(package, atlas, meta["base"], texts)
    if differing:
        raise ScratchError("refused, nothing copied:\n" + "\n".join(differing), 1)
    run_files = [f for f in sorted((package / "run").rglob("*")) if f.is_file()]
    copied = 0
    try:
        for folder in replaced:
            _replace_folder(package / "texts" / folder.removeprefix(_TEXTS), atlas / folder)
        for source, rel in new:
            copy_file(source, atlas / rel)
        for file in run_files:
            copy_file(file, run_target / file.relative_to(package / "run"))
        for rel in mutable_paths(symbol):
            if (proposal / rel).is_file():
                copy_file(proposal / rel, atlas / rel)
                copied += 1
    except OSError as exc:
        raise ScratchError(
            f"{exc}; the copy is not atomic, so the atlas holds a partial integration "
            f"(texts, run folder {run_target}, {copied} curated file(s)); inspect it with git "
            "status and remove it before trying again",
            1,
        ) from None
    print(
        f"integrated {symbol}/{run_id}: {copied} curated file(s), {len(run_files)} run file(s), "
        f"{len(new)} new text file(s), {len(replaced)} text folder(s) replaced "
        f"({unchanged} already present)"
    )
    return 0


def _plan_texts(
    package: Path, atlas: Path, base: Mapping[str, Any], texts: list[tuple[Path, str]]
) -> tuple[list[tuple[Path, str]], list[str], int, list[str]]:
    """``(new files, text folders to replace, files already present, refusals)``.

    A text folder (``local/literature/texts/<name>``) is judged whole against its base digest;
    a supplied PDF file by file, as before (R-SWA-21)."""
    new: list[tuple[Path, str]] = []
    replaced: list[str] = []
    unchanged = 0
    refused: list[str] = []
    folders: dict[str, list[tuple[Path, str]]] = {}
    for source, rel in texts:
        if rel.startswith(_TEXTS):
            folder = _TEXTS + rel.removeprefix(_TEXTS).split("/", 1)[0]
            folders.setdefault(folder, []).append((source, rel))
        elif not (atlas / rel).is_file():
            new.append((source, rel))
        elif (atlas / rel).read_bytes() != source.read_bytes():
            refused.append(f"{rel}: already present with different bytes")
        else:
            unchanged += 1
    for folder, files in sorted(folders.items()):
        target, at_base = atlas / folder, base.get(folder)
        if not target.is_dir():
            if os.path.lexists(target):
                refused.append(f"{folder}: exists and is not a directory")
            elif at_base is not None:
                refused.append(f"{folder}: in the atlas when the run started, and gone now")
            else:
                new += files
            continue
        now = tree_digest(target)
        if at_base is None:
            refused.append(f"{folder}: not in the atlas when the run started, and present now")
        elif now != at_base:
            refused.append(f"{folder}: changed in the atlas since the run started")
        elif tree_digest(package / "texts" / folder.removeprefix(_TEXTS)) == now:
            unchanged += len(files)
        else:
            replaced.append(folder)
    return new, replaced, unchanged, refused


def _replace_folder(source: Path, target: Path) -> None:
    """Put a copy of `source` where `target` is: staged beside it, then swapped in."""
    stage = target.with_name(f".{target.name}.integrate")
    shutil.rmtree(stage, ignore_errors=True)
    for file in sorted(source.rglob("*")):
        if file.is_file():
            copy_file(file, stage / file.relative_to(source))
    old = target.with_name(f".{target.name}.replaced")
    shutil.rmtree(old, ignore_errors=True)
    os.rename(target, old)
    os.rename(stage, target)
    shutil.rmtree(old)
