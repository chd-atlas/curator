"""Fixtures for the review archive's tests: an archive under ``tmp_path``.

Names are made up, except the owner, who is E.A.M here as elsewhere in these tests.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from chd_curator import review_page
from chd_curator.archive import compact_utc

BUILT_ON = "2026-11-19T09:00:00+00:00"
SHA = "sha256:" + "a" * 64


def git(archive: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(archive), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


def make_archive(tmp: Path, *, identity: bool = True) -> Path:
    """A git checkout on ``main`` with the marker and a README, in one commit.

    With ``identity=False`` the local config holds no user: the initial commit takes its
    identity from ``-c`` flags, which write nothing.
    """
    archive = tmp / "archive"
    archive.mkdir()
    git(archive, "init", "-b", "main")
    if identity:
        git(archive, "config", "user.name", "Test Curator")
        git(archive, "config", "user.email", "curator@example.invalid")
    (archive / ".review-archive").write_text("review archive\n", encoding="utf-8")
    (archive / "README.md").write_text("# Review archive\n\n## Removals\n\nNone yet.\n", "utf-8")
    git(archive, "add", "--", ".review-archive", "README.md")
    git(
        archive,
        "-c",
        "user.name=Test Curator",
        "-c",
        "user.email=curator@example.invalid",
        "commit",
        "-m",
        "start the archive",
    )
    return archive


def page_html(
    path: Path,
    *,
    symbol: str = "TBX5",
    gene: str = "HGNC:11604",
    built_on: str = BUILT_ON,
    sha: str = SHA,
    name: str = "E.A.M",
    role: str = "owner",
    extra: dict[str, Any] | None = None,
) -> Path:
    data = {
        "page": {
            "symbol": symbol,
            "gene": gene,
            "built_on": built_on,
            "atlas_commit": None,
            "literature_sha256": sha,
        },
        "reviewer": {"name": name, "role": role},
        "findings": [],
        **(extra or {}),
    }
    path.write_text(
        "<!doctype html><html><body>"
        f'<script type="application/json" id="data">{review_page.inline_json(data)}</script>'
        "</body></html>\n",
        encoding="utf-8",
    )
    return path


def decisions_doc(
    path: Path,
    *,
    name: str,
    role: str,
    finished_at: str,
    findings: list[dict[str, Any]],
    triage: dict[str, Any] | None = None,
    built_on: str = BUILT_ON,
    sha: str = SHA,
    symbol: str = "TBX5",
    gene: str = "HGNC:11604",
) -> Path:
    """A schema-valid format 1 decisions file (``findings`` are given whole)."""
    import json

    doc = {
        "format": 1,
        "gene": gene,
        "symbol": symbol,
        "page": {"built_on": built_on, "atlas_commit": None, "literature_sha256": sha},
        "reviewer": {"name": name, "role": role},
        "session": {"started_at": built_on, "finished_at": finished_at, "active_ms": 1000},
        "findings": findings,
        "papers_seen": [],
        "triage": triage,
    }
    path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def finding(fid: str, decision: str, digest: str, at: str, note: str = "") -> dict[str, Any]:
    return {
        "id": fid,
        "shown_digest": digest,
        "decision": decision,
        "note": note or ("because" if decision != "approve" else ""),
        "active_ms": 10,
        "decided_at": at,
    }


def put(
    archive: Path,
    tmp: Path,
    name: str,
    role: str,
    finished: str,
    findings: list[dict[str, Any]],
    triage: dict[str, Any] | None = None,
    run: str = "r1",
    built_on: str = BUILT_ON,
    page_extra: dict[str, Any] | None = None,
    sha: str = SHA,
    page: bool = True,
    gene: str = "HGNC:11604",
) -> Path:
    """File a decisions file by copying it into place; an owner's triage verdict that approves
    or fixes also gets the page it was made on (``page_extra`` is that page's extra data)."""
    src = decisions_doc(
        tmp / "src.json",
        name=name,
        role=role,
        finished_at=finished,
        findings=findings,
        triage=triage,
        built_on=built_on,
        sha=sha,
        gene=gene,
    )
    dest = (
        archive
        / "TBX5"
        / run
        / "decisions"
        / f"{compact_utc(finished)}--{review_page.slug(name)}.json"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    deciding = triage is not None and triage["decision"] in ("approve", "fix")
    if page and (page_extra is not None or (role == "owner" and deciding)):
        pages = archive / "TBX5" / run / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        page_html(
            pages / f"{compact_utc(built_on)}--{review_page.slug(name)}.html",
            name=name,
            role=role,
            built_on=built_on,
            sha=sha,
            extra=page_extra,
        )
    return dest


def commit_all(archive: Path) -> None:
    git(archive, "add", "--", "TBX5")
    git(archive, "commit", "-m", "add")


def flip(pmid: str, to: str = "excluded") -> dict[str, str]:
    return {"pmid": pmid, "to": to, "note": "because"}


def tri(decision: str, *flips: dict[str, str]) -> dict[str, Any]:
    return {"decision": decision, "note": "n" if decision != "approve" else "", "active_ms": 1,
            "flips": list(flips)}  # fmt: skip
