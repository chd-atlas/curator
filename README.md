# curator

Literature curation tools for the [CHD atlas](https://github.com/chd-atlas/chd-atlas.github.io).
`chd-curator` searches PubMed for a published gene, records the hits, adds papers and compares a
blind second extraction with the first, and writes a drafted gene synthesis. Five Claude Code skills
drive agents through those steps.
An agent never approves: a named person does, with `chd-atlas literature review` in the atlas.

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

Run from the root of an atlas checkout. `search`, `abstracts`, `diff`, `normalise` and `synthesis` ask the atlas about the
gene by running its CLI, `<root>/.venv/bin/chd-atlas` unless `--atlas-cli` or `CHD_ATLAS_CLI` names
another; `add-publication` does not. `search`, `add-publication`, `diff`, `normalise` and `synthesis` read the JSON Schemas
the atlas exports into `<root>/schemas/`, and every file curator writes is checked against them
first. curator never imports the atlas. The skills read the atlas checkout's
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
failure writes nothing (exit 1); two variants the atlas would refuse as one name repeated (the same
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
