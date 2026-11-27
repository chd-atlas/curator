"""``chd-curator read-requests``: a run's batch and judge manifests as ``ask`` request rows.

``ask --validate check-batch`` takes rows ``{key, gene, batch, mode, inputs}``; this writes them
from a run folder, so no pipeline has to gather texts itself. ``inputs.manifest`` is the
manifest file's text; ``inputs.texts`` is every file the manifest names, each as
``--- file: <path as the manifest names it> ---`` and the file's text (the marker the role
files tell the model to look for), in the order the manifest names them, once each.
``inputs.registries`` is ``REGISTRIES`` (the cohort and phenotype registries the cited rules
name, which a model with no tools cannot open) under the same marker, each by its path relative
to the root. A judge row also has ``inputs.owner_notes``: with ``--decisions`` (a format 1
decisions file, as ``review-decisions`` writes and the atlas's ``apply-review`` reads), one line
``- <id>: <note>`` per ``fix`` decision on a finding the judge manifest's ``## `` headings name,
in the file's order; otherwise, and when there is none, the literal ``none``. The notes are part
of the input, so its digest (and the cache) moves with them (R-SWA-45).

A manifest names a file on three kinds of line, outside its fenced blocks: ``- text path: P`` and
``- abstract path: P`` (``batch.py`` and ``compare.py``) and ``- supplement: P`` (``batch.py``
adds `` (N bytes)``, ``compare.py`` does not). ``extract`` and ``blind-read`` read
``batches/batch-NN.md`` (one row per batch of ``batches.json``, in its order, the same rows for
both roles); ``judge`` reads each ``judging/judge-NN.md`` that exists, sorted. ``gene`` is the
symbol as typed, which names the run's directory (``ask`` finds the run by it); ``mode`` is the
run's, with a ``reread`` run's rows ``new``, as ``ask`` expects.

A file that is missing, outside the root, not a ``.txt`` text or not UTF-8 is a
:class:`RequestsError` naming it, and nothing is written. "Outside" is lexical (R-SWA-54): the
named path, joined to the root and normalised without following links, must lie under the root;
the text is then read through any link, since in a scratch root every text the atlas already held
is a link into the atlas.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Final, Literal

from chd_curator.fs import write_bytes_atomically
from chd_curator.run import BatchPlan, RunPaths, load_batches

_NAMED: Final = re.compile(r"- (?:text path|abstract path|supplement): (.+)")
_SIZE: Final = re.compile(r"(.*) \(\d+ bytes\)")
_JUDGE: Final = re.compile(r"judge-([0-9]{2,})\.md")
_PATHISH: Final = re.compile(r"- ([^:]*(?:path|supplement)[^:]*):")
_ITEM: Final = re.compile(r"## (\S+) \((?:blind pass (\S+)|first pass only|blind pass only)\)")
REGISTRIES: Final = ("curation/cohorts.yaml", "curation/phenotypes.yaml")
NO_NOTES: Final = "none"
Role = Literal["extract", "blind-read", "judge"]


class RequestsError(Exception):
    """A manifest that cannot be turned into a request; the command exits 1, writing nothing."""


class RequestsUsageError(RequestsError):
    """A bad argument (a decisions file that cannot be used); the command exits 2."""


def named_files(manifest: str) -> list[str]:
    """The paths a manifest names, in order, each once (fenced blocks are not read)."""
    found: list[str] = []
    fenced = False
    for line in manifest.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else _NAMED.fullmatch(line)
        if match is None:
            continue
        path = match.group(1)
        sized = _SIZE.fullmatch(path)
        path = sized.group(1) if sized else path
        if path not in found:
            found.append(path)
    return found


def unrecognised(manifest: str) -> list[str]:
    """Lines outside fenced blocks whose key (``- <key>:``) mentions a path or a supplement but
    that ``named_files`` does not read: a form of naming a file this command does not know."""
    fenced = False
    odd: list[str] = []
    for line in manifest.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and _PATHISH.match(line) and not _NAMED.fullmatch(line):
            odd.append(line)
    return odd


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RequestsError(f"{what}: cannot be read as UTF-8 text: {exc}") from None


def _inside(base: str, name: str) -> Path | None:
    """`name` joined to `base` and normalised without following a link, when that lies under
    `base`; else None. An absolute `name` stands for itself."""
    joined = os.path.normpath(os.path.join(base, name))
    return Path(joined) if os.path.commonpath([base, joined]) == base else None


def _texts(root: Path, manifest: str, where: Path) -> str:
    base = os.path.normpath(os.path.abspath(root))
    blocks: list[str] = []
    odd = unrecognised(manifest)
    if odd:
        raise RequestsError(f"{where}: names a file in a form this command does not read: {odd[0]}")
    names = named_files(manifest)
    if not names:
        raise RequestsError(f"{where}: names no file, so a reader would be sent no text")
    for name in names:
        path = _inside(base, name)
        if path is None:
            raise RequestsError(f"{where}: names {name}, which is outside the root {root}")
        if path.suffix != ".txt":
            raise RequestsError(f"{where}: names {name}, which is not a .txt text")
        if not path.is_file():
            raise RequestsError(f"{where}: names {name}, which does not exist")
        text = _read(path, name)
        blocks.append(f"--- file: {name} ---\n{text}" + ("" if text.endswith("\n") else "\n"))
    return "\n".join(blocks)


def registries(root: Path) -> str:
    """The text of each of `REGISTRIES` under its ``--- file: <path> ---`` marker."""
    blocks: list[str] = []
    for rel in REGISTRIES:
        path = root / rel
        if not path.is_file():
            raise RequestsError(f"{rel}: the registry the rules cite is missing from {root}")
        text = _read(path, rel)
        blocks.append(f"--- file: {rel} ---\n{text}" + ("" if text.endswith("\n") else "\n"))
    return "\n".join(blocks)


def covered_ids(manifest: str) -> set[str]:
    """The finding ids a judge manifest's ``## `` item headings name (a blind pass's too)."""
    found: set[str] = set()
    for line in manifest.splitlines():
        match = _ITEM.fullmatch(line)
        if match is not None:
            found.update(i for i in match.groups() if i)
    return found


def fix_notes(path: Path, symbol: str) -> list[tuple[str, str]]:
    """``(finding id, note)`` for each ``fix`` decision of a format 1 decisions file for
    `symbol`, in the file's order. Raises :class:`RequestsUsageError` naming what is wrong."""
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestsUsageError(f"--decisions {path}: cannot be read as JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != 1:
        raise RequestsUsageError(f"--decisions {path}: not a format 1 decisions file")
    if data.get("symbol") != symbol:
        raise RequestsUsageError(
            f"--decisions {path}: the decisions are for {data.get('symbol')!r}, not {symbol!r}"
        )
    rows = data.get("findings")
    if not isinstance(rows, list) or not all(
        isinstance(r, dict) and isinstance(r.get("id"), str) for r in rows
    ):
        raise RequestsUsageError(f"--decisions {path}: `findings` is not a list of decisions")
    notes: list[tuple[str, str]] = []
    for r in rows:
        if r.get("decision") == "fix":
            note = r.get("note")
            if not isinstance(note, str) or not note.strip():
                raise RequestsUsageError(f"--decisions {path}: {r['id']}: a fix with no note")
            notes.append((r["id"], " ".join(note.split())))
    return notes


def _row(
    root: Path, gene: str, plan: BatchPlan, batch: str, path: Path, shared: str
) -> dict[str, Any]:
    manifest = _read(path, str(path))
    inputs = {
        "manifest": manifest,
        "texts": _texts(root, manifest, path),
        "registries": shared,
    }
    return {
        "key": batch,
        "gene": gene,
        "batch": batch,
        "mode": "new" if plan.mode == "reread" else plan.mode,
        "inputs": inputs,
    }


def read_requests(
    root: Path,
    symbol: str,
    run_id: str,
    role: Role,
    out: Path,
    notes: list[tuple[str, str]] | None = None,
) -> int:
    """Write the request rows of ``role`` for the run to ``out``; return the row count.
    `notes` (the judge only) are `fix_notes`; None gives every judge row ``none``."""
    if notes is not None and role != "judge":
        raise RequestsUsageError(f"--decisions is for the judge's rows, not {role}'s")
    run = RunPaths(root, symbol, run_id)
    plan = load_batches(run)
    shared = registries(root)
    rows: list[dict[str, Any]] = []
    if role == "judge":
        known = {b.id for b in plan.batches}
        for path in sorted(run.judging.glob("judge-*.md")) if run.judging.is_dir() else []:
            found = _JUDGE.fullmatch(path.name)
            if found is None or found.group(1) not in known:
                raise RequestsError(f"{path}: is not the judge manifest of a batch in batches.json")
            row = _row(root, symbol, plan, found.group(1), path, shared)
            covered = covered_ids(row["inputs"]["manifest"])
            lines = [f"- {fid}: {note}" for fid, note in notes or [] if fid in covered]
            row["inputs"]["owner_notes"] = "\n".join(lines) or NO_NOTES
            rows.append(row)
    else:
        for b in plan.batches:
            path = run.batches_dir / f"batch-{b.id}.md"
            if not path.is_file():
                raise RequestsError(f"{path}: the batch has no manifest; run `chd-curator batch`")
            rows.append(_row(root, symbol, plan, b.id, path, shared))
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    write_bytes_atomically(out, payload.encode("utf-8"))
    return len(rows)
