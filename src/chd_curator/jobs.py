"""The packaged job scripts, and the small helpers ``serve-and-ask.sh`` calls.

``workflow/jobs/`` holds three scripts that run a local model beside ``chd-curator ask``:
``serve-and-ask.sh``, ``llama-build.sbatch`` and ``cluster-ask.sbatch``. ``chd-curator job-script
<name>`` prints where a script is installed. The helpers below keep YAML parsing, port
choice, the site's server flags and the local-usage sidecar out of bash; the script runs them as
``python -m chd_curator.jobs <helper> ...``.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

from chd_curator.fs import write_bytes_atomically
from chd_curator.roles import WORKFLOW
from chd_curator.site import SiteError, load_site

JOB_SCRIPTS: Final = ("serve-and-ask.sh", "llama-build.sbatch", "cluster-ask.sbatch")
JOBS_DIR: Final = WORKFLOW / "jobs"


def job_script_path(name: str) -> Path:
    """The absolute path of a packaged job script; ``KeyError`` for any other name."""
    if name not in JOB_SCRIPTS:
        raise KeyError(name)
    return JOBS_DIR / name


def local_site_text(site: Path, role: str, base_url: str) -> str:
    """A one-role site YAML: the given site's entry for ``role``, only ``settings.base_url`` new.

    The site is loaded through ``load_site`` first, so the secret guards run and a role the site
    does not define is refused. Every other field is copied unchanged, which keeps ``model_key``
    (a transport setting does not enter it) equal to the original site's.
    """
    loaded = load_site(site)
    if role not in loaded.roles:
        raise SiteError(f"{site} has no role {role!r}")
    raw = YAML(typ="safe").load(site.read_text(encoding="utf-8"))["roles"][role]
    entry: dict[str, Any] = dict(raw)
    settings = dict(entry.get("settings") or {})
    settings["base_url"] = base_url
    entry["settings"] = settings
    out = io.StringIO()
    YAML(typ="safe").dump({"roles": {role: entry}}, out)
    return out.getvalue()


def server_args_of(site: Path, role: str) -> str:
    """The llama-server flags of the site's ``role``: its ``settings.server_args``, a string or a
    list of strings (joined with spaces; an element holding whitespace is refused, since the
    script word-splits the flags). They are not a transport setting, so they are part of the
    role's ``model_key`` and the register qualifies exactly the flags that run (R-SWA-55)."""
    loaded = load_site(site)
    if role not in loaded.roles:
        raise SiteError(f"{site} has no role {role!r}")
    raw = loaded.roles[role].settings.get("server_args")
    if isinstance(raw, str) and raw.split():
        return " ".join(raw.split())
    if (
        isinstance(raw, list)
        and raw
        and all(isinstance(a, str) and a and not any(c.isspace() for c in a) for a in raw)
    ):
        return " ".join(raw)
    raise SiteError(
        f"roles.{role}.settings.server_args must be the llama-server flags, a string or a list "
        "of single words"
    )


def local_usage_json(cpus: int, seconds: float, job_id: str | None) -> str:
    """The sidecar ``serve-and-ask.sh`` writes beside the answers (``<answers>.local-usage.json``):
    what the local model's server held, for ``report --local-usage``."""
    body = {"cpus": cpus, "seconds": round(seconds, 3), "job_id": job_id or None}
    return json.dumps(body, sort_keys=True) + "\n"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def healthy(url: str) -> bool:
    try:
        # No proxy, whatever the environment says: the server is on this node's loopback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as reply:
            return bool(reply.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main(argv: list[str]) -> int:
    match argv:
        case ["port"]:
            print(free_port())
        case ["health", url]:
            return 0 if healthy(url) else 1
        case ["server-args", site, role]:
            try:
                print(server_args_of(Path(site), role))
            except SiteError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        case ["local-usage", out, cpus, seconds, job_id]:
            text = local_usage_json(int(cpus), float(seconds), job_id)
            write_bytes_atomically(Path(out), text.encode("utf-8"))
        case ["site", site, role, base_url, out]:
            try:
                write_bytes_atomically(
                    Path(out), local_site_text(Path(site), role, base_url).encode("utf-8")
                )
            except SiteError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        case _:
            print(
                "usage: jobs.py port | health URL | site SITE ROLE BASE_URL OUT | "
                "server-args SITE ROLE | local-usage OUT CPUS SECONDS JOB_ID",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
