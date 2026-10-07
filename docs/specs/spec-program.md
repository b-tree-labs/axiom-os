# Spec: Program Tracking (phases 1–6 shipped; phase 7+ design)

**Owner:** Platform (B-Tree Labs / UT) • **Status:** Active • **Last updated:** 2026-10-06
**PRD:** [prd-program.md](../prds/prd-program.md) • **Key ADRs:** [ADR-161](../adrs/adr-161-program-tracking-is-a-composed-clerk.md), [ADR-162](../adrs/adr-162-the-watcher-primitive.md), [ADR-165](../adrs/adr-165-program-self-update-and-per-consumer-change-detection.md), [ADR-166](../adrs/adr-166-program-membership-is-a-principal-not-a-tracker-seat.md), [ADR-167](../adrs/adr-167-the-program-is-editable-through-its-own-tool.md), [ADR-171](../adrs/adr-171-adaptivity-every-external-system-is-an-optional-additive-connector.md), [ADR-172](../adrs/adr-172-products-are-the-composable-spine-up-a-ladder-to-a-north-star.md), [ADR-173](../adrs/adr-173-estimation-is-deterministic-the-model-proposes-a-size-never-a-date.md), [ADR-176](../adrs/adr-176-emergent-work-and-divergence.md), ADR-056 (CLI verbs over skill functions)

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

Phase 2 is the serving projection. The two reads, `program.status` and
`program.validate`, project to the composed Axiom MCP as read-only tools
through the skill registry (ADR-073); `program.status` is also served at
`GET /program/status` on the composed HTTP substrate behind the node's
authz; and `program.status` gains a `drift` scope that reports, from the
data file alone, what is detectably inconsistent (R11, the data-file
half). `render` writes files and stays CLI-only.

Phase 3 is the self-update core (R3/R4/R11/R12). `program.sync` reconciles
the data file against a pluggable read-only **source** and appends an
append-only **change log**, idempotently (a content hash per item and per
field, so a change seen twice is not logged twice); `program.changes`
answers **"what changed since this principal last looked"** against a
per-consumer **watermark**, in the same shape as `status`; and the thin
scheduled **CLERK** agent runs `sync` on its heartbeat. Reporting changes is
a read and advancing a watermark is a write, kept apart so `changes`
projects as a read-only MCP/HTTP tool. Live GitLab/GitHub capture feeders
(the ADR-162 watcher instances that fill the source seam) are phase 4, below.

Phase 4 is the live capture feeders (R4, R9, R13). `GitLabSource` and
`GitHubSource` implement the phase-3 source seam as instances of the ADR-162
watcher primitive — a shared cursor/debounce/content-identity-dedup core
(`axiom.infra.watcher`) each feeder rides rather than re-polling. They read a
program's tracker cursor-first (`updated_after`), overlay the live state onto
the node's authoritative data file (never clobbering the human-committed
fields), **attribute** activity to principals through a per-person account map
and compute the **proxy-assignee** for an owner with no tracker account
(ADR-166), and **detect** drift — missing accounts, due-date disagreements,
orphan issues, and (the GitHub source) repository-mirror gaps collapsed on
commit identity. A source is used only after a connector-readiness ladder (R9);
one that cannot be verified is skipped loudly. Writing back to a tracker is a
later phase — this phase ingests, attributes, and detects.

Phases 5–6 are the **mutation surface** (R2, R6, ADR-167): the data file is
the authoritative editable source of truth, edited through the program's own
tool and nothing else. `program person|lane|item` add/edit/remove/reassign
members, lanes and schedule items; `program invite` / `program redeem` are a
join flow riding the platform gate invitation primitive; and `program
ownership` reports an item's or lane's owner over time. Every mutation writes
`data.json` through the locked state helpers, appends a change-log entry (with
`by` — who acted), advances the snapshot so a later `sync` does not re-log it,
and is identity-gated in-body (the deputy or a maintainer, with an open-posture
operator allowance for solo/dev nodes). **None of it requires GitLab, GitHub,
Claude Code, or any harness** — external trackers are optional feeders, a
harness is one MCP client, and neither is a precondition for editing the
program. The mutations are CLI-only, never an anonymous MCP tool.
Phase 5 is crosslink health and account-aware reads (R11 link health, R13 the
non-tracker viewer). A feeder pass now also **checks that the program's links
still resolve** — each tracker-bound item's `issue` binding and each declared
endpoint — read-only, honouring connector readiness (an unreachable checker
reports `unverified`, never `healthy`). A confirmed-dead link is a `dead_link`
finding that flows through the change log (`dead_link_opened` /
`dead_link_cleared`) and the `changes` read like any other drift, and records
the correct backlink target under `proposed_fix` for the later posting phase —
detection only, nothing is written to a tracker. The data model gains a
validated `program.endpoints` block (stable link-out / backlink targets), which
`status`/`validate` surface so any renderer links to a stable endpoint rather
than a volatile artifact URL. And `status` now surfaces each item's captured
tracker **content** (title, state, last activity) alongside an `access`/`gated`
hint on every external link, so a consumer who can see the tracker sees the
item's substance without opening GitLab/GitHub — the link is "edit / see more
if you have access", not the only way in. Absent content is reported absent,
never invented.

## Contracts

### The data file — `axiom.program/0.1`

One JSON document per program. Top level:

| Key | Required | Shape |
|---|---|---|
| `schema` | yes | the literal `"axiom.program/0.1"` |
| `program` | yes | object; `id` and `name` are required non-empty strings. Optional fields are carried verbatim (`as_of`, `deputy`, `north_star` — the single visionary apex statement, §Forward design — URL fields, `children`, …). `tracker`, when present, is an object that must name its `host` (`kind`, `project_id`, `repo`, `credential` are deployment data). `feeders` (a list of live source kinds the clerk reconciles from), `mirrors` (declared origin↔mirror pairs, §Capture feeders), and `mirror_expected` (bool) are optional and carried verbatim. `endpoints`, when present, is an object mapping a name (`tracker_site`, `roadmap`, `canonical`, …) to a stable `http(s)` URL — validated for shape, carried verbatim (§Canonical endpoints). |
| `lanes` | no | list of objects, each with a unique non-empty `id`; other fields (`name`, `color`, and an optional `lead` principal the proxy-assignee rule reads) carried verbatim |
| `people` | no | list of objects, each with a unique `principal` in the platform form `@name` / `@name:context` (single leading `@`, ADR-020); `lanes` entries must reference declared lane ids. `accounts`, when present, is an object mapping an account system (deployment data — `gitlab`, `github`, …) to a username **or an explicit `null`** ("no account on that system"); validated for shape, carried losslessly (ADR-166). |
| `schedule` | no | list of items — see below |
| `products` | no | (phase 7+, §Forward design) list of product records, each with a unique `id`, a `name`, a `type` (one of the deployment's declared type families), a `lifecycle` state, an `owner` principal, an optional `target` date, the composition edges `composed_of` / `depends_on` / `derived_from` (each a list of product ids), and `evidence` (a list of crosslinks). The product space is a graph; every `schedule` item produces at least one declared product. Introducing it is the schema's next revision (`axiom.program/0.2`); a `0.1` file loads unchanged. |
| `provenance` | no | object, carried verbatim |
| `capture` | no | the feeders' recorded block (§Capture feeders): the last source, its readiness, the `link_health` summary (§Crosslink health), and the capture-half `findings`. Written by `sync` from a live source; a binding block like any other on the way through. |
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
- `produces` (phase 7+, §Forward design), when present, is a list of product
  ids this item produces; each must resolve to a declared `products` entry.
  Every item produces at least one product (the ADR-172 hard rule).
- Every other field (`kind`, URL fields, …) is carried verbatim.

**Attachments (phase 7+, §Forward design).** `program`, and each `lanes`,
`products`, `schedule`, and `people` entry, may carry an `attachments` list —
`{href, title?, source: "auto"|"manual"}`, each `href` an `http(s)` URL — a
crosslink watched by crosslink health (§Crosslink health) like any other link.

**Failure contract:** `load_program` / `save_program` raise
`ProgramValidationError` carrying `errors` — *every* defect found in one
pass, in document order — never just the first. Saving re-validates, so a
process cannot write a file the next reader refuses. Unknown fields are
never dropped: a field the loader drops is worse than a missing field.

Python surface (`axiom.extensions.builtins.program`): `PROGRAM_SCHEMA`,
`STATUS_VALUES`, `ProgramData` (lossless `raw` plus view accessors, including
`bindings` for the non-enumerated top-level blocks and `endpoints()` for the
declared canonical endpoints), `load_program`, `save_program`,
`validate_program`, `ProgramError`, `ProgramValidationError`.

### Skills (registry capabilities, ADR-056)

All three take an optional `data` param (path to the data file; default
`<state_dir>/program/data.json`) and return a `SkillResult`.

**Who may name the file.** `data` is honoured on the `cli` surface only.
On every other surface (MCP, web, chat) a read answers from the node's own
`<state_dir>/program/data.json`, and a `data` param is refused — a
caller-chosen path would let any served caller aim the reader at any JSON
file the node can open, with the validator's defect list echoing parts of
it back. A context with no surface (direct in-process wiring) is treated as
a served caller, per ADR-157's least-trusted rule.

**`program.status`** — the read tool (R1). Params: `scope` (required, one
of `person | lane | item | schedule | priorities | drift`), `key` (required for
`person`/`lane`/`item`), `fmt` (`brief` default, or `full`). The result
`value` carries the question (`scope`, `key`, `fmt`), the program's
identity (`id`, `name`, `as_of`), the resolved `endpoints` (§Canonical
endpoints), `items`, and `count`. Each item carries
exactly: `id`, `label`, `owner`, `dates` (`start`/`end`/`date`, each
present and `None` when the file does not state it), `status`
(`proposed`/`committed`/`None`), `pct`, `links`, and `tracker` (the captured
tracker content, below). `fmt=full` adds the entry's remaining fields, and for
`person` scope the person's own record.

Links are only what the file states: string fields that are URLs become
`{kind: "url", field, href, access: null, gated: false}`; an `issue` under a
declared tracker becomes `{kind: "tracker", host, project, ref, access, gated:
true}` — host-qualified data, no vendor URL grammar. The **access hint** names
the system opening a link needs (`access` = the tracker's declared kind for a
tracker link, `null` for a plain URL) and `gated` is `true` when opening it
needs that system's access.

**Account-aware reads (the non-tracker viewer, R13).** Each item also carries a
`tracker` field: the substance a feeder captured — `{title, state, assignee,
due, milestone, labels, updated_at, last_activity}` — so a consumer who can see
the tracker sees the item without opening GitLab/GitHub. The gated external
link is "edit / see more if you have access", not the only way in. **Absent is
absent:** when no feeder has populated the item's tracker content, `tracker` is
`null`, never an invented title or state.

Scoping semantics:

- `person <principal>` — the items that principal owns. A listed principal
  with no items is an empty list; an unlisted principal is a refusal.
- `lane <id>` / `item <id>` — likewise: declared-but-empty answers empty,
  undeclared refuses.
- `schedule` — every item, file order.
- `priorities` — the status-bearing items only (an entry with no status is
  not a priority anybody stated), `committed` before `proposed`, earliest
  `end`/`date` first, undated last.
- `drift` — see below; its `value` carries `findings` instead of `items`.

**Typed refusals.** Every `ok=False` result from `program.status` carries
`value: {"refused": <kind>}` beside its `errors`: `absent` (the file does
not know the principal, lane or item), `bad_request` (unknown scope or
fmt, missing key, or `data` off the CLI), `no_data` (the file is missing,
unreadable, or invalid). Transports map the kind, never the prose.

**The `drift` scope (R11, data-file half).** Reports every inconsistency
the data file shows by itself, each as an explicit finding
`{kind, subject, detail}`. Checks, in per-subject order:

| `kind` | `subject` | Fires when |
|---|---|---|
| `unowned_item` | item id | a schedule item names no `owner` |
| `owner_not_in_people` | item id | an item's `owner` is not a `people` principal |
| `unlaned_item` | item id | a schedule item has no `lane` |
| `unbound_item` | item id | a schedule item has no `issue` binding |
| `issue_without_tracker` | item id | an item has an `issue` but `program.tracker` is not declared, so the ref cannot resolve |
| `empty_lane` | lane id | a declared lane has no schedule items |

Findings are ordered schedule items in file order (each item's checks in
table order), then lanes in file order. The result `value` carries the
question and program identity as for every scope, plus `basis:
"data-file-only"`, `checked` (the check kinds above), `not_checked` (what the
data-file-only findings do not compare), `findings`, `count`, and the
`crosslink` facet (below). The label is present on every drift result,
including one with zero findings: "nothing found in the file" is never "in sync
with the tracker".

**The `crosslink` facet (R11 link health).** The live-checked half of drift,
surfaced on the drift read from what a feeder last recorded in the `capture`
block. It carries `basis` (`capture` when a feeder has run, else `unchecked` —
never silently "healthy"), `checked` (bool), the `source`, the `link_health`
summary, and `findings` (the `dead_link` and mirror findings — link and
repository-mirror health, not attribution). The data-file-only `findings`,
`count`, and `basis` are unchanged by the facet; crosslink health is computed
where a client and connector readiness exist (the feeder / `sync` path), not by
the served read itself — a served `status --scope drift` never reaches the
network. See §Crosslink health.

`owner_not_in_people` is a schema violation every other read refuses on.
Drift is the one read whose job is to report it, so it loads with
`load_program(path, require_listed_owners=False)` — which skips exactly
that rule. Every other schema defect still refuses the drift read
(`no_data`).

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
shape (schema, program id, section counts, binding-block names, and the
declared `endpoints` names). Invalid → `ok=False` with the complete defect
list as `errors`.

### Canonical endpoints (phase 5)

`program.endpoints` is an optional, validated map of a name to a stable
`http(s)` URL — the program's durable link-out and backlink targets, so a
renderer links to a stable endpoint and a later posting phase writes a backlink
to one, rather than to a volatile per-artifact URL. The names are the
deployment's data (`tracker_site`, `roadmap`, …); the reserved name `canonical`
declares the program's own stable public home. Each value is validated as an
`http(s)` URL on load (a non-URL is a defect, reported with every other).

`status` and `validate` surface the endpoints through
`skills/_source.py:resolve_endpoints`, which returns `{declared, served_path,
canonical}`:

- `declared` — the `program.endpoints` map.
- `served_path` — the stable path the node serves the program face at
  (`/program`, the `mount.py` prefix). A consumer that already knows the node's
  public host forms the absolute backlink from it.
- `canonical` — the preferred absolute backlink target. Precedence: (1) the
  node's own served `/program` URL, resolved at serve time from a
  node-public-URL primitive; (2) a declared `endpoints.canonical`.

**There is no node-public-URL primitive in Axiom today** — the HTTP server
knows only its bind host/port, not a public address — so (1) contributes
nothing and the absolute canonical URL stays deployment-config-supplied via
`endpoints.canonical`. The platform never invents or hardcodes a URL; the
`_node_served_url` seam returns `None` and is the single place to upgrade when
such a primitive lands, with no caller change.

### The self-update core (phase 3)

Three files live in `<state_dir>/program/`: `data.json` (the current state,
owned by the model), `snapshot.json` (the diff memory `sync` last
reconciled), and `changelog.jsonl` (the append-only history). Every write
goes through `axiom.infra.state` — `locked_append_jsonl` for the log,
`LockedJsonFile` for the snapshot and the watermarks — never a bare
`open()`, because the CLI, the scheduled clerk, and a served read can touch
them at once.

**The source seam (`skills/sources.py`).** `sync` reads the desired state
from a `ProgramSource`: an `origin` label plus `load() -> ProgramData | None`.
Phase 3 ships `FileSource` (read from a JSON data file; a missing file is
`None` = nothing to reconcile; a present-but-invalid file raises so `sync`
refuses rather than writing a broken state) and `NullSource` (always
`None` — what an unconfigured feeder resolves to, so the verb is always
callable). A future capture feeder (ADR-162) implements the same interface.
The `origin` is stamped onto every change-log entry (`file:<path>`,
`null`, …) so a reader can tell a hand edit from a feeder push — the shape
ADR-087 calls a `SourceOrigin`.

**The change log — `changelog.jsonl`.** One JSON object per line. Fields:
`seq` (monotonic 1-based position, what watermarks key on), `ts` (ISO-8601
Z), `kind` (one of the closed vocabulary below), `subject_kind`
(`item` | `lane` | `drift`), `subject` (the item id, lane id, or drift
finding subject), `field` (the field that changed, or the drift finding's
own kind, or `null`), `old`, `new`, and `source` (the origin). The closed
change-kind vocabulary:

| `kind` | Fires when | `subject_kind` |
|---|---|---|
| `item_added` / `item_removed` | a schedule item appears / disappears | `item` |
| `owner_changed` | an item's `owner` changed | `item` |
| `date_changed` | an item's `date`/`start`/`end` changed (`field` names which) | `item` |
| `status_changed` | an item's `status` changed | `item` |
| `pct_changed` | an item's `pct` changed | `item` |
| `issue_linked` / `issue_cleared` | an item's tracker `issue` was bound / unbound | `item` |
| `lane_added` / `lane_removed` | an item joined / left a lane (`field="lane"`), **or** a lane was defined / deleted in the program's `lanes` section (`field=null`) — the `subject_kind` tells the two apart | `item` / `lane` |
| `drift_opened` / `drift_cleared` | a data-file drift finding (a `status` drift kind + subject) appeared / disappeared | `drift` |
| `item_edited` | an item's non-tracked field (label, custom) changed | `item` |
| `lane_edited` | a lane's `name`/`color` changed | `lane` |
| `lane_owner_changed` | a lane's `lead` changed (`old`→`new`) — a first-class ownership transition | `lane` |
| `person_added` / `person_removed` | a principal joined / left `people[]` | `person` |
| `person_edited` | a member's name/accounts/other fields changed | `person` |
| `person_reassigned` | a member's lanes and/or program role changed (`old`→`new`) | `person` |
| `invited` / `redeemed` | an invitation to join was issued / redeemed into a membership | `invitation` |

The first twelve kinds are produced by the snapshot diff (`sync` and any
edit that touches a tracked item/lane/drift field). The rest are the
mutation surface's, for facts the snapshot does not track. A mutation entry
additionally carries **`by`** — the acting principal — so an owner change
names who reassigned it and when, immutably. The vocabulary stays closed:
extending it (ADR-165) is a deliberate edit documented here.
| `drift_opened` / `drift_cleared` | a data-file or capture drift finding (a drift kind + subject) appeared / disappeared | `drift` |
| `dead_link_opened` / `dead_link_cleared` | a crosslink-health `dead_link` finding appeared / disappeared — distinct kinds so a consumer filters "a link went dead" on its own | `drift` |

**`program.sync`** — params: `data` (the reconcile target, CLI-only), `source`
(an upstream file; default: the target itself). One pass: read the source;
when a *distinct* source's content differs, write it into `data.json`
(re-validated strictly on the way out); diff the source against the last
snapshot; append one change-log entry per difference; advance the snapshot.
The self-reconcile default (`source` = the data file) loads leniently so a
file whose only defect is a drift finding is still reconciled and reports
that drift, exactly as the `drift` read tolerates it; an explicit upstream
source loads strictly because it may be copied into `data.json`. The whole
reconcile runs under an exclusive lock on the snapshot, so two concurrent
syncs serialize rather than double-log (R11: no operation assumes it is the
only writer). **Idempotent:** a second run over an unchanged source produces
no diff and logs nothing. `sync` writes only the node's own state and reads
only read-only sources, so it is safe to run automatically; it is a write,
declares `surfaces = ("cli",)`, and is never an MCP tool. The first sync on
a fresh log is a baseline: every current item, lane, and drift finding is
reported as added/opened, which is exactly right for a consumer whose
watermark starts at zero — everything is new to someone who has looked at
nothing.

**`program.changes`** — params: `principal` (the caller; default:
`ctx.principal`), `since` (`last` default, or an ISO timestamp), `peek`,
`advance`. Returns the change-log entries after the window as `changes`,
each carrying the raw record plus, when the change is about an item that
still exists, that item's current `status`-shaped view (`owner`, `dates`,
`status`, `pct`, `links`) under `item` — "the same link/field shape as
status". `--since last` reads from the principal's stored watermark; an ISO
timestamp reads everything after that instant. The result also carries
`since` (mode/seq/ts), `advanced` (bool), `watermark` (the resulting
position), `count`, and the program identity. Missing data file or missing
log is not an error — `changes` answers from the log, so it reports zero
rather than refusing. Typed refusals (`bad_request`) cover a malformed
principal or `since`.

**Watermarks — `watermarks.json`.** A `LockedJsonFile` mapping each
principal to `{seq, ts, updated_at}`. A watermark never moves backward. The
store is per-principal: two principals each get only their own unseen
deltas, independent of one another.

**Read vs. advance — why a read tool stays a read.** Reporting deltas is a
read; advancing the watermark is a write. They are separated so `changes`
can declare `side_effects=False` and project as a read-only tool without
lying:

- On the operator's own `cli` surface the default is advance ("show me
  what's new and mark it seen"); `--peek` suppresses it.
- On every served surface (MCP, web, chat, or an unknown surface) the default
  is peek. Advancing there happens only when the caller sets `advance=true`
  explicitly, and the write it performs is the caller's *own* local reading
  position — never program state, never anything another consumer observes.
- The HTTP route is a `GET` and forwards only `principal`/`since`, never
  `advance`, so a plain HTTP read is unconditionally non-advancing.

`peek` always wins over `advance`. Advancing catches the watermark up to the
current end of the log.

### The capture feeders (phase 4)

The feeders are the live sources behind the phase-3 seam: a `GitLabSource` and
a `GitHubSource`, each an **instance of the ADR-162 watcher primitive**, not a
new poller.

**The shared watcher primitive — `axiom.infra.watcher`.** One small,
domain-free core the feeders ride and the release extension's own watchers
migrate onto when next touched (ADR-162): `Watcher.poll(state)` owns the three
shared behaviours and nothing vendor-specific — **debounce** (a too-soon poll
is reported `polled=False`, never "no changes"), **content-identity dedup** (a
`WatchItem.identity` seen twice collapses to one, so a commit arriving through
both an origin and its mirror is one commit — the ADR-162 double-count rule),
and **cursor advance** (an `updated_after` high-water mark that never moves
backward). `WatcherState` (cursor + last-poll time) persists through
`FileWatcherStore` under `<state_dir>/program/watchers/`. An instance declares
only its `fetch(since)` callable, a credential, and a subject mapping.

**The read-only clients (`skills/_clients.py`).** `GitLabClient` (REST v4,
stdlib `urllib`) and `GitHubCliClient` (the authenticated `gh` CLI) are
read-only and exception-safe — an unreachable host or a bad response degrades
to unverified/empty, never a crash. The credential is resolved **from the
vault** (the foreign-credential store `axi secrets get` reads) by the name the
deployment puts in `program.tracker.credential`; the plaintext lives only
inside the client and is never logged, printed, or placed on argv. The
`TrackerClient` protocol is what the sources depend on, so a unit test injects
a fake and no unit test touches the network.

**`GitLabSource` / `GitHubSource` (`skills/sources.py`).** `load()`:

1. Loads the node's current data file (lenient) — the authoritative structure:
   lanes, people, the account map, the item↔issue bindings.
2. Polls the watcher: issues (assignee, state, milestone/due, labels, title)
   and linked merge requests, cursor-first (`updated_after`).
3. Overlays the live facts onto the bound schedule items
   (`item.issue == issue.ref`) into a **namespaced `tracker` block**, records
   attributed `activity`, and computes the `assignment` — **never clobbering
   `owner`, the committed dates, or `status`.**
4. Records capture-half findings in a `capture` block and returns the overlaid
   `ProgramData` for `sync` to diff exactly as it diffs a file.

`GitHubSource` is additionally the **mirror-check home**: it reads the declared
`program.mirrors` pairs, confirms both sides agree by commit identity (a commit
seen through both origin and mirror collapses on its SHA — no double-count),
and emits `mirror_gap` / `mirror_stale` / `mirror_missing`. A side it cannot
read is reported unverified, never rounded up to synced. When the tracker is on
another host, it runs the mirror check only.

**Identity, attribution, and the proxy-assignee rule (ADR-166).** Program
membership is the principal in `people[]`, not a tracker seat. A tracker action
by an account username is attributed to the principal whose
`accounts[system]` matches (reverse lookup; an explicit `null` never matches).
A schedule item's real owner stays the principal and stays authoritative. For a
tracker-bound item whose owner has **no** account on the system, the feeder
computes the *intended* assignee — the lane lead (`lanes[].lead`) when the lead
has an account, else the deputy (`program.deputy`), else none — records it on
the item's `assignment`, and **names the real owner regardless**. This is
recorded for a later posting phase; nothing here writes to the tracker. The
owner's missing account surfaces as an `account_missing` finding (one per
owner, tracked `pending`) — an onboarding task, never a silent gap. People with
no tracker account read their status/deltas through the node surfaces; the
feeders never gate membership on an account.

**Capture-half findings reach the change log.** The feeder records findings in
the data file's `capture` block; `snapshot_of` unions them with the
data-file-only drift set, so each diffs as `drift_opened` / `drift_cleared` and
reaches `changes` with no extra plumbing. The finding kinds:

| `kind` | Fires when | subject |
|---|---|---|
| `account_missing` | a tracker-bound item's owner has no account on the system | the owner principal |
| `date_mismatch` | the tracker's due date disagrees with the item's committed date | item id |
| `untracked_issue` | an observed tracker issue binds to no schedule item (an orphan) | issue ref |
| `mirror_gap` | the mirror lacks commits the origin has | `<origin_repo>-><mirror_repo>` |
| `mirror_stale` | a declared mirror's job has not run recently, or a side was unreadable | the pair |
| `mirror_missing` | a tracked repo no declared pair covers, where a mirror is expected | repo |
| `dead_link` | a tracker-bound item's `issue` binding, or a declared endpoint, does not resolve (§Crosslink health) | item id / endpoint name |

Findings recompute over the full overlaid program each cycle (reading the
carried-forward `tracker` blocks), so a cursor-delta read does not drop a
finding it did not re-fetch, and a finding clears the cycle its condition does.

**Crosslink health (R11 — no dead links).** The tracker feeder's `load()`, after
the overlay and mirror pass, checks that the program's links still resolve
(`skills/_capture.py:check_links`), read-only and tri-state, reusing the
feeder's own read-only client (`_clients.py:ClientLinkChecker`): a bound item's
`issue` via the client's `issue_readable`, a declared endpoint via a
credential-less HEAD/GET (`_clients.py:resolves`). Each check is `True`
(resolves), `False` (confirmed gone — e.g. a 404/410), or `None` (could not be
verified). **An unreachable checker reports `unverified`, never `healthy`:** a
link that could not be checked is neither reported dead nor rounded up to
synced (the R11 absence posture). A confirmed-dead link is a `dead_link`
finding whose `subject` is the item (carrying the bad `ref`) or the endpoint
name, `verified: true`, and a `proposed_fix` recording the correct backlink
target (the program's `canonical` endpoint) for the later posting phase —
**detection only; nothing is written to the tracker.** The feeder records a
`link_health` summary (`{checked, dead, unverified, verified, basis}`) on the
`capture` block, and the `dead_link` findings diff as
`dead_link_opened`/`dead_link_cleared` through the change log and `changes`.
The tracker feeder owns the `dead_link` kind and recomputes it each cycle.

**Connector readiness (R9).** A source is used only after the verification
ladder: `reachable` → `authenticated` → `project_read`. `ConnectorReadiness`
reports each rung and the first that failed. A source that stops short is
**unverified**, and `sync` **skips it loudly** — it is recorded under the
result's `skipped` with the failed rung, never treated as "no changes". A
local source (file/null) is trivially verified.

**Source selection in `sync`.** `source_kind` chooses the feeder: `file`
(self-reconcile, the default and CLERK's safe heartbeat default), `gitlab`,
`github`, or `all`. With no `source_kind` and no explicit `--source` file,
`sync` reads the data file's `program.feeders`; absent that, it self-
reconciles, so a heartbeat on a node that has declared no live feeder stays a
local no-op. `all` fans out across the configured (or inferred) feeders,
reconciling each in sequence; a feeder owns only its own finding kinds, so one
feeder's pass never clears another's. Each reconcile stays idempotent and runs
under the snapshot lock. Writing back to a tracker is a later phase.

### The mutation surface (phases 5–6)

The data file is the authoritative editable source of truth, and it is edited
through the program's own tool — not by hand-patching JSON, not by writing to
a tracker. Every mutation shares one commit path (`skills/_mutate.py`): load
the file strictly, gate the caller, mutate a lossless deep copy, re-validate on
save, append the change log, and advance the snapshot — all under the snapshot
exclusive lock, so a mutation never races `sync` and `sync` never re-logs a
mutation.

**The membership / DRY model (ADR-166, ADR-167).** A program member is a
*membership*, not an identity store: a principal (`@name:context`, the
platform's one grammar, `axiom.infra.principal`) layered with a lane, a role,
and an optional `accounts` map, over the principal primitive. The extension
keeps **no parallel people registry**. When a directory provider is configured
(ADR-103, `AXIOM_DIRECTORY_PROVIDER` ≠ `none`) a supplied handle is resolved
through the directory seam (`MembershipResolver`) for advisory org roles; when
none is, the handle is accepted as given. Resolution never blocks membership
and never reaches the network hard — it degrades, matching ADR-166's rule that
a member is never gated on an account.

**The verbs.**

| Verb | Capability | Logs |
|---|---|---|
| `person add` | `program.person_add` | `person_added` |
| `person edit` | `program.person_edit` | `person_edited` |
| `person remove` | `program.person_remove` | `person_removed` (+ `owner_changed` for reassigned items) |
| `person reassign` | `program.person_reassign` | `person_reassigned` |
| `lane add` | `program.lane_add` | `lane_added` |
| `lane edit` | `program.lane_edit` | `lane_edited` and/or `lane_owner_changed` |
| `lane remove` | `program.lane_remove` | `lane_removed` |
| `item add` | `program.item_add` | `item_added` |
| `item edit` | `program.item_edit` | the field's kind, or `item_edited` for a label |
| `item remove` | `program.item_remove` | `item_removed` |
| `item reassign` | `program.item_reassign` | `owner_changed` |
| `invite` | `program.invite` | `invited` |
| `redeem` | `program.redeem` | `redeemed` (+ `person_added`) |
| `ownership` (read) | `program.ownership` | — |

**Validation.** Owner references stay valid: an item's owner must be a member
(`save_program` enforces it; the verbs pre-check for a clear message). `lane
remove` refuses to orphan work — a lane that still owns items is removed only
with `--reassign-to <lane>`; likewise `person remove --reassign-to <member>`
for a member who still owns items. `item reassign` applies the ADR-166
**proxy-assignee rule**: when the new owner has no account on the program's
tracker system (`program.tracker.kind`), the intended assignee — the lane lead
if it has an account, else the deputy if it has one, else none — is recorded on
`item.assignment` (which **names the real owner in every case**) and **never
posted to any tracker**; when the owner has an account, a stale `assignment` is
cleared.

**The invitation flow reuses the gate primitive.** `program invite` wraps
`axiom.webauth.invitations.mint_invitation` — the self-service keys flow — so
it inherits the lifecycle wholesale: a single-use, expiring, scrypt-hashed-at-
rest code with the shrink-only scope rule. The program facts (program id, lane,
role, accounts) ride as extra fields on the invitation record, which the
primitive carries losslessly. `program redeem` spends the code via
`redeem_invitation` (the code is the authentication) and records the
membership, honoring the missing-account onboarding finding (an invitee who
declares no accounts becomes a member with none). The gate-minted webauth key
is not persisted or surfaced — the program's concern is the membership; a
deployment that also wants self-service keys uses `gate.redeem`. Only
`axiom.webauth` (substrate, pure stdlib) is imported, so the flow runs entirely
offline. If the gate primitive is ever unavailable, the fallback is a minimal
local token+redeem — but the reuse is live and preferred.

**Ownership over time (`program.ownership`).** `data.json` carries the current
owner; the change log carries the history. This read returns `current` from the
data file plus a `history` timeline reconstructed from the `owner_changed`
(item) / `lane_owner_changed` (lane) transitions — each span a `principal` with
`from`/`to` timestamps and `set_by`. Because the timeline is the immutable log,
not `people[]`, a past owner is reported accurately even after they are removed
from the roster. Read-only.

**Identity gate and read-vs-write exposure.** A mutation is authorized in-body:
the caller (`ctx.principal.handle`, stamped by dispatch, never from params)
must be the program **deputy** (`program.deputy`) or a declared **maintainer**
(`program.maintainers`); on an **open-posture** node (the solo/dev/air-gapped
default, where the principal is the unproven OS operator and the node sets no
stricter floor) whoever holds the shell may edit, and the gate bites the moment
`AXIOM_IDENTITY_POSTURE` is raised. This is the data-driven layer ADR-114's
transport gate cannot express (it cannot read the deputy out of the file).
Structurally, every mutation declares `surfaces=("cli",)`, exactly as `sync`
and `render` do — so no mutation is ever an anonymous MCP tool. The precedent
is phase 3's read/advance split: a read tool must stay a read, and a write must
not be reachable where it cannot be gated. A deployment that wants a specific
mutation over MCP opts it in deliberately by adding `"mcp"` to its surfaces
**and** declaring `allowed_principals` (ADR-114 §1) — never anonymous.
`program.ownership` joins `status`/`validate`/`changes` as a read-only MCP tool
(`side_effects=False`).

### CLI — `axi program`

Per ADR-056, each verb is a 1:1 thin wrapper dispatching through
`invoke_capability` on the `cli` surface; handlers hold zero logic. The
mutation nouns nest (`axi program person add …`); each leaf names its
capability so the dispatcher stays logic-free.

```
axi program status --scope person|lane|item|schedule|priorities|drift
                   [--key K] [--fmt brief|full] [--data PATH] [--json]
axi program render   [--data PATH] [--out DIR] [--json]
axi program validate [--data PATH] [--json]
axi program sync     [--data PATH] [--source PATH]
                     [--source-kind file|gitlab|github|all] [--json]
axi program changes  [--principal @name:context] [--since last|ISO]
                     [--peek] [--advance] [--json]
axi program ownership --scope item|lane --key K [--data PATH] [--json]

axi program person add --principal @n:c [--lane L]... [--role R]
                       [--name N] [--account sys=user]... [--data PATH]
axi program person edit --principal @n:c [--name N] [--drives D]
                        [--account sys=user]... [--data PATH]
axi program person remove --principal @n:c [--reassign-to @m:c] [--data PATH]
axi program person reassign --principal @n:c [--lane L]... [--role R] [--data PATH]
axi program lane add --id L [--name N] [--lead @n:c] [--color C] [--data PATH]
axi program lane edit --id L [--name N] [--lead @n:c] [--color C] [--data PATH]
axi program lane remove --id L [--reassign-to L2] [--data PATH]
axi program item add --id I --label T [--owner @n:c] [--lane L]
                     [--date D | --start S --end E] [--status proposed|committed]
                     [--pct N] [--issue REF] [--data PATH]
axi program item edit --id I [--label T] [--lane L] [--date D | --start S --end E]
                      [--status …] [--pct N] [--issue REF] [--data PATH]
axi program item remove --id I [--data PATH]
axi program item reassign --id I --owner @n:c [--data PATH]
axi program invite --principal @n:c --lane L [--role R] [--name N]
                   [--account sys=user]... [--expires 7d] [--data PATH]
axi program redeem --code CODE [--data PATH]
```

`--peek`/`--advance` use `argparse.SUPPRESS` so an unset flag is absent from
the params dict and the skill applies the surface default (advance on the
CLI, peek when served) rather than seeing a stray `False`.

Output: the result `value` as JSON on stdout; errors on stderr with exit 1.
`--json` emits the full `{ok, value, errors}` envelope.

### MCP — the composed Axiom MCP (phase 2)

Registry-driven, the ADR-073 pattern every extension's reads use (attest,
audit, data platform): the skill spec opts in with `surfaces = ("cli",
"mcp", "agent_tool")` and declares `side_effects=False`, and the aggregator
binds the extension's `skills.bind` into its per-build registry and
projects every MCP-surfaced spec. The manifest's `[extension.mcp]` block
(`enabled = true`, `prefix = "axiom_program"`) declares the projection.

| Tool | Capability | Annotations |
|---|---|---|
| `axiom_program__status` | `program.status` | `read_only_hint=true`, `idempotent_hint=true` |
| `axiom_program__validate` | `program.validate` | `read_only_hint=true`, `idempotent_hint=true` |
| `axiom_program__changes` | `program.changes` | `read_only_hint=true`, `idempotent_hint=true` |

`program.render` and `program.sync` declare `surfaces = ("cli",)` and are not
tools — `render` writes files, `sync` writes the node's state. `changes`
declares `side_effects=False`, so its tool is read-only *by default*: the
read-vs-write sweep sees no mutating program tool, and the watermark advance
is an explicit per-call opt-in (`advance=true`), never the served default.
Dispatch goes through `invoke_capability` on the `mcp` surface (the
gateway's `tool.pre_invoke` chain sees it as it sees a CLI call); an
argument the spec does not declare is refused before the skill runs; the
identity gate binds the tools to the owner-scoped default (`@*:local`,
ADR-114 §1); responses are bounded by the agent-class byte budget
(ADR-157). Over MCP the data file is always the node's own (above).

### HTTP — `GET /program/status` (phase 2)

A `service` provide-block whose entry (`program.mount:mount_spec`) returns
a `MountSpec(prefix="/program", requires_authz=True)`, discovered by
`compose_app`. With no authz hook configured the substrate refuses to
serve the mount at all (fail-closed, SRV-022); with one, a request that
carries no valid credential is refused (401/403) before the route runs.
There is no anonymous read.

Query params: `scope`, `key`, `fmt` — exactly the skill's question; any
other param (including `data`) is a 422. The route dispatches
`program.status` through `invoke_capability` on the `web` surface and
returns the result `value` as JSON. Refusals map by kind: `absent` → 404,
`bad_request` → 422, `no_data` → 503; a refusal with no kind (a gateway
hook denial) → 403.

### HTTP — `GET /program/changes` (phase 3)

The same mount gains a second read route. Query params: `principal`,
`since` — and deliberately **not** `advance` (nor `peek`): a `GET` is a
read, so the one param that would move a watermark is never forwarded, and a
request that passes it gets a 422. The route is therefore unconditionally
non-advancing — advancing is reachable only where a caller asks for it
explicitly (the CLI, or an MCP call setting `advance`). Dispatch and refusal
mapping are as for `/program/status`.

### The CLERK scheduled agent (phase 3)

The coordinator is declared exactly the way every daemon agent is, so the
existing agent service runner schedules it with no new mechanism (ADR-161 —
CLERK owns the clock, not the work):

- A `[[extension.provides]] kind = "agent"` block names `CLERK`, its `entry`,
  its `persona` (`agents/clerk/persona.md` — a declared persona that does not
  resolve fails `axi ext lint` AEOS060, so the file ships), and
  `uses_skills = ["program.sync"]`.
- An `[agent]` lifecycle block: `startup = "daemon"`,
  `heartbeat_command = "program sync"`, `heartbeat_interval = 900`,
  `default_consent = "opt-in"`. `[sender] display_name = "CLERK"` is the
  channel nameplate; the canonical `@clerk:<context>` principal is derived.

**What makes it fire.** The background-service timer invokes the runner,
which every tick dispatches `axi <heartbeat_command>` — here `axi program
sync` — for each registrable agent past its `heartbeat_interval`. So the
scheduled action *is* `program sync`: reconcile the data file against its
read-only source and advance the log. Nothing leaves the node, so it is safe
to run automatically; posting to an external tracker is a later phase.

**Autonomy is not forced on.** Two gates, both off by default, stand between
this declaration and a firing heartbeat: the master `autonomy.enabled`
setting ships `false` (no OS timer is even installed until `axi settings set
autonomy.enabled true`), and `default_consent = "opt-in"` keeps CLERK out of
the auto-enabled core set, so on a decided machine it fires only after the
operator selects it in `axi agents register`. Opt-in, not core: although
phase-3 `sync` writes only local node state, CLERK is the coordinator that
will soon reach the network (capture + posting), so it opts in deliberately,
never silently. Declaring the agent arms nothing on its own.

## Design

Flat built-in layout per AEOS §5.1.1: `model.py` (the data-file contract),
`skills/` — the reads (`status`, `render`, `validate`, `sync`, `changes`,
`ownership`) and the mutation surface (`people.py`, `lanes.py`, `items.py`,
`invitation.py`), bound via `bind` / `bind_default` with `SkillSpec`s:
`status`, `validate`, `changes` and `ownership` declared read-only and
MCP-surfaced; `render`, `sync` and all thirteen mutation verbs side-effecting
and CLI-only. `skills/_source.py` decides which data file a read answers from;
`skills/_changelog.py` holds the snapshot, the diff, the change-log/watermark
stores, and the closed change-kind vocabulary; `skills/_mutate.py` is the
shared write core (load-for-edit, the deputy/maintainer gate, the locked
commit that advances the snapshot, the proxy-assignee rule, and the directory
resolution); `skills/sources.py` is the source seam and the live
`GitLab`/`GitHub` feeders; `skills/_capture.py` is the attribution /
proxy-assignee / mirror judgement; `skills/_clients.py` the read-only
vault-credentialed tracker clients; the shared watcher primitive lives in
`axiom.infra.watcher`. `cli.py` (the reads plus the nested mutation nouns),
`mount.py` (the HTTP routes), `agents/clerk/persona.md`, manifest with one
`cmd` noun, one `agent`, nineteen `skill` blocks, one `service` mount, the
`[extension.mcp]`, `[agent]`, and `[sender]` blocks.
`skills/` (`status`, `render`, `validate`, `sync`, `changes`, bound via
`bind` / `bind_default` with `SkillSpec`s — `status`, `validate` and
`changes` declared read-only and MCP-surfaced, `render` and `sync`
side-effecting CLI-only writes; `skills/_source.py` decides which data file
a read answers from and resolves the canonical endpoints; `skills/_changelog.py`
holds the snapshot, the diff, and the change-log/watermark stores;
`skills/sources.py` is the source seam and the live `GitLab`/`GitHub` feeders;
`skills/_capture.py` is the attribution / proxy-assignee / mirror / crosslink
judgement; `skills/_clients.py` the read-only vault-credentialed tracker clients
plus the crosslink checker; the shared watcher primitive
lives in `axiom.infra.watcher`), `cli.py`, `mount.py` (the HTTP routes),
`agents/clerk/persona.md`, manifest with one `cmd` noun, one `agent`, five
`skill` blocks, one `service` mount, the `[extension.mcp]`, `[agent]`, and
`[sender]` blocks.
Tests live in `tests/` beside the code, with the AEOS standard conformance
subclass in `tests/unit_tests/`. Fixtures use invented generic vocabulary
only.

Failure modes: unreadable or non-JSON file → `ProgramError`; schema
violations → `ProgramValidationError` with the full defect list; every
skill converts both into `ok=False` refusals, and `render` refuses before
touching the output directory.

## Forward design — the product spine and the instrument (phase 7+)

Everything above is shipped (phases 1–6). This section is the design of the next
stretch (prd-program R15–R22; ADR-171/172/173): the product spine, the layered
roadmap instrument, deterministic estimation, conversational shaping, and the
delivery-is-a-loop ethos. It is captured here before build. The governing
constraint, stated once and applied throughout, is that **almost none of it is
new machinery** — the instrument surfaces are the existing `data.json`, change
log, mutation verbs, feeders, and renders aimed at new questions. That is the
internal-machinery face of the same posture ADR-171 states for external
dependency (near-zero new engine, near-zero external dependency). Build follows
a resilience / failure-injection / adversarial-input gate before it is armed on
a live node.

The **build order** for this stretch — the design below sequenced as discrete,
individually shippable phases (P7 products spine, P8 roster lifecycle, P9
emergent work, P10 deterministic forecasting, P11 the roadmap instrument, P12
program updates into the one journal, P13 the optional data-platform substrate,
P14 harden-then-arm) — is
[docs/working/program-phase7-plus-plan.md](../working/program-phase7-plus-plan.md).
This section stays the design-of-record; that plan is only the sequencing.

### The data-model additions

The `axiom.program` schema gains these optional, additive blocks and fields — the
product spine of ADR-172 and the emergent-work and lifecycle additions of ADR-176.
A file that declares none of them is the schema as it stands; each is validated
for shape and carried losslessly, exactly like every existing block (a field the
loader drops is worse than a missing field). Introducing the product graph is the
schema's next revision (`axiom.program/0.2`); a `0.1` file loads unchanged.

- **`products` (top-level list).** Each product: `id` (unique, required), `name`
  (required), `type` (one of the deployment's declared type families, below),
  `lifecycle` (a state in that type's declared lifecycle), `owner` (a `people[]`
  principal, ADR-166), an optional `target` (ISO date), and three composition
  edges — **`composed_of`**, **`depends_on`**, **`derived_from`** — each a list
  of product ids. `evidence` is a list of crosslinks (below). The three edge
  kinds make the product space a directed **graph**, not a tree. **End-of-life
  lifecycle (R24, ADR-176):** the seeded lifecycle runs `active` → `legacy` →
  `superseded`, and a superseded product carries **`superseded_by`** (a product
  id resolving to the replacement, checked on load like every other edge). A
  replacing product thus records what it replaced, and the history is **shown,
  not erased** — a product never vanishes when a successor ships; it moves to
  `legacy`/`superseded` with the pointer that explains why.
- **`people[].status` (R24, ADR-176).** Each person carries a status —
  `active` / `alumnus` / `historical` (default `active` when absent) — so a
  contributor who has moved on is **kept and credited**, not deleted: their past
  ownership stays intact (the `program.ownership` timeline already reports a past
  owner accurately, §The mutation surface), and their name is reported as it was.
  A non-active person is rendered as such, never dropped from the roster.
- **`schedule[].produces`.** A list of product ids an item produces. **Every
  schedule item produces at least one product** — the ADR-172 hard rule: an item
  that produces nothing is a drift finding, not a valid state. Every product id
  (in `produces`, in an edge, or as an `owner`) resolves to a declared product /
  principal, checked on load like every other id reference.
- **`attachments` (on any node).** `program`, each `lanes[]`, `products[]`,
  `schedule[]`, and `people[]` entry may carry an `attachments` list:
  `{href, title?, source: "auto"|"manual"}`, each `href` validated as an
  `http(s)` URL and watched by crosslink health (R11) like any other link.
- **`program.north_star` (string).** A single visionary statement — the apex of
  the accountability ladder (item → product → program → North Star). Surfaced at
  the top of every program face, changed rarely, carried verbatim; the platform
  never generates it.
- **`emergent` (top-level list — R23, ADR-176).** The inbox of detected-but-
  unmapped work: landed work a feeder observed that maps to **no** tracked item
  (§The emergent-work flow). Each entry: `id` (unique, required), the `source`
  reference it was detected from (a commit/merge-request/record ref, carried
  verbatim), the attributed `actor` (a `people[]` principal where one resolves,
  ADR-166), a proposed **`alignment`** classification (`improvement` — it ladders
  up to an existing product and the North Star — or `side_trail` — it does not;
  the tracker proposes, never decides), an optional proposed `product` id the
  classifier read it against, and — once a human resolves it — a `decision` block
  (`kind`: `adopted` / `distraction` / `deferred`; `by`: the acting principal;
  `rationale`: the human's words; `change_ref`: the change-log `seq` the decision
  is recorded at, §The emergent-work flow). An unresolved entry carries the
  proposal and no decision; it is never silently absorbed into an item and never
  dropped. Every id reference (`actor`, `product`) resolves on load like every
  other.

Product **type families and lifecycles are data, not code** (R10). The deployment
declares the families it uses from the seven the platform seeds — software,
physical, model/data, research, communication, program, composite — and the
lifecycle states for each; a new family or lifecycle is configuration, and the
extension names none of them in code. A product's lifecycle does not stop at
*delivered*: the seeded lifecycles continue through *in use → iterating →
adopted* (R22, the loop ethos).

Product **evidence is a crosslink, never a copy.** A product points at the system
that already holds its proof — a release (the release extension), a
model-registry entry, a signed journal record (the attested book), a
research-surface page — and crosslink health watches the link. Per ADR-171 the
product record stands alone with no hard dependency on any of those systems: an
absent or unreachable evidence system makes the crosslink `unverified`, never
breaks the product.

### Rollup along the graph

Status and evidence roll up the **composition** edges: a composite product's
state derives from the states of what it is `composed_of`, and its evidence is
the union of its children's. **Dependency** (`depends_on`) edges do not roll
status up — they order sequencing (the forecast's gates, below). **Lineage**
(`derived_from`) edges carry provenance (what a product was derived from). All
three are part of the one graph the mind-map renders. The accountability checks
are informational drift findings first — an item producing no product, a product
with no items, a product past `target` with no evidence — tightened to refusals
per the deployment's posture.

### The roadmap is an instrument — renders over data that already exists

The roadmap is not a stored artifact; it is a set of **renders and
agent-orchestration over the same `data.json` + change log + skills phases 1–6
already ship** (R18). It is revealed in layers, simplest first (incremental
revelation):

- **See** — static views (`render` variants) projecting the same data by
  product, person, time, or lane; printable on demand. This is the default and
  the only layer a casual reader meets.
- **Reshape** — filter / group / zoom: non-mutating parameters on the read, no
  new state.
- **Search** — find an item, a product, or a recorded-but-uncommitted idea (an
  entry carried in the file with no committed status), and jump to its deep-link.
- **Model** — scenarios (below).
- **Ask** — the agent (CLERK) drives the model layer from conversation (shaping,
  below).

A **mind-map view** is one more render of the same product+item data: products as
nodes carrying all three edge kinds (composition / dependency / lineage) and the
`produces` edges from items, drawn radially from a target product, with nodes
that expand and collapse so the graph stays legible. A **product-detail view** is
the per-product render: its evidence, its `composed_of` / `depends_on` /
`derived_from` neighbours, the items that produce it, its lifecycle and target,
and its attachments — each a deep-link.

### Scenarios are non-destructive forks; what-if is the mutation verbs on a fork

A scenario is **a copy of the program's own data**, not a new data structure:
fork `data.json` into a scratch scenario, apply changes, render it beside the
baseline. The what-if operations — add resources, de-emphasize or drop,
re-sequence, buy-vs-build (swap a built product for a dependency on an
off-the-shelf one) — are **the existing mutation verbs (ADR-167) applied to the
fork**, not a second editing path. The forecast (below) recomputes the fork's
dates. The baseline is untouched until a scenario is **adopted**, at which point
its deltas replay onto the real data through those same mutation verbs, logged
with `by`. Considered-but-rejected options stay on the record (ADR-shaped:
options weighed, chosen, reason), and a scenario can resurrect a rejected one to
model it again. **No new engine:** a scenario is the data forked, the what-ifs
are the mutation verbs, the dates are the gates, and the driver is the existing
agent.

### The forecast is deterministic over change-log throughput

"When will X be ready" is computed, never guessed (R19, ADR-173). The forecast is
**measured throughput × resourcing, over sized remaining scope, along the
dependency gates**:

- **Throughput is measured from the change log.** Close rate per person, per
  lane, per size is read from the `item`/`status` history already in
  `changelog.jsonl`, kept as a **distribution** (not one velocity) so the answer
  carries spread honestly; anchored to delivered history, it sharpens as work
  closes (self-calibrating).
- **Scope is sized by t-shirt size** (XS–XL per item). The language model
  **proposes** a size from similar past items (one-tap confirm, optional — an
  unsized item takes the proposal) and may **rarely** ask one short question. It
  **never computes a duration and never invents a date.**
- **Resourcing is auto-pulled** from the data (owner, assignees, lane); the tool
  asks at most one or two questions, only where the data is silent.
- **Dependency gates** (`depends_on`) order the scope: a product cannot finish
  before what it depends on.
- **The answer is a range with its assumptions**, carrying its uncertainty via
  `axiom.uncertainty` (a scalar error bar is refused where the sources are
  known), never a single false-precise day. The range is the default; the
  how-computed (the pace distribution, the sized items, the gates) is drill-down.
- **Calibrated sizes flow back** into the tracker's estimate/weight fields,
  consent-gated, so the tracker's own data improves without manual entry.

Every permutation — a product ships, a lane clears, how-soon-if-one-more-person —
is the same forecast, run on the baseline or on a scenario fork. The read path
touches only local data (the file + the change log), per ADR-171: no external
call, works with nothing else present.

### Shaping is the agent proposing scenario deltas

Conversational shaping (R20) is the **ask** layer: the agent (CLERK) poses a
dynamic, data-grounded question bank — the fewest, highest-information trade-offs
the data reveals — and turns the answers into **proposed deltas on a scenario
fork**, shows the consequence through the forecast, and enacts only on human
adoption, through the same mutation verbs, logged. It reuses scenarios, the
forecast, the mutation verbs, the consent tiers, and the change log; the agent
orchestrates existing skills driven by conversation. **Propose → approve →
enact:** nothing lands unapproved, and every enacted change traces to what was
said (its reasoning on the record).

### Attachments, deep-links, and sharing

Every node's `attachments` are crosslinks: auto-inferred ones (a release tag, a
DOI, a registry entry, a tracker issue, a node page) and manually added ones,
each marked and each watched by crosslink health. Every aspect of the program — a
product, an item, a lane, a person, a view, a scenario — has a **stable
deep-link** served by the node's `/program` publication endpoint (the
`endpoints` / `_node_served_url` seam, §Canonical endpoints), not a volatile
artifact URL. Opening a deep-link lands the recipient **under the gate**: a
resolved member sees it in full, a partner a scoped projection (R7), an anonymous
caller nothing. The durable endpoint is the fix for the broken-backlink failure
mode (R11): a backlink targets the endpoint, so moving the rendered artifact
never breaks it. (It still depends on the node-public-URL primitive the
§Canonical endpoints seam lacks today.)

### Delivery is a loop (the ethos that recolors the rest)

A product ships minimal and viable (v0.x) and its lifecycle continues past
*delivered* (R22). Usage and feedback are **ingested signals on the same feeder
path** as commits and issues (the ADR-162 watcher instances), and they feed
prioritization (R18/R20) directly. The system **calibrates from outcomes**, not
only from closed work: the forecast learns which estimates held, and the shaping
agent learns which proposed priorities paid off — the loop closes on the tracker
itself. The loops **nest** at feature, product, and program scale, and a rendered
roadmap shows not only a ship date but the loops that follow it.

### Emergent work is the opposite-direction flow (R23, ADR-176)

Planned work flows intention → delivery: an item is proposed, committed,
delivered. **Emergent work runs the other way** — it arrives as a delivered fact
the plan must now account for — and the program treats it as a first-class flow,
not an exception. Four steps, each riding machinery phases 1–6 already ship:

- **Detect.** The capture feeders (§The capture feeders, ADR-162) already observe
  all landed work. The overlay pass already distinguishes an observed tracker
  issue that binds to a schedule item from one that does not (`untracked_issue`).
  Emergent detection widens that to the **work** level: landed work — a commit, a
  merge request, a signed record — attributed to an actor but mapping to **no**
  tracked item is appended to the `emergent` inbox (§The data-model additions).
  This is orphan detection one level above the orphan contributor and orphan
  issue the `drift` scope already reports. It is **never** folded into the nearest
  item and never dropped; the inbox is append-only and the feeder recomputes which
  entries remain unmapped each cycle (an entry whose work later gains a tracked
  item clears, exactly as a finding clears the cycle its condition does).
- **Classify.** Each emergent entry is positioned on a single **alignment axis**
  against the product graph: does the work ladder up to an existing product and,
  through it, toward the `program.north_star` (ADR-172) — `improvement` — or not —
  `side_trail`? The classifier reads only local data (the actor, the lane, the
  artifacts the work touched, the products those map to) and **proposes**; it
  never decides. The proposal is written to the entry's `alignment` / `product`
  fields, loudly provisional until a human resolves it.
- **Decide and log.** Resolution happens in the **conversational-shaping flow**
  (§Shaping is the agent proposing scenario deltas, R20): the agent surfaces the
  inbox, the human **adopts** (create a tracked item through the existing mutation
  verbs — ADR-167 — and attach it to an existing **or a new** product, capturing
  serendipity), names a **distraction** (stop, or park for later), or **defers**.
  Every resolution runs through the one commit path (§The mutation surface) and
  appends a change-log entry carrying `by` and the human's `rationale`, and the
  entry's `decision.change_ref` points at that `seq`. The change log thus records
  **why the program changed course**, not merely that an item appeared. Adopting
  an emergent item is an ordinary `item add` (optionally a product create), so it
  advances the snapshot and a later `sync` does not re-log it — the same
  once-only guarantee every mutation has.
- **Visualize.** The **divergence view** is one more render (the **see** layer,
  R18) over the `emergent` inbox and the product graph: it plots emergent work
  against the plan by alignment — advancing toward vs pulling away from the North
  Star — over time (keyed off the change-log timestamps the forecast already
  reads), so the program's **discovery-versus-drift balance** is a surface rather
  than an after-the-fact inference. No external call, no new store (ADR-171).

**Strictness is visibility, not prohibition.** Nothing is forbidden — a
side-trail is a legitimate choice — but no side-trail stays invisible, every
divergence surfaces for an explicit decision, and the agent may **gently flag**
(a prompt in the shaping flow, never a `refuse`-style block) when one actor
accumulates unresolved divergent entries. Serendipity is protected by making
**adoption cheap**: one `item add` + optional product create, straight from an
inbox entry. The program **surfaces, classifies, decides, and logs; it never
silently absorbs and never blocks.** The change-kind vocabulary (§The change log)
extends deliberately and reversibly (ADR-165) to carry the adopt / distraction /
defer decisions, exactly as phase 5 added `dead_link_opened` / `dead_link_cleared`.

### Lifecycle and absence — people who move on, products replaced, milestones not yet set (R24, R25)

- **People carry a status (R24).** `people[].status` (`active` / `alumnus` /
  `historical`) keeps a contributor who has moved on **on the record with their
  contributions credited**, never deleted. The `program.ownership` read already
  reconstructs a past owner accurately from the immutable change log even after
  they leave the roster (§The mutation surface); the status field makes that
  explicit on the roster itself, so a renderer shows an alumnus as an alumnus
  rather than dropping the row or blanking the name. A status change is an ordinary
  `person edit`, logged `person_edited`.
- **Products carry an end-of-life lifecycle (R24).** `active` → `legacy` →
  `superseded`, with `products[].superseded_by` pointing at the replacement
  (§The data-model additions). A product-detail render shows a superseded product
  with its successor link and a legacy product as still-present-but-frozen; the
  mind-map keeps both in the graph (lineage, `derived_from`, often connects a
  successor back to what it replaced). History is **shown, not erased** — this is
  the delivery-is-a-loop lifecycle (R22) carried to the end of life.
- **Absence is a follow-up, not a judgment (R25).** A tracked actor with **no
  current milestone** renders **forward-looking** — "to be set at the next sync" —
  never as a blank cell and never as a deficiency on any public surface. The same
  absence rolls up into a **private program-head follow-up list**: a read that
  returns the active actors who own no dated, uncommitted-or-future milestone, so
  the program head gets a generated **check-in agenda** rather than a scoreboard.
  The roll-up is **private** (served only to the deputy / program head, under the
  identity gate — §Identity gate and read-vs-write exposure — never an anonymous
  or public read) and is **derived, not stored** — computed from `people[]` and
  `schedule[]` on demand, like `drift`. Absence therefore routes to a **human
  decision** at the next sync, never to a silent verdict. This is the
  people-and-milestones instance of the platform's absence posture (R11:
  unverified is not synced; absence has kinds): a missing milestone is an open
  question for a person to answer, not a fact about them.

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
- **ADR-073** — the MCP projection is registry-driven; no parallel manifest
  tool list.
- **Phase 2: served reads answer from the node's file.** The `data` param
  stays a CLI convenience; serving surfaces refuse it rather than ignore it,
  so a caller learns the read did not do what it asked.
- **ADR-165 — self-update and per-consumer change detection.** `data.json`
  is the current state; an append-only change log is the history beside it;
  `sync` is the idempotent reconcile (content hash per item and per field)
  that writes it, under a pluggable read-only source seam. Change detection
  is per-consumer via a watermark, and reporting-is-a-read / advancing-is-a-
  write keeps `changes` a read-only serving tool. The coordinator is the
  existing daemon-agent mechanism (ADR-161), not a new scheduler.
- **ADR-162 — one watcher primitive.** The phase-4 feeders are *instances*
  of a single shared cursor/debounce/content-identity-dedup core
  (`axiom.infra.watcher`), extracted minimally in the feeders' own PR; the
  release extension's bespoke watchers migrate onto it when next touched, not
  in a big-bang rewrite.
- **ADR-166 — membership is a principal, feeders attribute to it.** The
  account map (`people[].accounts`) joins tracker seats to principals; the
  owner stays authoritative; the proxy-assignee rule (lane lead, else deputy,
  else none) is recorded, not posted; a missing account is an onboarding
  finding, not a silent gap.
- **prd-program R9 — connector readiness.** A source is used only after the
  reach → auth → per-project-read ladder; an unverified source is skipped
  loudly, never read as "no changes".
- **ADR-167 — the program is editable through its own tool.** The data file
  is the authoritative editable source of truth; the mutation surface edits
  it with no hard dependency on any external tracker or harness. Membership
  is a scoped membership over the principal primitive, resolved via the
  directory seam when configured; the invitation flow reuses the gate
  invitation primitive; mutations are deputy/maintainer-gated in-body and
  CLI-only, never anonymous MCP tools.
- **Mutations advance the snapshot, so `sync` does not re-log them.** A
  mutation shares the snapshot exclusive lock with `sync` and diffs against
  the last reconciled snapshot (or the pre-edit state on a never-synced
  node), so item/lane/drift deltas log exactly once with the right kind and
  a later self-reconcile is a no-op.
- **A mutation records `by`.** Every mutation entry names the acting
  principal, so an owner change is an immutable transition (old principal →
  new principal, when, who) and `program ownership` can report a past owner
  accurately even after they leave the roster.
- **Phase 5 — crosslink health, endpoints, account-aware reads (no new ADR).**
  Three additive, reversible decisions, each a direct consequence of an
  accepted ADR rather than a new hard-to-reverse one:
  - *Crosslink health rides the feeder* (ADR-165's change log + ADR-162's
    read-only clients + R11's "no dead links / unverified is not synced"): the
    live check happens where a client and connector readiness exist, records
    `dead_link` findings in the `capture` block, and reaches `changes` through
    the existing snapshot diff. The served `status --scope drift` stays a pure
    data-file read (no network); it surfaces what a feeder recorded. The
    change-kind vocabulary gains `dead_link_opened` / `dead_link_cleared` — a
    deliberate, reversible edit per ADR-165.
  - *Canonical endpoints are config-supplied* because Axiom has no
    node-public-URL primitive; the resolution has a single seam
    (`_node_served_url`) to upgrade when one lands.
  - *Account-aware reads make ADR-166 real*: "people with no tracker account
    read their status through the node surfaces" becomes the surfaced tracker
    content plus the gated-link hint.
- **ADR-171 — adaptivity / near-zero external dependency (the defining
  posture).** The data file is the authoritative source of truth and every verb
  works with no external system present; every external system (tracker, wiki,
  harness, directory/IdP, vault) is an optional, additive, gracefully-degrading
  connector, used only where configured **and** available **and** additive, and
  absent-or-unauthenticated it reports `unverified` and skips loudly rather than
  breaking or rounding up to synced. It generalizes R9/R10/R14 (and ADR-166/167)
  and governs every future addition — a new feeder or product type is data and
  config, not code; the platform meets people where they work. It is also the
  internal-machinery ethos: the instrument surfaces are existing parts
  re-aimed, not a new engine.
- **ADR-172 — products are the composable spine, up a ladder to a North Star.**
  Every item is accountable to at least one product (hard rule); products are a
  first-class record composing through composition / dependency / lineage edges
  (a graph, status and evidence roll up); seven type families seed a taxonomy
  whose types and lifecycles are data; evidence is a crosslink, never a copy; and
  the ladder's apex is a single `program.north_star`. The schema gains
  `products[]`, `schedule[].produces`, and `program.north_star` additively (the
  graph is `axiom.program/0.2`).
- **ADR-173 — estimation is deterministic; the model proposes a size, never a
  date.** The forecast is computed (measured throughput × resourcing, over sized
  scope, along the dependency gates), throughput a distribution read from the
  change log; the model only proposes a t-shirt size and rarely asks one
  question; the answer is a range with assumptions carrying `axiom.uncertainty`;
  calibrated sizes flow back consent-gated. The read path touches only local
  data (ADR-171).
- **ADR-176 — emergent work and divergence.** Work done but never planned is a
  first-class flow, opposite in direction to the plan: the feeders **detect** it
  (landed work mapping to no tracked item → the `emergent` inbox, orphan detection
  at the work level), the tracker **classifies** it on one alignment axis
  (improvement vs side-trail, against the product spine and the North Star — it
  proposes, never decides), a human **decides** in the shaping flow (adopt / name
  a distraction / defer), and the decision is **logged** with its rationale in the
  change log (an audit of why the program changed course), with a **divergence
  view** making the discovery-vs-drift balance visible. Strictness is
  **visibility, not prohibition** — nothing forbidden, nothing silently absorbed
  or dropped, adoption kept cheap to protect serendipity. It reuses the feeders
  (ADR-162), the ladder (ADR-172), the mutation verbs (ADR-167), the shaping flow
  (R20), and the change log (ADR-165) — **no new engine** (ADR-171). The schema
  gains the `emergent` inbox, `people[].status`, and `products[].superseded_by`
  additively.
- **Phase 7+ fold-ins (no new ADR).** The roadmap-as-instrument (R18), scenarios
  as non-destructive forks with what-ifs as the mutation verbs on a fork,
  conversational shaping (R20), and the delivery-is-a-loop ethos (R22) are
  additive, reversible applications of machinery that already exists — the
  mutation verbs (ADR-167), the change log (ADR-165), the feeders (ADR-162), and
  the renders — orchestrated by the existing agent (ADR-161). They are captured
  in prd-program R18–R22 and §Forward design rather than as separate
  hard-to-reverse decisions; the one architectural commitment they rest on — that
  these surfaces are existing parts re-aimed and not a new engine — is ADR-171's
  posture, so it is not re-decided here.

## Open questions

- Where the data file canonically lives per program (phase 1 defaults to
  `<state_dir>/program/data.json` and every verb takes `--data`); settles
  when `collect`/`post` land. Phase 3's `sync` is the first updating writer
  of `data.json`, but still from a file source; the multi-program question
  below is unchanged.
- ~~The live capture feeders (GitLab/GitHub) that fill the `sync` source
  seam as ADR-162 watcher instances, carrying `landed` vs `in-flight` and
  collapsing mirrors on commit identity.~~ Resolved in phase 4: `GitLabSource`
  / `GitHubSource` on the shared `axiom.infra.watcher` primitive. **Wiki and
  transcript feeders remain** — they are future instances of the same seam.
- **Writing back to the tracker** (posting progress, setting the computed
  proxy assignee, repairing a moved backlink) — phases 4–5 ingest, attribute,
  and detect only; the `assignment` and each `dead_link`'s `proposed_fix` are
  the recorded handoffs to that later consent-gated posting phase.
- **Webhook capture.** Phase 4 is cursor poll; the watcher primitive admits a
  webhook mode (ADR-162 rule 2 — one event schema regardless of capture
  mode), and wiring origin webhooks onto the bus is a later option.
- The session-boundary / event trigger that fires `sync` between heartbeats
  (phase 3 wires only the heartbeat cadence; event-driven hookup is later).
- ~~Whether `status`/`validate` project to MCP as read tools before the
  coordinator phase.~~ Resolved in phase 2: they do, read-only.
- Whether a node serves more than one program (today: one default data
  file per state dir, so the served reads answer for exactly one). Settles
  with the canonical-location question above.
- The tracker-comparing half of `drift` (R11): phase 4 landed assignee/
  account attribution, due-date agreement, orphan issues, and repository
  mirror agreement (synced / gap / unverified); phase 5 landed crosslink
  health (dead bound issues and dead declared endpoints, unverified ≠ healthy).
  **Roll-up membership across clerk-to-clerk composition (R8)** remains for a
  later phase.
- **The product spine (phase 7+, §Forward design).** The `axiom.program/0.2`
  schema revision (`products[]`, `schedule[].produces`, `attachments`,
  `program.north_star`), the type-family / lifecycle configuration surface, and
  the strictness ramp of the every-item-produces-a-product rule (informational →
  refusal, a deployment-posture dial).
- **The instrument surfaces (phase 7+).** The roadmap renders (see / reshape /
  search / model / ask, the mind-map, the product-detail view), scenarios as
  non-destructive forks, the deterministic forecast, and conversational shaping —
  all renders / agent-orchestration over existing data + skills; the build order
  within phase 7+ is open.
- **Usage/feedback as a feeder kind, and outcome calibration (R22).** Where
  adoption and usage signals enter (a feeder instance on the ADR-162 seam) and
  how "which deliveries got adopted / which estimates held / which priorities
  paid off" is measured to close the recursive loop.
- **The deep-link/share endpoint** depends on the node-public-URL primitive the
  §Canonical endpoints seam still lacks; until it lands, the absolute canonical
  URL stays deployment-config-supplied.
- **Emergent work and divergence (R23, ADR-176).** How the alignment axis is
  **scored** from local data — how an emergent entry's improvement-vs-side-trail
  reading is computed from the actor, the lane, the touched artifacts, and the
  products those map to, and how confident the proposal is allowed to look while
  staying loudly provisional (the human still decides). How **aggressively to flag
  accumulation** — the threshold and cadence at which the shaping agent gently
  prompts on unresolved divergent entries, a deployment-posture dial (visibility,
  never a block). And the detect-pass mechanics: attributing landed work to an
  actor when it binds no issue, and recomputing which inbox entries remain unmapped
  each cycle without re-appending.
- **People/product lifecycle rollups (R24).** Whether a product's status rolls up
  along the composition graph (a composite whose children are all `superseded`),
  and whether an `alumnus`/`historical` owner on an open item is itself a drift
  finding (reassign vs keep-credited) or only a render concern.
- **Arming gate.** Phase 7+ build follows a resilience / failure-injection /
  adversarial-input gate before it is armed on a live node.
