# ADR-127 — Data orchestration at our tier: declare the graph, let PULSE fire it

**Status:** Proposed (2026-09-24)
**Refines:** ADR-049 (data-platform orchestration boundary) · ADR-055 (PULSE) · ADR-056 (skill as function)
**Composes with:** ADR-128 (medallion tier boundaries) — same edges, two enforcement layers
**Relates to:** ADR-126 (typed decision receipts) · prd-receipts-surface R19 (computed blast radius)

## Which graph this is about

"The graph" is ambiguous across at least three layers, and conflating them is
how this week's two outages looked like one problem:

- **Infrastructure graph** — processes, pods, venvs, units. Edge: *this script
  needs that pod alive.* The Box token failure lived here: a shell script
  `kubectl exec`ing into a Dagster pod to borrow a shell.
- **Data graph** — datasets. Edge: *`silver.signals` derives from
  `bronze._rows`.* The conform failure lived here.
- **Governance graph** — pins, authority, federation topology.

**This ADR is about the data graph only.** Infrastructure dependency is a real
and separate problem — nothing here prevents another script from taking a hard
dependency on a pod — and it wants its own treatment.

## Context

Three things schedule data work on the UT site node today, and a fourth is
supposed to.

- **Dagster** declares the pipeline: `corpus__<slug>` assets per source, a
  cursor-persisted sensor per source, `silver_signals_conform` on
  `dp1_conform_schedule`. This is the design of record under ADR-049.
- **PULSE** (ADR-055) states that *every* recurring or one-shot domain event
  goes through it, constructing an `ActionEnvelope`, consulting
  `authz.decide`, executing, and writing a receipt.
- **PLINTH** operates the data platform and, per ADR-049 §6, "does not replace
  Dagster" — it gates and monitors.
- **Seven systemd timers** do the actual work.

The two accepted ADRs disagree. ADR-049 (May) gives "scheduling +
materialization authority" to Dagster. ADR-055 (later) claims every recurring
event for PULSE. Neither is wrong about its own concern and nobody drew the
boundary between them, so the work grew in the gap — as shell scripts on
timers, which is the one option no ADR sanctions.

**What that cost, measured 2026-09-21..24:**

- Both Dagster pods have been in `ImagePullBackOff` for over two days. Nothing
  noticed, because nothing depends on Dagster — *except* a shell script that
  `kubectl exec`s into the webserver pod to borrow it as a shell for fetching a
  Box OAuth token. When the image broke, that token call failed, `set -e`
  aborted the ingest at line 6, and **no telemetry landed at all**.
- On the days it did run, the conform step failed with `cannot drop columns
  from view` because it executes under a venv carrying axiom 0.47.0 while the
  view was created by 0.58.x. `silver.signals` was stale for three days behind
  a `WARN` the service exit code did not reflect.
- The node carries **four** axiom versions. The Dagster image would bake a
  fifth.

None of those are Dagster's fault. They are what happens when the declared
design is not the running one, and the running one has no declared
dependencies to check.

## The question this ADR answers

Not "how do we fix the image." **What does Dagster uniquely give us, and are we
paying for it?**

Dagster's distinctive contributions are: a declarative asset graph with
lineage; partitions and backfills; freshness policies; sensors with persisted
cursors; a run-history UI; and scheduling with retries and concurrency control.

Against what Axiom already has:

| Dagster gives | Axiom already has | Verdict |
|---|---|---|
| cron/interval scheduling | PULSE cadence | **duplicate** — and PULSE adds authz + receipts |
| retries, dead-letter, leases, concurrency | PULSE `lease`, `recovery`, `conflicts`, `blackout`, `chaos` | **duplicate** |
| sensors with persisted cursors | `IngestSource.list_changed(since)` — the portable connector contract of ADR-049 §4 | **duplicate** |
| run history | PULSE fire history + typed receipts (ADR-126) | **duplicate, and ours is governed** |
| **declarative asset graph / lineage** | *nothing* | **genuinely missing** |
| partitions and backfills | nothing | missing — **and unused** |
| freshness policy | ad-hoc checks (`reactor_data_freshness`, canary, coverage block) | partially present |

**The measurement that decides it:** our Dagster definitions contain **zero**
references to partitions or backfills. We run the service and use none of the
machinery that distinguishes it. What we use from Dagster — schedules, sensors,
retries — PULSE already does, with an authority gate and a receipt that Dagster
structurally cannot provide.

What we *don't* have, and genuinely need, is the **asset graph itself**: a
declaration that `silver.signals` derives from `bronze._rows` derives from
`box:serial_data`. That is a few dozen edges of data. It is not a service.

## Decision

**1. The declared asset graph is a first-class Axiom artifact, not a Dagster
artifact.** Sources, derivations and their dependencies are declared in the
platform, queryable by agents and by the receipts surface, independent of any
execution engine.

**2. PULSE owns *when*. The graph owns *what depends on what*.** These are not
competing and were never the same question. PULSE fires an action; the graph
resolves what that action entails and in what order. Every firing is
authority-gated and receipted, as ADR-055 requires — which resolves the
ADR-049/ADR-055 conflict in PULSE's favour **for scheduling**, while leaving
materialization semantics to the graph.

**3. At our tier, the execution engine is the lean runner, not Dagster.**
ADR-049 §4 already provides for "a minimal direct runner on a lean/offline node
with no lakehouse," and §"Consequences" notes nuclear facilities are
offline-first. We have been operating the heavy tier's dependencies while doing
the lean tier's work. We should stop pretending.

**4. Dagster remains a supported engine behind the same declaration — not
removed, not required.** A deployment whose scale justifies partitions,
backfills over large date ranges, or a dedicated lineage UI swaps the engine
without rewriting the graph. That is the tiering ADR-049 intended; this ADR
only states which tier we are in.

**5. No recurring data work outside the graph.** A systemd timer touching
medallion data is a defect in the same way an undeclared capability is. Timers
retire as the graph absorbs them, one at a time, not in a single cutover.

## How PLINTH uses this

PLINTH's charter is unchanged from ADR-049 §6 — it operates the platform and
does not become the scheduler. What changes is what it operates.

- **PLINTH owns the graph's integrity.** It registers connectors, validates
  that declared edges resolve, and refuses a registration whose upstream does
  not exist. Its existing provenance gate and `guarded_act` path (AEOS §4.1)
  apply to materializations exactly as they apply to any external mutation.
- **PULSE fires; PLINTH judges.** A cadence fires the action
  `data_platform.materialize` with an asset selection in the envelope. PLINTH's
  skill resolves the selection against the graph, applies the gate, and
  executes through whichever engine the tier provides. The executor seam
  already exists: ADR-056 makes the action string a qualified skill name and
  `SkillExecutor` dispatches it.
- **PLINTH reports freshness as a property of the graph**, not of a script. "Is
  gold current?" becomes a query with an answer, rather than a question you
  answer by reading seven shell scripts in the right order.

## Why this matters beyond the incident

**It makes the Receipts surface's central promise implementable.**
`prd-receipts-surface` R19 bans "Unknown" for blast radius, computing downstream
impact "as reachability over graphs the platform already maintains: schedules,
medallion lineage, version pins, federation topology." The platform does not
maintain medallion lineage. It lives in shell-script line order. Declaring the
graph is what turns R19 from an aspiration into a query.

**It makes data answerable by agents.** An agent asked "why is gold stale?"
currently cannot find out. With declared edges it is a traversal. This is the
difference between a data platform and a collection of cron jobs that happen to
share a database.

**It scales the way the deployment actually scales.** Partner sites and the
site-repo generalization program multiply sites, not asset
complexity. Seven timers per site times N sites is not a plan. One declared
graph per site, fired by PULSE under site-scoped authority, is — and it is the
same artifact a federation peer can be shown without being given access.

## Composition with ADR-128 (tier boundaries)

ADR-128 enumerates what each medallion tier may do — bronze receives, silver
transforms (twelve named transforms), gold derives and never transforms — and
enforces it with a static guard plus `axi data tier-audit`. That ADR and this
one are describing **the same edges from opposite ends**: ADR-128 is a rule
about what a node may do; this is a declaration of what a node depends on.

Two consequences follow, and both are adopted here:

**1. A graph node declares its tier.** The two enforcement layers then compose
instead of running independently: ADR-128's audit finds a gold base table that
nothing derives; this graph finds a declared asset whose tier does not match
where it landed. Neither finds both alone.

**2. Authored and analysis assets are nodes with no in-edges, never absent.**
A calibration curve, a roster, an analysis output (ADR-128 E1/E3) has no raw
form and must not be forced through bronze — a hand-authored record pushed
through bronze produces a raw row that is a copy of the authored record and a
conform step that copies it again. But omitting them from the graph is worse:
**absence from lineage is exactly how they became invisible.** They are
declared, with zero upstreams and a stated provenance, so "what feeds gold?"
has a complete answer.

## Consequences

- The Dagster py3.12 base image (`FOLLOWUP-dagster-py312`) stops being on the
  critical path. Every published `dagster/dagster-k8s` tag, including `latest`,
  ships **Python 3.10.21**; axiom requires ≥3.11. That base must be built and
  maintained by hand, indefinitely, for an engine whose distinguishing features
  we do not use.
- The dagster-webserver pod stops being load-bearing for anything. In
  particular the Box OAuth token fetch must get its own credential path rather
  than `kubectl exec`ing into a UI pod — that coupling is what turned a broken
  image into a total ingest outage.
- Two pods, a Postgres run store, and a fifth axiom version leave the node.
- We lose partitions and backfills **that we were not using**. Recovering a
  date range becomes a skill parameter rather than a Dagster backfill, which is
  the same work without the daemon.
- We lose the Dagster run UI. PULSE fire history plus typed receipts is the
  replacement, and it is authority-gated, which the Dagster UI is not.

## What would reverse this

Stated up front so the decision is falsifiable rather than permanent:

- Asset count into the hundreds, where hand-declared edges stop being
  reviewable.
- A real need for partitioned backfills over long date ranges.
- A second team materializing into the same lakehouse who need the UI as a
  shared surface.
- dbt or Iceberg entering the stack for real, where Dagster's integrations stop
  being incidental.

Any of those makes the heavy tier the right tier, and §4 means switching costs
a configuration change rather than a rewrite.

## Open questions

1. Where the graph is declared — extension manifests (like capabilities), a
   site profile (like pins), or a table. Manifests keep it with the code that
   produces the asset; a profile keeps it with the deployment that schedules it.
2. Whether freshness is a property of an edge or a separate policy object.
3. Whether the lean runner reuses `conform_rows` directly or gains a thin
   graph-walking layer above it.
