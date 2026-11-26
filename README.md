# curator

Literature curation tools for the [CHD atlas](https://github.com/chd-atlas/chd-atlas.github.io).
`chd-curator` searches PubMed for a published gene, records the hits, adds papers and compares a
blind second extraction with the first, applies the judges' findings, and writes a drafted gene synthesis. It also checks a
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

Run from the root of an atlas checkout. `search`, `abstracts`, `texts`, `diff`, `normalise`, `synthesis`
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

### Gather a run's texts

`chd-curator texts <SYMBOL> --run <run id> [--refresh]` fetches the free texts of the gene's triaged
papers: the `included` entries and the `excluded` ones whose reason is `full_text_not_accessible`.
Per paper it writes, under `local/literature/texts/<PMID digits>/`, `abstract.txt` (title, year,
publication types, abstract) and, for the first source that gives a full text, `fulltext.txt` with
the raw bytes in `raw/`. The sources are tried in this order: a PDF you put at
`local/literature/supplied/<PMID digits>.pdf`, Europe PMC's open-access XML, PMC's efetch XML, then
OpenAlex's free copies; each is tried while no full text has been found. A supplied PDF is taken
whenever it converts as a PDF with at least one word, at any length (a file that is not a PDF, or a
PDF with no text to read, is rejected and the other sources are tried). A copy the tool finds counts
as a full text only when `convert.full_text_verdict` accepts it, so a landing page or a short PDF is
recorded and passed over.

Supplements come whole, never filtered by gene or sheet, whichever source gave the full text, a
supplied PDF included: Europe PMC's archive for a paper with a PMC id and, when that gave nothing
(no PMC id, no archive, a failed request or an archive that does not open) and the paper has a DOI,
the supplement links on the DOI's landing page. Each file that converts goes to
`supplements/<name>.txt`, its raw bytes to `raw/supplements/<name>`. A supplement that is a zip is
opened one level, its files named `<zip>__<file>`; a zip inside it is not opened. A name is made a
file name: a control character or a path separator becomes `_`, and a name over 100 UTF-8 bytes is
cut, its extension kept. A supplement not read is recorded as an attempt and kept nowhere: a file
that is not text, one that converts to an HTML page though its name does not end `.htm` or `.html`
(a login or bot-check page served for a workbook's URL), an Office or OpenDocument package that is
not an xlsx or docx (a pptx, an odt...: one file, not an archive), a zip inside a zip, an archive
file over 50 MiB or past 200 MiB in all, an archive file that cannot be read, an archive that does
not open, a request that failed, a link whose URL cannot be read, a publisher page that could not be
read, and an earlier file a replacing fetch did not carry over (below).

`fetch.json` records what was fetched, every attempt included, and the supplied PDF's sha256; a
paper that has one makes no request, PubMed included, unless `--refresh` is given or its supplied
PDF has appeared, changed or gone since. When a paper is fetched again and its earlier record is
usable, the earlier directory is kept when the step that has less failed: a full-text source failed
and the new fetch has no full text where there was one (not when that text came from a supplied PDF
that has since changed or gone), or a supplement source failed (Europe PMC's archive request, the
publisher page, a publisher download), a supplement file of the earlier fetch, by name, is missing
from the new one, and the new fetch gained nothing: no full text where there was none, and no
supplied PDF that appeared or changed. A fetch that gained replaces the earlier one even then; the
command prints a line naming the paper, what it gained and the earlier supplement files not carried
over, and `why` names each of them: as not carried over when the new fetch made no attempt on it,
else by that attempt's outcome; they count in `R` (below). A source failed
when an attempt's outcome starts `failed:` or `bot check`: a host not reached, an error answer (a
404 included, except the 404 by which Europe PMC's full text and archive, PMC efetch and OpenAlex's
work say there is none), an answer that is not what was asked, an id efetch refuses before sending a
request (not a PMC id), an OpenAlex copy whose URL cannot be read, any non-200 answer to a request
for an OpenAlex copy or a publisher supplement file, or a publisher page answering 403, 429 or 503
or serving a challenge page. "No record", "not available (404)", "free copies: 0" and a rejected
copy are answers, not failures. When the earlier directory is kept, the command prints a line naming
the paper, what the new fetch lacked and the failed attempts, and `texts.json` shows the earlier
texts. In every other case the new fetch replaces the old one. To drop a paper's texts, delete
`local/literature/texts/<PMID digits>/`; the next run fetches it anew. A paper is fetched into a
`<PMID digits>.tmp` directory that replaces its directory only once its `fetch.json` is written, so
a fetch that stops with an error leaves the old directory as it was.

`<run>/texts.json` lists the papers for the other run commands, the ones still needing a text first
(papers that are not case reports, then newest year, then PMID), the rest by PMID. Each entry
carries `attempts` (`via`, `url`, `outcome`) and the `licence` stated by the source of its full text
(Europe PMC's record for a Europe PMC or PMC copy, the copy's own for OpenAlex, none for a supplied
PDF), or Europe PMC's when it has no full text. An entry's `why` says, for a paper with no full
text, what the full-text sources answered and its PubMed types, and, for any paper, each supplement
not read, named (an archive file by its name, a link by its file name, else Europe PMC's archive or
the publisher page) with its outcome. The NCBI key is never written: a recorded URL has its key
parameter dropped, and an attempt of a source that sent no request (efetch refusing an id that is
not a PMC id) has the URL `""`. The command prints one summary line, after one line for each paper
whose earlier directory was kept; its `R rejected` counts the supplements not read, as listed above.
It exits 1 when PubMed fails or answers no record for a PMID, a paper's `fetch.json` is unusable
(the message names the file and `--refresh`, and the key, with its value when the value is wrong; a
file that is not JSON or not a JSON object names no key), a supplied PDF cannot be read, or a file
cannot be written; a source failing for one paper is recorded, not fatal. It exits 2 for an unknown
gene, a missing literature file, a literature file that is not YAML or has no triage list or a
triage entry without a `PMID:<digits>`, a bad run id, or a run that already holds a `texts.json`,
before any request. The atlas gitignores `local/`, so no fetched text is committed.

### Plan a run's batches

`chd-curator batch <SYMBOL> --run <run id> --mode new|backfill [--fields <f,...>]
[--findings <id> ...] [--budget-kb 170] [--root .]` splits a run's papers into reading batches and
writes `batches.json` plus `batches/batch-NN.md`, the manifest both blind readers open. It reads
the run's `texts.json` and the gene's literature file and runs after `texts`. A budget of N KB is
N x 1,000 bytes of main text per batch (default 170,000). Papers are packed in (year, PMID number)
order; a paper whose own text, or whose supplements together, exceed the budget gets a batch of its
own. A paper's year and title come from `curation/publications.yaml`, else from `texts.json`. In
`--mode new` the papers are the `included` triage entries no finding cites. In `--mode backfill`
they are the papers of `--findings` (default every finding whose declared fields are all empty:
absent, null, `[]` or `{}`), `--fields` is required, a backfill fills fields a finding lacks and
never rewrites a filled one,
and each name must be a `Finding` property of the exported schema other than `id`, `publication`,
`approved_by`, `approved_on`, `approved_digest`, `extracted_by` and `verified_by`, and not a field `compare` never reads
(`summary`, `locators`, `shares_probands_with`, `review_status`, `review_note` and
`review_settled_by`, because a difference in it would pass as agreement); the manifest then
lists the fields and each paper's findings as curated, with `approved_by`, `approved_on` and
`approved_digest` left out because a reader needs none of them. A manifest gives absolute paths to
each paper's text, abstract and supplements (with sizes) and names no other file of the run.
Example output: `planned 1 batch for 3 papers (budget 170,000 bytes): 01 3 papers 637 bytes`; a
backfill's default selection appends `; skipped N findings already holding a declared field` when
it left any out. `<SYMBOL>` may be an alias or a previous symbol (the run directory is named as
typed, the literature file by the approved symbol, as for `merge`). It writes the manifests first and `batches.json` last, and exits 2 with nothing
written when `batches.json` exists or `batches/` holds a file, `texts.json` is missing, a selected
paper is not in it or has neither text nor abstract or has no year, nothing is selected, a field
is unknown or forbidden, or an explicit `--findings` id already holds a declared field.
`--mode reread` is not planned by this command yet.

### Merge a run

`chd-curator merge <SYMBOL> --run <run id> [--study-type PMID:<n>=<type> ...] [--root .]
[--atlas-cli PATH]` merges a run's extraction batches into the gene's literature file
(`<SYMBOL>` may be an alias; the run directory is named as typed). It reads `batches.json` and,
per batch, `extract/batch-NN.findings.yaml`, `.papers.json` and `.evidence.yaml`, and never plans
batches itself. In a run planned `--mode new` it numbers every finding from the `LIT` counter of
`curation/.id_registry.yaml`, in (year, PMID number, batch, position in the file) order, where a
paper's year is `publications.yaml`'s, else `texts.json`'s. A `shares_probands_with` entry naming a
local id of the run becomes that finding's final id, one already in the file is kept, and `unknown`
and `[]` stay as they are. It adds a Publication (from PubMed `esummary`, with the reader's
reading written above its `study_type`, as `add-publication` does) for each cited paper that
`publications.yaml` lacks, all fetched in one request before anything is written; a reader's null
study type must be named with `--study-type PMID:<n>=<type>` (several allowed). A paper `papers.json` excludes goes
from `included` to `excluded` in the triage, with its reason and note. The evidence is written to
`evidence/batch-NN.yaml` under the final ids, and `merge.json` records the ids, the Publications
added and the triage changes. In a run planned `--mode backfill`, which fills declared fields a finding lacks and never
rewrites a filled one, the findings file is
`findings: {<finding id>: {<field>: <value>}}`: each id must be a finding in the file and in its
batch, each field one the run declared, and only the declared fields change, plus two things
that follow from them: a finding whose declared fields changed loses `verified_by`, and a
common-variant finding that gains its first variant gets `variant_classes: [snv_indel]`. A
backfill reads no `papers.json`, and its evidence file is copied to `evidence/` unchanged. A
backfill that changes the declared fields of an approved finding leaves its `approved_by`,
`approved_on` and `approved_digest` as they are, so the finding no longer matches its approval and
the atlas reports `LIT015` (approval void) until it is approved again; the summary line counts the
findings the pass files name, not the findings that changed.

Example output: `merged 3 findings (CHDA:LIT:0009002..CHDA:LIT:0009004) from 2 batches; 2
publications added; 1 papers excluded; LIT counter 9004`. It exits 2, with every curated file and
the run directory unchanged, when any batch is inconsistent: a paper of a batch missing from its
`papers.json`, or one named there that the batch does not hold; a finding that cites a paper
outside its batch, repeats a local id or one already in the file, or carries `verified_by` or an
approval; an evidence key that is not a finding of its batch; a paper that already has a finding;
a paper its `papers.json` gives a study type and reading but that no finding of its batch cites
(cite it, or exclude it with a reason: the atlas refuses an included paper nothing cites);
an exclusion reason outside the atlas's list, or of a paper not `included` now; a study type the
atlas does not allow, or a null one left unnamed; a `--study-type` for any paper that needs no
Publication from it; a paper with no year; a `shares_probands_with` that names neither a finding
of the run nor one in the file; any file a write of it would refuse (the atlas's schema, an
anchor, an approval changed); or a run that already has `merge.json`; a curated file that is missing, malformed or not UTF-8; or a
backfill value for a declared field the finding already holds, equal or not. Exit 2 is every
refusal. It exits 1, also with nothing written, when PubMed fails or answers no record for a PMID,
or an OS error occurs while a file is read or written. Exit 2 is "nothing written" for every
refusal above, which are all found before the first write, except one: if a
curated file changes between the merge reading it and writing it, the write is refused (exit 2)
and the message names the files this merge had already written (evidence, `merge.json`, the
registry, `publications.yaml`), which stay as written.
The files are written in this order: `evidence/` and `merge.json`; the id registry;
`publications.yaml`; the literature file. A crash part-way leaves `merge.json` in place, so a
re-run is refused until the curator removes it.

### Compare a run's two readings

`chd-curator compare <SYMBOL> --run <run id> [--root .] [--atlas-cli PATH]` sets the blind reader's
`verify/batch-NN.findings.yaml` against what `merge` wrote and writes `comparison.json` and, for
each batch with anything to judge, `judging/judge-NN.md`. It needs `merge.json`, and a verify file
for every batch of `batches.json` (and for none other). Both passes go through the notation rules
before they are compared. In a run planned `--mode new` the first pass is the findings of the
literature file that `merge.json` numbered; they pair with the blind pass's findings by
(publication, design, cohorts) within a batch. A key several findings share is never paired by
that rule; such findings pair only where exactly one finding on each side is identical to the
other in every compared field, span and review mark, and every other one is listed as only in the
first pass or only in the blind pass. In a `--mode backfill` run the blind pass is
`findings: {<finding id>: {<field>: <value>}}`: each finding pairs with itself, the blind copy
being the finding as merged with each declared field replaced by the blind reader's value, or
removed when the blind reader gave none; a finding neither reader filled in a declared field is in
no list.

A pair is agreed; span only when its only difference is a variant's `as_reported` span
(the first pass's span stays); or differs, with the names of its differing fields. The order
of a variant's phenotypes and the notation rules' fixes are never differences. Review marks
(`review_status` and `review_settled_by` of the finding and of each variant) are compared as one
pseudo-field, `review marks`; `review_note` is not compared. A pair that differs in the span and
another field lists only the other field. A backfill that declares a field the comparison never
reads (`summary`, `locators`, the review fields) is refused, because every difference in it would
read as agreement. A manifest names, for each differing pair and each unpaired finding, the paper's
text, abstract and supplement paths (absolute), each differing field as
`<field>: first <json> | second <json>`, and both findings as YAML, the first as merged and
without its approval. Example output: `compared 3 pairs: 1 agreed, 1 span only, 1 differ; 0 only
in the first pass, 1 only in the blind pass`.

It exits 2, writing nothing, when the judges have written anything (a `judge/*.decisions.yaml` or
a `judging/*.findings.yaml`), the run has no `merge.json`, a verify file is missing, misnamed or
for a batch the plan lacks, a blind finding cites a paper its batch does not read, a blind id
appears in two batches, a blind finding does not satisfy the atlas's `Finding` schema, or a backfill
names an id outside its batch or an undeclared field. A re-run is otherwise allowed, so a
corrected verify file can be compared again: it removes the `judging/judge-*.md` files an earlier
run wrote, writes the new ones and `comparison.json` last. It exits 1 on an OS error.

### Assemble a run

`chd-curator assemble <SYMBOL> --run <run id> [--verifier NAME] [--root .] [--atlas-cli PATH]`
applies the judges' findings, the notation rules and the blind agreement to the gene's literature
file. It needs `merge.json` and `comparison.json`, and reads every `judging/judge-NN.findings.yaml`
(a `findings:` list; each finding must cite a paper of batch NN, satisfy the atlas's `Finding`
schema and carry no approval field or `verified_by`). The steps, in this order:

1. **The judges' findings.** A finding whose id `comparison.json` lists under `differs` or
   as read by the first pass only replaces the file's finding of that id; every `differs` id
   needs one, and a first-pass-only finding the judges did not write stays as merged. A finding with a local id
   `CHDA:LIT:07NNnnn` is added, numbered from the `LIT` counter in (year, PMID number, position)
   order; its paper needs a Publication (otherwise the command names `chd-curator
   add-publication <PMID>` and exits 2), and a triage entry that `merge` set to `excluded` goes
   back to `included`, without its reason and note; an entry anyone else excluded is refused. A
   `shares_probands_with` entry naming a judge's local id becomes its final id. The judge's
   decisions file stays as the judge wrote it: the run's loader for decisions (so `quotes` and `review-page`) reads a
   decision keyed by a local id under its final id once `assemble.json` maps it, and an
   unassembled run is refused by `review-page`, naming that id.
   A finding only the blind reader reported (`comparison.json` lists it as read by the blind pass only; there is none in
   a backfill, whose blind fields `compare` pairs by finding id) is the judge's to account for: the
   judge's decisions file needs an entry keyed by that blind id (`CHDA:LIT:08NNnnn`) holding a
   row whose `field` is `finding`. Its `decision` is `second` when the judge adopted the finding
   (the judge also writes it as a finding of its own, with a local `07` id), `first` or `neither`
   when it did not; `quote`, `where` and `rule` give the reason. `assemble` exits 2, with nothing
   written, naming every such id that has no entry or whose entry has no `finding` row
   ("reported only by the blind reader, but no judge decision records whether it was added"), and
   records the decisions in `assemble.json`, under its blind-only entry.
2. **What a judge may change.** In a `new` run (a `reread` plan is treated alike, though no command writes one yet, so that
   path is untested) a judge's finding for a `differs` id may
   differ from the file's finding (both after the notation rules, a variant's `as_reported` span
   ignored) only in the compared fields `comparison.json` lists for it; `review marks` lists the
   marks. `summary`, `extracted_by`, `locators`, `review_note` and `shares_probands_with` are not
   compared and are
   free. A first-pass-only finding may change in any field. In a `backfill` run only the declared
   fields are taken from a judge's finding, and every other field of it must equal the file's;
   a backfill adds no finding.
3. **Notation.** The rules apply to every finding of the run (the findings `merge.json` numbered,
   the replaced ones and the added ones); in a backfill only when `variants` is a declared field,
   and only to the findings `comparison.json` paired, because elsewhere they would rewrite the
   variants of an approved finding. Each change is recorded in `assemble.json` with its rules.
4. **Blind agreement.** `verified_by` goes on every finding listed as agreed or span only. A
   judged or added finding carries none. Before marking an id an earlier assemble did not mark,
   the command derives its verdict again with `compare`'s own pair verdict, on the file's finding
   as it stands and the blind finding `comparison.json` paired it with; a finding edited since
   `compare` and no longer agreed exits 2 naming the id and saying compare must be re-run (an
   already marked id is not re-derived, because `normalise` rewrites variants after the first
   assemble). In a `new` run the name is the verify
   findings' `extracted_by` (one value across `verify/`, else exit 2; `--verifier` is refused); a
   backfill's blind pass records no `extracted_by`, so `--verifier NAME` is required there.

A last check re-reads the result and refuses it if anything but these steps moved: a finding
dropped, reordered or added unexpectedly, a field changed that no step above is entitled to
change, another triage entry, or a top-level key.

The files are written in this order: `assemble.json`; the id registry (only when findings were
added); the literature file. `assemble.json` goes first on purpose: it maps each judge's local id
to the final id, and written last, a crash between it and the literature file would let a re-run
number the same added finding again. A crash after it leaves a map naming ids the file lacks, and
a re-run then exits 2 saying a previous assemble stopped part-way; the curator removes
`assemble.json` and runs again. The registry is written before the literature file, so a crash
between them has already raised the `LIT` counter: the re-run numbers the added finding from the
raised counter, and the number the crashed run would have used is never used. After a complete assemble a re-run changes no byte. After a fix
round (the judge edited a finding, an added one still under its local id) a re-run replaces the
findings it numbered before under their final ids, numbers only local ids it has not seen, and
keeps every earlier mapping and notation record in `assemble.json`.

`assemble.json` also records, in `judged`, the sha256 of each judge finding it applied, keyed by
the final id it decides (the finding as parsed from the judge's file: sorted-key compact JSON,
UTF-8). A re-run does not apply a finding whose digest is unchanged, so what the file holds for it
since (an approval `apply-review` wrote, a placement `normalise` wrote) stays as it is: the judges'
file keeps its findings across fix rounds, the owner approves some findings between rounds, and
the re-run still works. `assemble.json` records, in `approvals`, the `approved_digest` each of
those findings carried just after the write (null when none). A judge finding whose digest changed
replaces the file's finding and keeps its approval fields: a void approval (a backfill's first
assemble voids the one a finding carried) stays void. The one refusal is exit 2 naming the id
(`approved; a judge may not change an approved finding`) when the file's `approved_digest` now
differs from the recorded one, that is when the owner approved the finding after that assemble.

Example output: `replaced 1 judged findings, added 1 (CHDA:LIT:0009005), fixed 0 names by rule,
marked 2 verified, 1 blind-only decided`. The line states the totals `assemble.json` records: `replaced` counts the
judged findings that replaced a merged one (a fix round's second replacement of an added finding
is not counted again), `added` every finding numbered, across rounds, `blind-only decided`
the findings only the blind reader reported whose decisions `assemble.json` records. It exits 2, with every file
unchanged, for every refusal found before the first write (and a write refused after earlier ones
exits 2 naming the files already written, `assemble.json` first; they stay as written): no
`merge.json` or `comparison.json`; modes in `batches.json` and
`merge.json` that disagree; a judge file that is misnamed or for a batch the plan lacks; a judge
finding with an approval field or `verified_by`, citing a paper outside its batch, repeating an
id, or with an id that is neither a `differs` id, a first-pass-only id, an id of an earlier assemble
nor `CHDA:LIT:07...`; a `differs` id with no judge finding, or one `comparison.json` also lists as
agreed; a finding only the blind reader reported with no `finding` decision of the judge; a finding to be marked `verified_by` that no longer agrees with its blind finding; a changed judge finding for a finding approved since the assemble that applied it; a field changed that
no decision names; a paper with no Publication; a triage entry
someone else excluded; an `assemble.json` naming ids the file lacks; a blind pass with more than
one `extracted_by`; a file any write would refuse (the atlas's schema, an anchor, an approval
changed) or changed since it was read. It exits 1 on an OS error.

### Check a batch before handing it back

`chd-curator check-batch <SYMBOL> --run <run id> --batch <NN> --pass extract|verify|judge [--root .]
[--atlas-cli PATH]` is what an extract, verify or judge agent runs on its own batch before it
reports. It applies that one pass to a copy of the atlas root in a temporary directory, runs
`chd-atlas validate` on the copy, and removes the copy. The copy holds `curation/` (copied) and a
symlink to every other top-level entry of `--root` except `local`, `.git` and `.venv`; nothing
under `--root` is written.

How the pass is applied, by the run's mode in `batches.json`:

- `new` or `reread`, `--pass extract` or `verify`: the findings of `extract/batch-NN.findings.yaml`
  (or `verify/`) are appended as `merge` would number them, from the `LIT` counter (raised in the
  copy, so `ID002` is not among the expected errors). No Publication is added. The exclusions of
  the pass's own `batch-NN.papers.json` are applied to the copy's triage by the function `merge`
  uses, so an excluded paper is not reported as cited by no finding; a paper of the batch that is
  neither excluded nor cited is reported (`LIT003`). A missing `papers.json`, or an exclusion
  `merge` would refuse, exits 2. A `shares_probands_with` entry naming a `CHDA:LIT:09NNnnn` id of
  another batch of `batches.json` is dropped in the copy only (`merge` numbers every batch
  together), and the command prints `NOT CHECKED: <finding> shares_probands_with <local id>
  (batch NN; merge resolves it)` before the summary line. Any other entry that names no finding of
  the pass or of the file exits 2, an `08` or `07` id of an extract pass included, which `merge`
  would refuse. The deferral is per pass: a verify pass defers another batch's `08NNnnn` id
  (`compare does not read it`) and a judge pass another batch's `07NNnnn` id (`assemble resolves
  it`); every other unresolvable ref exits 2 with the message of the command that would refuse it.
- `--pass judge`: the findings of `judging/judge-NN.findings.yaml` (the other batches' judge
  files are not read) go through `assemble`'s own acceptance and application functions, for this
  batch: the same id classes, the same limits on what a judge may change (the fields
  `comparison.json` lists for a `differs` id in a `new` or `reread` run; the declared fields in a
  backfill), the same Publication and triage rules for an added finding. It needs `merge.json` and
  `comparison.json` and exits 2 with `assemble`'s message where `assemble` would refuse; the
  check that a judge changed only the fields a `differs` id names covers every `differs` id a
  judge finding of this batch's file replaces, as in `assemble`. A finding only the blind
  reader reported, on a paper of this batch, needs its `finding` decision as in `assemble`. It does
  not run the notation rules or write `verified_by`.
- `backfill`, `--pass extract` or `verify`: the declared fields are filled by `merge`'s own
  backfill functions, so a field a finding already holds is refused (exit 2). Check a backfill
  pass before `merge`; after it the fields are filled.

It prints the validator's summary line, then either `OK: only the errors expected before merge and
normalise` (exit 0) or `UNEXPECTED ERRORS:` and each error line the validator printed that is not
expected (exit 1). The expected errors, which are not listed, are `LIT003` for a paper that is
not a paper of the batch, `LIT011`, `LIT022`
when its message ends `a mane variant needs hgvs_c`, `... grch38` or `... normalisation`, and
`LIT025` when it ends `but the variant has no hgvs_p`. Warnings are never listed.

The validator's answer is believed only when it exits 0 or 1, its last line is `N error(s), M
warning(s)`, exactly N lines begin `ERROR [`, the exit code agrees with N (0 only for none), and
nothing printed holds a traceback. Otherwise the command prints `UNEXPECTED ERRORS:`, the reason
and the validator's raw output, and exits 1: a crashed validator is never `OK`. It also exits 1
when the validator cannot be run. Exit 2, with nothing validated, for a `--pass` that is not one of
the three, a batch not in `batches.json`, a missing or unreadable pass file, a pass that does not
apply (an unknown `shares_probands_with`, a backfill field already filled, a judge id of no known
kind), or an unreadable gene or run. It exits 1 on an OS error while a file is read.

### Review a run

`chd-curator quotes <SYMBOL> --run <run id>` checks every quote in a run's `evidence/` and
`judge/` files against the text of the finding's paper and writes
`local/literature/runs/<SYMBOL>/<run id>/quotes.json`. A quote whose paper has no text is
recorded as not checked (`checked: false`, `found: false` in `quotes.json`), never as not
found. A decision keyed by a finding only the blind reader reported is checked against that
finding's own paper, its publication in the verify pass.

`chd-curator review-page <SYMBOL> --run <run id> --reviewer <name> [--role owner|second_opinion]
[--with <decisions.json> ...]` writes the run's review page, one self-contained HTML file:
`<run>/review.html` for the owner, `<run>/review-<name>.html` for a second opinion (which shows no
approval state and no other reviewer's verdict; `--with` is for the owner page only, and each file
must be a format 1 decisions file by a second opinion on the same gene). It refuses to build, exit 1,
when `chd-atlas validate` reports an error other than `LIT015`, an approval whose finding changed
since (a backfill voids approvals, and the page shows such a card as "approval void: re-approve");
a `LIT015` on a finding the page has no card for is refused too, naming the finding. The page's
validate line is the summary validate printed. A finding only the blind reader reported has no
card: the page lists it apart, read-only, under "Only the blind reader reported this" (its paper,
the blind reader's summary, the judge's decision and the quote with its check), with no decision
control, so a decisions file never names it. The page saves two ways: opened as a claude.ai artifact it
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
VariantValidator REST service (at most one request a second). It
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
`search`, `abstracts`, `texts`, `add-publication` and `normalise` need the network.

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
