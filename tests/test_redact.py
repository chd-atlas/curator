"""Removing a reviewer from the review archive (CP-D16): the plan, the redaction, the commit."""

from __future__ import annotations

import json
import shlex
import subprocess
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archive_helpers import (
    BUILT_ON,
    commit_all,
    finding,
    flip,
    git,
    make_archive,
    page_html,
    put,
    tri,
)
from chd_curator import redact
from chd_curator.archive import ArchiveError, GitError, compact_utc
from chd_curator.archive_read import read_archive, state
from chd_curator.cli import app
from chd_curator.decisions import page_data_from_text
from chd_curator.redact import (
    REMOVED,
    apply_redact,
    name_pattern,
    plan_redact,
    redacted_page,
)

G, J = "CHDA:LIT:0000901", "CHDA:LIT:0000902"
D1 = "sha256:" + "2" * 64
AT = "2026-11-20T10:00:00+00:00"
URL_OWNER = "https://claude.ai/artifact/owner-page"
URL_X = "https://claude.ai/artifact/dr-x-page"
RUN = "TBX5/r1"
OWNER_PAGE = f"{RUN}/pages/{compact_utc(BUILT_ON)}--e-a-m.html"
X_PAGE = f"{RUN}/pages/{compact_utc(BUILT_ON)}--dr-x.html"
X_DECISIONS = f"{RUN}/decisions/{compact_utc(AT)}--dr-x.json"


def _entry(name: str, role: str | None = None, note: str = "my note") -> dict[str, str]:
    entry = {"name": name, "decision": "fix", "note": note, "date": "2026-11-20"}
    if role is not None:
        entry["role"] = role
    return entry


def _flip(name: str, role: str) -> dict[str, str]:
    return {**_entry(name, role), "pmid": "111", "to": "excluded", "finished_at": AT}


def _owner_data() -> dict[str, object]:
    return {
        "second_opinions": {
            G: [_entry("Dr X"), _entry("Dr Y", note="y note")],
            J: [_entry(" dr x ")],
        },
        "earlier": {G: {"decision": "fix", "note": "owner note", "date": "2026-11-19"}},
        "triage_earlier": {
            "owner": {"decision": "fix", "note": "owner triage", "date": "2026-11-19"},
            "clinicians": [_entry("Dr X"), _entry("Dr Y", note="y note")],
            "flips": [
                _flip("Dr X", "second_opinion"),
                _flip("Dr Y", "second_opinion"),
                _flip("E.A.M", "owner"),
            ],
        },
        "not_on_page": [
            {**_entry("Dr X", "second_opinion"), "id": "CHDA:LIT:0000999", "where": "elsewhere"},
            {**_entry("Dr Y", "second_opinion"), "id": "CHDA:LIT:0000999", "where": "elsewhere"},
        ],
    }


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    repo = make_archive(tmp_path)
    runs = repo / "TBX5" / "r1"
    (runs / "pages").mkdir(parents=True)
    owner = page_html(runs / "pages" / f"{compact_utc(BUILT_ON)}--e-a-m.html", extra=_owner_data())
    page_html(
        runs / "pages" / f"{compact_utc(BUILT_ON)}--dr-x.html", name="Dr X", role="second_opinion"
    )
    (owner.with_suffix(".artifact")).write_text(URL_OWNER + "\n", encoding="utf-8")
    (runs / "pages" / f"{compact_utc(BUILT_ON)}--dr-x.artifact").write_text(
        URL_X + "\n", encoding="utf-8"
    )
    put(repo, tmp_path, "Dr X", "second_opinion", AT, [finding(G, "fix", D1, AT)],
        tri("later", flip("111")))  # fmt: skip
    put(repo, tmp_path, "Dr Y", "second_opinion", AT, [finding(G, "fix", D1, AT)])
    put(repo, tmp_path, "E.A.M", "owner", AT, [finding(G, "approve", D1, AT)])
    commit_all(repo)
    return repo


def _files(repo: Path) -> dict[str, bytes]:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


def _data(repo: Path, rel: str) -> dict[str, object]:
    return page_data_from_text((repo / rel).read_text(encoding="utf-8"), rel)


def test_the_dry_run_lists_and_changes_nothing(archive: Path) -> None:
    head = git(archive, "rev-parse", "HEAD")
    plan = plan_redact(archive, "Dr X")
    assert plan.delete == (
        X_DECISIONS,
        f"{RUN}/pages/{compact_utc(BUILT_ON)}--dr-x.artifact",
        X_PAGE,
    )
    assert plan.rewrite == (OWNER_PAGE,)
    assert plan.republish == (URL_OWNER,)
    assert plan.unpublish == (URL_X,)
    assert plan.elsewhere == ()
    result = CliRunner().invoke(
        app, ["archive-redact", "--name", "Dr X", "--archive", str(archive)]
    )
    assert result.exit_code == 0, result.output
    assert X_PAGE in result.output and URL_OWNER in result.output and URL_X in result.output
    assert git(archive, "status", "--porcelain") == ""
    assert git(archive, "rev-parse", "HEAD") == head


def test_apply_redacts_only_the_named_reviewer_and_keeps_every_other_byte(
    archive: Path, atlas_root: Path
) -> None:
    from datetime import date

    before = _files(archive)
    plan = plan_redact(archive, "Dr X")
    sha = apply_redact(archive, "Dr X", plan, date(2026, 11, 25))
    after = _files(archive)

    assert set(plan.delete).isdisjoint(after)
    assert sorted(set(before) - set(after)) == sorted(plan.delete)
    old = before[OWNER_PAGE].decode()
    new = after[OWNER_PAGE].decode()
    cut = '<script type="application/json" id="data">'
    assert old[: old.index(cut)] == new[: new.index(cut)]
    assert old[old.index("</script>") :] == new[new.index("</script>") :]
    data = _data(archive, OWNER_PAGE)
    expected = _owner_data()
    # Dr X's entries are redacted in every structural field, with their decision kept
    gone = {"name": REMOVED, "note": ""}
    assert data["second_opinions"][G][0] == {**_entry("Dr X"), **gone}  # type: ignore[index]
    assert data["second_opinions"][G][1] == expected["second_opinions"][G][1]  # type: ignore[index]
    assert data["second_opinions"][J][0] == {**_entry(" dr x "), **gone}  # type: ignore[index]
    held = data["triage_earlier"]
    assert held["clinicians"][0] == {**_entry("Dr X"), **gone}  # type: ignore[index]
    assert held["clinicians"][1:] == expected["triage_earlier"]["clinicians"][1:]  # type: ignore[index]
    assert held["flips"][0] == {**_flip("Dr X", "second_opinion"), **gone}  # type: ignore[index]
    assert held["flips"][1:] == expected["triage_earlier"]["flips"][1:]  # type: ignore[index]
    assert held["owner"] == expected["triage_earlier"]["owner"]  # type: ignore[index]
    assert data["not_on_page"][0] == {  # type: ignore[index]
        **_entry("Dr X", "second_opinion"),
        **gone,
        "id": "CHDA:LIT:0000999",
        "where": "elsewhere",
    }
    assert data["not_on_page"][1:] == expected["not_on_page"][1:]  # type: ignore[index]
    assert data["earlier"] == expected["earlier"]

    pattern = name_pattern("Dr X")
    assert [p for p, b in after.items() if pattern.search(b.decode("utf-8"))] == []
    assert after["README.md"].decode().splitlines()[-1] == (
        "- 2026-11-25: one reviewer removed: 3 files deleted, their name and notes redacted "
        "from 1 owner page."
    )
    assert "None yet." not in after["README.md"].decode()
    assert git(archive, "log", "-1", "--format=%s").strip() == "redact a removed reviewer"
    assert git(archive, "rev-parse", "--short", "HEAD").strip() == sha
    assert git(archive, "status", "--porcelain") == ""
    paths = (archive / ".git" / "redact-paths.txt").read_text(encoding="utf-8")
    assert paths.splitlines() == [*plan.delete, OWNER_PAGE]  # the owner page's old blobs hold notes
    assert (archive / ".git" / "redact-expressions.txt").read_text(encoding="utf-8") == (
        "regex:(?i)(?<!\\w)Dr\\ X(?!\\w)==>a removed reviewer\n"
    )
    # the reader still reads the archive: the owner page is found by fields redaction leaves
    view = read_archive(archive, "TBX5", atlas_root)
    assert {v.name for v in view.verdicts} == {"Dr Y", "E.A.M"}
    assert state(view, {G: D1}, {})["triage"]["flips"] == []


def test_a_name_in_an_owners_note_stops_apply_until_edited(archive: Path, tmp_path: Path) -> None:
    from datetime import date

    later = "2026-11-21T10:00:00+00:00"
    put(
        archive,
        tmp_path,
        "E.A.M",
        "owner",
        later,
        [finding(J, "fix", D1, later, "agree with dr x")],
    )
    commit_all(archive)
    head = git(archive, "rev-parse", "HEAD")
    plan = plan_redact(archive, "Dr X")
    assert plan.elsewhere == (f"{RUN}/decisions/{compact_utc(later)}--e-a-m.json",)
    with pytest.raises(ArchiveError, match="still found in files redaction does not edit"):
        apply_redact(archive, "Dr X", plan, date(2026, 11, 25))
    result = CliRunner().invoke(
        app, ["archive-redact", "--name", "Dr X", "--archive", str(archive), "--apply"]
    )
    assert result.exit_code == 2
    assert git(archive, "status", "--porcelain") == ""
    assert git(archive, "rev-parse", "HEAD") == head
    assert (archive / X_PAGE).exists()


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("about", False),
        ("AB said", True),
        ("ab.", True),
        ("(AB)", True),
        ("CAB", False),
        ("ABC", False),
    ],
)
def test_initials_match_on_word_boundaries_only(text: str, found: bool) -> None:
    assert bool(name_pattern("AB").search(text)) is found
    assert bool(name_pattern(" AB ").search(text)) is found


def test_one_clinician_written_two_ways_is_redacted_whole(archive: Path, tmp_path: Path) -> None:
    later = "2026-11-22T10:00:00+00:00"
    put(archive, tmp_path, " dr x", "second_opinion", later, [finding(J, "fix", D1, later)])
    commit_all(archive)
    plan = plan_redact(archive, "Dr X")
    assert f"{RUN}/decisions/{compact_utc(later)}--dr-x.json" in plan.delete
    assert X_DECISIONS in plan.delete
    other = plan_redact(archive, "DR X ")
    assert other == plan


def test_a_failed_rescan_restores_every_touched_path(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = git(archive, "rev-parse", "HEAD")
    monkeypatch.setattr(redact, "_rescan", lambda top, name: ["somewhere.txt"])
    result = CliRunner().invoke(
        app, ["archive-redact", "--name", "Dr X", "--archive", str(archive), "--apply"]
    )
    assert result.exit_code == 1, result.output
    assert git(archive, "status", "--porcelain") == ""
    assert git(archive, "rev-parse", "HEAD") == head
    assert not (archive / ".git" / "redact-paths.txt").exists()
    with pytest.raises(GitError):
        from datetime import date

        apply_redact(archive, "Dr X", plan_redact(archive, "Dr X"), date(2026, 11, 25))


@pytest.mark.parametrize("installed", [True, False])
def test_apply_prints_the_procedure_the_copies_to_delete_and_the_republish_lines(
    archive: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    monkeypatch.setattr(
        redact.shutil, "which", lambda cmd: "/bin/git-filter-repo" if installed else None
    )
    result = CliRunner().invoke(
        app, ["archive-redact", "--name", "Dr X", "--archive", str(archive), "--apply"]
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    d, rw = str(archive), f"{archive}-rewrite"
    hint = "git filter-repo is not on this PATH: uv tool install git-filter-repo"
    assert (hint in lines) is not installed
    assert (
        "History still holds the name and the notes. On the owner's word, in a fresh clone:"
        in lines
    )
    assert not any("unmeasured until" in line or "not installed here" in line for line in lines)
    assert f"  git clone --no-local {d} {rw}" in lines
    # the owner page's original blobs hold the notes: it leaves history, then returns redacted
    assert f"  mkdir -p {rw}/{RUN}/pages && cp {d}/{OWNER_PAGE} {rw}/{OWNER_PAGE}" in lines
    assert f"  git -C {rw} add -- {OWNER_PAGE}" in lines
    assert (
        f'  git -C {rw} -c user.name="$(git -C {d} config --get user.name)" '
        f'-c user.email="$(git -C {d} config --get user.email)" '
        "commit -m 'file the redacted owner pages again'" in lines
    )
    order = [
        next(i for i, line in enumerate(lines) if marker in line)
        for marker in (
            "filter-repo --replace-text",
            " add -- ",
            "commit -m",
            "must print 0",
            "push --force",
        )
    ]
    assert order == sorted(order)
    assert f"  git -C {d} reflog expire --expire=now --all && git -C {d} gc --prune=now" in lines
    assert any(
        line.startswith(f"  git -C {d} log --all --reflog -p | grep -ciP")
        and line.endswith("must print 0")
        for line in lines
    )
    assert sum("must print 0" in line for line in lines) == 2
    copies = [line for line in lines if line.startswith("Delete by hand the copies outside")]
    assert len(copies) == 1
    for kind in (
        "local/literature/runs/<SYMBOL>/<run>/review-dr-x.html",  # the clinician's page
        "the file they sent back",
        "the owner's own review pages built while their verdicts were in the archive",
        "local/literature/runs/<SYMBOL>/<run>/review.html",
        "any store-export directories",
    ):
        assert kind in copies[0]
    assert (
        "GitHub may keep the force-pushed-over commits reachable by id until GitHub Support "
        "removes them (unmeasured)." in lines
    )
    assert f"republish redacted: {URL_OWNER} from {OWNER_PAGE}" in lines
    assert f"unpublish: {URL_X}" in lines


def test_a_rewritten_owner_page_with_no_artifact_says_so(archive: Path) -> None:
    (archive / OWNER_PAGE).with_suffix(".artifact").unlink()
    git(archive, "add", "-A", "--", RUN)
    git(archive, "commit", "-m", "drop the artifact")
    result = CliRunner().invoke(
        app, ["archive-redact", "--name", "Dr X", "--archive", str(archive), "--apply"]
    )
    assert result.exit_code == 0, result.output
    assert f"rewritten, no claude.ai copy recorded: {OWNER_PAGE}" in result.output.splitlines()
    assert "republish redacted" not in result.output


CLEAR = {
    "--skip-worktree": "--no-skip-worktree",
    "--assume-unchanged": "--no-assume-unchanged",
}


def _printed_commands(output: str) -> list[list[str]]:
    """Every `git -C ... update-index ...` line of a refusal, split as a shell would."""
    return [
        shlex.split(line)
        for line in output.splitlines()
        if line.strip().startswith("git -C") and " update-index " in line
    ]


@pytest.mark.parametrize(
    "bits",
    [["--skip-worktree"], ["--assume-unchanged"], ["--skip-worktree", "--assume-unchanged"]],
    ids=["skip", "assume", "both"],
)
@pytest.mark.parametrize("apply", [False, True])
def test_a_path_git_hides_refuses_the_removal_and_the_printed_commands_clear_it(
    archive: Path, bits: list[str], apply: bool
) -> None:
    """R-P4-19: with a bit set, deleting the working copy leaves `git status` clean while HEAD
    still holds the file, so the plan would list nothing for it. R-P4-21: the printed commands,
    run as printed, clear exactly the bits set (one `--no-skip-worktree --no-assume-unchanged`
    command clears only the assume-unchanged bit, measured on git 2.34.1)."""
    for bit in bits:
        git(archive, "update-index", bit, "--", X_DECISIONS)
    (archive / X_DECISIONS).unlink()
    head = git(archive, "rev-parse", "HEAD")
    args = ["archive-redact", "--name", "Dr X", "--archive", str(archive)]
    result = CliRunner().invoke(app, args + (["--apply"] if apply else []))
    assert result.exit_code == 2, result.output
    assert X_DECISIONS in result.output
    assert git(archive, "rev-parse", "HEAD") == head
    assert not (archive / ".git" / "redact-paths.txt").exists()
    commands = _printed_commands(result.output)
    expected = [
        ["git", "-C", str(archive), "update-index", CLEAR[bit], "--", X_DECISIONS] for bit in bits
    ]
    assert sorted(commands) == sorted(expected)
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)
    assert git(archive, "ls-files", "-v", "--", X_DECISIONS).startswith("H ")
    again = CliRunner().invoke(app, args)
    assert again.exit_code == 0, again.output


def test_an_archive_with_no_readme_is_refused_as_a_missing_removals_line_is(archive: Path) -> None:
    git(archive, "rm", "-q", "--", "README.md")
    git(archive, "commit", "-m", "no readme")
    head = git(archive, "rev-parse", "HEAD")
    result = CliRunner().invoke(
        app, ["archive-redact", "--name", "Dr X", "--archive", str(archive), "--apply"]
    )
    assert result.exit_code == 2, result.output
    assert "README.md" in result.output
    assert git(archive, "rev-parse", "HEAD") == head and git(archive, "status", "--porcelain") == ""
    assert (archive / X_PAGE).exists()


def test_a_file_the_scan_cannot_read_is_refused_naming_it(archive: Path) -> None:
    stray = archive / RUN / "notes.txt"
    stray.write_text("nothing\n", encoding="utf-8")
    stray.chmod(0)
    try:
        result = CliRunner().invoke(
            app, ["archive-redact", "--name", "Dr X", "--archive", str(archive)]
        )
    finally:
        stray.chmod(0o644)
    assert result.exit_code == 2, result.output
    assert f"{RUN}/notes.txt: cannot be read" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("kept", "files", "pages"),
    [
        (1, "1 file", "1 owner page"),
        (3, "3 files", "2 owner pages"),
        (0, "0 files", "0 owner pages"),
    ],
)
def test_the_readme_line_counts_with_the_right_singulars(kept: int, files: str, pages: str) -> None:
    text = redact._readme_with_removal(
        "## Removals\n\nNone yet.\n", date(2026, 11, 25), kept, int(pages[0])
    )
    assert (
        f"- 2026-11-25: one reviewer removed: {files} deleted, their name and notes redacted "
        f"from {pages}." in text.splitlines()
    )


@pytest.mark.parametrize("readme", ["# R\n", "# R\n\n## Removals\n\n## Removals\n\nNone yet.\n"])
def test_apply_refuses_a_readme_without_exactly_one_removals_heading(
    archive: Path, readme: str
) -> None:
    from datetime import date

    (archive / "README.md").write_text(readme, encoding="utf-8")
    git(archive, "add", "--", "README.md")
    git(archive, "commit", "-m", "readme")
    head = git(archive, "rev-parse", "HEAD")
    with pytest.raises(ArchiveError, match="Removals"):
        apply_redact(archive, "Dr X", plan_redact(archive, "Dr X"), date(2026, 11, 25))
    assert git(archive, "status", "--porcelain") == ""
    assert git(archive, "rev-parse", "HEAD") == head


def test_a_second_removal_appends_after_the_first_entry(archive: Path, tmp_path: Path) -> None:
    from datetime import date

    apply_redact(archive, "Dr X", plan_redact(archive, "Dr X"), date(2026, 11, 25))
    for left in (archive / ".git").glob("redact-*.txt"):  # the procedure's last step
        left.unlink()
    put(archive, tmp_path, "Dr Z", "second_opinion", AT, [finding(G, "fix", D1, AT)])
    commit_all(archive)
    apply_redact(archive, "Dr Z", plan_redact(archive, "Dr Z"), date(2026, 11, 26))
    tail = (archive / "README.md").read_text(encoding="utf-8").splitlines()[-2:]
    assert tail[0].startswith("- 2026-11-25:") and tail[1].startswith("- 2026-11-26:")


def test_the_owner_and_an_unknown_name_and_an_unreadable_owner_page_are_refused(
    archive: Path,
) -> None:
    with pytest.raises(ArchiveError, match="owner"):
        plan_redact(archive, "Owner")
    with pytest.raises(ArchiveError, match="no file names this reviewer"):
        from datetime import date

        apply_redact(archive, "Dr W", plan_redact(archive, "Dr W"), date(2026, 11, 25))
    page = archive / OWNER_PAGE
    page.write_text(page.read_text(encoding="utf-8").replace('id="data"', 'id="d"'), "utf-8")
    with pytest.raises(ArchiveError, match=r"e-a-m\.html"):
        plan_redact(archive, "Dr X")


def test_redacted_page_leaves_a_page_naming_nobody_byte_identical(tmp_path: Path) -> None:
    text = page_html(tmp_path / "p.html", extra=_owner_data()).read_text(encoding="utf-8")
    assert redacted_page(text, "dr w") == text
    assert json.dumps(page_data_from_text(redacted_page(text, "dr y"), "p"))  # still parses


def _spy_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(top: Path, name: str) -> tuple[str, ...]:
        raise AssertionError("the archive was changed before the refusal")

    monkeypatch.setattr(redact, "_rescan", boom)


def _unchanged(archive: Path, head: str) -> None:
    assert git(archive, "status", "--porcelain") == ""
    assert git(archive, "rev-parse", "HEAD") == head
    assert (archive / X_PAGE).exists()


def test_a_second_removal_is_refused_while_the_first_ones_rewrite_files_remain(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_redact(archive, "Dr X", plan_redact(archive, "Dr X"), date(2026, 11, 25))
    before = {
        f.name: f.read_text(encoding="utf-8") for f in (archive / ".git").glob("redact-*.txt")
    }
    put(archive, tmp_path, "Dr Z", "second_opinion", AT, [finding(G, "fix", D1, AT)])
    commit_all(archive)
    head = git(archive, "rev-parse", "HEAD")
    _spy_rescan(monkeypatch)
    with pytest.raises(ArchiveError, match="earlier removal"):
        apply_redact(archive, "Dr Z", plan_redact(archive, "Dr Z"), date(2026, 11, 26))
    assert git(archive, "status", "--porcelain") == ""
    assert git(archive, "rev-parse", "HEAD") == head
    after = {f.name: f.read_text(encoding="utf-8") for f in (archive / ".git").glob("redact-*.txt")}
    assert after == before and set(before) == {"redact-expressions.txt", "redact-paths.txt"}
    assert (archive / RUN / "decisions" / f"{compact_utc(AT)}--dr-z.json").exists()


def test_an_owner_entry_carrying_the_clinicians_name_is_never_redacted_or_deleted(
    archive: Path, tmp_path: Path
) -> None:
    data = _owner_data()
    data["not_on_page"].append({**_entry("Dr X", "owner"), "id": "CHDA:LIT:0000998"})  # type: ignore[attr-defined]
    data["triage_earlier"]["flips"].append(_flip("Dr X", "owner"))  # type: ignore[index]
    text = page_html(tmp_path / "p.html", extra=data).read_text(encoding="utf-8")
    new = page_data_from_text(redacted_page(text, "dr x"), "p")
    assert new["not_on_page"][2] == data["not_on_page"][2]  # type: ignore[index]
    assert new["triage_earlier"]["flips"][3] == data["triage_earlier"]["flips"][3]  # type: ignore[index]
    assert new["not_on_page"][0]["name"] == REMOVED  # type: ignore[index]
    assert new["triage_earlier"]["flips"][0]["name"] == REMOVED  # type: ignore[index]
    # an owner's decisions file whose reviewer is named like the clinician is kept and reported
    later = "2026-11-23T10:00:00+00:00"
    put(archive, tmp_path, "Dr X", "owner", later, [finding(J, "fix", D1, later)])
    commit_all(archive)
    owner_file = f"{RUN}/decisions/{compact_utc(later)}--dr-x.json"
    plan = plan_redact(archive, "Dr X")
    assert owner_file not in plan.delete
    assert owner_file in plan.elsewhere


def test_a_missing_identity_is_refused_before_anything_is_changed(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = git(archive, "rev-parse", "HEAD")
    git(archive, "config", "--local", "--unset", "user.name")
    plan = plan_redact(archive, "Dr X")
    _spy_rescan(monkeypatch)
    with pytest.raises(ArchiveError, match="user.name"):
        apply_redact(archive, "Dr X", plan, date(2026, 11, 25))
    _unchanged(archive, head)


def test_a_stale_plan_is_refused_before_anything_is_changed(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_redact(archive, "Dr X")
    later = "2026-11-24T10:00:00+00:00"
    put(archive, tmp_path, "Dr X", "second_opinion", later, [finding(J, "fix", D1, later)])
    commit_all(archive)
    head = git(archive, "rev-parse", "HEAD")
    _spy_rescan(monkeypatch)
    with pytest.raises(ArchiveError, match="stale"):
        apply_redact(archive, "Dr X", plan, date(2026, 11, 25))
    _unchanged(archive, head)


def test_the_name_in_the_readme_stops_apply(archive: Path) -> None:
    (archive / "README.md").write_text("# R\n\n## Removals\n\nthanks to Dr X\n", "utf-8")
    git(archive, "add", "--", "README.md")
    git(archive, "commit", "-m", "readme")
    assert plan_redact(archive, "Dr X").elsewhere == ("README.md",)


def test_a_name_stored_as_escapes_is_still_found(archive: Path, tmp_path: Path) -> None:
    later = "2026-11-21T10:00:00+00:00"
    put(
        archive,
        tmp_path,
        "E.A.M",
        "owner",
        later,
        [finding(J, "fix", D1, later, "ask dr zo\u00eb")],
    )
    commit_all(archive)
    raw = (archive / RUN / "decisions" / f"{compact_utc(later)}--e-a-m.json").read_text("utf-8")
    assert "zo\\u00eb" in raw and "zoë" not in raw  # the file really holds an escape
    assert plan_redact(archive, "Dr Zoë").elsewhere == (
        f"{RUN}/decisions/{compact_utc(later)}--e-a-m.json",
    )


@pytest.mark.parametrize(
    ("leftover", "pattern_text"),
    [
        ("README.md", "thanks Dr X"),
        (f"{RUN}/pages/other.html", "<p>Dr X</p>"),
        (f"{RUN}/decisions/other.json", '{"n": "dr \\u0058"}'),
    ],
)
def test_the_real_rescan_reads_every_kind_of_file(
    tmp_path: Path, leftover: str, pattern_text: str
) -> None:
    clean = make_archive(tmp_path)
    assert redact._rescan(clean, "Dr X") == ()
    (clean / leftover).parent.mkdir(parents=True, exist_ok=True)
    (clean / leftover).write_text(pattern_text, encoding="utf-8")
    assert redact._rescan(clean, "Dr X") == (leftover,)


def test_a_name_derived_slug_in_a_path_stops_apply_but_only_as_a_whole_token(
    archive: Path, tmp_path: Path
) -> None:
    pages = archive / RUN / "pages"
    (pages / "20261120T000000Z--dr-x.artifact").write_text("https://claude.ai/a\n", "utf-8")
    (archive / RUN / "notes--dr-x.txt").write_text("nothing\n", "utf-8")
    (archive / RUN / "notes--dr-xavier.txt").write_text("nothing\n", "utf-8")
    later = "2026-11-25T10:00:00+00:00"
    put(archive, tmp_path, "Dr. X", "second_opinion", later, [finding(J, "fix", D1, later)])
    commit_all(archive)
    plan = plan_redact(archive, "Dr X")
    assert plan.elsewhere == (
        f"{RUN}/decisions/{compact_utc(later)}--dr-x.json",
        f"{RUN}/notes--dr-x.txt",
        f"{RUN}/pages/20261120T000000Z--dr-x.artifact",
    )


def test_a_failed_write_restores_every_touched_path(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = git(archive, "rev-parse", "HEAD")

    def broken(path: Path, content: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(redact, "write_bytes_atomically", broken)
    with pytest.raises(GitError, match="disk full"):
        apply_redact(archive, "Dr X", plan_redact(archive, "Dr X"), date(2026, 11, 25))
    _unchanged(archive, head)
    assert not (archive / ".git" / "redact-paths.txt").exists()


def test_a_failing_commit_restores_every_touched_path_index_included(archive: Path) -> None:
    head = git(archive, "rev-parse", "HEAD")
    hook = archive / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    with pytest.raises(GitError, match="commit failed"):
        apply_redact(archive, "Dr X", plan_redact(archive, "Dr X"), date(2026, 11, 25))
    _unchanged(archive, head)
    assert not (archive / ".git" / "redact-paths.txt").exists()


def test_a_rescan_that_cannot_read_a_file_restores_every_touched_path(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = git(archive, "rev-parse", "HEAD")

    def unreadable(top: Path, name: str) -> tuple[str, ...]:
        raise ArchiveError("x.txt: cannot be read")

    monkeypatch.setattr(redact, "_rescan", unreadable)
    with pytest.raises(GitError, match="rescan the archive: x.txt: cannot be read"):
        apply_redact(archive, "Dr X", plan_redact(archive, "Dr X"), date(2026, 11, 25))
    _unchanged(archive, head)
    assert not (archive / ".git" / "redact-paths.txt").exists()


def test_the_owners_own_name_deletes_and_rewrites_nothing_and_stops_apply(archive: Path) -> None:
    """README: `--name <the owner's own name>` finds the owner's files as files that still name
    the reviewer, deletes and rewrites nothing, and `--apply` refuses it; `--name owner` is
    refused."""
    plan = plan_redact(archive, "E.A.M")
    assert plan.delete == () and plan.rewrite == ()
    assert OWNER_PAGE in plan.elsewhere
    head = git(archive, "rev-parse", "HEAD")
    result = CliRunner().invoke(
        app, ["archive-redact", "--name", "E.A.M", "--archive", str(archive), "--apply"]
    )
    assert result.exit_code == 2 and "still found in files redaction does not edit" in result.output
    _unchanged(archive, head)
    refused = CliRunner().invoke(
        app, ["archive-redact", "--name", "owner", "--archive", str(archive)]
    )
    assert (
        refused.exit_code == 2
        and "the owner is not a reviewer that can be removed" in refused.output
    )


@pytest.mark.parametrize("other", ["Dr. X", "Dr X Smith"])
def test_a_name_written_another_way_stops_apply_until_those_files_are_edited_by_hand(
    archive: Path, tmp_path: Path, other: str
) -> None:
    later = "2026-11-25T10:00:00+00:00"
    put(archive, tmp_path, other, "second_opinion", later, [finding(J, "fix", D1, later)])
    commit_all(archive)
    plan = plan_redact(archive, "Dr X")
    assert (
        f"{RUN}/decisions/{compact_utc(later)}--{'dr-x' if other == 'Dr. X' else 'dr-x-smith'}.json"
        in plan.elsewhere
    )
    assert X_DECISIONS in plan.delete  # the exact key is still removed
    result = CliRunner().invoke(
        app, ["archive-redact", "--name", "Dr X", "--archive", str(archive), "--apply"]
    )
    assert result.exit_code == 2 and "still found in files redaction does not edit" in result.output
    # a longer name must not be edited as if it were this reviewer's file
    assert "is yours to edit or delete" in result.output
    assert (
        'another reviewer whose name contains this one (such as "Dr X Smith") must not be'
        in result.output
    )


def test_the_printed_reflog_recheck_sees_the_name_before_the_printed_expiry_and_not_after(
    tmp_path: Path,
) -> None:
    """R-P4-21: `git log --all -g -p` prints nothing before the expiry (measured on git 2.34.1),
    so a re-check written that way printed 0 whether or not the name was still in the reflog. Run
    the printed lines on a repository (one that never held the name otherwise, as a rewritten
    archive does not) whose reflog holds a commit naming the reviewer."""
    archive = make_archive(tmp_path)
    (archive / "TBX5" / "r1").mkdir(parents=True)
    (archive / "TBX5" / "r1" / "spill.txt").write_text("Dr X said so\n", encoding="utf-8")
    git(archive, "add", "--", "TBX5/r1/spill.txt")
    git(archive, "commit", "-m", "names the reviewer")
    git(archive, "rm", "-q", "--", "TBX5/r1/spill.txt")
    git(archive, "commit", "-m", "removes it")
    git(archive, "reset", "--hard", "HEAD~2")
    lines = redact.procedure(archive, "Dr X", redact.RedactPlan((), (), (), (), ()))
    (recheck,) = [line for line in lines if "log --all --reflog -p" in line]
    (expiry,) = [line for line in lines if "reflog expire" in line]
    command = recheck.replace("      must print 0", "")

    def count() -> int:
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        return int(done.stdout.strip())

    assert count() > 0  # the reflog still holds the name
    subprocess.run(["bash", "-c", expiry], check=True, capture_output=True)
    assert count() == 0
