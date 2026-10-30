# ADR-120: Tenancy Control Plane — centralized provisioning, migration fan-out, and a registry + drift guard so schema-per-tenant can't accrete entropy

**Status:** Proposed (2026-09-12)
**Refines:** ADR-052 (Database Tenancy) — closes its deferred **D3** (migration orchestration across tenants) and **D4** (schema-per-tenant factory/router), and adds the operational guardrail layer ADR-052 did not.
**Builds on:** ADR-025 §9 (tenant offboarding — the reaper registry whose philosophy this generalizes), ADR-050 (vocabulary: `tenant` / `site`), ADR-049 (cross-tenant *reads* belong to the data platform, not OLTP joins), ADR-031 (each extension owns its migrations dir).
**Related:** the conformance drift-detection design (bronze→silver: detect → HERALD notify → RACI-gated repair) — reused verbatim in shape for tenancy drift.

---

## Context

ADR-052 adopted **schema-per-tenant** (D4) for the hard-isolation cases — regulatory tenants, few-but-heavy tenants, and (ADR-005) the staging namespaces and per-site partitions. The *primitives* already exist: `ensure_schema()` provisions, `session_for()` sets and resets `search_path`, Alembic runs per-schema via `version_table_schema`, and `infra/tenancy.py` offboards a tenant through a reaper registry.

The danger schema-per-tenant introduces is **not** in the primitive — it is **operational entropy** that, left ad-hoc, surfaces as crashes:

- a migration applied to 4 of 5 tenant schemas leaves the 5th a **latent failure** that fires on the next query that hits it;
- a tenant created outside the provisioner is **invisible to the migrator** forever;
- a new feature doing a raw `connect()` or a hardcoded `schema=` **silently reads the wrong tenant**.

ADR-052 explicitly deferred the orchestration (D3) and the schema-per-tenant factory/router (D4). The decision here is to close both **and** add the guardrail, as **one control plane**, so that adopting schema-per-tenant does not mean each new tenant or feature is a fresh chance to diverge.

The philosophy is not new to us. `infra/tenancy.py` (ADR-025 §9) already states the right principle for offboarding: *coverage is the hard part; the danger is a delete that succeeds while quietly missing a subsystem; never imply completeness you cannot demonstrate — track the `UNREAPED` gap explicitly.* This ADR generalizes that principle from teardown to the **whole tenant lifecycle**.

## Decision

A single **Tenancy Control Plane** (`axiom.infra.tenancy`, extended) owns the full tenant lifecycle behind one contract. Nothing becomes a tenant, and no tenant-touching code runs, except through it.

- **Tenant Registry** — the source of truth: every live tenant `(extension, tenant_key, schema, migration_version, health, created/updated_at)`. Provisioning writes here; **a schema that is not in the registry is not a tenant** and is flagged.
- **Drift Detector** — a scheduled check that every registered tenant is at head migration and structurally consistent. A straggler emits a **HERALD** alert and a **RACI-gated** `migrate-tenant` action — the *same* detect → notify → gated-repair pattern as conformance drift. Drift is surfaced and gated, **never silent**.
- **Provisioner** (closes D4) — a per-tenant schema factory: create schema → apply canonical DDL → grants → put the shared `_platform` schema on `search_path` → register. One call; invariant-enforced; never an ad-hoc `CREATE SCHEMA`.
- **Migration fan-out** (closes D3) — one runner enumerates the registry, applies each extension migration to **every** tenant schema, records per-tenant version, and **fails loud on the first failure, naming which tenants converged** (no partial-silent state); resumable.
- **Session router** — `session_for(extension, tenant)` sets the correct `search_path` and resets it on pool checkout. The **only** connection path.
- **Reaper / offboard** — the existing ADR-025 §9 registry (teardown with honest coverage + `UNREAPED`).
- **Enforcement** — a lint/CI guard: no raw `connect()`, no hardcoded `schema=`, no `CREATE SCHEMA` outside the provisioner. A new feature cannot reintroduce the footgun.

**The invariant (why entropy cannot accrete):** every tenant-touching subsystem is **registered** and exposes all four verbs — **provision / migrate / validate / reap** — and anything not yet covered sits on a visible `UNREAPED`-style list, **never silently assumed**. New tenants and features enter only through the plane; the drift check and CI gates catch any straggler before it becomes a crash.

## Phased plan (each phase ships value on its own)

- **Phase 1 — Registry + Drift (FIRST; the guardrail).** Build the tenant registry and backfill the tenants that exist today; build the drift detector (is every tenant at head? structurally consistent?) with the HERALD + RACI-gated `migrate-tenant` action. **Ships value immediately and alone:** it makes the *current* tenant state observable and catches *existing* drift, before any factory or fan-out is written. This is the guardrail that makes leaning on schema-per-tenant safe, so it comes first.
- **Phase 2 — Migration fan-out (closes ADR-052 D3).** The runner over the registry: per-tenant version, fail-loud-on-partial, resumable. Now a new migration converges all tenants safely.
- **Phase 3 — Provisioner factory (closes ADR-052 D4) + Enforcement.** The per-tenant schema factory (registers on create) and the lint/CI guard (no raw connect / hardcoded schema / rogue `CREATE SCHEMA`). Now a new tenant can only enter through the plane.
- **Phase 4 — CI gates + shared `_platform` schema + the `validate` verb.** The merge firewall: "a new tenant provisions clean" and "a new migration fans out clean and leaves drift zero"; the shared `_platform` schema convention for reference data/functions; the per-subsystem `validate` verb. Now a new tenant or feature cannot merge without passing.

## Consequences

- Schema-per-tenant becomes safe to adopt for staging namespaces and sites, because its operational cons are **centrally owned and invariant-enforced** rather than re-litigated per tenant.
- **Cost, deliberately:** one control-plane component to build and maintain, and the Phase-4 CI gates add a real merge requirement (a new tenant/migration must pass fan-out + drift-clean). That gate *is* the entropy firewall.
- **Reuse, not a new subsystem:** the drift half is the conformance detect→notify→gated-repair machinery; the reap half is the existing ADR-025 §9 registry; provisioning/routing extend `ensure_schema`/`session_for`.
- Unchanged: the **row-level** canonical signal store (`silver/gold.signals` keyed by `site`) and the **schema-per-extension** default (`session_for(ext)`). This plane is the operational layer for the *schema-per-tenant* option specifically.
- **Residual risk:** at very high tenant counts the fan-out time and catalog cost still bite (ADR-052's scaling caveat stands). The plane makes that cost **managed and visible**, not free — the trigger to move a runaway extension to row-level.

## References

ADR-052 (D3/D4 seams), ADR-025 §9, ADR-050, ADR-049, ADR-031; `infra/db.py` (`session_for`, `ensure_schema`, Alembic `version_table_schema`), `infra/tenancy.py` (`offboard` / reaper registry / `UNREAPED`); the conformance drift-detection design.
