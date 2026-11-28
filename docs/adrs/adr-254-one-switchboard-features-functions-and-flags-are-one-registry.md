# ADR-254: One switchboard: features, functions and flags are one registry

**Status:** Accepted (owner decision, 2026-11-10; owner decisions of 2026-11-12 folded into D3, D5 and D6 before landing)
**Related:** [ADR-164](adr-164-a-node-declares-its-functions.md) (a node declares its functions; `features`), [ADR-210](adr-210-a-feature-flag-is-declared-by-its-extension-and-resolved-default-env-node-account-user.md) (feature flags; `flags`), ADR-052 (extension-owned schemas), ADR-056 / ADR-072 (one skill, every surface), ADR-174 (one runtime database), ADR-182 (migrations expand, then contract), ADR-185 (chaining declarations), #1754 (`[extension.optional]`, optional extensions switched with `features enable|disable`)
**Amends:** ADR-164 (the `features` noun now carries every switch, not only node functions), ADR-210 §3 (one more resolution rule) and §5 (the noun is `features`; `flags` is an alias through the 0.70 line)
**Spec:** [`spec-flags.md`](../specs/spec-flags.md) (the flag kind), extension README `src/axiom/extensions/builtins/features/README.md`

Amended 2026-11-12 by the owner: two command nouns over one registry; `features` covers what a node runs (extensions, functions, the role, agents and maintenance policies) and no longer lists or sets feature flags, and `flags` covers every flag verb (list, resolve, set, explain, the kill switch, cohorts and their import, rollouts, stage plans, schedules, prerequisites, approvals, variants) as a visible noun. MCP follows the split (`axiom_features__*` for the node, `axiom_flags__*` for flags; a flag tool that existed only as `axiom_features__<verb>` keeps that name, deprecated, for one release). `/api/v1/flags` is the canonical flags endpoint and `/api/v1/features` keeps returning the whole registry. The 0.71.0 removal of the `flags` noun and the `axiom_flags__*` tools (D5) is cancelled; the `flags/registry.py` and `flags/service.py` import shims may still go at 0.71.0. The registry, the resolver, the extension-off rule (D3), audit (D4) and storage (D6) do not change. Details: [`spec-flags.md`](../specs/spec-flags.md) §0.1.

## Context

A node had two places to turn things on and off, with two nouns, two stores and
two audit paths, and neither knew about the other.

- `features` (ADR-164, #1754): the node's role and functions, which commands
  show, the background-agents service, the site policies, and since #1754 the
  optional extensions (`[extension.optional]`). Node scope only. Written to the
  node file (`node.toml`) so discovery can read it before any database is up.
  Its writes went through `invoke_capability` and so into the action audit, but
  noted no change (the change log showed the verb, not old and new).
- `flags` (ADR-210): product surfaces declared by extensions in `[[flags]]`,
  values `on | off | soon`, overrides per node, account and user in the
  `flag_overrides` table (schema `flags`), resolution default, env, variant,
  node, account, user; a CLI, MCP tools (`axiom_flags__*`) and `/api/v1/flags`,
  which appkit's `FlagProvider` reads for the rail. Every write audited with its
  change.

Turning an extension off with `features disable` left its flags resolving
whatever their layers said, and its commands only disappeared because discovery
dropped them. A person asking "what is on here" had to run two commands and
reconcile the answers; a settings page would need two panels. ADR-210 §5 said
"the CLI noun is `flags`, never `features`", a line written when `features` was
only the ADR-164 function set.

The owner's decision of 2026-11-10: one switchboard, one noun, one audit.

## Decision

**One registry of switches under the `features` noun. A switch has a kind,
and the kind fixes its scopes. An extension that is off takes its functions
and flags with it. Every write is audited once, the same way. The node file
stays what discovery reads.**

- **D1 — Three kinds, one registry.** Every switch is declared in a manifest,
  as today, and listed together with its kind, its owner (the extension that
  declares it; `""` for the platform's own) and the scopes it can be set at:
  `extension` (an optional extension, `[extension.optional]`), `function`
  (an ADR-164 node function, a command, the background-agents service, and the
  site policies agents, maintenance and record of truth, whose value is a
  level), `flag` (a `[[flags]]` declaration, ADR-210). `features.list` returns
  the whole board or one kind; `features.set` sets any kind with that kind's
  rules; `features.resolve` resolves the flags for a caller.
- **D2 — Scopes by kind.** Extension and function switches change what runs on
  a machine, so they are node scope only and are set on the node itself; a
  request to set one for an account or a user is refused and says why. Flags
  keep node, account and user scopes and the `soon` value, which only a flag
  has.
- **D3 — The extension-off rule.** An extension that is off on a node makes
  every function and flag it owns resolve `off`, on every surface (CLI, MCP,
  HTTP, and so appkit's slice), whatever their own layers say. The resolution
  reports the source `extension-off`, so "why is this off for me" still has an
  answer. A flag whose extension is off **may be set** (owner decision,
  2026-11-12, replacing an HTTP 409 refusal): the value is stored, the flag
  still resolves `off` with source `extension-off`, and every surface (CLI list
  and resolve, MCP, HTTP `/features` and `/flags`, the in-process resolver
  appkit's slice reads) shows it as waiting on that extension (`waiting_on:
  "<extension>"`, and the words "waiting on extension <name>"), with the value
  the layers give it (`staged`). When the extension turns on, the stored value
  takes effect at once. This lets a rollout be staged before an extension is
  turned on. A command of an off extension still cannot be turned on by its own
  name, and the refusal names the extension to turn on: only flag values
  change. Flags of an off extension stay in the registry so the board can list
  them and say why they are off.
- **D4 — One audit.** Every switch write is a mutating skill reached through
  `invoke_capability`, so it lands in the action audit chain with the caller as
  principal, refused or not; and every write notes its change (`note_change`,
  kind `extension`, `function` or `flag`, subject, old, new), so the change log
  shows what changed, for role and policy changes too. A stored row (a flag
  override, or a recorded switch, D6) carries `updated_by` and `updated_at`.
- **D5 — One noun, and a scheduled alias.** `features` is the user-facing noun
  on every surface: CLI `features`, MCP `axiom_features__list|resolve|set|
  enable|disable|role`, HTTP `/api/v1/features`. `flags <verb>` keeps working
  as a hidden alias (`tier = "internal"`, so help does not list it) and prints
  one line to stderr naming the new noun and the release it goes in;
  `axiom_flags__list|resolve|set` and `/api/v1/flags` keep answering as
  aliases, unchanged in shape, so appkit moves on its own schedule. The
  schedule (owner decision, 2026-11-12): the alias stays through the 0.70 line.
  The CLI noun, the `axiom_flags__*` MCP tools and the `flags/registry.py` /
  `flags/service.py` re-exports are removed at 0.71.0. The `/api/v1/flags`
  HTTP route stays until an appkit release that calls `/api/v1/features` is
  deployed on every node we run, even if that is later than 0.71.0. The alias records its writes under the `flags.set` name, so a
  reader of the chain can tell which surface a write came through.
- **D6 — Early reads stay; the record and its cache.** An extension or function
  switch is always written to the node file, which discovery reads before any
  database is up and which a node without a database has as its only record.
  When the node has a database, the switch is also recorded in `flag_overrides`
  as a node-scope row keyed `extension:<name>` or `function:<name>` (flag keys
  never contain `:`, so the table's schema and constraints are untouched; this
  is an expand, ADR-182). Reads of switch state come from the node file, the
  copy that decides what runs; when the record and the file disagree, the board
  says so and names the fix, and never reconciles them on its own, in either
  direction. Drift also raises one alert through the platform's one
  notification path (ADR-249, ADR-252: a notification with a cause, kind
  `node.switch_drift`, owned by KEEL), so it does not depend on someone reading
  `features list`; reading the same drift again raises nothing more, and a drift
  that is fixed and comes back is raised again (owner decision, 2026-11-12). A
  node whose role needs no database is never asked for one, for reading or
  writing.
- **D7 — What moves, what stays.** The flag declarations and the resolver move
  into the `features` extension (`features/flag_registry.py`,
  `features/resolver.py`); the switchboard registry (`features/registry.py`),
  the merged skills, CLI and HTTP face are new there. The `flags` extension
  keeps the storage, because the schema name is a contract: `flag_overrides` in
  schema `flags`, its migrations and `OverrideStore`, and the alias surfaces.
  The old import paths (`flags.registry`, `flags.service`, `flags.skills`,
  `flags.api`) stay importable as re-exports. Nothing an external caller
  imports is deleted; migrations are append-only.

### Technical design

| Piece | Where | Reads | Writes |
|---|---|---|---|
| switchboard rows (`Switch`, `switchboard`, `classify`) | `features/registry.py` | node file, discovered commands (with the off extension's, `discover_cli_commands(include_off=True)`), optional blocks, flag resolution | nothing |
| optional-extension switch, the off set | `axiom.infra.optional_extensions` (`optional_blocks` cached per process, `switched_off` read fresh from the node file) | manifests once, node file per call | nothing |
| flag declarations | `features/flag_registry.py` | manifests, `axiom.flags` entry points | nothing |
| flag resolution and D3 | `features/resolver.py` (`FlagService`, `NoOverrides` for a node with no database) | registry, env, variant, `flag_overrides`, the off set | `flag_overrides` (a flag override) |
| node-scope switch | `features/skills/_node.py` | node file | node file (`node_functions.enable/disable/forget`), then `features/record.py` |
| record, drift and its alert (D6) | `features/record.py` | `flag_overrides` node rows, the alert memo | `flag_overrides` rows `extension:<n>` / `function:<n>`; one `notifications.send` with a cause per new drift; `<state>/features/switch-drift-alerted.json` |
| flag verbs and aliases | `features/skills/_flags.py`, bound by `features/skills/__init__.py` as `features.*` and `flags.*` | | |
| CLI | `features/cli.py`; `flags/cli.py` the alias | | |
| HTTP | `features/api.py`: `register_routes` (`/features`), `register_flag_routes` (`/flags`, via `flags/api.py`) | | |

Resolution of a flag, later wins, then D3 over all of it: default for the
environment, `AXIOM_FLAG_<KEY>`, the variant's flag set, node, account, user
(`protected_in` skips the stored layers); if the owner is in the off set, `off`
with source `extension-off`, and the resolution also carries `waiting_on` (key
to extension) and `staged` (key to the value and source the layers gave, which
applies once the extension is on). The off set is read from the node file on every
resolve, like `_switched_on` in discovery, so a long-lived server sees a switch
without restarting.

A name can be both a command and a flag (a verb and the flag that gates it).
`enable` and `disable` pick the node switch, which is what they have always
meant; `set` picks the flag, which is the scoped verb; `--kind` says it
outright.

## Options considered

- **Keep two nouns and cross-link them.** Cheapest; it lost because the
  question "what is on here" still has two answers, the extension-off rule has
  no single home, and a settings page needs two panels.
- **Fold the node switches into the flags table with a `kind` column.** The
  primary key is `(scope, scope_id, key)`; a flag and a command with one name
  would collide unless `kind` joined the key, which is a contract change on a
  live table. Prefixed keys in the same table give the record (D6) with no
  schema change.
- **Make the database the only record for extension switches.** Discovery
  reads the node file before any database is up, and a collector node has no
  database at all. The file stays, as the cache discovery reads; the database
  is the record where there is one.
- **Drop `flags` outright.** appkit reads `/api/v1/flags` and the MCP tools
  are in use; an alias through the 0.70 line costs little and lets each
  consumer move in its own pull request.

## Consequences

- One command answers "what is on this node and why": `features` lists
  extensions, functions and flags with kind, owner, scope and source. One page
  can show the same (appkit follow-up: one Settings page listing the three
  kinds; the rail slice reads `sources` and sees `extension-off`).
- Turning an extension off is complete: its commands, tools, routes, schedules
  (#1754) and now its flags go with it, and nothing can be configured behind
  its back.
- The change log shows switch writes with old and new, filterable by kind
  (`extension`, `function`, `flag`), beside the flag writes it already showed.
- A node's switch state survives the node file (recorded) and the database
  (the file); drift between them is visible and raises one alert, never
  silent and never auto-reconciled.
- Costs: the `flags` extension is now storage plus alias, which reads oddly
  until the alias goes; `features` claims chain level B and carries two more
  skills. A flag of an off extension can be pre-set, so a stored value can sit
  unused for as long as its extension stays off; every surface says so
  (`waiting_on`), so it is never a silent setting.
- Follow-ups: at 0.71.0 remove the `flags` alias noun and the `axiom_flags__*`
  tools, with the `flags/registry.py`, `flags/service.py` re-exports; remove
  the `/api/v1/flags` route once an appkit release calling `/api/v1/features`
  runs on every node;
  appkit `FlagProvider` and `FlagsAdmin` move to `/api/v1/features` and the
  `axiom_features__*` names; a Settings page for the three kinds.
