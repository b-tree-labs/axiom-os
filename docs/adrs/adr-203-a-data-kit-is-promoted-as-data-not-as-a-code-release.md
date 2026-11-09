# ADR-203: A data kit is promoted as data, not as a code release

**Status:** Proposed  •  **Date:** 2026-10-24  •  **Deciders:** platform owner

**Related:** [prd-tenant-data-kit](../prds/prd-tenant-data-kit.md) (the kit, §5 loop,
§6 host requirements, §11 status), ADR-195 (a node has a binding; a sandbox is not
the data read), ADR-199 (one door; served verbs by registry), ADR-157 (serving
budgets), ADR-151 (roles separate governing from reading), ADR-114 (MCP authority),
ADR-056 and ADR-072 (one skill, projected to every surface), ADR-052 (schema per
tenant), ADR-128 (tier boundaries), ADR-033 in the consumer layer (adapter code
lives in the site repository).

## Context

A kit is a folder a contributor builds and proves on their own machine: sources,
normalizers (Python, bronze to silver), derived silver, gold SQL (silver to gold),
verbs (named, parameterised questions over gold), and chart specs. `kit-init`,
`kit-up`, `kit-try`, `kit-check` and `kit-down` make that loop work locally today,
against a private Postgres the kit owns.

Nothing gets a kit onto the shared node. The PRD's route is a pull request to the
site repository, CI, and a deploy. That makes every contribution a code release:
someone who is not the contributor has to merge, cut and roll out a version before
a contributor sees their own gold verb answer in chat. It is slow, it puts the
platform's release cadence between a contributor and their data, and it mixes two
different risks: a SQL view over the contributor's own rows, and arbitrary Python.

The owner's requirement: promotion must not need a code update, and it must be
safe. Safe here means what the PRD already states as a metric: cross-tenant reads
possible from tenant-supplied SQL or code, zero, proven by test rather than review.

## Decision

1. **A kit is promoted as a bundle of declarations, not as code shipped with the
   platform.** `kit-promote` packs the checked kit (`kit.toml`, `gold/*.sql` and
   `.toml`, `verbs/*.toml`, `charts/*.json`, `derived/*`, optional `conform/*.py`)
   into a content-addressed bundle and sends it to the bound node over the same
   authenticated door a bound read uses. The node stores it as data (a per-site
   kit registry: site, kit, version, content hash, submitter, state). No
   platform release, pull request or deploy is involved.

2. **A key submits for its own site only.** The submitting credential's site
   (ADR-195's binding, `site_authority`) is the kit's site. A bundle naming
   another site is refused. Home-site operators may submit for any site.

3. **The node re-proves before anyone approves, without touching its data.** On
   receipt the node runs the static half of `kit-check`: declarations parse, SQL
   references only the kit's own schema and the silver it is granted, and a
   bundle carrying Python is refused (decision 6). The sandbox's isolation proof
   writes another site's copy of the rows into silver and removes it; that is
   acceptable on a contributor's own machine and never on the shared node. The
   node's proof is structural instead, checked from the catalog after applying
   inside the same transaction: the site's role holds SELECT on nothing but
   `silver.signals`, shared gold views and its own schema; row-level security is
   enabled on `silver.signals` with a policy for that role limited to its site;
   every shared gold view and every kit view runs with the invoker's rights; and
   every declared verb runs under the role. Any failure rolls the apply back and
   leaves the bundle `rejected` with the reason.

   Enabling row-level security on `silver.signals` makes every role that is not
   the table's owner see no rows unless a policy admits it. Before the first
   apply on a node, every existing reader of silver (the role the gold door
   connects as, dashboards, conform) is given an explicit permissive policy, and
   the change is made at a quiet time with a lock timeout, because it takes a
   brief exclusive lock on a table the conform loop writes to.

4. **An operator approves; the node applies.** A principal holding the govern role
   (ADR-151) approves a checked bundle from the CLI, MCP or web. Applying is one
   transaction per bundle:
   - gold SQL becomes views in a platform-created `gold_<site>` schema, owned by a
     per-site role, with `security_invoker = true`;
   - `silver.signals` carries a row-level-security policy by site for tenant roles,
     so a tenant view sees its own site's rows and nothing else, whatever its SQL
     says;
   - verbs are registered in a runtime verb registry (data, not code);
   - chart specs are registered in the chart catalog, scoped to the site;
   - sources declared in `kit.toml` are registered as connectors for the site.
   A later policy rule may auto-approve declaration-only bundles; the default is a
   human approval.

5. **Declared verbs are served by one runner, as skills.** A declared-verb runner
   loads the registry and exposes each verb as a skill (`kit.<site>.<verb>`) with
   its declared parameters and description. It runs the verb's SQL under the
   site's role, inside the ADR-157 budget, after the same `site_authority` check
   every gold read gets. Because it is a skill, it reaches the CLI, MCP, chat
   tools and the served door (ADR-199's registry) with no per-verb code
   (ADR-056, ADR-072). Listing is per principal: a site's verbs are listed to that
   site's principals and to home-site operators.

6. **Contributor Python never runs inside a platform process.** A normalizer in a
   promoted bundle runs only in an isolated per-site conform worker: its own
   container, no network, the bundle mounted read-only, CPU, memory and time
   limits, and a database role whose only grant is inserting silver rows for its
   own site through a narrow function. The platform reads what the worker wrote,
   as it reads any producer. Until that worker exists, the node refuses a bundle
   that carries `conform/*.py` with a message saying so; declaration-only bundles
   promote.

7. **An agent in a kit is a declaration too.** A kit may declare an agent as a
   persona and an allowlist of tools, where the tools are its own verbs and the
   platform's read verbs. It runs on the platform's agent runtime, appears in MCP
   and chat only after promotion (ADR-114 gates it like any agent), and carries no
   code. An agent that needs code is an extension and follows the extension
   lifecycle, which is a release by definition.

8. **Every promotion is versioned and reversible.** Applying a new version
   supersedes the old in one transaction; `kit-retire` removes a kit's objects
   and registrations; the registry keeps every bundle, its hash, its proof and its
   approver, which is the audit trail.

9. **The sandbox may read silver.** Kit-only verbs (`kit-try`, `conform-try`) read
   silver in the contributor's own sandbox. They are not served (no `serve="face"`)
   and never run against the shared node. This amends ADR-199 §5, which forbids
   user-facing verbs from reading silver: the sandbox is the contributor's
   workbench, not a served surface.

## Consequences

- A contributor sees their own gold verb answer in chat minutes after an operator
  approves, without anyone cutting a release.
- The safety argument moves from code review to enforced boundaries: row-level
  security on silver, `security_invoker` views, a role that can see one site, a
  server-side isolation proof on every bundle, and a worker with no network for
  code. Each has a test that fails if the boundary is removed.
- The node needs new state: the kit registry, per-site roles and schemas, the RLS
  policy on `silver.signals`, the verb registry, and chart catalog entries. These
  are expand-only migrations (ADR-182).
- The PRD's pull-request route remains available for kits the site wants in its
  repository, and is the route for extensions with code. It is no longer the only
  way to promote.
- Until the isolated conform worker exists, a kit that needs new Python conform
  code cannot promote that part. Its gold, verbs, charts and declared agent can.

## Phases

1. Bundle, server-side check, approval and apply for declarations: gold views
   under per-site roles with RLS, verb registry and runner, chart catalog entries,
   source registration. Isolation proof on the node. Retire and supersede.
2. Declared agents (persona plus tool allowlist), listed in MCP after promotion.
3. The isolated per-site conform worker for normalizer code.
4. An auto-approval policy for declaration-only bundles, if the owner wants one.

## Addendum: Automations (2026-10-26)

A kit can declare an **automation**: run one of its own verbs on a cadence and, when
a value in the verb's rows crosses a line, notify the submitter or annotate the data.
It is a declaration like the others (`automations/<name>.toml`, TOML only; the bundle
refuses any other file there at both ends), so it promotes by the same route:

```toml
description = "Tell me when the hourly mean runs hot."   # required
every = "1h"                    # or cron = "0 7 * * 1-5"; at least 5 minutes apart
verb = "my_view_by_hour"        # a verb declared in this kit
params = { limit = 24 }         # only the verb's declared parameters
[when]                          # absent: act on every run
column = "mean"
above = 30.0                    # or below = ...; or any_rows = true
[then]
notify = "submitter"            # and/or:
annotate = { label = "hot", note = "mean above 30" }
```

- **Checked like the rest.** `kit-check` (and the node's submit) refuse an empty
  description, a verb the kit does not declare, an undeclared parameter, a column that
  appears in neither the verb's query nor its object's, a malformed or too-frequent
  cadence, and a `[then]` with no action. `kit-try` runs each automation once against
  the sandbox and prints what it would send or write; it sends and writes nothing.
- **The node runs it as the site.** Approval writes each automation into the kit
  registry in the same transaction as the views and verbs, then projects it onto a
  PULSE cadence under the owner `@kit_<site>:<site>`. A fire runs the verb through the
  registered-verb runner as `kit_<site>`, evaluates the condition in Python over the
  rows, and acts: a HERALD message to the submitting principal (their inbox always,
  with the rows, capped; the node's Teams channel gets the subject only, since a
  shared channel is a wider audience than the site's key), and/or rows inserted, as
  `kit_<site>`, into `gold_<site>.annotations`, a table that role owns and nobody
  else can read or write. Each fire records its last run, outcome, rows matched and
  error; a failed fire is dead-lettered to the operator.
- **Replaced and removed with the kit.** Re-approving replaces the site's
  automations (an unchanged cadence keeps its schedule; a changed one is re-timed;
  a dropped one is cancelled), and `kit-retire` cancels them. `kit-automations`
  lists a site's automations with schedule, last run and outcome, served through the
  data door like `kit-verbs`, each key held to its own site.
- **It needs the orchestrator.** The cadence fires from the data orchestrator, the
  scheduler's one production host. A node that does not run it fires nothing;
  `data orchestrator-install` writes a per-user systemd unit for it that references
  the node's existing env file and holds no credential.

Not in scope: automations that call a webhook or another node, conditions over
more than one column, and a contributor pausing their own automation without a new
promotion (the operator can retire; the contributor promotes without the file).

## Addendum: Operating it on a live node, and the kit's life after approve (2026-10-27)

A live end-to-end run against a site node (0.70.8) stopped in several places a
superuser sandbox never shows. The decisions below close them, and add the four
lifecycle verbs a site with more than one contributor needs. The decision above
is unchanged: no contributor code is promoted.

- **The submit path runs no DDL.** The serving app may log in with a read-only
  role. `data kit-host-setup --serve-role <login>` is the operator's one-time step,
  run as the database's writer: it creates the registry, grants the serving login
  exactly USAGE on `kit_registry`, INSERT with SELECT (`id`) on `bundles` and
  `withdrawals` plus their sequences, and SELECT on `verbs`, `charts` and
  `automations`, and records the login so each approve grants the new site's role
  to it (a served `kit-verb` runs as that role). It also gives every current reader
  of `silver.signals` its policy and turns row-level security on, once. A node that
  was never set up answers a submit with HTTP 503 naming that command, never 500.
- **An approve stays off the hot table's lock.** Each step on `silver.signals` runs
  only when it is missing (the site's own policy on its first apply; a view's
  `security_invoker`; a grant), so a re-approve takes no ACCESS EXCLUSIVE lock. A
  lock not granted within the timeout, or any other failure of the moment, is
  retried with backoff; if it still fails the bundle stays `checked` with the error
  recorded (`last_error`, shown by `kit-review`). Only a failure of the bundle itself
  (its isolation proof, SQL the database refuses) marks it `rejected`.
- **Code stays behind; it is not refused.** `kit-promote` packs the declarations
  and leaves `conform/` on the contributor's machine, saying so. Every `kit-init`
  kit holds an example normalizer, so packing it refused every new kit. The node
  still refuses any bundle that carries code.
- **Rollback.** `kit-rollback <bundle> --site S` (operator) re-applies an earlier
  `applied` or `superseded` bundle from the registry through the same apply and the
  same isolation proof. The submitter's bundle in force becomes `superseded`. A
  failed rollback never marks the earlier bundle `rejected`.
- **Ownership within a site.** Each view, verb, chart and automation a bundle
  creates is recorded in `kit_registry.objects` with the principal that submitted
  it. A bundle replaces and drops only its own submitter's objects. One that would
  replace another contributor's, or drop a view another contributor's view depends
  on, is not applied and stays `checked` until an operator approves it with
  `--override` (recorded in `kit_registry.events`). Objects promoted before this are
  adopted by the submitter of the bundle that registered them.
- **Retire lists first, and is RESTRICT.** `kit-retire S` (the site is positional)
  lists every object it would drop and every dependent outside them (a view in
  another schema; with `--principal`, another contributor's view or verb) and
  changes nothing, ending with "re-run with --apply to retire", or, when there are
  dependents, "re-run with --force to also drop the dependents outside the kit".
  `--apply` retires and still refuses, dropping nothing, while a dependent exists;
  `--force` cascades. `kit-retire S --principal P --apply` removes only P's objects;
  whole-site retire stays the operator's. Bundles are never deleted, and each retire
  is written to `kit_registry.events`.
- **Kit objects are read at the credential's site.** `kit-verbs`, `kit-charts`,
  `kit-automations` and `kit-verb` (served through `/data/query`), `kit-submit` and
  `kit-withdraw` take the site from the verified credential. Any credential bound to
  a site, the node's home site included, sees and runs that site's objects only, and
  a `site` parameter naming another is refused ("this credential reads site 'A'
  only"), never answered as empty. Only a caller with no site on its credential (an
  operator on the node) names a site. On a bound laptop the four reads go to the node
  (`read_target.KIT_READS`, the one list both sides use); unbound, they read the
  install's own registry and say so.
- **A verb is bounded.** A verb runs under the node's statement timeout; one that
  runs past it is a refusal naming the bound ("did not finish within Ns; narrow its
  window"), never HTTP 500. The starter's gold view matches each pulled channel in
  its feed and reads a declared window (the last 7 days by default), and `kit-check`
  warns on a gold view with no predicate on `ts`.
- **Withdraw is a request.** `kit-withdraw --site S`, from a bound laptop, goes
  through the data door's write path (`/data/submit`, `data_platform:invoke`) and
  records one row in `kit_registry.withdrawals`; a credential bound to one site may
  withdraw only from it. The operator sees it in `kit-review` and applies it with
  `kit-retire S --principal P --apply`, which marks it applied. A contributor never drops
  objects directly.

## Open questions

- The RLS policy on `silver.signals` must not slow the platform's own reads; the
  policy applies to tenant roles only, which is to be measured on the node's data.
- Where the conform worker runs (on the node, or on the producing site's own
  node) is a phase 3 decision.
