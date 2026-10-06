# Product Requirements: the tenant data kit

**Product / Feature:** The tenant data kit: how a data provider whose data
lives in a shared medallion shapes it, from bronze to gold, and sees the result
in chat and charts.

**Owner:** data platform   •   **Status:** Draft   •   **Last updated:** 2026-10-05

**Related:** [prd-data-platform](prd-data-platform.md) (the medallion this kit
writes into), ADR-128 (tier boundaries: bronze receives, silver transforms, gold
derives), ADR-131 (every install has a medallion), ADR-052 (schema-per-tenant),
ADR-056 and ADR-072 (one skill, projected to every surface), ADR-102 (the
runnable-notebook surface), the conformance RECIPE, the extension lifecycle
(`ext init --template`, `ext activate`, `ext validate`).

---

## 1) Elevator pitch

A data provider runs one command in their own install, gets a working example
of every kind of contribution, iterates against their own data on their own
machine with an assistant beside them, and promotes the result to the shared
platform through CI, where it appears in their chat and their charts and nobody
else's.

## 2) Problem / opportunity

- A tenant's data lands in a medallion they do not operate. Today the only
  thing they can shape is the edge: a provider that reads their instrument.
  Everything after bronze is platform code or the host operator's hand.
- The pieces for the first tier exist and have not been assembled for a
  person: the conformance spine, `conform-try`, four scaffold templates, a
  gated activation, a validation smoke, a runnable-notebook format, a chart
  spec and renderer, and skills that project to chat and MCP. The first
  intended author of a normalizer has been waiting since the pieces were built.
- Silver and gold have no tenant path at all. A tenant who wants a derived
  channel, a daily rollup or a question their chat can answer has to ask.
- The opportunity: the people closest to the data are the ones who know what a
  good building block is. A kit that lets them build it, test it on their own
  data, and ship it is what turns hosting data into a platform they use.

## 3) Goals and success metrics

- Primary goal: a tenant takes a contribution from idea to their own chat and
  charts without the host operator writing code or running a command by hand.
- Success metrics:
  - Time from `kit init` to the first contribution visible in the tenant's
    chat, on their own data: under one working day for the first; under one
    hour for the next.
  - Contributions that reach production through CI with no manual step on the
    host: all of them.
  - Cross-tenant reads possible from tenant-supplied code or SQL: zero, proven
    by test rather than review.
  - Every kit step is reachable from the CLI, the notebook and an agent, with
    byte-identical results (the "one spine" rule the conformance RECIPE
    already tests).

## 4) Key users

- **The data provider's engineer** (primary). Knows their rig and their data;
  comfortable in Python and a notebook; may have never seen the platform.
- **Their agent.** The engineer's own harness (or the platform chat) driving
  the same verbs. Must be able to run the whole loop from the skill alone.
- **The host operator.** Approves promotion to production. Should not have to
  read tenant code to know it cannot touch another tenant's data.
- **A colleague at the same tenant.** Sees what the first engineer promoted,
  in chat and charts, with no setup.

## 5) The experience, end to end

The picture a provider should have: **one folder, five kinds of contribution,
one loop, three ways to drive it.**

### The folder

`kit init` writes a `data/` folder into the tenant's site repository. One
manifest, one subfolder per tier, and a working example in each:

```text
data/
  kit.toml                  which tenant, which contributions, which envs
  conform/                  bronze to silver: a normalizer (Python, pure function)
    example_frame.py
    tests/
  silver/                   building blocks: declared, not coded
    derived.toml            e.g. delta = outlet - inlet, with units and derivation
    roles.toml              what each channel means, so sites can be compared
  gold/                     published objects: SQL over the tenant's own silver
    daily_stats.sql
    daily_stats.toml        title, grain, units, description (what chat reads)
  verbs/                    questions chat can answer: declared over gold
    heat_balance.toml       name, description, parameters, the query it runs
  charts/                   saved views: chart specs over gold objects
    daily_overview.json
  notebooks/
    first-contribution.py   the walkthrough, runnable (jupytext percent format)
```

Every example runs against bundled sample bronze out of the box, so the first
thing a provider sees is the whole loop working, before they change anything.

### The loop

Each step is one verb, and each verb is a skill (ADR-056), so the CLI, the
notebook and an agent all call the same function.

| Step | Verb | What it does | Where it runs |
|---|---|---|---|
| Scaffold | `kit init`, `kit new <kind> <name>` | Writes the folder, or one more contribution from a template | Local |
| Bring data | `kit sample` | Their own raw files (they are the producer), plus a scoped pull of their own served gold for comparison | Local |
| Try | `kit try [<name>]` | Runs every layer on local data: bronze to silver to gold, then each verb and chart. Prints a table and an inline chart per tier | Local medallion (ADR-131) |
| Check | `kit check` | The CI gate: conform lint, tier rules, tenant isolation, verb and chart validation, the domain-name guard | Local and CI |
| Promote | `kit promote --env staging` then `--env prod` | Opens the PR, waits for CI, activates behind the approval gate, validates | Host, through CI |
| See it | (nothing to run) | The tenant's chat lists the new verb; the chart page offers the new chart; gold freshness shows the new object | Host |

### Three ways to drive it

1. **CLI.** The verbs above. Every verb ends by naming the next one, read from
   one declared ladder, as `neut daq` already does.
2. **The workbench notebook.** A runnable notebook per contribution, in the
   jupytext percent format the runnable runbooks already use (ADR-102): a plain
   `.py` file that diffs in review and opens in Jupyter, VS Code or any
   notebook host. Cells call the kit verbs; tables and charts render inline
   from the same chart renderer the web app uses.
3. **The assistant.** The platform chat, or the provider's own harness through
   MCP, holding the same verbs as tools. It reads the notebook, proposes the
   next cell, runs `kit try`, explains a failing check, and drafts the PR. What
   makes this more than a notebook with a chat pane is that **the assistant has
   no private path**: every action it takes is a kit verb that the person could
   have run, recorded in the notebook as the cell that ran it.

### What appears in chat and charts, and for whom

- **Chat.** A declared verb becomes a tool for every principal who can read
  that tenant's site, and for nobody else. Its description, parameters and
  units come from its declaration. A gold object's description is what chat
  reads to answer "what data do we have".
- **Charts.** A chart spec becomes an entry in the chart catalog for the same
  principals. A cross-tenant chart exists only for a principal holding read
  grants on both tenants (the named-feature track), never by a tenant's
  declaration.
- **Freshness and gaps.** A new gold object appears in the freshness and gap
  views the moment it exists, with no per-object setup.

## 6) The rule that keeps it safe: declare on the host, code at the edge

One process conforms every tenant's bronze, and one database serves every
tenant's gold. Tenant Python running there would be one institution's code
running against another's data (ADR-023-A1 §A1.3). So the kit is built on one
rule:

| Contribution | Form | Runs on the host as |
|---|---|---|
| Normalizer | Python | **MVP:** reviewed into a platform package. **v1:** a per-tenant conform worker holding only that tenant's package and bronze partitions (partitions are already connector-day). |
| Silver building block | Declaration (expressions, roles, units) | Platform code evaluating the declaration |
| Gold object | SQL | A view in the tenant's schema, created and queried under a tenant database role that **row-level security** confines to the tenant's rows |
| Verb | Declaration (parameters plus a query over the tenant's gold) | The platform's verb runner, under the same tenant role |
| Chart | Chart spec (JSON) | The platform renderer |
| Anything else in Python | Notebook cells, local helpers, client-side verbs | Only on the tenant's own machine, against the served gold their credential can read |

The consequence for the host operator: approving a promotion means approving a
declaration whose blast radius is the tenant's own rows, enforced by the
database rather than by reading the SQL. The one exception is the MVP
normalizer review, which is why it moves to a per-tenant worker in v1.

What this requires that does not exist today:

- **Row-level security by site** on the shared silver and gold tables, with a
  tenant role per site. Measured 2026-10-05: no table on the host has it.
- **A tenant gold schema** per site, created by the platform, writable only by
  promotion.
- **A declared-verb runner** that projects a verb declaration to chat, MCP and
  CLI like a skill (ADR-072) and executes it under the tenant role.
- **Per-principal tool listing**, so chat offers a tenant's verbs only to its
  readers.
- **The chart catalog reading declared gold objects**, not only the shared
  signals table.

## 7) MVP: the smallest kit worth handing over

The MVP is the first contribution, start to finish, for the first tenant
author. It deliberately leaves the hardest host work for v1.

1. **`kit init` with the five examples** and bundled sample bronze. Acceptance:
   `kit try` succeeds on a fresh clone with nothing configured.
2. **`kit try` on a local medallion**, with an inline table and chart per tier.
   Acceptance: a broken normalizer, a gold query that does not parse, and a verb
   naming an unknown object each fail with a sentence that names the file.
3. **The workbench notebook** for the first contribution, built from the same
   verbs. Acceptance: running it top to bottom equals running the CLI loop, and
   a test asserts it.
4. **`kit check` as the CI gate** in the site repo's workflow. Acceptance:
   a gold query that reads another tenant's rows is refused locally and in CI.
5. **Promotion of the normalizer and the gold object.** The normalizer through
   review into the platform package (the existing path); the gold object as a
   view in the tenant schema under row-level security. Acceptance: the
   tenant's chat answers "what data do we have" including the new object, and
   another tenant's chat does not list it.
6. **Documentation and agent skill.** A guide per contribution kind, the
   walkthrough notebook, and one skill (`build-and-promote-a-contribution`) that
   an agent can follow from `kit init` to a promoted contribution.

**Not in the MVP:** declared verbs on the host (the provider runs verbs
client-side until the runner exists), the per-tenant conform worker, the chart
catalog reading tenant objects, and a web view of a tenant's contributions.

## 8) Phases

- **0 (now):** hand the existing conform lane to its first author on released
  software, as the kit's first contribution kind. This is the MVP's item 5
  normalizer path and needs no new host work.
- **MVP:** items 1 to 6 above.
- **v1:** row-level security and tenant roles everywhere a tenant can read;
  the declared-verb runner and per-principal tool listing; the chart catalog
  reading tenant objects; the per-tenant conform worker.
- **v2:** a web view of a tenant's contributions and their state per
  environment, in the shared app; a contribution gallery tenants can copy from
  each other, opt-in per contribution.

## 9) Non-functional constraints

- **Tenancy:** no tenant-supplied code runs in a shared host process; tenant
  SQL runs only under a role the database confines to the tenant's rows.
- **Determinism:** `kit try` on the same sample produces the same rows, byte
  for byte; the assistant's actions are kit verbs and are replayable.
- **Units:** a contribution that publishes a value without a unit fails
  `kit check`, unless it declares the unit unknown explicitly.
- **Platforms:** macOS, Linux and Windows for every local step; the local
  medallion must not require Docker on Windows to run `kit try` on a sample.
- **Licensing:** no dependency more restrictive than Apache-2.0.

## 10) Open questions

1. **Notebook host.** Jupyter through jupytext is already shipped and is the
   MVP. Marimo (Apache-2.0, reactive, plain `.py` files, assistant built in)
   is the candidate to evaluate for v1, because reactive execution removes the
   stale-cell class of mistake that matters most when a cell writes data.
2. **Local medallion form.** Postgres in Docker matches the host exactly;
   an embedded engine starts instantly and runs everywhere. The kit needs one
   default.
3. **SQL or dbt for gold.** The platform's ADR names dbt for silver to gold and
   the host does not run it. Plain SQL views are the MVP; dbt models are the
   same text with tests and lineage, and the choice can wait for v1.
4. **The review bar for an MVP normalizer** that runs against every tenant's
   rows until the per-tenant worker exists.
