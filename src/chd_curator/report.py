"""``chd-curator report``: what a run cost, on which tier, and how each finding was settled.

``write_report`` reads four things and writes ``report.json`` (keys sorted) and ``report.md``,
each atomically.

**Answers** (``--answers <role>=<path>``, one or more per role; rows of ``chd-curator ask``).
Per role, summed over its files: ``answers`` (rows), ``cached`` (rows with ``parsed`` and
``attempts`` 0, answered from the cache), ``failed`` (rows with ``error``), ``seconds`` (the
rows' ``seconds``, error rows included) and ``tokens``. A row's tokens are
``backends.base.token_count`` of its ``usage``: ``total_tokens`` when the backend reported one
(openai_compat also reports prompt and completion counts, which are parts of it); otherwise the
sum of every count (anthropic and claude_code report input, output and cache counts, which are
disjoint). Tier: ``local`` for ``triage-screen``, ``capable`` for
every other role. ``tokens.local`` is the ``local`` tier's tokens, and the summary's percentage
is local over all, rounded half up.

**Trace** (``--trace``, a Nextflow trace file: tab-separated with a header). Columns parsed:
``process`` (matched to a role by its last ``:`` segment, upper-cased with ``-`` as ``_``, so
``BLIND_READ`` is ``blind-read``), ``cpus`` (an integer) and ``realtime`` (Nextflow's duration
text, ``1h 30m``, ``36s``, ``350ms``, ``1d 2h``, or a bare integer of milliseconds as the raw
trace writes it); ``status`` is read when present, and a ``CACHED`` task spent nothing and is
skipped; a ``realtime`` of ``-`` on any other task refuses the report (exit 2, naming the line). A
missing ``process``, ``cpus`` or ``realtime`` column, or a realtime or cpus that does not
parse, is refused (exit 2) naming it. ``core_hours`` of a role is the sum of
``cpus x realtime`` in hours over its tasks; a process no role carries is listed in
``trace_unmatched_processes`` and counted nowhere.

**Findings.** Each count names the file and field that decides it, in the run folder
(``<root>/local/literature/runs/<SYMBOL>/<run id>/``):

* ``agreed``: the ids in ``assemble.json``'s ``verified``, the findings ``assemble`` marked
  ``verified_by`` because two blind readers agreed (``agreed`` or ``span_only`` in
  ``comparison.json``).
* ``judged``: the ids that are keys of ``assemble.json``'s ``judged``, the findings a judge's
  finding replaced or added.
* ``corrected_by_owner``: the findings whose latest ``decision`` is ``fix`` in the ``findings``
  of the ``decisions/*.json`` files (``review-decisions`` output). Files are read in name order,
  so for a finding in two files the last file wins. 0 when there is no ``decisions/``. Only ids
  among the run's findings (``agreed`` or ``judged``) count; any decision id outside them is
  listed in ``decisions_outside_run`` and named in a warning. With no ``assemble.json`` nothing
  can be matched: none counts, and the ids are listed in ``decisions_unmatched`` instead.
* ``normalised``: the distinct findings with at least one entry of ``outcome`` ``placed`` in
  ``normalise.json``'s ``variants``, that is, whose variant code placed. 0 when there is no
  such file.
* ``total``: the findings in ``agreed`` or ``judged`` (their union). The other three counts are
  not parts of it: a finding is agreed or judged, and may also be corrected or normalised.

With no ``assemble.json`` nothing is settled yet: the counts are 0 and a warning says so.

**Tokens of failed attempts are counted.** An ``error`` row carries the ``usage`` and
``seconds`` its attempts spent, and they count in every total like an answer's.

**Notes.** For each role and ``model_key`` with answers that has no register entry for the role
(a shadow run, or a model never qualified), ``note: <role> ran a model with no register entry
(<model_key>); its answers were recorded in shadow or unqualified`` in ``notes``, and the same
fact in ``unqualified_models``. A test backend (``replay``, ``fake``) gets none.

**Warnings.** For each role, the answer rows whose ``rules_digest`` differs from the register
entry's for that row's ``model_key``: ``warning: <role> ran on rules that changed since its
qualification (<n> answers)``. A row whose model has no entry for the role (a test backend, or
a shadow run), or whose entry has no rules digest (a reference), cannot be compared and gives
no warning.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from chd_curator.backends.base import token_count
from chd_curator.fs import write_bytes_atomically
from chd_curator.register import Entry, load_register
from chd_curator.roles import ROLES
from chd_curator.run import RunFormatError, RunPaths, load_assemble, load_normalise

REPORT_FORMAT: Final = 1
LOCAL_ROLES: Final = ("triage-screen",)
_DURATION: Final = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)")
_SECONDS: Final = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_TEST_KINDS: Final = ("replay", "fake")
_TRACE_COLUMNS: Final = ("process", "cpus", "realtime")


class ReportError(Exception):
    """An input of the report is missing, unreadable or malformed (exit 2)."""


def _tier(role: str) -> str:
    return "local" if role in LOCAL_ROLES else "capable"


def _run_paths(run_dir: Path) -> RunPaths:
    d = run_dir.resolve()
    layout = ("local", "literature", "runs")
    if (
        len(d.parents) < 5
        or tuple(x.name for x in (d.parents[3], d.parents[2], d.parents[1])) != layout
    ):
        raise ReportError(
            f"{run_dir} is not a run folder "
            "(expected <root>/local/literature/runs/<SYMBOL>/<run id>)"
        )
    return RunPaths(d.parents[4], d.parent.name, d.name)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportError(f"{path}: cannot be read: {exc}") from None
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            raise ReportError(f"{path}: line {n} is not JSON") from None
        if not isinstance(data, dict) or "key" not in data or "attempts" not in data:
            raise ReportError(f"{path}: line {n} is not an answer row (needs key and attempts)")
        rows.append(data)
    return rows


def _process(
    role: str, files: Sequence[Path], register: Sequence[Entry]
) -> tuple[dict[str, Any], int, dict[str | None, int]]:
    unqualified: dict[str | None, int] = {}
    seconds = 0.0
    tokens = failed = cached = stale = answers = 0
    qualified = {e.model_key: e.rules_digest for e in register if e.role == role}
    for path in files:
        for r in _read_rows(path):
            answers += 1
            usage = r.get("usage", {})
            if isinstance(usage, dict):
                tokens += token_count(usage)
            secs = r.get("seconds", 0)
            if isinstance(secs, int | float) and not isinstance(secs, bool):
                seconds += secs
            if "error" in r:
                failed += 1
                continue
            if r["attempts"] == 0 and "parsed" in r:
                cached += 1
            raw_key = r.get("model_key")
            mkey = raw_key if isinstance(raw_key, str) and raw_key else None
            if mkey is None or (mkey not in qualified and mkey.split(":", 1)[0] not in _TEST_KINDS):
                unqualified[mkey] = unqualified.get(mkey, 0) + 1
            was = qualified.get(mkey) if mkey is not None else None
            if was and r.get("rules_digest") != was:
                stale += 1
    entry = {
        "role": role,
        "tier": _tier(role),
        "answers": answers,
        "cached": cached,
        "failed": failed,
        "seconds": round(seconds, 3),
        "tokens": tokens,
        "core_hours": None,
    }
    return entry, stale, unqualified


def _seconds(text: str) -> float | None:
    text = text.strip()
    if re.fullmatch(r"[0-9]+", text):
        return int(text) / 1000
    parts = text.split()
    if not parts:
        return None
    total = 0.0
    for part in parts:
        m = _DURATION.fullmatch(part)
        if m is None:
            return None
        total += float(m.group(1)) * _SECONDS[m.group(2)]
    return total


def _read_trace(path: Path) -> tuple[dict[str, float], list[str]]:
    """Core-hours per role, and the process names no role carries (sorted)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportError(f"{path}: cannot be read: {exc}") from None
    if not lines:
        raise ReportError(f"{path}: the trace is empty")
    header = lines[0].split("\t")
    missing = [c for c in _TRACE_COLUMNS if c not in header]
    if missing:
        raise ReportError(f"{path}: the trace has no column {', '.join(missing)}")
    col = {name: header.index(name) for name in (*_TRACE_COLUMNS, "status") if name in header}
    by_role = {role.upper().replace("-", "_"): role for role in ROLES}
    hours: dict[str, float] = {}
    unmatched: set[str] = set()
    for n, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) < len(header):
            raise ReportError(
                f"{path}: line {n} has {len(cells)} columns, the header {len(header)}"
            )
        if "status" in col and cells[col["status"]].strip() == "CACHED":
            continue
        process = cells[col["process"]].strip().rsplit(":", 1)[-1]
        cpus_text = cells[col["cpus"]].strip()
        if not re.fullmatch(r"[0-9]+", cpus_text):
            raise ReportError(f"{path}: line {n}: cpus {cpus_text!r} is not an integer")
        secs = _seconds(cells[col["realtime"]])
        if secs is None:
            raise ReportError(
                f"{path}: line {n}: realtime {cells[col['realtime']]!r} is not a Nextflow duration"
            )
        role = by_role.get(process.upper())
        if role is None:
            unmatched.add(process)
        else:
            hours[role] = hours.get(role, 0.0) + int(cpus_text) * secs / 3600
    return hours, sorted(unmatched)


def _owner_decisions(p: RunPaths) -> dict[str, str]:
    latest: dict[str, str] = {}
    files = sorted(p.decisions.glob("*.json")) if p.decisions.is_dir() else []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ReportError(f"{path}: cannot be read as JSON: {exc}") from None
        rows = data.get("findings") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ReportError(f"{path}: is not a decisions file (no 'findings' list)")
        for entry in rows:
            if not isinstance(entry, dict) or "id" not in entry or "decision" not in entry:
                raise ReportError(f"{path}: a findings entry lacks id or decision")
            latest[str(entry["id"])] = str(entry["decision"])
    return latest


def _placed(p: RunPaths) -> int:
    load_normalise(p)  # refuses a malformed file, naming it
    if not p.normalise.is_file():
        return 0
    data = json.loads(p.normalise.read_text(encoding="utf-8"))
    return len({v["finding"] for v in data["variants"] if v["outcome"] == "placed"})


def _percent_local(local: int, total: int) -> int:
    return (200 * local + total) // (2 * total)


def _summary(findings: Mapping[str, int], local: int, total: int) -> str:
    tokens = f"{_percent_local(local, total)}% of tokens local" if total else "no tokens recorded"
    return (
        f"{findings['total']} findings: {findings['agreed']} agreed, {findings['judged']} "
        f"judged, {findings['corrected_by_owner']} corrected by the owner; {tokens}"
    )


def _markdown(report: Mapping[str, Any]) -> str:
    f = report["findings"]
    lines = [
        f"# Run report: {report['run']['symbol']} {report['run']['run_id']}",
        "",
        report["summary"],
        "",
        "## Findings",
        "",
        "| settled by | findings |",
        "| --- | --- |",
        f"| two blind readers agreed | {f['agreed']} |",
        f"| a judge decided | {f['judged']} |",
        f"| corrected by the owner | {f['corrected_by_owner']} |",
        f"| normalised by code | {f['normalised']} |",
        f"| total (agreed or judged) | {f['total']} |",
        "",
        "## Processes",
        "",
        "| process | tier | answers | cached | failed | seconds | tokens | core-hours |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for p in report["processes"]:
        hours = "-" if p["core_hours"] is None else f"{p['core_hours']:.3f}"
        lines.append(
            f"| {p['role']} | {p['tier']} | {p['answers']} | {p['cached']} | {p['failed']} "
            f"| {p['seconds']} | {p['tokens']} | {hours} |"
        )
    t = report["tokens"]
    lines += ["", f"Tokens: {t['local']} local, {t['capable']} capable, {t['total']} in all."]
    if report["trace_unmatched_processes"]:
        lines += [
            "",
            "Trace processes no role carries (not counted): "
            + ", ".join(report["trace_unmatched_processes"]),
        ]
    if report["notes"]:
        lines += ["", "## Notes", "", *(f"- {n}" for n in report["notes"])]
    if report["warnings"]:
        lines += ["", "## Warnings", "", *(f"- {w}" for w in report["warnings"])]
    return "\n".join(lines) + "\n"


def write_report(
    run_dir: Path,
    answers: Sequence[tuple[str, Path]],
    trace: Path | None,
    out_dir: Path,
    *,
    register: Sequence[Entry] | None = None,
) -> tuple[Path, Path]:
    """Write ``report.json`` and ``report.md`` into ``out_dir`` (created) and return their paths.

    ``answers`` pairs a role with one of its answers files; ``register`` defaults to the packaged
    one. Raises :class:`ReportError` for every refusal, before anything is written.
    """
    paths = _run_paths(run_dir)
    entries = list(load_register() if register is None else register)
    files: dict[str, list[Path]] = {}
    for role, path in answers:
        if role not in ROLES:
            raise ReportError(f"unknown role {role!r}; the roles are {', '.join(ROLES)}")
        files.setdefault(role, []).append(path)
    processes: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    notes: list[str] = []
    unqualified: list[dict[str, Any]] = []
    for role in ROLES:
        if role in files:
            processes[role], stale, unqual = _process(role, files[role], entries)
            for mkey in sorted(unqual, key=lambda k: (k is None, k or "")):
                unqualified.append({"role": role, "model_key": mkey, "answers": unqual[mkey]})
                shown = mkey or "no model_key recorded"
                notes.append(
                    f"note: {role} ran a model with no register entry ({shown}); "
                    "its answers were recorded in shadow or unqualified"
                )
            if stale:
                warnings.append(
                    f"warning: {role} ran on rules that changed since its qualification "
                    f"({stale} answers)"
                )
    unmatched: list[str] = []
    if trace is not None:
        hours, unmatched = _read_trace(trace)
        for role, h in hours.items():
            if role not in processes:
                processes[role] = {
                    "role": role,
                    "tier": _tier(role),
                    "answers": 0,
                    "cached": 0,
                    "failed": 0,
                    "seconds": 0.0,
                    "tokens": 0,
                    "core_hours": None,
                }
            processes[role]["core_hours"] = round(h, 4)
    try:
        record = load_assemble(paths)
        normalised = _placed(paths)
    except RunFormatError as exc:
        raise ReportError(str(exc)) from None
    decided = _owner_decisions(paths)
    if record is None:
        agreed: set[str] = set()
        judged: set[str] = set()
        warnings.append(
            "warning: the run has no assemble.json, so no finding is counted as agreed or judged"
        )
    else:
        agreed, judged = set(record.verified), set(record.judged)
    outside: list[str] = []
    unmatched_ids: list[str] = []
    if record is None:
        unmatched_ids = sorted(decided)
        if unmatched_ids:
            warnings.append(
                f"warning: decisions/ holds {len(unmatched_ids)} decision(s) that cannot be "
                "matched to findings because the run has no assemble.json; none counted"
            )
    else:
        outside = sorted(set(decided) - agreed - judged)
    if outside:
        warnings.append(
            "warning: decisions/ names findings that are not findings of this run, "
            f"not counted: {', '.join(outside)}"
        )
    corrected = sum(1 for i, d in decided.items() if d == "fix" and i in agreed | judged)
    findings = {
        "total": len(agreed | judged),
        "agreed": len(agreed),
        "judged": len(judged),
        "corrected_by_owner": corrected,
        "normalised": normalised,
    }
    ordered = [processes[r] for r in ROLES if r in processes]
    local = sum(p["tokens"] for p in ordered if p["tier"] == "local")
    capable = sum(p["tokens"] for p in ordered if p["tier"] == "capable")
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "run": {"symbol": paths.symbol, "run_id": paths.run_id},
        "findings": findings,
        "processes": ordered,
        "tokens": {"local": local, "capable": capable, "total": local + capable},
        "trace_unmatched_processes": unmatched,
        "summary": _summary(findings, local, local + capable),
        "warnings": warnings,
        "notes": notes,
        "unqualified_models": unqualified,
        "decisions_outside_run": outside,
        "decisions_unmatched": unmatched_ids,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "report.json", out_dir / "report.md"
    payload = json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
    write_bytes_atomically(json_path, (payload + "\n").encode("utf-8"))
    write_bytes_atomically(md_path, _markdown(report).encode("utf-8"))
    return json_path, md_path
