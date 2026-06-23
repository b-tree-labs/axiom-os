# ADR-052: Database Tenancy — Schema-per-Extension via a DatabaseProvider Primitive

**Status:** Proposed (2026-05-29)
**Supersedes:** none
**Refines:** none — establishes the platform's default DB-tenancy contract
**Builds on:** ADR-050 (vocabulary — `tenant` / `site` / no `facility` in platform code)
**Related:** ADR-012 (provider identity — three-layer pattern this primitive follows), ADR-031 (extension self-containment — each extension owns its migrations dir), ADR-048 (brand-scoped extension visibility — sibling/distribution model), ADR-049 (data-platform orchestration boundary — where cross-extension *reads* belong)
**Specs:** `spec-aeos-1.0.md` (`[database]` manifest block follows from this ADR).

---

## Context

`expman` is the first domain-consumer builtin extension with primary persistence; `signals` already ships Alembic migrations under the same Postgres. Every additional extension that needs a database (Keplo, Vyzier, downstream consumer extensions) will compound the same question if we don't lock the answer:

> *How do colocated extensions share an RDBMS without colliding, without each extension reinventing credentials/pools/schemas, without leaking each other's tables, and without spawning a per-extension Postgres on every Axiom install?*

Ben's constraint (2026-05-29) was the right shape: **guided, predictable, safe, options-not-cages, no duplicate RDBMS per install**.

The platform also already named the concepts in ADR-050: **tenant** (data-owner / partition / isolation key within an extension's data) and **site** (the physical install location). This ADR provides the database *mechanism* for those concepts; ADR-050 owns the *vocabulary*.

---

## Decision

### D1 — One Postgres per Axiom install; **schema-per-extension** is the default

Axiom assumes exactly **one Postgres instance** per install (dev: `axi db up`'s docker-compose backend; prod: the operator's deployment). Extensions never spin up their own RDBMS.

Each extension owns one **Postgres schema** named after itself (PEP 503-ish normalized — see D2): `expman`, `signals`, `keplo`, … Tables live in that schema; the public schema is reserved for cross-extension types (e.g. shared enums, the eventual `tenant`/`site` reference tables) and Axiom-managed metadata.

This is Postgres-native isolation: `search_path` + role grants enforce that one extension's connection cannot read another extension's tables by default.

### D2 — A `DatabaseProvider` primitive in `axiom.infra.db`

Extensions never see a DSN, credentials, the connection pool, or schema-creation DDL. They consume a single API:

```python
from axiom.infra.db import session_for

with session_for("expman") as session:
    session.add(...)
    session.commit()
```

The provider:

1. Owns a process-wide **shared Engine + pool**, configured from `AXIOM_DB_URL` (default `postgresql://axiom:axiom@localhost:5432/axiom_db` — the dev-env convention `axi db up` already uses).
2. Computes the schema name from the extension name (normalize: lowercase, hyphens → underscores, ASCII-safe, length-capped).
3. Ensures the schema exists (`CREATE SCHEMA IF NOT EXISTS …` — idempotent; safe to call on every session).
4. Returns a SQLAlchemy/SQLModel `Session` with `search_path = "<extension>, public"` set per-connection (so unqualified table names resolve to the extension's own schema).

The primitive follows the **ADR-012 provider pattern**: `DatabaseProvider` carries the three-layer identity (`name`, `config_hash`, `instance_id`) so audit records can name *which* DB provider served a query, and so a future "different Postgres per site" setup is config, not code.

### D3 — Alembic, per extension, against the extension's own schema

Each extension brings its own `migrations/` directory (the `signals` extension's layout is the working precedent — `alembic.ini`, `env.py`, `versions/`). Two refinements from D1/D2:

- `env.py` calls `engine_for("<ext>")` (a sibling helper to `session_for`) so it gets the shared engine *bound to the extension's schema*.
- `alembic.ini` / `env.py` sets `version_table_schema = "<ext>"` so Alembic's `alembic_version` table itself lives in the extension's schema — each extension is fully self-contained.

Orchestration across extensions (`axi db migrate` runs every installed extension's migrations in dependency order) is a follow-up, *not* this ADR; the per-extension Alembic story works today.

### D4 — Within-extension tenancy: a **guided menu**, not a single answer

For multitenancy *inside* an extension (multiple `tenant`s per ADR-050 — many facilities, cohorts, projects in one `expman`), Axiom offers three named patterns. The extension author picks at scaffold time; Axiom hands them the boilerplate for the choice:

| Pattern | When | Provider hands you |
|---|---|---|
| **Single-tenant** (default) | One logical tenant per install | The scoped session from D2; nothing else |
| **Row-level via `tenant_id`** | Many tenants, soft isolation, cheap cross-tenant queries | A `TenantContext` middleware + a `tenant_id` mixin for SQLModel |
| **Schema-per-tenant** | Hard isolation, regulatory, few-but-heavy tenants | A per-tenant schema factory + session router |

This stays out of the cage trap: most extensions get the single-tenant default; the ones that need more have a documented, supported path that doesn't require reinventing the wheel.

### D5 — Cross-extension reads go through the data platform, **not** the OLTP layer

Extensions **must not** join across schemas in their OLTP repositories. Cross-extension reads are an analytics concern and ride the data-platform Bronze → Silver → Gold layers (per ADR-049's boundary) — Vega-style classification gates apply there, where they belong.

This keeps extension boundaries clean, prevents schema-coupling, and matches `prd-data-platform.md`'s topology: each extension's schema is one Bronze source; cross-extension views live in Silver/Gold.

### D6 — Role + grant model (deferred, but the contract is the seam)

`DatabaseProvider` connects with a *role* (not just credentials) whose grants are scoped to the extension's schema. Today (MVP) the role is the same `axiom` superuser the dev env uses; the *contract* is that the provider opens the connection that future hardening grants/revokes against. Per-extension roles + revocation drills are an explicit follow-up in the implementation issue, not blocking the primitive.

### D7 — Manifest declaration

An extension that needs the DB declares it in its `axiom-extension.toml`:

```toml
[database]
needs_schema = true
migrations_path = "migrations"    # default; optional
```

This is the seam for `axi db migrate` (D3 follow-up): the platform finds every extension with `[database]` and runs its migrations on `axi update --migrate`, on install, and on demand. The `[database]` block is also what makes "is this extension misbehaving?" answerable — the manifest is the audit surface.

---

## Consequences

**Positive**

- Extension developer carries *one* import and *one* concept (the scoped session). Credentials, pool, schema names, search_path, schema provisioning: all the platform's problem. ← Ben's "not burdened."
- One Postgres per install regardless of how many extensions ship — no duplicate-RDBMS surface area. ← Ben's "no needless duplicates."
- Naming the patterns (single / row-level / schema-per-tenant) keeps options open without putting them in front of every author. ← Ben's "options they need."
- Postgres-native isolation (schemas + roles) is well-understood, well-trodden, and reviewable — no bespoke security perimeter. ← Ben's "safe."
- `expman` becomes the **reference extension** for the primitive: it builds against `expman` schema from day one (EM-005 #33). When `DatabaseProvider` lands, expman is a one-line refactor — not a schema migration.

**Negative**

- Each extension now has a real platform surface to live up to (a schema, a role, a migrations dir). The trade for that is "every extension behaves the same way" — predictability across the portfolio.
- Within-extension multitenancy still requires extension-author judgment (pick a pattern). The menu reduces that to a labeled choice, but it isn't zero.
- Cross-extension joins are *intentionally* hard. Extensions that "really want" a join must instead model the question as a Silver/Gold view — slower to build, but exactly the analytics-boundary discipline ADR-049 set up.

**Reversibility**

- Schema-per-extension is a Postgres-native convention; moving to database-per-extension later is mechanical (per-schema dump + restore). Moving in the other direction (currently per-DB → schema-per-extension) is the painful one, which is the case for picking schema-per-extension as the *default*.

---

## Notes

- The dev story is unchanged: `axi db up` (INFRA-2's deployment providers) gives one Postgres; `AXIOM_DB_URL` keeps working; everything new is *on top*.
- The `axiom.infra.provider_base` (ADR-012) three-layer identity carries over: `DatabaseProvider` has a `name` (the install's DB provider id), a `config_hash` (DSN + role fingerprint), and an `instance_id` (per-process). That identity rides on audit records.
- Cross-platform: the contract is "Postgres." SQLite is explicitly out (the platform standard already excludes it). MS-SQL / others are not in scope.
- Operational surfacing: an extension's schema is the natural unit for backup-and-restore policy, dump scope, and disaster-recovery boundaries — orthogonal to this ADR but enabled by it.
