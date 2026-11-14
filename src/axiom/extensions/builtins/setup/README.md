<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# `setup` — one command to get a computer ready

`axi setup` (and `axi start`; a product's CLI under its own name) detects the
machine, then walks seven steps. For each: **check** (read only), and if it is not
right, **plan** one sentence, **ask**, **apply**, and **check** again.

| Step | Checks | Applies (after a yes) |
|---|---|---|
| `upgrade` | installed version against the index | the installer's own upgrade (uv tool, pipx, pip), then re-runs setup on the new code |
| `vault` | OpenBao configured, answering, unsealed | `vault_setup.setup(runtime="auto")`: Docker if it works, else the `bao` binary as the user; container and port from `vault_setup.resolve_target` (`AXI_OPENBAO_CONTAINER`, `AXI_OPENBAO_PORT`), the same as `vault setup`. A container of that name whose mounts are not this home's state folder is another home's vault: setup stops, names the override, and never removes or replaces it |
| `shell` | the login shell's start-up file has the marked vault block | `shell_profiles.apply` (backup first; the token file is named, never the token) |
| `completion` | a new terminal completes the CLI (marked block present, the shell registers a completer, the CLI answers) | `axiom.cli.completion_setup` (backup first; sources argcomplete's script from the state dir) |
| `node` | the node answers from this network | nothing: the remedy (a VPN) is the person's |
| `key` | a key named `<host>-key` is in the vault and `/gate/verify` accepts it | the browser sign-in (`auth.node_signin`), or `--invitation` without a browser |
| `mcp` | every AI app found has the tool server | `mcp.install` for the ones that lack it |

A product inserts its own steps after `key` with the brand's `setup_steps_fn`,
and supplies its node and site with `setup_defaults_fn` (never hard-coded here).
A step lists what it `needs`; while that is red it waits and says so.

The checklist ends every run: a green line for what works, and for each red line
exactly one remedy. `axi doctor` prints this install's own rows (Python, package,
entry point, model, agents), then this same checklist, and changes nothing. Its rows
follow the deployment shape: on a laptop (no medallion, no declared node functions) the
background agents are information, not an issue, and a client bound to a site counts the
site's model as its provider. The package row reads the install's own metadata, so a
PyPI, uv-tool or pipx install is recognised; only a source checkout is told
`pip install -e .`. A row shown with a dot is information and is never counted. `--yes` answers every question, `--dry-run` shows the plan,
`--only STEP` runs one step. Without a terminal and without `--yes`, nothing is
changed. The last node and site used are kept in `<state>/setup-state.json`, so
running it again needs no flags.

Every step is a skill (`setup.<step>`, with `action` = check, plan or apply), and
the walk is `setup.run`, so a chat surface can drive the same steps (ADR-056).
