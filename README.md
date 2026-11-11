# curator

Literature curation tools for the [CHD atlas](https://github.com/chd-atlas/chd-atlas.github.io).
`chd-curator` searches PubMed for a published gene, records the hits, adds papers and compares a
blind second extraction with the first. Four Claude Code skills drive agents through those steps.
An agent never approves: a named person does, with `chd-atlas literature review` in the atlas.

## Install

```bash
uv tool install git+https://github.com/chd-atlas/curator@v0.1.0
```

Then, in Claude Code:

```text
/plugin marketplace add chd-atlas/curator
/plugin install curator@curator
```

The one-step form, with `--marketplace`, needs Claude Code 2.1.275 or later.

## Run

Run from the root of an atlas checkout. `search`, `abstracts` and `diff` ask the atlas about the
gene by running its CLI, `<root>/.venv/bin/chd-atlas` unless `--atlas-cli` or `CHD_ATLAS_CLI` names
another; `add-publication` does not, and reads the atlas's exported schemas. curator never imports
the atlas. The skills read the atlas checkout's `curator/curator-rules.md`. Their hand-off commands,
`.venv/bin/chd-atlas validate` and `.venv/bin/chd-atlas literature review <SYMBOL>`, assume the
atlas checkout's own `.venv`; adapt them if your atlas executable is elsewhere.

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
   `--pending-review <id> --note "<what differs>"`. Add `--against <the verifier's file>` to see
   where the blind pass read a finding differently.

Run `.venv/bin/chd-atlas validate` after each step.

`NCBI_API_KEY` is optional, read from the environment only and never written to a file.
`search`, `abstracts` and `add-publication` need the network.

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
