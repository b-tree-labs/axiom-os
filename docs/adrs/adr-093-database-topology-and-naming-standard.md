# ADR-093 — Data persistence topology & naming standard: serving/analytics tiers, one provider seam for every store, polyglot by exception

**Status:** Proposed · **Date:** 2026-07-14
**Owner:** @ben
**Amends:** ADR-052 (Database Tenancy — schema-per-extension). Keeps its `session_for` primitive, schema-per-module default, per-module Alembic, and tenancy menu. **Amends** its D1 (*one* Postgres per install → *two sanctioned tiers*), generalizes its provider from SQL-only to a **StoreProvider family**, renames the shared namespace `public` → `shared`, and gives its D5 cross-boundary rule a concrete mechanism (the Publisher contract, D5 here).
**Builds on:** ADR-049 (data-platform boundary — Gold is the cross-boundary product), ADR-050 (tenant/site vocabulary), ADR-012 (provider three-layer identity), ADR-031 (modules own their migrations).
**Concurrent:** ADR-094 (permission-aware retrieval — its `access-policy` config and identity→tier grants live in the serving tier defined here).

## Context

ADR-052 set the right relational defaults but is still *Proposed*, is scoped to *extensions*
(silent on core modules like `rag`/`memory` and on the data platform's own database), and the
deployed serving stack was built around it — so the topology grew by accident. Two further
gaps surfaced (Ben, 2026-07-14): the standard must cover **more than Postgres** (KV, document,
object, and vector stores will all appear — sessions, caches, blobs, embeddings), and it must
be **communicative** down to the name (`public` invites the very dumping it should prevent).

As every module with a declared persistence dependency layers in, the design must be
**intentional, communicative, and foundational** — the precedent every future module is
measured against, whatever store it reaches for.

### Deployed drift this ADR corrects (audited 2026-07-14)

| Intended | Deployed | Fix |
|---|---|---|
| One home per logical dataset | Two Postgres instances (serving `axiom_db` + data-platform `axiom`) with duplicated `bronze`/`silver`/`gold` | **Named two-tier split** (D1) |
| Module tables in own schema | RAG `documents`/`chunks` in `public` | Core modules get schemas; `public`→`shared` (D3, D4) |
| Cross-boundary data via the platform | `bronze_manifest_sync.py` kubectl-execs a JSON copy | **Publisher contract** (D5) |
| — | `axiom_db` / `axiom` / sibling `*_db` / orphan schemas | **Naming convention** (D6) |
| — | (no story for KV / blob / vector) | **One provider seam, polyglot by exception** (D2, D3) |

## Decision

### D1 — Two sanctioned tiers

- **Serving tier** (`axiom-serving`): the low-latency, transactional, retrieval path — module
  OLTP, the RAG serving store (embeddings the chat queries), memory, identity/authz, Timescale
  telemetry, and all ephemeral/session state. One logical relational database, `axiom`.
- **Analytics tier** (`axiom-analytics`): the data platform — medallion `bronze`/`silver`/`gold`,
  Dagster-orchestrated, plus Dagster's own `dagster_meta`. Heavy/batch load, isolated from
  serving latency. Relational database `axiom_analytics`.

These are the **only two** sanctioned homes for data. No third accidental instance; **no logical
dataset with two physical homes.** A tier is a *role*, not a hostname — dev may co-locate both on
one endpoint; prod separates them; the contract is identical either way. Every store kind in D3
is placed into one of these two tiers.

### D2 — One provider seam for every backing store

Modules never see a DSN, credentials, a connection pool, a bucket ARN, or a Redis URL. They
consume a **StoreProvider family** — one scoped, credential-free handle per store kind, all
following the ADR-012 three-layer provider identity so audit records name *which* provider served
a call:

```python
from axiom.infra.db import session_for       # relational (SQL)      — ADR-052, unchanged
from axiom.infra.stores import kv_for         # key-value
from axiom.infra.stores import docstore_for   # document
from axiom.infra.stores import blobstore_for  # object / blob
from axiom.infra.stores import vectorstore_for# vector
```

The **per-module namespace invariant** is universal — a module's data is isolated by its own
name in *every* store — and is realized per kind (D3). The provider ensures the namespace
exists, scopes the handle to it, and binds it to the correct tier. This is ADR-052 D2
generalized: `session_for` is simply the SQL member of the family.

### D3 — Relational is the default; polyglot **by justified exception**

A module uses the serving relational tier unless it declares a concrete reason another store
fits better. This prevents polyglot sprawl (a different engine per whim). When an exception is
justified, the same discipline applies:

| Kind | Provider | Module namespace | Tier | Use / guardrail |
|---|---|---|---|---|
| **Relational** (Postgres) | `session_for(mod)` | schema `<mod>` | serving; medallion→analytics | **default** for records + relationships |
| **Key-value** (Redis/Valkey) | `kv_for(mod)` | key prefix `<mod>:` | serving | cache, sessions, rate-limits, locks — **never a system of record** |
| **Document** (e.g. Mongo) | `docstore_for(mod)` | collection prefix `<mod>_` | serving / analytics | only when the document model genuinely beats a schema |
| **Object / blob** (S3/MinIO) | `blobstore_for(mod)` | bucket `axiom-<tier>` + key prefix `<mod>/` | both | raw bronze bytes, exports, artifacts |
| **Vector** | `vectorstore_for(mod)` | collection/namespace `<mod>` | serving | **default = pgvector *inside* the serving relational tier**; a separate vector service only if scale demands |

Two invariants across kinds:

- **Non-relational, ephemeral stores are never the system of record.** Anything in a KV/cache
  must be reconstructible from a tier of record (serving relational or analytics Gold). A cache
  loss is a performance event, never a data-loss event.
- **No cross-store distributed transactions.** Consistency across stores/tiers rides the
  Publisher/data-platform boundary (D5), not a two-phase commit.

### D4 — Schemas and the `shared` namespace

Every module owns a schema named after itself — **core** (`rag`, `memory`, `identity`, `authz`,
`policy`) exactly as **extensions** (`expman`, `signals`, …). The cross-module shared namespace
is a schema named **`shared`** (shared enums, the `tenant`/`site` reference tables, common
types). Postgres's `public` schema is **neutralized**: `REVOKE CREATE ON SCHEMA public FROM
PUBLIC`, nothing of ours lives there, and it is retained *only* for objects third-party
extensions insist on installing there. Per-connection `search_path = "<module>, shared"` (with
`public` appended solely so extension-provided functions resolve). RAG's `documents`/`chunks`
move from `public` into `rag`.

### D5 — The cross-tier / cross-store seam is a declared **publication**, not an app copy

Data crosses **analytics → serving** only as a first-class, versioned **Gold publication**
(ADR-049: Gold is the boundary product). A **`Publisher`** primitive delivers **named datasets**
into a declared serving namespace: idempotent, scheduled, audited, provenance-stamped. The
kubectl-exec `bronze_manifest_sync.py` is retired.

- The *contract* is fixed: `(named dataset, target namespace, refresh cadence, provenance)`.
- The *mechanism* is swappable behind it — batch upsert, logical replication, or `postgres_fdw`
  for SQL; a bucket sync for blob — chosen per dataset without changing the contract.
- **RAG specifically:** analytics ingests (bronze → embed) and **publishes** `documents`/`chunks`
  (with `source_url` per ADR-091) into the serving `rag` schema. Serving never reaches into the
  analytics tier at query time. This resolves the two-DB split that motivated this ADR: one
  authoritative serving RAG store, fed by a contract.

### D6 — Naming conventions (across every store kind)

- **The module name is the namespace root everywhere** — schema `<mod>`, key prefix `<mod>:`,
  collection prefix `<mod>_`, bucket key prefix `<mod>/`. One name, one owner, in any store.
- **Instance / endpoint:** `axiom-serving`, `axiom-analytics`. **Environment is encoded by the
  deployment target, never by a name suffix** — no `_dev` / `_staging` / `_prod` on any database,
  schema, bucket, or key prefix.
- **Relational database:** `axiom` (serving), `axiom_analytics` (analytics), `dagster_meta`
  (Dagster's own). Drop the redundant `_db`. A **different consumer/domain is a different
  install**, not another `*_db` in a shared instance.
- **Buckets:** `axiom-serving` / `axiom-analytics`, module-scoped by key prefix.
- **Role:** `axiom_<schema>`, grants scoped to that schema (ADR-052 D6 seam).

### D7 — Migrations & manifest declaration

Per-module Alembic with `version_table_schema = "<schema>"` for core modules and extensions
alike (ADR-052 D3). Each module declares **every** persistence dependency — not just SQL — in its
`axiom-extension.toml`, so the platform can provision namespaces, run migrations, and audit
"which module touches which store":

```toml
[[store]]
kind = "sql"     ; needs_schema = true ; migrations_path = "migrations"
[[store]]
kind = "kv"      ; tier = "serving"     ; reason = "session + rate-limit state"
```

Publications are versioned **separately** from either tier's schema, so a Gold product's shape is
an explicit, reviewable interface — not an implicit side effect of an analytics migration.

### D8 — Reconciliation plan (drift → conformance, staged, non-breaking)

1. **Serving RAG out of `public`:** create `rag` (+ `shared`) schema; `ALTER … SET SCHEMA`
   `documents`/`chunks` into `rag`; point the shim at `search_path = rag, shared`. The
   `source_url` columns from the 2026-07-13 backfill travel with the tables.
2. **Neutralize `public`:** `REVOKE CREATE ON SCHEMA public FROM PUBLIC`; migrate any of our
   objects out; leave only extension-owned objects.
3. **Retire `bronze_manifest_sync`:** stand up the `Publisher`; deliver the provenance /
   `source_url` Gold product into `rag` under the D5 contract.
4. **De-duplicate medallion:** consolidate `bronze`/`silver`/`gold` into the analytics tier; drop
   serving-side copies except published products.
5. **Adopt names:** `axiom` / `axiom_analytics` going forward; alias `axiom_db` during transition.
6. **Retire orphans:** drop the orphan smoke-test schema; populate-or-drop empty `authz` /
   `vault` / `notifications` per their owning module's plan.

Each step is independently shippable and reversible; none requires a big-bang cutover.

## Consequences

- **One discipline, any store:** a module gets a scoped handle and a namespace whether it needs
  SQL, KV, blob, or vectors — same seam, same naming, same tiering. Polyglot without chaos.
- **Intentional split:** OLTP/analytics separation is a named contract that isolates analytical
  load from serving latency.
- **Communicative:** a reader infers *where data lives and why* from the name — `shared` means
  shared, a `<mod>` namespace means that module, a tier means that workload; `public` means
  "not ours."
- **Foundational precedent:** every future module gets a namespace per store it declares, a
  migrations dir, and — if it needs cross-tier data — a declared publication.
- **Costs, honestly:** the StoreProvider family and the Publisher are real infrastructure (vs. a
  DSN and a copy); more provider surface; and the reconciliation migrates a live system. The
  trade is a persistence layer that stays coherent as it grows.

## Alternatives considered

- **Postgres-only standard.** Rejected — it fractures the first time a module needs Redis or a
  bucket; better to set the seam now and default to relational.
- **Single Postgres, everything as schemas** (ADR-052 D1 as-written). Rejected by decision — does
  not isolate heavy analytical/batch load from live serving.
- **Keep the app-level sync** (`bronze_manifest_sync.py`). Rejected — an out-of-band copy with no
  contract, provenance, or versioning; the accident this ADR ends.
- **Keep `public` as the shared namespace.** Rejected — it is the default catch-all, so it invites
  exactly the dumping we are trying to prevent; a named `shared` schema plus a locked `public` is
  clearer and safer.
- **A separate vector database by default.** Rejected as default — pgvector in the serving tier
  keeps embeddings transactional with their documents; a dedicated vector service is an exception
  under D3 when scale demands, not the baseline.
- **Free polyglot** (any store, any time). Rejected — relational-by-default with justified
  exceptions prevents a different engine per whim.
