# Write a log extension

A log is a signed, append-only record of what people did: each entry is a person's statement,
signed by them at the moment they confirm it, chained to the entry before it, and never changed
afterwards (ADR-142, ADR-143). Your extension declares a log in one file; the platform does the
rest: it validates the declaration, signs and chains the entries, keeps them per site, checks
the chain, and shows the log on every surface. In code the thing is called a `logbook`
(ADR-150): the manifest kind, the `[logbook]` table, the routes and the skills. Everything a
person reads says "log".

This guide takes you from nothing to a log people can sign in. Two worked examples sit beside
it: the [example log](examples/example_logbook/README.md), which uses only keys the platform
enforces today, and the [field log](examples/field_log/README.md), which also shows keys that
are declared but not yet enforced.

## 1. Scaffold it

```sh
axi ext init my_notes --template logbook
```

This writes the usual extension layout plus `logbook/my_notes.toml`, a copy of the example log
renamed, and a `kind = "logbook"` entry in `axiom-extension.toml` that points at it. The log's
handle, the word people type after `axi log`, is the id with `_` read as `-`: `my-notes`. Set
`handle = "notes"` in `[logbook]` to choose another.

## 2. Declare the log, section by section

Open `logbook/my_notes.toml`. Every key is validated when the node loads it, and a key the
loader does not know is refused, so a typo fails at once rather than vanishing.

- **`[logbook]`**: `id` (one word, `[a-z0-9_]+`, unique on the node), `version`, `display`
  (what people see), an optional `handle`, and `assurance = { posture = "..." }`, the floor
  every entry type inherits. `attested` can be signed at the command line; `sso` needs a
  single sign-on session.
- **`[interval.<kind>]`**: a period of work opened by one entry type and closed by another
  (`opens`, `closes`). Entries signed while it is open carry its number. A type can require
  one: `requires_interval = "<kind>"`, or `"none_open"` for the entry that opens it.
- **`[[obligation]]`**: what is due while an interval is open. `type` is the entry type owed,
  `every` the cadence in minutes (`{ default = 60 }`, or a `site_key` a site may set),
  `while = "interval.<kind>.open"`, `warn_before`, who to `notify`, and whether it is
  `required`.
- **`[[type]]`**: each kind of entry: `id` (UPPER_SNAKE), `meanings` (what signing means:
  `authored`, `performed`, `observed`, ...), and `roles`, who may sign it.
- **`fields`**: each value an entry holds: `id`, `type` (`text`, `quantity` with its `unit`,
  `number`, `choice` with its `choices`, `attest_checkbox`, ...), `required`, and `observe`.
  An `observe = true` field is one only the signer may enter: no surface fills it in and no
  software may offer a value for it (ADR-144).
- **`[[export]]`**: a form a person can export the log as (`id`, a `template` file beside the
  log file, and the `site_overrides` a site may set: `header`, `filename`). The template is
  filled with the selected entries, the chain head and the verification result; nothing in it
  runs. Export one with `axi log my-notes export --form <id> --out <dir>`.
- **`assurance`** on a type raises the floor for that type only. **`cosign`** names the role of
  a second person who must co-sign, and the assurance they need.

## 3. Roles and sites

A person signs only the types their role allows, at a site. Roles are held per site, and for
now they live in the node's state directory (interim, until the authorization seam carries
site relations). Grant one:

```sh
axi log role grant @you:example-site member --site example-site
axi log role list --site example-site
```

## 4. Check it

```sh
axi ext lint
axi ext doctor
axi log validate logbook/my_notes.toml
```

`axi ext doctor` reports `logbook.declared`: whether the log loads, its entry types, and any
key that is declared but not yet enforced. `axi log validate logbook/my_notes.toml` prints the same list.

## 5. Sign and verify

Copy the extension to `<project>/.axi/extensions/my_notes` and restart the app. Then:

```sh
axi log list
axi log my-notes about
axi log my-notes new OPEN_PERIOD --title "Start"
axi log my-notes show
axi log my-notes verify
axi log my-notes due
```

`new` shows the exact entry and asks: sign, hold or ask. Only `sign` signs; it exits 3 when
nothing was signed. `verify` walks the chain and names the first broken entry, if any.

## 6. Where it shows

Nothing more to build. The log appears:

- in chat and over MCP, through the read skills `attest.logbook_list`, `attest.show`,
  `attest.verify` and `attest.obligations` (tools `axiom_attest__*`). None of them signs;
- over HTTP at `/api/v1/attest/logbooks/<id>`, `/api/v1/attest/<id>/records` and
  `/api/v1/attest/<id>/verify`;
- on the Log page of the web app, with its entry types as facets.

## Declared but not yet enforced

The loader accepts these keys so a log can be written once, and lists them under "declared,
not yet enforced" so nobody mistakes them for rules in force. The field log declares the first.

| Key | What it will do | Who enforces it |
|---|---|---|
| `logbook.partition` | One chain per partition (one per field, say) instead of one per site | Not yet planned |

Other keys the loader defers the same way: `[logbook].retention` (shown, not yet applied),
`[[crosscheck]]`, a type's `voice`, a co-signature's `window` and `party`, and
`assurance.personal_key`. A deadline relative to a task (within 7 days of something) cannot be
declared yet: an obligation's cadence is `every` so many minutes while an interval is open.

## Rules that never bend

- **Software proposes; a person signs.** An agent or an integration may make a draft for a
  named person (`axi log my-notes propose NOTE --title "Note" --for @you:example-site`); it can never sign, and no signing skill is on
  MCP or the agent surface.
- **Software never supplies an observed value.** A proposal leaves every `observe` field empty
  for the person to enter.
- **Use the library, never the tables.** Code that writes or reads entries goes through
  `axiom.attest` (`drafts`, `confirm`, `sign`, `query`, `verify`). Never write the attest
  tables or import the extension's service directly.
