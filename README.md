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
[--with <decisions.json> ...]` writes the run's review page, one self-contained HTML file:
`<run>/review.html` for the owner, `<run>/review-<name>.html` for a second opinion (which shows no
approval state and no other reviewer's verdict; `--with` is for the owner page only, and each file
must be a format 1 decisions file by a second opinion on the same gene). It refuses to build, exit 1,
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
the schema; nothing is written on either. A card decided on an earlier version of it is left out
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
its `page.built_on`, `page.literature_sha256` and reviewer (a bad symbol or run id is refused too).
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
twice); a flip is pending while its paper is still in the literature file's triage and that decision
is not yet the flip's `to` (a paper still undecided counts, one no longer in triage does not).
`--json` prints the whole state. It writes nothing, and refuses (exit 2) an archive with uncommitted
changes (untracked files included), a non-JSON file under a decisions directory, a decisions file the
last commit does not hold, a file that fails the schema, is unfinished, is misnamed, is for another
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
archive reader refuses (not a review archive, uncommitted changes, a non-JSON file under a decisions
directory, a decisions file the last commit does not hold, or one that is not valid JSON, fails the schema, is unfinished, is misnamed, is for another symbol, names
a finding twice or is a second opinion keyed as the owner, or two genes under one symbol) and, with
no symbol, a top-level directory whose name is not a symbol. Unlike `archive-state`, it does not
compare the archive's gene with the atlas's.

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
