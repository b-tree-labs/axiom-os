# Spec: Program Tracking (phase 1)

**Owner:** Platform (B-Tree Labs / UT) • **Status:** Active • **Last updated:** 2026-10-05
**PRD:** [prd-program.md](../prds/prd-program.md) • **Key ADRs:** [ADR-161](../adrs/adr-161-program-tracking-is-a-composed-clerk.md), [ADR-162](../adrs/adr-162-the-watcher-primitive.md), ADR-056 (CLI verbs over skill functions)

## Overview

The `program` extension keeps a program's status answerable from one data
file (prd-program R2). Phase 1 ships the contract and the read side: the
`axiom.program/0.1` data-file schema with load/validate/save, the
parameterized `status` read, the static status-page `render`, and the
`validate` check. Everything is a registry skill per ADR-056; `axi program`
is the thin CLI face. The coordinator agent (CLERK), capture watchers
(ADR-162), and the write verbs (`collect`, `draft`, `post`, `priorities` as
a proposer, `attach`) are later phases — nothing in this phase depends on
them, and nothing here names a vendor or a domain (R10).

## Contracts

### The data file — `axiom.program/0.1`

One JSON document per program. Top level:

| Key | Required | Shape |
|---|---|---|
| `schema` | yes | the literal `"axiom.program/0.1"` |
| `program` | yes | object; `id` and `name` are required non-empty strings. Optional fields are carried verbatim (`as_of`, `deputy`, URL fields, `children`, …). `tracker`, when present, is an object that must name its `host` (`kind` and `project_id` are deployment data). |
| `lanes` | no | list of objects, each with a unique non-empty `id`; other fields (`name`, `color`, …) carried verbatim |
| `people` | no | list of objects, each with a unique `principal` in the platform form `@name` / `@name:context` (single leading `@`, ADR-020); `lanes` entries must reference declared lane ids |
| `schedule` | no | list of items — see below |
| `provenance` | no | object, carried verbatim |
| *anything else* | no | a **host-qualified binding block**: must be an object, is otherwise uninterpreted and carried verbatim through every load/save round trip. Binding-block names are the deployment's data, never platform vocabulary. |

Schedule items:

- `id` (required, unique) and `label` (required).
- Dates: either a single ISO `date`, or a `start`/`end` ISO pair — one of
  the two forms is required, and a `start` without an `end` (or vice versa)
  is a defect.
- `lane`, when present, must reference a declared lane id.
- `owner`, when present, must be a principal listed in `people` — an
  item's owner is always a principal the ownership map knows.
- `status`, when present, is exactly `proposed` or `committed` (closed
  vocabulary; a date is proposed until its owner commits it).
- `pct`, when present, is an integer 0–100.
- `issue`, when present, is a reference into the program's tracker binding
  (resolved against `program.tracker.host` / `project_id`; the platform
  never constructs a vendor URL from it).
- Every other field (`kind`, URL fields, …) is carried verbatim.

**Failure contract:** `load_program` / `save_program` raise
`ProgramValidationError` carrying `errors` — *every* defect found in one
pass, in document order — never just the first. Saving re-validates, so a
process cannot write a file the next reader refuses. Unknown fields are
never dropped: a field the loader drops is worse than a missing field.

Python surface (`axiom.extensions.builtins.program`): `PROGRAM_SCHEMA`,
`STATUS_VALUES`, `ProgramData` (lossless `raw` plus view accessors,
including `bindings` for the non-enumerated top-level blocks),
`load_program`, `save_program`, `validate_program`, `ProgramError`,
`ProgramValidationError`.

### Skills (registry capabilities, ADR-056)

All three take an optional `data` param (path to the data file; default
`<state_dir>/program/data.json`) and return a `SkillResult`.

**`program.status`** — the read tool (R1). Params: `scope` (required, one
of `person | lane | item | schedule | priorities`), `key` (required for
`person`/`lane`/`item`), `fmt` (`brief` default, or `full`). The result
`value` carries the question (`scope`, `key`, `fmt`), the program's
identity (`id`, `name`, `as_of`), `items`, and `count`. Each item carries
exactly: `id`, `label`, `owner`, `dates` (`start`/`end`/`date`, each
present and `None` when the file does not state it), `status`
(`proposed`/`committed`/`None`), `pct`, and `links`. `fmt=full` adds the
entry's remaining fields, and for `person` scope the person's own record.

Links are only what the file states: string fields that are URLs become
`{kind: "url", field, href}`; an `issue` under a declared tracker becomes
`{kind: "tracker", host, project, ref}` — host-qualified data, no vendor
URL grammar.

Scoping semantics:

- `person <principal>` — the items that principal owns. A listed principal
  with no items is an empty list; an unlisted principal is a refusal.
- `lane <id>` / `item <id>` — likewise: declared-but-empty answers empty,
  undeclared refuses.
- `schedule` — every item, file order.
- `priorities` — the status-bearing items only (an entry with no status is
  not a priority anybody stated), `committed` before `proposed`, earliest
  `end`/`date` first, undated last.

**Absent is absent.** The skill answers only from the data file. Missing
fields come back as `None`, unknown keys as refusals; nothing is inferred
or invented.

**`program.render`** — params: `data`, `out` (output directory; default
`<state_dir>/program/site`). Writes exactly one static page,
`<out>/status.html`, from a built-in template, stdlib only. Page contract:
one section per lane in file order (a lane with no items keeps its
heading; items with no lane group under "Unlaned"); every item renders a
row anchored `id="item-<id>"` with its id, label, owner, dates, status
chip, percent bar, and links (URLs as anchors, tracker refs as
`host/project#ref` text); all file-sourced text is HTML-escaped. An
invalid data file renders nothing.

**`program.validate`** — params: `data`. Valid → `ok` with the file's
shape (schema, program id, section counts, binding-block names). Invalid →
`ok=False` with the complete defect list as `errors`.

### CLI — `axi program`

Per ADR-056, each verb is a 1:1 thin wrapper dispatching through
`invoke_capability` on the `cli` surface; handlers hold zero logic.

```
axi program status --scope person|lane|item|schedule|priorities
                   [--key K] [--fmt brief|full] [--data PATH] [--json]
axi program render   [--data PATH] [--out DIR] [--json]
axi program validate [--data PATH] [--json]
```

Output: the result `value` as JSON on stdout; errors on stderr with exit 1.
`--json` emits the full `{ok, value, errors}` envelope.

## Design

Flat built-in layout per AEOS §5.1.1: `model.py` (the data-file contract),
`skills/` (`status`, `render`, `validate`, bound via `bind` /
`bind_default` with `SkillSpec`s — `status` and `validate` declared
read-only, `render` a side-effecting write), `cli.py`, manifest with one
`cmd` noun and three `skill` blocks. Tests live in `tests/` beside the
code, with the AEOS standard conformance subclass in `tests/unit_tests/`.
Fixtures use invented generic vocabulary only.

Failure modes: unreadable or non-JSON file → `ProgramError`; schema
violations → `ProgramValidationError` with the full defect list; every
skill converts both into `ok=False` refusals, and `render` refuses before
touching the output directory.

## Decisions

- **ADR-161** — program tracking is a composed extension; skills are
  commons, the coordinator is thin and arrives later. Phase 1 ships only
  skills, deliberately.
- **ADR-162** — capture is the watcher primitive's job; this extension
  therefore contains no feeders, pollers, or bus code in phase 1.
- **ADR-056** — every CLI verb is a registered skill function; the
  `validate` verb exists as `program.validate` for exactly this reason.
- **prd-program R10** — no vendor, no domain: tracker hosts, binding-block
  names, lane names and principals are all deployment data.

## Open questions

- Where the data file canonically lives per program (phase 1 defaults to
  `<state_dir>/program/data.json` and every verb takes `--data`); settles
  when `collect`/`post` land and the file gains an updating writer. Owner:
  the coordinator phase.
- Whether `status`/`validate` project to MCP as read tools before the
  coordinator phase. Owner: the serving-face work.
