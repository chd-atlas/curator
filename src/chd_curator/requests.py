"""``chd-curator read-requests``: a run's batch and judge manifests as ``ask`` request rows.

``ask --validate check-batch`` takes rows ``{key, gene, batch, mode, inputs}``; this writes them
from a run folder, so no pipeline has to gather texts itself. ``inputs.manifest`` is the
manifest file's text; ``inputs.texts`` is every file the manifest names, each as
``--- file: <path as the manifest names it> ---`` and the file's text (the marker the role
files tell the model to look for), in the order the manifest names them, once each.

A manifest names a file on three kinds of line, outside its fenced blocks: ``- text path: P`` and
``- abstract path: P`` (``batch.py`` and ``compare.py``) and ``- supplement: P`` (``batch.py``
adds `` (N bytes)``, ``compare.py`` does not). ``extract`` and ``blind-read`` read
``batches/batch-NN.md`` (one row per batch of ``batches.json``, in its order, the same rows for
both roles); ``judge`` reads each ``judging/judge-NN.md`` that exists, sorted. ``gene`` is the
symbol as typed, which names the run's directory (``ask`` finds the run by it); ``mode`` is the
run's, with a ``reread`` run's rows ``new``, as ``ask`` expects.

A file that is missing, outside the root, not a ``.txt`` text or not UTF-8 is a
:class:`RequestsError` naming it, and nothing is written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final, Literal

from chd_curator.fs import write_bytes_atomically
from chd_curator.run import BatchPlan, RunPaths, load_batches

_NAMED: Final = re.compile(r"- (?:text path|abstract path|supplement): (.+)")
_SIZE: Final = re.compile(r"(.*) \(\d+ bytes\)")
_JUDGE: Final = re.compile(r"judge-([0-9]{2,})\.md")
Role = Literal["extract", "blind-read", "judge"]


class RequestsError(Exception):
    """A manifest that cannot be turned into a request; the command exits 1, writing nothing."""


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


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RequestsError(f"{what}: cannot be read as UTF-8 text: {exc}") from None


def _texts(root: Path, manifest: str, where: Path) -> str:
    base = root.resolve()
    blocks: list[str] = []
    for name in named_files(manifest):
        path = (base / name).resolve()
        if not path.is_relative_to(base):
            raise RequestsError(f"{where}: names {name}, which is outside the root {root}")
        if path.suffix != ".txt":
            raise RequestsError(f"{where}: names {name}, which is not a .txt text")
        if not path.is_file():
            raise RequestsError(f"{where}: names {name}, which does not exist")
        text = _read(path, name)
        blocks.append(f"--- file: {name} ---\n{text}" + ("" if text.endswith("\n") else "\n"))
    return "\n".join(blocks)


def _row(root: Path, gene: str, plan: BatchPlan, batch: str, path: Path) -> dict[str, object]:
    manifest = _read(path, str(path))
    return {
        "key": batch,
        "gene": gene,
        "batch": batch,
        "mode": "new" if plan.mode == "reread" else plan.mode,
        "inputs": {"manifest": manifest, "texts": _texts(root, manifest, path)},
    }


def read_requests(root: Path, symbol: str, run_id: str, role: Role, out: Path) -> int:
    """Write the request rows of ``role`` for the run to ``out``; return the row count."""
    run = RunPaths(root, symbol, run_id)
    plan = load_batches(run)
    rows: list[dict[str, object]] = []
    if role == "judge":
        known = {b.id for b in plan.batches}
        for path in sorted(run.judging.glob("judge-*.md")) if run.judging.is_dir() else []:
            found = _JUDGE.fullmatch(path.name)
            if found is None or found.group(1) not in known:
                raise RequestsError(f"{path}: is not the judge manifest of a batch in batches.json")
            rows.append(_row(root, symbol, plan, found.group(1), path))
    else:
        for b in plan.batches:
            path = run.batches_dir / f"batch-{b.id}.md"
            if not path.is_file():
                raise RequestsError(f"{path}: the batch has no manifest; run `chd-curator batch`")
            rows.append(_row(root, symbol, plan, b.id, path))
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    write_bytes_atomically(out, payload.encode("utf-8"))
    return len(rows)
