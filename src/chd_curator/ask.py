"""``chd-curator ask``: one validated structured model call per request.

Every model step of the pipeline is this command. For each request row ``{key, inputs}`` it
assembles the role's input (``roles.assemble``), checks the site's backend for that role is
qualified (``register.allowed``), and then, in this order: cache, context refusal, ask, parse,
validate, retry, cache. Nothing is guessed: a request that cannot be assembled, a repeated key,
an unqualified model (``--shadow`` lifts that for ``triage-screen`` only), a ``check-batch``
request that does not fit its run and a configuration that would fail every request
(``_preflight``) are all refused (``AskUsageError``, exit 2) before the first call. A refused
credential (HTTP 401 or 403, ``ConfigurationError``) stops the run: no further call, the answers
file written with an error row for every request not asked, then ``AskStopped`` (exit 2).

* **Cache.** ``<cache>/<role>/<sha256(model_key NUL input digest)>.json``, so a changed model
  or setting misses it. Only a validated answer is stored. A stored answer is validated again
  when it is read; one that no longer passes (or cannot be read) is a miss, never a result.
* **Context.** The estimate is ceil(UTF-8 bytes of system and user / 3), and ``max_tokens``
  (``settings.max_tokens``, default 4096) is added; an input that does not fit the backend's
  ``context_tokens`` is refused with no call, never truncated. Each retry is checked again.
* **Parse.** One JSON object, optionally inside one fenced block; anything else is an error.
* **Validate.** The role's answer schema, plus for triage that ``pmid`` is the request key and
  that ``excluded`` never carries the reason ``none``; plus, for every role, that an answer
  holds no e-mail address its input lacks (Claude Code puts the logged-in account into every
  call, and a copied address would reach a curated file). With ``check-batch`` (readers and
  judge) the answer is also written as the contract files of its batch under the run
  directory and put through ``check-batch``'s own logic; a refused answer leaves those paths
  as they were before the attempt (the previous bytes back, or the file removed), so merge
  never reads an invalid file.
* **Retry.** At most two, each with the errors appended verbatim to the ``user`` text. A
  transient backend error is not a retry: it backs off 5, 20, 60 s and then gives up.

``attempts`` counts the answers the backend returned (a transient error returns none).
``usage`` and ``seconds`` sum those answers, on an answer row and an error row alike; a cache
hit is ``attempts: 0`` with an empty usage and 0 seconds, since this run spent none. An answer
row records the accepted answer's ``meta`` (a cache hit the stored one).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from chd_curator import assemble as assemble_module
from chd_curator import check as check_module
from chd_curator import merge as merge_module
from chd_curator.atlas import AtlasError, default_atlas_cli, target
from chd_curator.backends import (
    BackendError,
    ConfigurationError,
    Request,
    TransientError,
    make_backend,
)
from chd_curator.backends.base import Backend, token_count
from chd_curator.diff import DiffError
from chd_curator.fs import write_bytes_atomically
from chd_curator.records import RecordError
from chd_curator.register import Entry, allowed
from chd_curator.roles import ROLES, RoleError, RoleInput, assemble, canonical
from chd_curator.run import RunFormatError, RunPaths, load_batches
from chd_curator.site import BackendSpec, Site, model_key
from chd_curator.write import schema_error_lines

TRIAGE: Final = ("triage-screen", "triage-check")
# Spec section 3: in shadow the check decides every hit; only the screen is recorded and scored.
SHADOWABLE: Final = "triage-screen"
PASS_OF: Final = {"extract": "extract", "blind-read": "verify", "judge": "judge"}
BACKOFF_SECONDS: Final = (5, 20, 60)
MAX_RETRIES: Final = 2
DEFAULT_MAX_TOKENS: Final = 4096
_ROW_KEYS: Final = frozenset({"key", "inputs", "gene", "batch", "mode"})
_BATCH_ID: Final = re.compile(r"[0-9]{2,}")
_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_FENCE: Final = re.compile(r"```(?:json)?[ \t]*\n(.*)\n```", re.S)
_EMAIL_ERROR: Final = "the answer contains an email address that is not in its input"
_REFUSED: Final = "--- your previous answer was refused ---"
_END_REFUSED: Final = "--- end of refusal ---"

Validate = Literal["schema", "check-batch"]
Row = dict[str, Any]


class AskUsageError(Exception):
    """A request, site, register or run that cannot be asked about; the command exits 2."""


class AskStopped(AskUsageError):
    """A configuration error (a refused credential) stopped the run after the answers file was
    written; the command exits 2."""


@dataclass(frozen=True)
class AskSummary:
    """``asked``: requests the backend answered at least once; ``cached``: answered from the
    cache; ``failed``: keys with an error row; ``usage``: the sum of reported usage;
    ``tokens``: the sum of each row's ``backends.base.token_count``, the report's rule."""

    asked: int
    cached: int
    failed: list[str]
    usage: dict[str, int]
    tokens: int = 0


@dataclass
class _Progress:
    """What one request has spent so far, so an error row can say it too."""

    attempts: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


@dataclass(frozen=True)
class _Req:
    key: str
    inputs: dict[str, str]
    gene: str | None
    batch: str | None
    mode: str


@dataclass(frozen=True)
class _Ctx:
    role: str
    spec: BackendSpec
    mkey: str
    backend: Backend
    validate: Validate
    root: Path
    cache_dir: Path | None
    run_id: str | None
    atlas_cli: Path
    symbols: Mapping[str, str]
    sleep: Callable[[float], None]
    max_tokens: int
    stop: threading.Event = field(default_factory=threading.Event)
    stopped: list[str] = field(default_factory=list)  # the configuration error, once set


def _load_requests(path: Path, check_batch: bool) -> list[_Req]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise AskUsageError(f"cannot read the requests {path}: {exc}") from None
    found: list[_Req] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        where = f"{path}:{number}"
        try:
            row = json.loads(line)
        except ValueError:
            raise AskUsageError(f"{where}: not a JSON object") from None
        if not isinstance(row, dict):
            raise AskUsageError(f"{where}: not a JSON object")
        extra = sorted(set(row) - _ROW_KEYS)
        if extra:
            raise AskUsageError(f"{where}: unexpected key(s) {extra}")
        key, inputs = row.get("key"), row.get("inputs")
        if not isinstance(key, str) or not key:
            raise AskUsageError(f"{where}: a request needs a non-empty string `key`")
        if not isinstance(inputs, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in inputs.items()
        ):
            raise AskUsageError(f"{where}: request {key!r} needs `inputs`, a mapping of strings")
        if key in seen:
            raise AskUsageError(f"{where}: the key {key!r} is requested twice")
        seen.add(key)
        gene, batch, mode = row.get("gene"), row.get("batch"), row.get("mode", "new")
        if mode not in ("new", "backfill"):
            raise AskUsageError(f"{where}: request {key!r} has mode {mode!r}; new or backfill")
        if check_batch:
            for name, value in (("gene", gene), ("batch", batch)):
                if not isinstance(value, str) or not value:
                    raise AskUsageError(f"{where}: request {key!r} needs `{name}` to check-batch")
            if not _BATCH_ID.fullmatch(str(batch)):
                raise AskUsageError(
                    f"{where}: request {key!r} has batch {batch!r}; digits expected"
                )
        found.append(_Req(key, inputs, gene, batch, mode))
    return found


def _preflight_check_batch(
    reqs: list[_Req], role: str, root: Path, run_id: str | None, atlas_cli: Path
) -> dict[str, str]:
    """Refuse a request that does not fit its run; return each gene's approved symbol."""
    if role not in PASS_OF:
        raise AskUsageError(f"--validate check-batch is for readers and the judge, not {role}")
    if not run_id:
        raise AskUsageError("--validate check-batch needs --run")
    plans: dict[str, Any] = {}
    claimed: set[tuple[str, str]] = set()
    symbols: dict[str, str] = {}
    for r in reqs:
        gene, batch = str(r.gene), str(r.batch)
        if gene not in plans:
            try:
                plans[gene] = load_batches(RunPaths(root, gene, run_id))
                symbols[gene] = target(root, gene, atlas_cli).symbol
            except (RunFormatError, AtlasError) as exc:
                raise AskUsageError(str(exc)) from None
        plan = plans[gene]
        if batch not in {b.id for b in plan.batches}:
            raise AskUsageError(f"request {r.key!r}: batch {batch} is not in {gene}'s batches.json")
        planned = "new" if plan.mode == "reread" else plan.mode
        if r.mode != planned:
            raise AskUsageError(
                f"request {r.key!r}: mode {r.mode!r} but batches.json plans {plan.mode!r}"
            )
        if (gene, batch) in claimed:
            raise AskUsageError(f"request {r.key!r}: batch {batch} of {gene} is asked twice")
        claimed.add((gene, batch))
    return symbols


def _estimate(system: str, user: str) -> int:
    return math.ceil(len((system + user).encode("utf-8")) / 3)


def _parse(text: str) -> tuple[Any, list[str]]:
    body = text.strip()
    fenced = _FENCE.fullmatch(body)
    if fenced:
        body = fenced.group(1)

    def refuse(name: str) -> None:
        raise ValueError(f"the constant {name} is not JSON")

    try:
        value = json.loads(body, parse_constant=refuse)
    except ValueError as exc:
        return None, [f"<root>: the answer is not one JSON object ({exc})"]
    if not isinstance(value, dict):
        return None, ["<root>: the answer is not one JSON object"]
    return value, []


def _semantic(parsed: Any, role: str, key: str, schema: Mapping[str, Any]) -> list[str]:
    errors = schema_error_lines(parsed, Draft202012Validator(schema))
    if errors or role not in TRIAGE:
        return errors
    if parsed["pmid"] != key:
        errors.append(f"pmid: the answer is for {parsed['pmid']} but the request is {key}")
    if parsed["decision"] == "excluded" and parsed["reason"] == "none":
        errors.append("reason: an excluded hit needs a reason other than none")
    return errors


def _foreign_email(texts: list[str], system: str, user: str) -> bool:
    return any(
        m.group(0) not in system and m.group(0) not in user
        for text in texts
        for m in _EMAIL.finditer(text)
    )


def _yaml_bytes(obj: Any) -> bytes:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.width = 1_000_000
    out = StringIO()
    yaml.dump(obj, out)
    return out.getvalue().encode("utf-8")


def _contract_files(c: _Ctx, r: _Req, parsed: Mapping[str, Any]) -> dict[Path, bytes]:
    """The files the answer is, at the paths ``check-batch`` and the next command read."""
    run = RunPaths(c.root, str(r.gene), str(c.run_id))
    n = str(r.batch)
    if c.role == "judge":
        return {
            run.judging / f"judge-{n}.findings.yaml": _yaml_bytes({"findings": parsed["findings"]}),
            run.judge / f"judge-{n}.decisions.yaml": _yaml_bytes(parsed["decisions"]),
        }
    base = (run.extract if c.role == "extract" else run.verify) / f"batch-{n}"
    files = {
        base.with_name(base.name + ".findings.yaml"): _yaml_bytes({"findings": parsed["findings"]}),
        base.with_name(base.name + ".evidence.yaml"): _yaml_bytes(parsed["evidence"]),
        base.with_name(base.name + ".report.md"): str(parsed["report"]).encode("utf-8"),
    }
    if r.mode == "new":
        papers = json.dumps(parsed["papers"], sort_keys=True, ensure_ascii=False, indent=1)
        files[base.with_name(base.name + ".papers.json")] = (papers + "\n").encode("utf-8")
    return files


def _check_batch(c: _Ctx, r: _Req, parsed: Mapping[str, Any]) -> list[str]:
    """Install the contract files and run check-batch on them; on any refusal put every path
    back as it was. Returns the errors (empty when the batch passes and the files stay)."""
    files = _contract_files(c, r, parsed)
    before = {p: (p.read_bytes() if p.is_file() else None) for p in files}
    problems: list[str] = []
    kept = False
    try:
        for path, payload in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            write_bytes_atomically(path, payload)
        checked = check_module.check_batch(
            c.root,
            str(r.gene),
            c.symbols[str(r.gene)],
            str(c.run_id),
            str(r.batch),
            PASS_OF[c.role],
            c.atlas_cli,
        )
        problems = list(checked.lines) if checked.code != 0 else []
        kept = not problems
    except (
        check_module.CheckError,
        merge_module.MergeError,
        assemble_module.AssembleError,
        RunFormatError,
        RecordError,
        DiffError,
        AtlasError,
    ) as exc:
        problems = [str(exc)]
    finally:
        if not kept:
            for path, old in before.items():
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    write_bytes_atomically(path, old)
    return problems


def _cache_path(c: _Ctx, ri: RoleInput) -> Path | None:
    if c.cache_dir is None:
        return None
    name = hashlib.sha256(f"{c.mkey}\0{ri.digest}".encode()).hexdigest()
    return c.cache_dir / c.role / f"{name}.json"


def _read_cache(path: Path | None) -> tuple[Any, dict[str, Any]]:
    """``(parsed, meta)`` of a stored answer; ``(None, {})`` when there is none."""
    if path is None or not path.is_file():
        return None, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    meta = data.get("meta")
    return data.get("parsed"), meta if isinstance(meta, dict) else {}


def _row(
    r: _Req,
    ri: RoleInput,
    c: _Ctx,
    parsed: Any,
    attempts: int,
    usage: Any,
    secs: Any,
    meta: Mapping[str, Any],
) -> Row:
    return {
        "key": r.key,
        "parsed": parsed,
        "attempts": attempts,
        "usage": usage,
        "seconds": secs,
        "model_key": c.mkey,
        "input_digest": ri.digest,
        "rules_digest": ri.rules_digest,
        "meta": dict(meta),
    }


def _error(r: _Req, message: str, p: _Progress) -> Row:
    """An error row: what went wrong and what the attempts before it spent."""
    return {
        "key": r.key,
        "error": message,
        "attempts": p.attempts,
        "usage": dict(p.usage),
        "seconds": p.seconds,
    }


def _problems(
    c: _Ctx, r: _Req, ri: RoleInput, parsed: Any, texts: list[str], user: str
) -> list[str]:
    errors = _semantic(parsed, c.role, r.key, ri.schema)
    if errors:
        return errors
    if _foreign_email(texts + [canonical(parsed)], ri.system, user):
        return [_EMAIL_ERROR]
    return _check_batch(c, r, parsed) if c.validate == "check-batch" else []


def _ask_one(c: _Ctx, r: _Req, ri: RoleInput) -> Row:
    """One request's row. Whatever goes wrong inside it (a file that cannot be written, a check
    that crashes) is this request's error row, naming the exception, and the run goes on."""
    progress = _Progress()
    try:
        return _run_one(c, r, ri, progress)
    except Exception as exc:
        return _error(r, f"{type(exc).__name__}: {exc}", progress)


def _run_one(c: _Ctx, r: _Req, ri: RoleInput, p: _Progress) -> Row:
    path = _cache_path(c, ri)
    hit, hit_meta = _read_cache(path)
    if hit is not None and not _problems(c, r, ri, hit, [], ri.user):
        return _row(r, ri, c, hit, 0, {}, 0.0, hit_meta)
    transient = 0
    user = ri.user
    while True:
        estimate = _estimate(ri.system, user)
        if estimate + c.max_tokens > c.spec.context_tokens:
            return _error(
                r,
                f"the input is estimated at {estimate} tokens and the answer may take "
                f"{c.max_tokens}, over the backend's context of {c.spec.context_tokens}",
                p,
            )
        if c.stop.is_set():
            again = "not asked" if p.attempts == 0 else "not asked again"
            return _error(
                r, f"{again}: the run stopped on a configuration error ({c.stopped[0]})", p
            )
        try:
            answer = c.backend.ask(Request(r.key, ri.system, user, ri.schema, c.max_tokens))
        except TransientError as exc:
            if transient >= len(BACKOFF_SECONDS):
                return _error(r, f"transient errors: {exc}", p)
            c.sleep(BACKOFF_SECONDS[transient])
            transient += 1
            continue
        except ConfigurationError as exc:
            c.stopped.append(str(exc))  # before the flag, so a reader of the flag finds it
            c.stop.set()
            return _error(r, str(exc), p)
        except BackendError as exc:
            return _error(r, str(exc), p)
        p.attempts += 1
        p.seconds += answer.seconds
        for name, count in answer.usage.items():
            p.usage[name] = p.usage.get(name, 0) + count
        if answer.key != r.key:
            return _error(
                r, f"the backend answered for {answer.key!r}, which this request is not", p
            )
        parsed, errors = _parse(answer.text)
        if not errors:
            errors = _problems(c, r, ri, parsed, [answer.text], user)
        if not errors:
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                body = {
                    "parsed": parsed,
                    "model_key": c.mkey,
                    "input_digest": ri.digest,
                    "meta": dict(answer.meta),
                }
                write_bytes_atomically(path, (canonical(body) + "\n").encode("utf-8"))
            return _row(r, ri, c, parsed, p.attempts, p.usage, p.seconds, answer.meta)
        if p.attempts > MAX_RETRIES:
            return _error(r, "the answer was refused after 3 attempts: " + "; ".join(errors), p)
        user = f"{ri.user}\n\n{_REFUSED}\n" + "\n".join(errors) + f"\n{_END_REFUSED}"


def _preflight(spec: BackendSpec, backend: Backend) -> None:
    """R-SWA-44: what would fail every request alike fails the command (exit 2) before the
    first call: an unset or empty ``key_env`` variable (named, never its value), an anthropic
    backend with no ``key_env``, and the backend's own ``preflight`` (``claude_code``: a
    cluster job variable, or ``claude --version`` failing; ``replay``: no ``settings.dir``)."""
    if spec.key_env is not None and not os.environ.get(spec.key_env):
        raise AskUsageError(f"{spec.id}: environment variable {spec.key_env} is not set")
    if spec.kind == "anthropic" and spec.key_env is None:
        raise AskUsageError(f"{spec.id}: key_env is required for the anthropic backend")
    check = getattr(backend, "preflight", None)
    if callable(check):
        try:
            check()
        except (BackendError, TransientError) as exc:
            raise AskUsageError(str(exc)) from None


def ask(
    role: str,
    requests: Path,
    answers: Path,
    site: Site,
    register: list[Entry],
    root: Path,
    cache_dir: Path | None,
    validate: Validate,
    run_id: str | None,
    shadow: bool,
    *,
    atlas_cli: Path | None = None,
    make: Callable[[BackendSpec], Backend] = make_backend,
    sleep: Callable[[float], None] = time.sleep,
) -> AskSummary:
    if role not in ROLES:
        raise AskUsageError(f"unknown role {role!r}; the roles are {', '.join(ROLES)}")
    if validate not in ("schema", "check-batch"):
        raise AskUsageError(f"--validate {validate!r} is not schema or check-batch")
    if shadow and role != SHADOWABLE:
        raise AskUsageError(
            f"--shadow is for {SHADOWABLE} only, not {role}: its answers decide, so its model "
            "must be qualified"
        )
    spec = site.roles.get(role)
    if spec is None:
        raise AskUsageError(f"{role}: not configured in the site config")
    if allowed(role, spec, register) is None and not shadow:
        raise AskUsageError(f"{role}: {model_key(spec)} is not qualified for this role")
    reqs = _load_requests(requests, validate == "check-batch")
    cli = atlas_cli if atlas_cli is not None else default_atlas_cli(root)
    symbols: dict[str, str] = {}
    if validate == "check-batch":
        symbols = _preflight_check_batch(reqs, role, root, run_id, cli)
    max_tokens = spec.settings.get("max_tokens", DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise AskUsageError(f"{spec.id}: settings.max_tokens must be an integer of at least 1")
    inputs: list[RoleInput] = []
    for r in reqs:
        try:
            inputs.append(assemble(role, root, r.inputs, r.mode))
        except RoleError as exc:
            raise AskUsageError(f"request {r.key!r}: {exc}") from None
    try:
        backend = make(spec)
    except BackendError as exc:
        raise AskUsageError(str(exc)) from None
    _preflight(spec, backend)
    ctx = _Ctx(
        role,
        spec,
        model_key(spec),
        backend,
        validate,
        root,
        cache_dir,
        run_id,
        cli,
        symbols,
        sleep,
        max_tokens,
    )
    with ThreadPoolExecutor(max_workers=spec.concurrency) as pool:
        rows = list(pool.map(lambda pair: _ask_one(ctx, *pair), zip(reqs, inputs, strict=True)))
    payload = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows)
    answers.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomically(answers, payload.encode("utf-8"))
    if ctx.stop.is_set():
        raise AskStopped(
            f"the run stopped on a configuration error: {ctx.stopped[0]}; {answers} holds an "
            "error row for every request not answered"
        )
    total: dict[str, int] = {}
    for row in rows:
        for name, count in row.get("usage", {}).items():
            total[name] = total.get(name, 0) + count
    return AskSummary(
        asked=sum(1 for row in rows if row["attempts"] > 0),
        cached=sum(1 for row in rows if "parsed" in row and row["attempts"] == 0),
        failed=[row["key"] for row in rows if "error" in row],
        usage=total,
        tokens=sum(token_count(row.get("usage", {})) for row in rows),
    )
