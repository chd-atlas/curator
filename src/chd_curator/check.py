"""``chd-curator check-batch``: an agent's batch, applied to a scratch copy and validated.

An extract, verify or judge agent runs this on its own batch file before handing it back. The
batch is applied the way ``merge`` or ``assemble`` would apply it, but to a copy of the atlas root
in a temporary directory, and ``chd-atlas validate`` is run on that copy. Nothing under the real
root is written.

* An extract or verify pass of a ``new`` or ``reread`` run is appended with ``append_numbered``,
  so its findings are numbered from the ``LIT`` counter (which is raised in the copy: ``ID002`` is
  not hidden). No Publication is added and no triage entry changes.
* A judge pass replaces, by id, the findings of the file it names, and numbers the ones with a
  local id ``CHDA:LIT:07...``. An id an earlier ``assemble.json`` mapped is a replacement of the
  finding it was numbered as.
* In a ``backfill`` run an extract or verify pass goes through merge's backfill (the same
  functions, so a field a finding already holds is refused), and a judge pass replaces findings
  by id.

Errors expected before ``merge`` and ``normalise`` are not reported: ``LIT003`` (a paper that
another batch covers), ``LIT011`` (a Publication ``merge`` adds), ``LIT022`` for a ``mane``
variant lacking ``hgvs_c``, ``grch38`` or ``normalisation``, and ``LIT025`` for a paper's ``p.``
with no ``hgvs_p``. Anything else is. The validator's answer is believed only when its exit code
is 0 or 1, its last line is the summary, the summary's error count equals the ``ERROR`` lines and
agrees with the exit code, and nothing holds a traceback; otherwise its raw output is the report
and the exit code is 1, so a crashed validator is never "OK".

Exit codes (see the CLI): 0 only when nothing unexpected was reported, 1 for an unexpected error
or an answer that cannot be believed, 2 for a batch file that does not load.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from chd_curator.assemble import accept_judged, apply_judged, judge_findings, read_state
from chd_curator.atlas import AtlasError, run_validate
from chd_curator.diff import DiffError, parse_findings_file
from chd_curator.merge import (
    PREFIX,
    Curated,
    append_numbered,
    apply_backfill,
    backfill_fields,
    check_backfill_declaration,
    check_new_findings,
    paper_years,
    read_curated,
)
from chd_curator.records import read_counter, with_counter
from chd_curator.run import (
    Batch,
    BatchPlan,
    RunPaths,
    load_batches,
)

PASSES: Final = ("extract", "verify", "judge")
_NOT_COPIED: Final = frozenset({"curation", "local", ".git", ".venv"})
_SUMMARY: Final = re.compile(r"([0-9]+) error\(s\), ([0-9]+) warning\(s\)")
_ERROR: Final = re.compile(r"ERROR \[([A-Z]+[0-9]+)\] (.*)")
_HIDDEN_CODES: Final = frozenset({"LIT003", "LIT011"})
_LIT022_ENDINGS: Final = (
    "a mane variant needs hgvs_c",
    "a mane variant needs grch38",
    "a mane variant needs normalisation",
)
_LIT025_ENDING: Final = "but the variant has no hgvs_p"
OK_LINE: Final = "OK: only the errors expected before merge and normalise"


class CheckError(Exception):
    """A batch file that does not load; the command exits 2."""


@dataclass(frozen=True)
class Checked:
    """What the command prints, line by line, and its exit code (0 or 1)."""

    code: int
    lines: tuple[str, ...]


def _expected(error_code: str, message: str) -> bool:
    return (
        error_code in _HIDDEN_CODES
        or (error_code == "LIT022" and message.endswith(_LIT022_ENDINGS))
        or (error_code == "LIT025" and message.endswith(_LIT025_ENDING))
    )


def report(exit_code: int, stdout: str, stderr: str) -> Checked:
    """The report on one run of ``chd-atlas validate``; see the module docstring."""
    lines = [line.rstrip() for line in stdout.splitlines() if line.strip()]
    raw = [line.rstrip() for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
    summary = _SUMMARY.fullmatch(lines[-1]) if lines else None
    errors = [line for line in lines if line.startswith("ERROR [")]
    problem: str | None = None
    if "Traceback (most recent call last)" in stdout + stderr:
        problem = "its output holds a traceback"
    elif exit_code not in (0, 1):
        problem = f"it exited {exit_code}"
    elif summary is None:
        problem = "its last line is not the summary"
    elif len(errors) != int(summary.group(1)):
        problem = f"the summary counts {summary.group(1)} errors and {len(errors)} are listed"
    elif (exit_code == 0) != (int(summary.group(1)) == 0):
        problem = f"it exited {exit_code} over a summary of {summary.group(1)} errors"
    if problem is not None or summary is None:
        return Checked(
            1, ("UNEXPECTED ERRORS:", f"the validator's output cannot be trusted: {problem}", *raw)
        )
    unexpected = []
    for line in errors:
        found = _ERROR.fullmatch(line)
        if found is None or not _expected(found.group(1), found.group(2)):
            unexpected.append(line)
    if unexpected:
        return Checked(1, (lines[-1], "UNEXPECTED ERRORS:", *unexpected))
    return Checked(0, (lines[-1], OK_LINE))


def _pass_file(run: RunPaths, batch: Batch, which: str) -> Path:
    if which == "judge":
        return run.judging / f"judge-{batch.id}.findings.yaml"
    folder = run.extract if which == "extract" else run.verify
    return folder / f"batch-{batch.id}.findings.yaml"


def _read_findings(path: Path, schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        return parse_findings_file(path.read_bytes(), schema)
    except DiffError as exc:
        raise CheckError(f"{path}: {exc}") from None


_LOCAL_ID: Final = re.compile(r"CHDA:LIT:0[789]([0-9]{2})[0-9]{3}")


def _defer_other_batches(
    findings: Sequence[Mapping[str, Any]], plan: BatchPlan, batch: Batch
) -> tuple[list[dict[str, Any]], list[str]]:
    """`findings` without the ``shares_probands_with`` entries that name a local id of another
    batch of `plan` (merge numbers every batch together and resolves them; this check applies
    one batch alone), and a ``NOT CHECKED`` line for each one dropped."""
    others = {b.id for b in plan.batches} - {batch.id}
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for finding in findings:
        row = dict(finding)
        shared = row.get("shares_probands_with")
        if isinstance(shared, list):
            kept = []
            for ref in shared:
                found = _LOCAL_ID.fullmatch(ref) if isinstance(ref, str) else None
                if found is not None and found.group(1) in others:
                    notes.append(
                        f"NOT CHECKED: {row['id']} shares_probands_with {ref} "
                        f"(batch {found.group(1)}; merge resolves it)"
                    )
                else:
                    kept.append(ref)
            row["shares_probands_with"] = kept
        out.append(row)
    return out, notes


def _applied(
    root: Path, run: RunPaths, plan: BatchPlan, batch: Batch, which: str, approved: str
) -> tuple[Curated, bytes, bytes | None, list[str]]:
    """The literature file after the batch's pass, the registry when its counter moved, and the
    ``NOT CHECKED`` lines."""
    path = _pass_file(run, batch, which)
    if not path.is_file():
        raise CheckError(f"{path}: the batch has no such file")
    if which == "judge":
        state = read_state(root, run, approved)
        c = state.curated
        judged, notes = _defer_other_batches(
            judge_findings(run, plan, state.schema, only=batch.id), plan, batch
        )
        if plan.mode == "backfill":

            def in_scope(fid: str) -> bool:
                return fid in batch.findings

        else:

            def in_scope(fid: str) -> bool:
                return str(c.findings.get(fid, {}).get("publication")) in batch.papers

        accepted = accept_judged(state, judged, in_scope)
        applied = apply_judged(state, run, accepted)
        counter = read_counter(c.reg, PREFIX)
        literature, new_counter = applied.literature, applied.new_counter
    else:
        c = read_curated(root, run, approved, refuse_merged=False)
        schema: Any = c.lit_validator.schema
        notes = []
        counter = new_counter = read_counter(c.reg, PREFIX)
        if plan.mode == "backfill":
            declared = check_backfill_declaration(plan, schema)
            literature = apply_backfill(c, backfill_fields(path, batch, c, declared))
        else:
            found, notes = _defer_other_batches(_read_findings(path, schema), plan, batch)
            check_new_findings(found, batch, c, {})
            years = paper_years(c, run, sorted({str(f["publication"]) for f in found}))
            literature, _, new_counter = append_numbered(c.lit, found, years, counter)
    registry = with_counter(c.reg, PREFIX, new_counter) if new_counter != counter else None
    return c, literature, registry, notes


def _build_scratch(
    root: Path, scratch: Path, c: Curated, literature: bytes, reg: bytes | None
) -> None:
    """A copy of ``curation/`` holding the new files, and a symlink for every other entry of
    `root` but ``local``, ``.git`` and ``.venv``."""
    shutil.copytree(root / "curation", scratch / "curation")
    for entry in sorted(root.iterdir()):
        if entry.name not in _NOT_COPIED:
            (scratch / entry.name).symlink_to(entry.resolve())
    (scratch / c.lit_path.relative_to(root)).write_bytes(literature)
    if reg is not None:
        (scratch / c.reg_path.relative_to(root)).write_bytes(reg)


def check_batch(
    root: Path, symbol: str, approved: str, run_id: str, batch_id: str, which: str, atlas_cli: Path
) -> Checked:
    """Apply one batch's pass to a scratch copy of `root`, validate the copy, remove it.

    `symbol` names the run directory and `approved` the literature file. Raises
    :class:`CheckError` (or ``MergeError``, ``AssembleError``, ``RunFormatError``,
    ``RecordError``) for a batch that does not load, ``OSError`` for a file that cannot be read.
    The scratch directory is removed whichever way this returns or raises."""
    if which not in PASSES:
        raise CheckError(f"--pass {which!r} is not one of {', '.join(PASSES)}")
    run = RunPaths(root, symbol, run_id)
    plan = load_batches(run)
    batch = next((b for b in plan.batches if b.id == batch_id), None)
    if batch is None:
        raise CheckError(f"batch {batch_id} is not in batches.json ({run.batches_json})")
    with tempfile.TemporaryDirectory(prefix="chd-curator-check-") as scratch:
        c, literature, registry, notes = _applied(root, run, plan, batch, which, approved)
        _build_scratch(root, Path(scratch), c, literature, registry)
        try:
            code, stdout, stderr = run_validate(Path(scratch), atlas_cli)
        except AtlasError as exc:
            return Checked(1, (*notes, "UNEXPECTED ERRORS:", str(exc)))
    done = report(code, stdout, stderr)
    return Checked(done.code, (*notes, *done.lines))
