# curator

Literature curation tools for the [CHD atlas](https://github.com/chd-atlas/chd-atlas.github.io).
`chd-curator` searches PubMed for a published gene, records the hits, adds papers and compares a
blind second extraction with the first, and writes a drafted gene synthesis. It also checks a
run's quotes against their papers, builds the review page a person reads, and turns that page's
saved decisions into the file the atlas applies. Five Claude Code skills drive agents through the
extraction steps.
An agent never approves: a named person does, in the atlas, with `chd-atlas literature review` or
`chd-atlas literature apply-review`.

## Install

```bash
uv tool install git+https://github.com/chd-atlas/curator@v0.0.1
```

Then, in Claude Code:

```text
/plugin install curator --marketplace chd-atlas/curator
```

Claude Code's documentation gives 2.1.275 as the first version with `--marketplace` (not
measured here). On an older version, add the marketplace first:

```text
/plugin marketplace add chd-atlas/curator
/plugin install curator@curator
```

## Run

Run from the root of an atlas checkout. `search`, `abstracts`, `diff`, `normalise`, `synthesis`
and `review-page` ask the atlas about the gene by running its CLI, `<root>/.venv/bin/chd-atlas`
unless `--atlas-cli` or `CHD_ATLAS_CLI` names another; `add-publication`, `quotes` and
`review-decisions` do not. `search`, `add-publication`, `diff`, `normalise`, `synthesis`,
`review-page` and `review-decisions` read the JSON Schemas the atlas exports into
`<root>/schemas/`. The records curator writes into `curation/` are checked against them first, and
so is the decisions file `review-decisions` writes; `quotes.json` and `review.html` are not atlas
records and no schema checks them. curator never imports the atlas. The skills read the atlas checkout's
`curator/curator-rules.md`. Their hand-off commands, `.venv/bin/chd-atlas validate` and
`.venv/bin/chd-atlas literature review <SYMBOL>`, assume the atlas checkout's own `.venv`; adapt
them if your atlas executable is elsewhere.

For one published gene:

1. `literature-search` runs `chd-curator search <SYMBOL>` and records every hit.
2. `literature-triage` (a cheap model) marks each hit included or excluded in
   `curation/literature/<SYMBOL>.yaml`.
3. `literature-extract` (a mid-tier model) writes findings and adds each paper with
   `chd-curator add-publication`.
4. `literature-verify` (a different model, fresh context) extracts blind into
   `local/literature/<SYMBOL>.verify.yaml`; `chd-curator diff` compares and, with
   `--mark-verified`, records agreement.
5. You review in the atlas: `.venv/bin/chd-atlas literature review <SYMBOL>` lists the unapproved
   findings. Approve with `--approve <id> --by "<your name>"`, or mark one with
   `--pending-review <id> --note "<what differs>" --settled-by <who>`, where `<who>` is what would
   settle it: `full_text`, `authors`, `expert` or `atlas_model`. `chd-atlas literature questions`
   lists the open ones. Add `--against <the verifier's file>` to see where the blind pass read a
   finding differently.

### Review a run

`chd-curator quotes <SYMBOL> --run <run id>` checks every quote in a run's `evidence/` and
`judge/` files against the text of the finding's paper and writes
`local/literature/runs/<SYMBOL>/<run id>/quotes.json`. A quote whose paper has no text is
recorded as not checked (`checked: false`, `found: false` in `quotes.json`), never as not
found.

`chd-curator review-page <SYMBOL> --run <run id> --reviewer <name> [--role owner|second_opinion]
[--archive <dir>]` writes the run's review page, one self-contained HTML file:
`<run>/review.html` for the owner, `<run>/review-<name>.html` for a second opinion (which shows no
approval state and no other reviewer's verdict, and refuses `--archive`). With `--archive` the owner
page reads the review archive (see `archive-state` below): each card shows the owner's own earlier
verdict (only when it is not a plain approve) and each clinician's latest verdict on it, the triage
card lists each reviewer's latest triage verdict with its date (the owner's only when it is not a
plain approve) and the open flips, a fold lists verdicts on findings with no card on this page (the
owner's plain approves left out), and the page says which archive commit it read. The owner's own open
flips are ticked again on their rows. Approve or Needs a fix on the triage card closes every flip
listed whose paper is not flipped on that card; Ask later, or no verdict, closes none. Undoing a flip
while the card has no verdict shows a banner and refuses "I'm done". A triage state belongs to the
page it was made on: a page restored in a browser or store that already holds another page's state
for the same literature file keeps from it only the papers this page asks to be read, and its session
is not finished. An archive the reader refuses (see `archive-state`) refuses the page, exit 2, and
nothing is written; a failing git command exits 1. The command refuses to build, exit 1,
when `chd-atlas validate` reports an error. The page saves two ways: opened as a claude.ai artifact it
saves to the page's store; opened as a plain file it saves in the browser. A page published as a claude.ai artifact needs the `downloads` capability beside `db`: claude.ai never lets a page download a file itself, so without `downloads` "Download my review" cannot save there (the page says the review was not saved). "Download my review"
(available at any time) gives a decisions file, which the atlas's `literature apply-review`
applies, and `review-decisions` below gives one from the store; both leave out a card decided on
an earlier version of it. "I'm done" marks the review finished; the atlas refuses a file without
it.

`chd-curator review-decisions <SYMBOL> --run <run id> --export <dir> --out <file> [--page <html>]`
converts the page's saved decisions into that decisions file without anyone retyping them. When the
page ran as a claude.ai artifact, Claude exports its store with ArtifactData's out_dir option,
every document (a `list` pages at 100, and a short export shows in the count line below); the
command reads `<dir>/review/<id>.json` and `<dir>/session/main.json`, plus the page (default
`<run>/review.html`) for its provenance, reviewer and each card's digest, and writes the format 1
file (notes copied byte for byte). The page writes its own identity (literature file sha256, build
time, reviewer) into `session/main`, and an export that is not that page's is refused. The command
exits 1 when the reviewer has not pressed "I'm done" or changed a choice after it (press it again),
and 2 for an export from another page, reviewer or gene, a flip or a seen paper the page does not
hold, a document for a card not on the page, a decision outside approve|fix|later, or a file failing
the schema; nothing is written on either. A triage document that is not stamped with this page's build time (made on another page, or by a page
that stamped nothing) is read for nothing but the papers this page asks to be read: a verdict in it
is left out of the file, one line says so ("left out a triage verdict that was not made on this
page"), and the command still succeeds. A card decided on an earlier version of it is left out
and named ("left out, decided on an earlier version: ..."), and the line before the path counts
what was written, so a wrong `--export` directory shows. The step after is the atlas's
`chd-atlas literature apply-review <SYMBOL> --decisions <file> --by <name>`.

`chd-curator archive-page <SYMBOL> --run <run id> --page <html> --archive <dir> [--artifact <url>]` files a
review page, byte for byte, in the review archive: a private git checkout with `.review-archive`
committed at its top (any other `--archive`, including a subdirectory of one, is refused, exit 2). The
page lands at `<SYMBOL>/<run id>/pages/<built_on in UTC>--<reviewer slug>.html`, and `--artifact` (a
`https://claude.ai/` URL) beside it as `.artifact`. Filing is append-only, judged against what is
committed: when the last commit holds the target, a different file is refused (exit 2) whatever the
working tree now holds there, and an identical one prints `already filed` and is restored from the
commit if the working copy is missing or differs; a target git ignores is refused. What is not yet
committed is committed, with only those paths, under the archive's own `user.name` and `user.email`
(unset: refused before anything is written; the commit is given those two values explicitly, and
every `GIT_AUTHOR_*`, `GIT_COMMITTER_*`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`, `GIT_CONFIG_VALUE_*`
and `GIT_CONFIG_PARAMETERS` variable is removed from its environment); a page by a second opinion
whose name reads as `owner` is refused; the message is `file <SYMBOL> <run id>: page`. A commit that leaves a
filed path out of the last commit is an error (exit 1). The command never pushes.

`chd-curator archive-decisions <SYMBOL> --run <run id> --decisions <json> --archive <dir> [--root <atlas>]`
files a finished decisions file, byte for byte, beside the page it was made on, at
`<SYMBOL>/<run id>/decisions/<finished_at in UTC>--<reviewer slug>.json`. It is refused (exit 2, nothing
written) when the file is not valid JSON or fails the atlas's exported schema, its session is not
finished, it is for another symbol, it is a second opinion whose name reads as `owner`, it names one
finding twice, the reviewer name has no ASCII letter or digit for a file name, its gene differs from
the filed page's or from one already filed for the symbol, or no page filed under that run matches
its `page.built_on`, `page.literature_sha256` and reviewer, or its session finished before its page
was built (a bad symbol or run id is refused too).
These include the checks the archive reader applies, so the archive does not accept a file its reader would
refuse. The same append-only rule applies: a different file at the same name is refused and the
message names it, an identical one prints `already filed`. The message is `file <SYMBOL> <run id>:
decisions`. For an owner's file the last line printed is the `chd-atlas literature apply-review`
command to run next (values quoted for the shell); a second opinion prints none.

`chd-curator archive-state <SYMBOL> --archive <dir> [--root <atlas>] [--atlas-cli <path>] [--json]` reads
every decisions file filed for the gene and prints one line: how many findings have verdicts, the
files and reviewers they come from, how many verdicts are pending, how many triage flips are pending
and the archive's commit. A verdict is pending when a reviewer's latest verdict on the finding is a
`fix` or `later` made on the finding's current digest (a finding with two such reviewers counts
twice). A flip is a verdict on one paper: a reviewer's latest flip of it counts, whatever their
later triage verdicts say, and it is pending while the paper is in the literature file's triage with
a decision other than the flip's `to` (a paper still undecided counts, one no longer in triage does
not), unless the owner closed it by approving or fixing (not asking later) the triage on a filed page
that showed that flip, in a file that finished after the flip did, without flipping the paper
themselves. `--json` prints the whole state. It writes nothing, and refuses (exit 2) an archive with
uncommitted changes (untracked files included), anything in a decisions directory other than a
.json file directly in it, a decisions file the last commit does not hold, an owner's approve or fix
of the triage whose filed page cannot be found or read, a file that fails the schema, is unfinished,
finished before its page was built, is misnamed, is for another
symbol, names one finding twice or is a second opinion keyed as the owner, two genes under one
symbol, or an archive whose gene for the symbol is not the atlas's.

`chd-curator agreement [SYMBOL ...] --archive <dir> [--root <atlas>] [--json]` reports, for each
clinician, how often their latest verdict on a version of a card (its shown digest) agrees with the
owner's latest on the same version: both approve or both fix is agreed, one approve and one fix is
disagreed (the report quotes both notes), and either side saying later counts as ask later. A
clinician verdict on a version no owner decisions file covers is unpaired; a later version is never
set against an earlier one, and triage verdicts are not counted. With no symbol it reads every
top-level directory of the archive that is not a dot-directory. The report is printed here only: the
command writes nothing, and `--json` prints the whole report instead. It refuses (exit 2) what the
archive reader refuses (not a review archive, uncommitted changes, anything in a decisions directory
other than a .json file directly in it, a decisions file the last commit does not hold, an owner's
approve or fix of the triage whose filed page cannot be found or read, or a file that is not valid
JSON, fails the schema, is unfinished, finished before its page was built, is misnamed, is for
another symbol, names a finding twice or is a second opinion keyed as the owner, or two genes under
one symbol) and, with no symbol, a top-level directory whose name is not a symbol. Unlike `archive-state`, it does not
compare the archive's gene with the atlas's.

`chd-curator archive-redact --name <name> --archive <dir> [--apply]` removes a reviewer from the review
archive on their request. A reviewer is found by name stripped and casefolded (so "Dr X" and " dr x"
are one clinician); `--name owner` is refused, and `--name <the owner's own name>` finds the owner's
files as files that still name the reviewer, deletes and rewrites nothing, and `--apply` refuses it.
Without `--apply` it only prints five lists: the files to delete (the reviewer's decisions files,
their own pages and those pages' `.artifact` files), the owner pages to rewrite, the artifact URLs to
republish (the rewritten owner pages) and to unpublish (the reviewer's own pages), and the files that
still name the reviewer afterwards. Rewriting an owner page replaces the reviewer's `name` with
`a removed reviewer` and their `note` with an empty string in the page's data (the clinicians' verdicts
on each card, the earlier triage verdicts, and the second-opinion entries of the open flips and of the
verdicts on findings with no card) and leaves every other byte of the page alone. Nothing else is
edited, so a name in an owner's own note, or anywhere else, stops `--apply` (exit 2, nothing changed)
until you edit that file and commit. A file whose path holds the reviewer's file-name slug (`dr-x` for
"Dr X") as a whole token also counts as naming them, and `-` is a boundary there. So a name written
another way ("Dr. X" is another key) or a longer name containing it ("Dr X Smith") stops `--apply`
too. Check each file listed under "still name the reviewer": one that names this reviewer (written
another way, or in the owner's own note) is yours to edit or delete, then commit and run again; one
that names another reviewer whose name contains this one ("Dr X Smith") must not be changed here,
because this command cannot remove one without the other, so do that removal by hand. Both the dry
run and `--apply` refuse (exit 2) an archive in which a path carries a skip-worktree or
assume-unchanged bit (git then hides a deleted or edited working copy from `git status`; the message
names the paths and prints one `git update-index` command for each bit set, clearing skip-worktree
and assume-unchanged separately, because one command clearing both clears only assume-unchanged)
and a file the scan cannot read.
`--apply` also refuses an archive with uncommitted changes, a removal that touches no file, a missing
README or one without exactly one `## Removals` line, and `.git/redact-*.txt` files left by an earlier
removal (finish that history rewrite first). It deletes, rewrites, replaces `None yet.` under
`## Removals` (or appends after the last entry) with a dated line (`- <date>: one reviewer removed: N
files deleted, their name and notes redacted from M owner pages.`), scans again, and commits `redact a
removed reviewer` under the archive's own identity (a match in the scan, a failed write or a failed
commit restores every touched path and exits 1). It then writes `.git/redact-expressions.txt` and
`.git/redact-paths.txt` (the deleted files and the rewritten owner pages, whose original blobs hold the
clinician's notes), and prints the steps to rewrite history in a fresh clone, re-add the redacted owner
pages in one commit, and expire the checkout's reflog, then the copies outside the archive to delete by
hand (on this machine: the clinician's page and the file they sent back, the owner's own review pages
built while the clinician's verdicts were in the archive, such as
`local/literature/runs/<SYMBOL>/<run>/review.html`, and any store-export directories made from them)
and the pages to republish or unpublish. History holds the name and the notes until that rewrite is
run, which is the owner's word;
after it the checkout's reflog still holds them until the printed `reflog expire` and `gc` are run, and
GitHub may keep the force-pushed-over commits reachable by id until GitHub Support removes them
(unmeasured). `git filter-repo` is not installed where this was written, so the printed procedure has
never been run and is unmeasured as of 2026-11-25; when `git-filter-repo` is not on this PATH the output
says `uv tool install git-filter-repo`; its reflog re-check was measured on git 2.34.1 to count matches before the expiry and 0 after. It never pushes.

Once a gene has approved findings, `gene-synthesis` (the top model tier) drafts its "In short"
paragraph into `local/literature/<SYMBOL>.synthesis.yaml` and `chd-curator synthesis <SYMBOL> --draft
<file>` writes it, unapproved, into the gene's file; a redraft replaces an unapproved one and
refuses a draft carrying an approval, and refuses to replace an approved synthesis: an agent
never removes an approval. The atlas's own checks (`SYN001` to `SYN004`) run in
`chd-atlas validate`. You then run `.venv/bin/chd-atlas literature review <SYMBOL>`, which lists each sentence
beside what it cites and prints an `approve with:` line carrying the digest of what it listed;
you run that line with your own name in `--by`.

`chd-curator normalise <SYMBOL>` places each finding variant the paper named by its `c.` (placement
`mane`, no normalisation yet) on the gene's pinned MANE Select transcript, using the public
VariantValidator REST service (at most one request a second, the only network use besides PubMed). It
fills `hgvs_c`, `hgvs_p` and `grch38` and records the run under `normalisations`. A variant
VariantValidator refuses, or whose predicted `p.` disagrees with the paper's, becomes `unplaced` and
`pending_review` with a note naming why; it never touches an approval or `verified_by`. A network
failure writes nothing (exit 1), but an HTTP 404 refuses that one variant (unplaced, pending review). A 404 on a
variant's first request carries no versions to record: if every variant asked is such a 404, nothing is written
(exit 1). A 404 on the second (genomic) request keeps the first answer's metadata, so that run is recorded. A variant whose paper named another transcript, and which
VariantValidator refused, keeps the paper's `p.` on MANE's protein when `p.` names a residue, and its note says so. A paper's unversioned
accession that is MANE's, from a paper published before 2025 where MANE's version is `.1`, is read as
MANE's (CP-D8): the reading is printed, and a variant that ends unplaced carries it in its `review_note`. `--run <run id>`
also writes `<run>/normalise.json` (one entry per variant asked or skipped, with that reading) for the review page;
the run directory must exist, and must not already hold a `normalise.json` (exit 2 otherwise, before any request). A
missing or malformed `curation/publications.yaml` (where the paper's year is read) exits 1 and writes nothing. Two variants the atlas would refuse as one name repeated (the same
`grch38`, in one genotype context or one `with` group, ruling D16') write nothing either (exit 2). Run `chd-atlas validate` after it: the atlas, not curator,
checks a refused variant's kept residue. Editing a finding's variants changes its approval digest, so
re-approve it afterwards.

Run `.venv/bin/chd-atlas validate` after each step.

`NCBI_API_KEY` is optional, read from the environment only and never written to a file.
`search`, `abstracts`, `add-publication` and `normalise` need the network.

### Keep reviews: the review archive

The review archive is a private git repository, your checkout of it passed as `--archive`. Every
review page is filed in it as sent and every decisions file as received. Later owner pages read it
(`review-page --archive`), and `agreement` measures how often each clinician agrees with you. The
owner sends and receives; a session never contacts a clinician.

- **Send by file (the default).** The session builds the clinician's page (`review-page --role
  second_opinion --reviewer <name>`), files it with `archive-page`, and gives you the path; you email
  it. The page states what the archive keeps, in a notice in its introduction, just before the
  instructions for sending the review back (the save buttons are in the page header, above it), and
  never reads the archive.
- **On claude.ai, for an invited clinician with an account (not yet measured).** The session
  publishes the clinician's page as its own private artifact, with the `db` and `downloads`
  capabilities, and files it with `archive-page --artifact <url>`. You invite the clinician from the
  Share menu. On their "I'm done" the session exports the store, converts it with
  `review-decisions` and files the result. Whether an invited editor can write to the store, and
  what a signed-out or view-only visitor sees, have not been measured.
- **Receive and file.** You give the session the returned file; it files it with `archive-decisions`
  and pushes the archive (the commands never push). For your own review, `archive-decisions` prints
  the `chd-atlas literature apply-review` command to run next. A store export is converted and not
  kept.
- **Remove.** On a clinician's request, `archive-redact --name <name> --archive <dir>` lists what it
  would delete and redact; `--apply` does it and commits. Rewriting the archive's history,
  republishing your claude.ai copies and unpublishing the clinician's own page follow the steps it
  prints, on your word.

## Develop

curator's tests run against a real atlas checkout and stop if any of these is unset:

- `CHD_ATLAS_ROOT`: the checkout, whose `schemas/` and `curator/curator-rules.md` are read;
- `CHD_ATLAS_CLI`: its `chd-atlas` executable;
- `CHD_ATLAS_PYTHON`: the interpreter of its environment, which runs the atlas's own models over
  the skills' examples in a subprocess.

```bash
uv sync
CHD_ATLAS_ROOT=… CHD_ATLAS_CLI=… CHD_ATLAS_PYTHON=… .venv/bin/python -m pytest
```
