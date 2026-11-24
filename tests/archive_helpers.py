"""Fixtures for the review archive's tests: made-up names, an archive under ``tmp_path``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from chd_curator import review_page

BUILT_ON = "2026-11-19T12:00:00+00:00"
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
