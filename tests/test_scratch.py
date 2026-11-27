"""``scratch-root`` and ``export-state``: a copy every writing step runs in, and its state.

The guarantee under test is that a write in a scratch root never reaches the atlas. It is
checked by hashing every file of a fixture atlas (and its file list) before and after real
curator write paths run in the scratch root: ``merge`` (the literature file through
``write.py``, ``publications.yaml``, ``.id_registry.yaml``, and ``merge.json`` plus ``evidence/``
under the run folder), ``triage-apply`` is not needed because ``merge`` already sets triage.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chd_curator.cli import app
from chd_curator.scratch import MUTABLE
from run_fixture import copy_run_fixture
from test_merge import _pubmed

runner = CliRunner()
MUTABLE_TBX5 = tuple(m.format(symbol="TBX5") for m in MUTABLE)


def make_atlas(tmp_path: Path, atlas_root: Path, name: str = "atlas") -> Path:
    """A fixture atlas (the run fixture plus the real schemas) at ``tmp_path / name``."""
    holder = tmp_path / f"{name}-holder"
    holder.mkdir()
    p = copy_run_fixture(holder)
    shutil.copytree(atlas_root / "schemas", p.root / "schemas")
    final = tmp_path / name
    os.rename(p.root, final)
    return final


def snapshot(root: Path) -> dict[str, str]:
    """Relative path -> sha256 for every file under `root` (links followed)."""
    return {
        str(f.relative_to(root)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(root.rglob("*"))
        if f.is_file()
    }


def scratch(atlas: Path, dest: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["scratch-root", "--atlas", str(atlas), "--dest", str(dest), "--gene", "TBX5", *args],
        catch_exceptions=False,
    )
    return result.exit_code, result.output


def merge(root: Path, stub: Path) -> None:
    result = runner.invoke(
        app,
        ["merge", "TBX5", "--run", "r1", "--root", str(root), "--atlas-cli", str(stub)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


def test_the_scratch_root_links_everything_but_the_three_files(
    tmp_path: Path, atlas_root: Path
) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    (atlas / "curation/.id_registry.yaml").unlink()  # an absent file stays absent
    code, _ = scratch(atlas, tmp_path / "work")
    assert code == 0
    work = tmp_path / "work"
    for top in ("schemas", "ontologies"):  # one link each, to the resolved path
        assert (work / top).is_symlink()
        assert (work / top).resolve() == (atlas / top).resolve()
    for top in ("curation", "local"):  # real directories all the way down
        for d in [work / top, *(p for p in (work / top).rglob("*") if p.is_dir())]:
            assert d.is_dir() and not d.is_symlink(), d
    real = {str(f.relative_to(work)) for f in work.rglob("*") if f.is_file() and not f.is_symlink()}
    assert real == {m for m in MUTABLE_TBX5 if (atlas / m).is_file()}
    assert not (work / "curation/.id_registry.yaml").exists()
    assert not (work / "curation/.id_registry.yaml").is_symlink()
    for rel in snapshot(atlas):
        if rel not in MUTABLE_TBX5:
            top = rel.split("/")[0]
            if top in ("curation", "local"):  # a link per file
                assert (work / rel).is_symlink(), rel
                assert (work / rel).resolve() == (atlas / rel).resolve(), rel
            else:  # the one link at the top
                assert (work / top).is_symlink(), rel
        elif (atlas / rel).is_file():
            assert (work / rel).read_bytes() == (atlas / rel).read_bytes()
    code, out = scratch(atlas, work)  # an existing --dest is a usage error
    assert code == 2 and "already exists" in out


def test_a_write_in_the_scratch_root_never_reaches_the_atlas(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    before = snapshot(atlas)
    assert scratch(atlas, tmp_path / "work")[0] == 0
    _pubmed(monkeypatch)
    work = tmp_path / "work"
    merge(work, stub_atlas)
    after_in_scratch = snapshot(work)
    # The writes happened: the literature file, both other curated files and the run folder.
    for rel in MUTABLE_TBX5:
        assert after_in_scratch[rel] != before[rel], rel
    assert (work / "local/literature/runs/TBX5/r1/merge.json").is_file()
    assert (work / "local/literature/runs/TBX5/r1/evidence").is_dir()
    # A brand new file under a mirrored directory lands in the scratch root only.
    (work / "local/literature/runs/TBX5/r1/new.txt").write_text("x")
    assert snapshot(atlas) == before
    assert not (atlas / "local/literature/runs/TBX5/r1/new.txt").exists()


def test_export_state_then_scratch_root_round_trips_the_state(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    scratch(atlas, tmp_path / "work")
    _pubmed(monkeypatch)
    merge(tmp_path / "work", stub_atlas)
    result = runner.invoke(
        app,
        ["export-state", "--root", str(tmp_path / "work"), "--gene", "TBX5", "--run", "r1"]
        + ["--dest", str(tmp_path / "state")],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    state = snapshot(tmp_path / "state")
    exported_files = [f for f in (tmp_path / "state").rglob("*") if f.is_file()]
    assert exported_files and not any(f.is_symlink() for f in exported_files)
    assert set(MUTABLE_TBX5) <= set(state)
    assert "local/literature/runs/TBX5/r1/merge.json" in state
    assert "local/literature/texts/91000002/fulltext.txt" in state
    assert "local/literature/texts/91000002/supplements/s1.txt" in state
    before = snapshot(atlas)
    assert scratch(atlas, tmp_path / "next", "--state", str(tmp_path / "state"))[0] == 0
    again = snapshot(tmp_path / "next")
    for rel, digest in state.items():  # every exported byte reappears
        assert again[rel] == digest, rel
        assert not (tmp_path / "next" / rel).is_symlink(), rel
    assert again[MUTABLE_TBX5[0]] != before[MUTABLE_TBX5[0]]  # the state, not the atlas
    assert snapshot(atlas) == before
    # An existing --dest is refused.
    again_code = runner.invoke(
        app,
        ["export-state", "--root", str(tmp_path / "work"), "--gene", "TBX5", "--run", "r1"]
        + ["--dest", str(tmp_path / "state")],
    )
    assert again_code.exit_code == 2


def test_export_state_names_a_listed_text_that_is_missing(tmp_path: Path, atlas_root: Path) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    scratch(atlas, tmp_path / "work")
    gone = tmp_path / "work/local/literature/texts/91000002/fulltext.txt"
    gone.unlink()  # a link in the scratch root: the atlas's file stays
    assert (atlas / "local/literature/texts/91000002/fulltext.txt").is_file()
    result = runner.invoke(
        app,
        ["export-state", "--root", str(tmp_path / "work"), "--gene", "TBX5", "--run", "r1"]
        + ["--dest", str(tmp_path / "state")],
    )
    assert result.exit_code == 1
    assert "91000002/fulltext.txt" in result.output
    assert not (tmp_path / "state").exists() or not any((tmp_path / "state").rglob("*.txt"))


def _write_everywhere(work: Path) -> None:
    """New files and directories at several depths of a scratch root."""
    for rel in (
        "local/new-top.txt",
        "local/literature/new-mid.txt",
        "local/literature/runs/TBX5/r1/new-deep.txt",
        "local/literature/runs/TBX5/r2/sub/deeper/new.txt",
        "local/literature/texts/91000002/new-in-text.txt",
        "curation/new-curation.txt",
        "curation/literature/new-lit.txt",
    ):
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("new")


def _tree(root: Path) -> dict[str, str]:
    """Every entry's relative path (directories as '/') with file hashes, links followed."""
    out = {str(p.relative_to(root)) + "/": "" for p in root.rglob("*") if p.is_dir()}
    return {**out, **snapshot(root)}


@pytest.mark.parametrize("name", ["local", "curation"])
def test_a_top_level_directory_that_is_itself_a_link_is_mirrored_not_linked(
    tmp_path: Path, atlas_root: Path, name: str
) -> None:
    """An atlas worktree here has `local` as a link to the main checkout's `local/`."""
    atlas = make_atlas(tmp_path, atlas_root)
    elsewhere = tmp_path / "elsewhere" / name
    elsewhere.parent.mkdir()
    os.rename(atlas / name, elsewhere)
    (atlas / name).symlink_to(elsewhere)
    before = (_tree(atlas), _tree(elsewhere))
    assert scratch(atlas, tmp_path / "work")[0] == 0
    assert not (tmp_path / "work" / name).is_symlink()
    _write_everywhere(tmp_path / "work")
    assert (_tree(atlas), _tree(elsewhere)) == before


def test_a_directory_link_inside_a_mirrored_tree_is_mirrored_not_linked(
    tmp_path: Path, atlas_root: Path
) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    shared = tmp_path / "shared"
    (shared / "deep").mkdir(parents=True)
    (shared / "deep/kept.txt").write_text("kept")
    (atlas / "local/literature/shared").symlink_to(shared)
    before = (_tree(atlas), _tree(shared))
    assert scratch(atlas, tmp_path / "work")[0] == 0
    mirrored = tmp_path / "work/local/literature/shared"
    assert mirrored.is_dir() and not mirrored.is_symlink()
    assert (mirrored / "deep").is_dir() and not (mirrored / "deep").is_symlink()
    assert (mirrored / "deep/kept.txt").read_text() == "kept"
    (mirrored / "new.txt").write_text("new")
    (mirrored / "deep/sub").mkdir()
    (mirrored / "deep/sub/new.txt").write_text("new")
    _write_everywhere(tmp_path / "work")
    assert (_tree(atlas), _tree(shared)) == before


def test_a_directory_link_loop_is_refused(tmp_path: Path, atlas_root: Path) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    (atlas / "local/literature/loop").symlink_to(atlas / "local/literature")
    code, out = scratch(atlas, tmp_path / "work")
    assert code == 2 and "loop" in out


def test_writers_that_replace_a_file_link_never_write_through_it(
    tmp_path: Path, atlas_root: Path, stub_atlas: Path
) -> None:
    """The run folder already exists as links (no --state). `write_batches` rewrites `batches.json`
    (run.py's `_write_json`), and `write_evidence` an evidence file (`_write_yaml`), each
    through a link to a file of the atlas."""
    from chd_curator.run import Quote, RunPaths, load_batches, write_batches, write_evidence

    atlas = make_atlas(tmp_path, atlas_root)
    run = atlas / "local/literature/runs/TBX5/r1"
    (run / "evidence").mkdir()
    (run / "evidence/batch-01.yaml").write_text("CHDA:LIT:0009001: []\n")
    before = _tree(atlas)
    work = tmp_path / "work"
    assert scratch(atlas, work)[0] == 0
    wrun = work / "local/literature/runs/TBX5/r1"
    for linked in ("batches.json", "evidence/batch-01.yaml"):
        assert (wrun / linked).is_symlink()
    paths = RunPaths(work, "TBX5", "r1")
    plan = load_batches(paths)
    write_batches(paths, replace(plan, batches=plan.batches[:1]))
    assert not (wrun / "batches.json").is_symlink()
    assert len(load_batches(paths).batches) == 1
    write_evidence(
        RunPaths(work, "TBX5", "r1"), "01", {"CHDA:LIT:0009002": [Quote("f", "q ü", "w")]}
    )
    assert (wrun / "evidence/batch-01.yaml").read_text() != "CHDA:LIT:0009001: []\n"
    assert not (wrun / "evidence/batch-01.yaml").is_symlink()
    assert _tree(atlas) == before


def test_review_page_replaces_a_linked_page_and_never_writes_through_it(
    tmp_path: Path, stub_atlas: Path, atlas_root: Path
) -> None:
    """The decisions fixture is a run that `review-page` can build; build it in the atlas
    first, so the scratch root holds `review.html` as a link."""
    from test_decisions import FIXTURE

    atlas = tmp_path / "atlas"
    shutil.copytree(FIXTURE, atlas, ignore=shutil.ignore_patterns("export"))
    shutil.copytree(atlas_root / "schemas", atlas / "schemas")

    def page(root: Path) -> None:
        result = runner.invoke(
            app,
            ["review-page", "TBX5", "--run", "r1", "--reviewer", "E.A.M", "--root", str(root)]
            + ["--atlas-cli", str(stub_atlas)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

    page(atlas)
    html = atlas / "local/literature/runs/TBX5/r1/review.html"
    html.write_text("<old page>")
    before = _tree(atlas)
    work = tmp_path / "work"
    assert scratch(atlas, work)[0] == 0
    assert (work / "local/literature/runs/TBX5/r1/review.html").is_symlink()
    page(work)
    written = work / "local/literature/runs/TBX5/r1/review.html"
    assert not written.is_symlink() and written.read_text() != "<old page>"
    assert _tree(atlas) == before


# ---- the fix wave: states, export and what the root mirrors -----------------------------------


def export(work: Path, dest: Path, run: str = "r1") -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["export-state", "--root", str(work), "--gene", "TBX5", "--run", run, "--dest", str(dest)],
        catch_exceptions=False,
    )
    return result.exit_code, result.output


def test_export_state_without_a_run_folder_exports_the_three_files_and_round_trips(
    tmp_path: Path, atlas_root: Path
) -> None:
    """R-SWA-42: SEARCH, TRIAGE_REQUESTS and TRIAGE_APPLY run before TEXTS, with no run folder;
    their only state is the three files."""
    atlas = make_atlas(tmp_path, atlas_root)
    work = tmp_path / "work"
    assert scratch(atlas, work)[0] == 0
    (work / MUTABLE_TBX5[0]).write_text((work / MUTABLE_TBX5[0]).read_text() + "# searched\n")
    code, out = export(work, tmp_path / "state", run="r9")
    assert code == 0, out
    assert sorted(snapshot(tmp_path / "state")) == sorted(MUTABLE_TBX5)
    assert scratch(atlas, tmp_path / "next", "--state", str(tmp_path / "state"))[0] == 0
    assert (tmp_path / "next" / MUTABLE_TBX5[0]).read_text().endswith("# searched\n")


def test_export_state_with_a_run_folder_but_no_texts_json_exports_the_folder(
    tmp_path: Path, atlas_root: Path
) -> None:
    atlas = make_atlas(tmp_path, atlas_root)
    work = tmp_path / "work"
    assert scratch(atlas, work)[0] == 0
    run = work / "local/literature/runs/TBX5/r9"
    run.mkdir()
    (run / "triage.requests.jsonl").write_text("{}\n")
    code, out = export(work, tmp_path / "state", run="r9")
    assert code == 0, out
    assert sorted(snapshot(tmp_path / "state")) == sorted(
        [*MUTABLE_TBX5, "local/literature/runs/TBX5/r9/triage.requests.jsonl"]
    )
    assert scratch(atlas, tmp_path / "next", "--state", str(tmp_path / "state"))[0] == 0
    assert (tmp_path / "next/local/literature/runs/TBX5/r9/triage.requests.jsonl").is_file()


@pytest.mark.parametrize(
    "stray",
    ["curation/stray.yaml", "local/literature/stray.txt", "local/literature/runs/TBX5/r2/x.json"],
)
def test_export_state_refuses_a_written_file_it_would_not_export(
    tmp_path: Path, atlas_root: Path, stray: str
) -> None:
    """M7: a real file under curation/ or local/ that is not one of the three files, under the
    run folder or a listed text would be lost between steps; refuse, naming it."""
    atlas = make_atlas(tmp_path, atlas_root)
    work = tmp_path / "work"
    assert scratch(atlas, work)[0] == 0
    (work / stray).parent.mkdir(parents=True, exist_ok=True)
    (work / stray).write_text("x")
    code, out = export(work, tmp_path / "state")
    assert code == 1 and stray in out
    assert not (tmp_path / "state").exists()


def test_a_state_lacking_a_mutable_file_the_atlas_has_is_refused(
    tmp_path: Path, atlas_root: Path
) -> None:
    """M6: the root would silently lack the file, and integrate would then delete nothing but
    propose nothing either; refuse at once, naming it."""
    atlas = make_atlas(tmp_path, atlas_root)
    work = tmp_path / "work"
    assert scratch(atlas, work)[0] == 0
    assert export(work, tmp_path / "state")[0] == 0
    (tmp_path / "state/curation/.id_registry.yaml").unlink()
    code, out = scratch(atlas, tmp_path / "next", "--state", str(tmp_path / "state"))
    assert code == 1 and "curation/.id_registry.yaml" in out
    assert not (tmp_path / "next").exists()


def _two_states(tmp_path: Path, atlas_root: Path) -> tuple[Path, Path, Path]:
    """Two states of one run, as EXTRACT and BLIND_READ leave them: each a batch file the
    other lacks, everything else byte-identical."""
    atlas = make_atlas(tmp_path, atlas_root)
    states = []
    for name, folder in (("a", "extract"), ("b", "verify")):
        work = tmp_path / f"work-{name}"
        assert scratch(atlas, work)[0] == 0
        target = work / f"local/literature/runs/TBX5/r1/{folder}/batch-09.findings.yaml"
        target.write_text(f"findings: []  # {name}\n")
        assert export(work, tmp_path / f"state-{name}")[0] == 0
        states.append(tmp_path / f"state-{name}")
    return atlas, states[0], states[1]


def test_scratch_root_lays_the_union_of_several_states(tmp_path: Path, atlas_root: Path) -> None:
    """R-SWA-43: MERGE needs the union of the fan-out's states."""
    atlas, a, b = _two_states(tmp_path, atlas_root)
    code, out = scratch(atlas, tmp_path / "next", "--state", str(a), "--state", str(b))
    assert code == 0, out
    run = tmp_path / "next/local/literature/runs/TBX5/r1"
    assert (run / "extract/batch-09.findings.yaml").read_text() == "findings: []  # a\n"
    assert (run / "verify/batch-09.findings.yaml").read_text() == "findings: []  # b\n"


@pytest.mark.parametrize(
    "rel", ["curation/literature/TBX5.yaml", "local/literature/runs/TBX5/r1/batches.json"]
)
def test_two_states_that_differ_on_a_file_are_refused_naming_it(
    tmp_path: Path, atlas_root: Path, rel: str
) -> None:
    atlas, a, b = _two_states(tmp_path, atlas_root)
    (b / rel).write_text((b / rel).read_text() + "\n")
    code, out = scratch(atlas, tmp_path / "next", "--state", str(a), "--state", str(b))
    assert code == 1 and rel in out
    assert not (tmp_path / "next").exists()


def test_the_root_mirrors_only_local_literature(tmp_path: Path, atlas_root: Path) -> None:
    """M11: every gene's runs live under local/literature, and nothing else of local/ (data,
    packages, ingest) is any step's input."""
    atlas = make_atlas(tmp_path, atlas_root)
    (atlas / "local/data").mkdir()
    (atlas / "local/data/big.tsv").write_text("x")
    (atlas / "local/top.txt").write_text("x")
    assert scratch(atlas, tmp_path / "work")[0] == 0
    local = tmp_path / "work/local"
    assert local.is_dir() and not local.is_symlink()
    assert sorted(p.name for p in local.iterdir()) == ["literature"]
    assert (local / "literature/runs/TBX5/r1/batches.json").is_symlink()
