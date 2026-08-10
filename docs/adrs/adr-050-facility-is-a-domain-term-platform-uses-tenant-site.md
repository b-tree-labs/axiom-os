# ADR-050 — "facility" is a domain term; the platform uses "tenant" + "site"

**Status:** Accepted (2026-05-28)

## Context

"facility" is overloaded. In a domain consumer (e.g. a nuclear-engineering consumer at
a deploying site) a **facility** is a *test/experiment facility* — a domain object
(an experiment rig, an experiment),
surfaced by the consumer's `facility` command + facility packs. In the platform, the
same word had crept in for unrelated concepts: the data-owning org / multi-tenancy
partition (`facility_id`), the deployment location, and network-policy routing
("facility VPN", "facility-policy tags").

Conflating the platform's tenancy/site concepts with the domain's facility object
is a trap: it leaks domain meaning into the domain-agnostic platform and creates
real ambiguity (a `facility_id` row-partition vs an actual domain facility). It
surfaced while reviewing the data-platform docs (`facility_id` partition keys, a
"Model Corral Tables" section, a "Facility Manager" persona — all consumer-shaped
content in the generic spec).

## Decision

- **"facility" is reserved for the downstream domain.** It does not appear in
  Axiom for the building / org / tenant / site sense. (The domain consumer keeps it for the
  test/experiment facility + its `facility` command + facility packs.)
- **Axiom uses `tenant`** for the data-owner / multi-tenancy / isolation /
  partition concept — `tenant_id`, "multi-tenant isolation", cross-tenant queries.
  A downstream facility *maps to* a tenant.
- **Axiom uses `site`** for a physical deployment location (acquisition /
  processing-served sites). The deploying-site / self-hosted-node / HPC-cluster mapping is the consumer's.
- **Disclaimers that name the domain are fine** ("downstream products define
  schemas, e.g. for a domain's facilities") — they point *at* the domain rather than
  using "facility" as a platform concept.

## Consequences

- **Docs (done):** the data-platform spec + PRD are renamed — `facility_id`→
  `tenant_id`, "multi-tenant facility"→"multi-tenant", "Cross-Facility"→
  "Cross-Tenant", "Facility Manager"→"Operations Manager", "regulated facilities"→
  "regulated deployments", etc. The "§4.3 Model Corral Tables" inventory (a
  consumer table set referencing the Model Corral spec) is **removed** from the
  generic spec — the downstream-tables disclaimer (§4.3 now) covers it.
- **Code (tracked migration):** runtime usages migrate with care + tests — the
  `facility.toml` config filename, any `facility_id` columns/schema, gateway/router
  "facility-policy tags" + "facility VPN" strings, and `infra/cli_tiers.py`
  `facility:*` (the consumer `facility` command's tier mapping — which also shouldn't
  live in Axiom infra; a layering fix in its own right). These are live /
  user-facing, so they don't change in the docs pass.
- **Separate leak, same class (resolved):** the data-architecture spec's former
  external-partnership section named a specific external product (DeepLynx). That
  vendor-specific content belongs in the domain consumer repo, not the generic
  spec; the spec now carries only a vendor-neutral external-interop statement.
