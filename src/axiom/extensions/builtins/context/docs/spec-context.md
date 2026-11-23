# Spec: `axi context`

**Status:** Living. Decisions: ADR-051 (cross-provider context), ADR-221 (the repo standard).
**Code:** `src/axiom/extensions/builtins/context/` (`core.py`, `generators.py`, `standard.py`, `cli.py`, `templates/working-with-ai.md`).

`axi context` keeps every AI assistant's instruction file in step with one
canonical `AGENTS.md`, and keeps the repo standard's managed block in that
file current. What the standard requires of a repository is in
`docs/specs/spec-repo-standard.md` at the Axiom repo root.

## Verbs

| Verb | Writes | Exit | What it does |
|---|---|---|---|
| `init [--force]` | yes | 0 | Promote a hand-written `CLAUDE.md` to `AGENTS.md`, or write the starter (only when absent, or with `--force`); insert or refresh the managed block; link `CLAUDE.md`; generate every tool file; install the pre-commit hook. Idempotent. |
| `sync` | yes | 0, or 1 with no `AGENTS.md` | Refresh the managed block if the repo has adopted it; regenerate every tool file; manage the `CLAUDE.md` symlink. |
| `check` | no | 1 on drift | The gate. Fails when a tool file is missing or drifted, `CLAUDE.md` is wrong, the managed block is outdated, or its markers are unbalanced. |
| `status` | no | 0 | Findings, each with the command that fixes it. |

All take `--root` (default: git toplevel, else cwd) and `--format human|json`.

## Targets

| Row | Path | Statuses |
|---|---|---|
| `claude` | `CLAUDE.md` -> `AGENTS.md` | sync: created, written, unchanged, conflict. check: ok, missing, drift, conflict |
| `standard` | `AGENTS.md#repo-standard` (the managed block) | sync: written, unchanged, conflict, absent. check: ok, drift, conflict, absent |
| `cursor` | `.cursor/rules/axiom.mdc` | sync: created, written, unchanged, conflict. check: ok, missing, drift, conflict |
| `junie` | `.junie/guidelines.md` | as above |
| `copilot` | `.github/copilot-instructions.md` | as above |
| `gemini` | `GEMINI.md` | as above |

`absent` means the repository has not adopted the standard. It is shown,
never counted as drift. `conflict` means hand-authored content sits where a
generated file belongs (or the block's markers are unbalanced); nothing is
overwritten.

Tool files are a pure rendering of `AGENTS.md` after the block refresh: a
DO-NOT-EDIT marker, Cursor's MDC frontmatter where needed, and root-relative
links rebased for the file's depth. So every tool file carries the block.

## The managed block

- Markers: `<!-- axiom:repo-standard:begin -->` / `<!-- axiom:repo-standard:end -->`,
  the same `axiom:<name>` convention as the other managed regions; spliced
  with `axiom.memory.rendering.splice_marked_block`.
- State (`standard.block_state`): `current`, `outdated` (the region differs
  from `render_block()` in any byte), `missing`, `unbalanced`.
- Version: `BLOCK_VERSION`, written as `axiom repo standard vN` on the first
  line inside the markers. Informational; staleness is the byte comparison.
- Insertion (`init` only): before the first `## ` heading that is not inside
  another `<!-- axiom:*:begin/end -->` region, followed by one blank line;
  appended when there is no such heading. No existing byte moves.
- Refresh (`sync`): only the bytes between the markers change, wherever the
  block sits.

## Findings (`status`)

| Code | Severity | Fix |
|---|---|---|
| `context.uninitialized` | info | `axi context init` |
| `context.standard-missing` | info | `axi context init` |
| `context.standard-outdated` | warn | `axi context sync` (the message names the version found and the current one) |
| `context.standard-unbalanced` | warn | fix the markers by hand, then `axi context sync` |
| `context.missing` | warn | `axi context sync` |
| `context.drift` | warn | `axi context sync` |
| `context.conflict` | warn | merge into `AGENTS.md`, delete the file, `axi context sync` |

`axi hygiene stat repo` folds these into the repo-standard audit.

## Pre-commit hook

`init` appends a managed block (`# >>> axi context (managed) >>>`) to
`.git/hooks/pre-commit` that runs `axi context check`. Only a primary
checkout (a `.git` directory) is handled. A repository that uses the
pre-commit framework declares the same check in `.pre-commit-config.yaml`
(spec-repo-standard.md §5).

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
