# features: what a node runs, and the one registry

One registry of everything that can be turned on or off on a node
([ADR-254](../../../../../docs/adrs/adr-254-one-switchboard-features-functions-and-flags-are-one-registry.md)),
in three kinds, under two command nouns (owner decision, 2026-11-12):

| Kind | What it turns on or off | Declared in | Scopes | Values | Noun |
|---|---|---|---|---|---|
| `extension` | an optional extension | `[extension.optional]` | node | on, off | `features` |
| `function` | an ADR-164 node function, a command, the background-agents service, the site policies | `[[extension.provides]] functions = [...]`, the node file | node | on, off (a policy: its level) | `features` |
| `flag` | a product surface | `[[flags]]` (ADR-210) | node, account, user | on, off, soon | `flags` |

Everything is declared in manifests; the registry reads the declarations together and gives
each one row: kind, owner (the extension that declares it), scopes, state and where the state
came from. **An extension that is off makes every function and flag it owns resolve off, on
every surface** (source `extension-off`). A flag of an off extension may still be set: the
value is stored and every surface shows it as "waiting on extension <name>" (`waiting_on`); it
applies the moment the extension is turned on. A command of an off extension is still refused
by its own name.

This extension owns the registry, the flag resolver and the skills of both nouns. Feature
flags at the command line are the `flags` noun (the `flags` extension, which also keeps their
storage); their verbs are in [`docs/specs/spec-flags.md`](../../../../../docs/specs/spec-flags.md).

## Command line

```
features                               what this node is for, and what runs on it
features list --kind extension         one kind: extension | function
features enable NAME | disable NAME    turn an extension, function, command or background-agents on or off
features set NAME on|off|clear [--kind extension|function]   the same, by value (node scope only)
features clear NAME
features role NAME | agents POLICY | maintenance LEVEL | record-of-truth upstream|local
```

`features` lists no flag: it says how many are declared and points at `flags list`. A flag verb
given to `features` (`resolve`, `explain`, `kill`, `cohort`, `rollout`, `stage`, `approve`,
`changes`, `cancel`, `tick`) exits 2 and names the `flags` command to run, and `features set`,
`enable`, `disable` or `clear` on a flag's name is refused with `flags set NAME VALUE`. The CLI
name is the install's own (branding), not a fixed word.

## Surfaces

| Surface | Names |
|---|---|
| MCP | `axiom_features__list`, `axiom_features__set`, `axiom_features__enable`, `axiom_features__disable`, `axiom_features__role`; the flag verbs are `axiom_flags__*`, and the flag tools that were only `axiom_features__<verb>` keep that name, deprecated, until 0.71.0 |
| HTTP | `GET /api/v1/features` (the whole registry: every kind, for node setup and the console), `PATCH /api/v1/features/{key}`; `/api/v1/flags` is the canonical flags endpoint |

Every verb goes through `invoke_capability`, so a write is gated, audited and measured the
same from the CLI, MCP and the web.

## Where things live

| Piece | Module |
|---|---|
| the registry (`Switch`, `switchboard`, `classify`) | `registry.py` |
| flag declarations (`[[flags]]`, `axiom.flags` entry points) | `flag_registry.py` |
| flag resolution and the extension-off rule | `resolver.py` |
| the record of a node-scope row, drift against the node file, and its alert | `record.py` |
| the node verbs (`features.*`) | `skills/_node.py` |
| the flag verbs (`flags.*`) | `skills/_flags.py`, `skills/_loop.py`, `skills/_modes.py` |
| the `features` CLI; the `flags` CLI | `cli.py`; `axiom.extensions.builtins.flags.cli` |
| the node file (`node.toml`) | `axiom.infra.node_functions` |
| the optional-extension setting discovery reads | `axiom.infra.optional_extensions` |
| flag override storage (`flag_overrides`, schema `flags`) | `axiom.extensions.builtins.flags.store` |

## Record and cache

An `extension` or `function` row is written to the node file always (discovery reads it
before any database is up; a node without a database has nothing else) and, when this node
has a database, recorded in `flag_overrides` as a node-scope row keyed `extension:<name>` /
`function:<name>`. Reads come from the node file; when the two disagree `features` says so
(`drift`) and names the fix. It never reconciles them on its own, in either direction. Each
drift also raises one alert through the one notification path (a notification with a cause,
`node.switch_drift`, owned by KEEL), so an operator hears of it without reading `features`;
the same drift read again raises nothing more, and a drift that is fixed and comes back is
raised again.

## Audit

Every write is a mutating skill, so `invoke_capability` journals it in the action audit
(`audit actions`) with the caller as principal, and each write notes its change (kind
`extension`, `function` or `flag`; old and new) for the change log.

## Cohorts, rollouts, kill switch and explain

A flag's exposure is decided in the record, after the code is on main (ADR-257 D5), with the
`flags` noun:

    flags explain KEY [--user U] [--account A] [--role R]   why it has its value, for whom
    flags cohort set KEY --principal P --rule JSON --reason R   preview; add --apply to write
    flags rollout set KEY --rules JSON --reason R               preview; add --apply to write
    flags kill KEY --reason R                                   off for everyone, at once
    flags kill KEY --release

Cohorts and rollouts live in two tables beside `flag_overrides` (schema `flags`);
the node file is never written. Resolution, most decisive first: extension-off,
kill switch, prerequisite, user, account, rollout rules (then the stage plan), node,
variant, environment, default.

Every standard rollout mode (spec §8): a percentage moves any unit (`--bucket-by node`,
`country`, `cohort:a,b`); geography and local time are attributes; a cohort lists up to
100,000 members (`flags cohort import KEY --file F`) or follows a directory group
(`--source-group G --refresh-every 1h`); a flag may declare variants and a rule may
split across them; `flags stage set KEY --plan JSON` starts a progressive rollout the
scheduler advances while its gates hold; any write takes `--at T --timezone Z`; a flag
may `requires` another; a flag declares its `kind`; and a policy per environment can
hold a change for a second person (`flags approve ID`).
Cohort and rollout writes are destructive (a person approves the plan off the
command line); the kill switch is a reversible write and needs no plan. The rules,
the attribute catalogue and the bucket are in `docs/specs/spec-flags.md` §7.
