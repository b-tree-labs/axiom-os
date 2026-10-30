# ADR-221: Axiom owns one repo standard, and keeps its shared part current in every repo

**Status:** Accepted (2026-10-29). The owner chose option A.
**Related:** ADR-051 (cross-provider context, `axi context`), ADR-087 (cross-mem write-back markers), ADR-118 (generated files are committed), ADR-128 (gold serves, silver does not), ADR-218 (the vault), `docs/specs/spec-repo-standard.md`, `src/axiom/extensions/builtins/context/docs/spec-context.md`

## Context

More and more of the people who open a repository built on Axiom are not
developers. Many have never built software, and the first thing they do is
ask an AI assistant to do it with them. Which assistant varies: Claude Code,
Cursor, GitHub Copilot, Gemini CLI, Codex, JetBrains Junie. Whatever the tool,
the repository is the only thing that can train it and keep its user on track.

ADR-051 already made `AGENTS.md` the one hand-authored instruction file and
made `axi context sync` fan it out to every tool that does not read it
natively. That solved *distribution*. It did not say what the file should
contain, so every repository wrote its own guidance, or none. The parts that
should be the same everywhere (plan first, take small steps, run the tests,
never commit a secret, read gold rather than silver, ask before anything
irreversible) were written several times in several voices, and drifted the
moment one copy was improved.

A repository needs two kinds of instruction. One is particular to it: what it
is, where code goes, which rules protect the people and systems it touches,
how to run it. Only its authors can write that. The other is the same in
every repository: how to work with an AI here without getting lost. Axiom can
write that once and keep it current.

## Decision

**Axiom owns one repo standard.** It has three layers, and Axiom supplies the
shared part of each.

1. **Tell.** `AGENTS.md` is canonical. It holds the repository's authored
   sections and one Axiom-managed block, "Working here with an AI", between
   `<!-- axiom:repo-standard:begin -->` and `<!-- axiom:repo-standard:end -->`.
   `axi context sync` keeps that block current and renders `AGENTS.md` into
   every tool's own file (`CLAUDE.md` as a symlink; `GEMINI.md`,
   `.github/copilot-instructions.md`, `.cursor/rules/axiom.mdc`,
   `.junie/guidelines.md` generated), so every assistant reads the same words.
2. **Help.** The block points the assistant at Axiom's verbs for hygiene,
   context, memory and secrets. An assistant that can call Axiom's MCP tools
   uses them; one that cannot is told the terminal command that does the same
   thing (`axi context check`, `axi hygiene stat repo`, `axi memory show`,
   `axi vault set`).
3. **Check.** `axi context check` runs before every commit (the git hook `axi
   context init` installs, and a local hook in `.pre-commit-config.yaml`) and
   in CI. It fails when a tool file has drifted from `AGENTS.md` or the
   managed block is stale. `axi hygiene stat repo` audits the rest of the
   standard (required files, required sections, CI concurrency, the pre-commit
   hook) and folds in the context findings, so one command answers whether a
   repository meets the standard. `--strict` makes it a CI gate.

The required files and sections are listed in `docs/specs/spec-repo-standard.md`.

### The managed block is versioned and upgraded by sync

- The block's text ships with Axiom
  (`src/axiom/extensions/builtins/context/templates/working-with-ai.md`). Its
  first line inside the markers states the version, `axiom repo standard vN`.
- Staleness is decided by comparing the whole block with the current
  rendering, not by the version number, so an edit that forgot to bump the
  version is still caught, and a hand edit inside the markers is caught too.
- Upgrading Axiom and running `axi context sync` rewrites only the bytes
  between the markers. Every byte outside them is left as it was. The block
  stays wherever it sits in the file, so an author may move it.
- The version number is for people and for `axi context status`, which
  reports "managed block outdated: vX, current is vY" with the fix.
- Unbalanced markers (one missing, two blocks, end before begin) are never
  guessed at. Sync leaves the file alone, check fails, and status says to fix
  the markers by hand.

The markers follow the convention every Axiom-managed region already uses
(`axiom:cross-mem`, `axiom:init`, `axiom:capabilities`), and the splice is the
shared `splice_marked_block`. There is one mechanism, not a second one.

### What stays authored per repository

Everything outside the markers: the title and introduction, what the
repository is and is not, where code goes, its safety rules, how to run and
test it, and anything else its authors want an assistant to know. The block
itself says that the authored sections win where they are more specific, and
that anything they forbid stays forbidden. Axiom never edits authored text,
and the standard requires sections, not wording: the audit matches headings
loosely, so a repository keeps its own voice.

### How a repository opts in

- **A new repository:** `axi context init` writes a starter `AGENTS.md` with
  the required section headings and the managed block, links `CLAUDE.md`,
  generates every tool file, and installs the pre-commit hook.
- **An existing repository:** the same command. It keeps the existing
  `AGENTS.md` (or promotes a hand-written `CLAUDE.md` to it), inserts the
  block before the first authored `##` section (never inside another managed
  region), and leaves every existing byte where it was. The author then fills
  any required sections `axi hygiene stat repo` reports as missing.
- **Having the markers is the opt-in.** A repository without them is reported
  by `axi context status` ("managed block missing") but never failed by
  `axi context check`, so upgrading Axiom breaks no repository that has not
  adopted the standard.

## Options considered

- **A. Axiom-owned and synced (chosen).** One copy of the shared guidance,
  improved once and delivered everywhere on the next sync, and checked so it
  cannot silently rot. It reuses what exists: `AGENTS.md` as canonical,
  `axi context` for distribution, the managed-region markers, and the hygiene
  audit surface. Its cost is that Axiom now owns words that appear in other
  people's repositories, so a change to the block is reviewed like a change
  to an interface.
- **B. A one-time template copy.** `init` would write the guidance once and
  let each repository own it from then on. It is simpler, and it drifts from
  the first improvement: the repositories that most need the newer guidance
  are the ones nobody revisits. This is the state the portfolio was already
  in, by hand.
- **C. Guidance documents only.** A page explaining how to work with an AI,
  linked from each README. Nothing reaches the assistant unless a person
  thinks to paste it in, which the people this is for will not know to do,
  and nothing checks it.

A won because the problem is keeping many repositories consistent for people
who cannot tell when they have drifted. Only an owned, synced and checked copy
does that.

## Consequences

- Every adopting repository tells every assistant the same working rules, in
  one voice, and gets improvements by running `axi context sync`.
- The block must stay short enough that a tool actually follows it (about 40
  to 70 lines; a test holds it there) and free of links, because the tool
  files rebase links by directory depth.
- The block names Axiom's verbs, so renaming one of them means a block
  version bump in the same change.
- A consumer distribution whose command is not `axi` still reads `axi` in the
  block. The block is not branded per install because a branded block would
  make `axi context check` disagree between two installs of the same
  repository. A consumer that needs its own command named says so in an
  authored section.
- Follow-ups: an `axi install` step that runs `axi context init` for managed
  environments (ADR-051 §D); a TIDY heartbeat sweep across a workspace using
  `axi hygiene stat repo`; deciding whether Axiom's own `AGENTS.md` adopts the
  block.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
