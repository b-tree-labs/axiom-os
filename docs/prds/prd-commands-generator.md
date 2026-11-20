# PRD: `axi commands` — cross-harness slash-command generator

**Status:** 🟡 Partial — generator extension shipped (#113); 🟦 designed
tier-aware filtering, hot-load refresh hook, conflict reporting CLI
flags. See parent [`prd-axi-cli.md §Status & Phasing`](prd-axi-cli.md#status--phasing).
**Owner:** Benjamin Booth •  **Last updated:** 2026-05-02
**Parents:** [prd-axi-cli.md](prd-axi-cli.md), [spec-axi-cli.md](../specs/spec-axi-cli.md), [spec-aeos-0.1.md](../specs/spec-aeos-0.1.md)
**Related:** [spec-extension-loading.md](../specs/spec-extension-loading.md) — supplies the `extensions.changed` event the generator subscribes to for auto-refresh

---

## 1. Problem

Axiom's value lives behind one CLI surface (`axi <noun> <verb>`), but
users invoke that surface from many different interactive harnesses —
Claude Code, Cursor, VS Code, Codex, OpenCode, Vim, Neovim, and direct
terminal. Each harness has its own slash-command and palette
conventions. Without a generator, either:

1. **Users author per-harness shims by hand.** Every harness in their
   workflow gets a partial, drifting copy of the noun-verb tree.
2. **The MCP server alone exposes everything.** Tools surface as
   contextually-named LLM tool calls (`mcp__axiom__ext_init`) — usable
   but ugly, breaking the natural "type `/axi ext init`" muscle memory.

We need a single source of truth (the installed-extensions noun-verb
tree, plus the chat slash-command registry) and a generator that emits
the right per-harness shims.

This PRD specifies that generator and the surfacing rules it honors.

---

## 2. Goals

- **One source of truth, many harnesses.** A user who installs an
  extension automatically gets correct shims wherever they invoke Axiom.
- **Honor incremental revelation.** The generator does not dump every
  verb into every harness. It honors the capability-tier and intent-group
  model from `prd-axi-cli.md` so a novice sees a clean small surface and
  a power user sees the full one.
- **Stay in sync across versions.** When extensions are added, removed,
  or upgraded, every previously-generated harness's shims refresh
  automatically. No silent drift.
- **Deterministic conflict resolution.** When two extensions both claim
  `/foo`, the resolution is documented, predictable, and reportable.
- **Auditable.** `axi commands list [--conflicts]` always shows what
  *would* be generated and why.

## 3. Non-goals

- Replacing per-harness UX entirely. Each harness has its own native
  tool-discovery surface (Claude's MCP tools, Cursor's tools menu,
  Codex's contextual invocation). The generator complements these.
- Mediating runtime invocation. Generated shims shell out to the same
  `axi` binary the user uses directly — no parallel execution path.
- Standardizing slash commands across harnesses. Each harness gets the
  surface that best fits its conventions (Claude nests; Cursor flattens;
  Codex has no slash-command surface and only gets MCP registration).

---

## 4. User experience

### 4.1 First-run

After installing Axiom:

```bash
$ axi commands generate
  [ok] claude     wrote 12 file(s) under .
  [ok] vscode     wrote 2 file(s) under .
Generated 14 file(s) across 2 harness(es).
```

By default, only the user's currently-reached tier is emitted — for a
novice with no Axiom usage history, that's the `starter` set (~10–15
verbs). The user can widen the surface explicitly:

```bash
$ axi commands generate --tier core      # opt up to core verbs
$ axi commands generate --tier all       # everything
```

Active harness selection defaults to "all of the harnesses we can detect
in this environment" (e.g. `.claude/` already exists → emit Claude
shims). Explicit selection:

```bash
$ axi commands generate --harness claude,vscode
```

### 4.2 Steady state

When an extension is added/removed/upgraded, `axi update` calls
`axi commands regenerate` automatically, refreshing every previously-
generated harness from the new noun-verb tree. The user sees a single
line in the update output:

```
↻ Refreshed shims for claude, vscode (3 new verbs, 1 removed)
```

### 4.3 Conflict reporting

When two extensions both claim a noun (or two extensions both define a
slash command with the same name):

```bash
$ axi commands list --conflicts
…
Conflicts (2):
  status               winner=hygiene       shadowed=other-ext (alphabetical-tiebreak)
  /save                winner=chat          shadowed=community-ext (lower-tier)
```

The losing definitions remain reachable via the namespace escape hatch
(`/<extension>:<command>`).

---

## 5. Surfacing rules

The generator consumes the same `effective surfacing rule` from
`spec-axi-cli.md §Capability Tiers and Familiarity Tracking`:

```
emit(verb V from extension E) iff
  tier_rank(V) ≤ min(global_tier(user), familiarity_tier(user, E))
```

Overrides:

- `--tier {starter|core|advanced|internal|all}` — pin the surfacing tier
  for this generation, bypassing the user's competency state.
- `--include-extensions <list>` / `--exclude-extensions <list>` — hard
  filter regardless of tier.

The generator emits a single comment block at the top of every shim file
recording the rule that was in effect when it was generated, so the
shim's contents are reproducible.

---

## 6. Conflict resolution

Mirrors Axiom's existing 3-tier scope hierarchy: **builtin < user <
project**. Higher tier wins; alphabetical-by-extension tiebreaks within
tier. Deterministic.

`--strict` upgrades any conflict to a non-zero exit (CI gate).

The `<extension>:<command>` namespace form is **always** available as
escape hatch for callers who need to address a shadowed verb explicitly.

---

## 7. Harness coverage

| Harness | Output | Notes |
|---|---|---|
| Claude Code | `.claude/commands/axi/<noun>/<verb>.md` | Nested namespace → `/axi <noun> <verb>` |
| Cursor | `.cursor/commands/axi-<noun>-<verb>.md` + `.cursor/mcp.json` | Flat namespace + MCP registration |
| VS Code | `.vscode/tasks.json` + `.vscode/mcp.json` | Run-Task palette + native MCP |
| Codex | `.codex/config.toml` `[mcp_servers.axiom]` | MCP registration only |
| OpenCode | `.opencode/opencode.json` `mcp.servers.axiom` | MCP registration only |
| Neovim | `.axi/shims/neovim/lua/axi.lua` | `:Axi <noun> <verb>` with tab-complete |
| Vim | `.axi/shims/vim/plugin/axi.vim` | `:Axi` with custom completion |

Per-harness renderer details live in the implementation; the contract
each renderer satisfies is:

- **Idempotent**: running `generate` twice produces byte-identical
  output for the same inputs.
- **Merge-safe**: where the harness's config file has other content
  (e.g. `.codex/config.toml`, `.opencode/opencode.json`), the renderer
  preserves it and updates only its own block.
- **Honors the surfacing rule**: emits only verbs cleared by the
  effective-tier filter.
- **Carries the generated marker**: a comment line identifying the
  generator, so users and tooling can tell shims from hand-written
  files.

---

## 8. State

`~/.axi/agents/commands/commands_state.json` records which harnesses
were generated to which output directories, with file counts and last-
generated timestamp. `axi update` reads this on every run to decide what
to refresh.

---

## 9. CLI

```
axi commands generate [--harness <list>|all] [--out-dir DIR] [--tier <tier>|all] [--strict] [--dry-run]
axi commands list [--conflicts]
axi commands regenerate
```

`regenerate` re-renders every harness in state, refreshing from the
current installed-extensions tree. Called from `axi update`.

---

## 10. Out of scope (deferred)

- Auto-detecting installed harnesses without `--harness` (today: user
  enumerates).
- Emitting Zed shims (same shape as VS Code; queued for follow-up).
- JetBrains plugin SDK integration.
- Per-extension `kind = "slash_command"` AEOS schema (today: chat ext
  owns the slash registry centrally; future work).
- Generator-driven completion-script emission (auto-completion is
  governed by `prd-axi-cli.md §Reliable shell auto-complete` and shipped
  by the `axi completions` verb, but the generator could publish a tier-
  aware completion snapshot in a future revision).

---

## 11. Dependencies

- `prd-axi-cli.md` — Capability tiers, intent groups, smart help model
- `spec-axi-cli.md` — Manifest schema, history schema, completion lifecycle
- `spec-aeos-0.1.md` — AEOS `kind = "cmd"` declaration with new
  `tier` / `intent_groups` / `verb_overrides` fields

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs.
Apache-2.0 licensed._
