# ADR-115 — Generic medallion answering: the platform/pack/site demarcation for data questions

**Status:** Accepted (2026-09-18) — implementation phased; specs drafted
**Deciders:** Platform owner
**Related:** ADR-049 (orchestration/reads ride the data platform), ADR-052 (schema tenancy), ADR-056 (skills), ADR-072/073 (capability projection, registry-driven surfaces), ADR-113 (deterministic quantitative answering), ADR-114 (identity + effect gates), [spec-medallion-answering.md](../specs/spec-medallion-answering.md), [spec-chat-prompt-pipeline.md](../specs/spec-chat-prompt-pipeline.md), [prd-data-platform.md](../prds/prd-data-platform.md), [prd-deterministic-quantitative-answering.md](../prds/prd-deterministic-quantitative-answering.md).

## Context

ADR-113 established ground-or-abstain quantitative answering: deterministic tools compute, the
provenance gate verifies, coverage reconciles gated quantities against producing tools. In the
first deployed consumer, every producing tool is a **hand-written per-quantity SQL function at
the consumer/site layer**, and the serving layer **hardcodes its dispatch table**. Consequences
observed in production:

- The platform's base install can answer **no** quantitative question about loaded data — the
  data platform ships ops verbs only (ingest/backup/diagnose), zero query-answering.
- The most reusable machinery (aggregate-over-a-gold-column) lives at the most specific layer,
  rewritten per quantity; two such functions shipped broken-since-written because nothing
  generic was tested once.
- A chat install without the consumer's verb set silently loses all data answering; the
  demarcation between platform, domain pack, and site is inverted.

## Decision

1. **The data platform ships a generic answering surface in the base install:**
   `gold.tables / gold.describe / gold.aggregate / gold.series` — schema-introspecting ADR-056
   skills over the gold tier, projected to CLI/MCP/agent-tool (ADR-072/073), gated by ADR-114,
   read-only, fail-closed on access tiers, returning the standard provenance envelope. One verb
   with a `table` argument (not per-table projections); a restricted filter grammar compiled to
   parameterized SQL. Contract: spec-medallion-answering.
2. **Domain packs specialize by declaration.** A named quantity (table, column, fn, unit,
   exclusion filter, provenance note) is registered as data and executed by the platform's one
   executor. Bespoke SQL verbs remain allowed for shapes the grammar can't express; the coverage
   map records which is which.
3. **Sites carry configuration only** (calibrations, channel maps, personas, access tiers) —
   no answering code.
4. **The chat pipeline discovers its tools** from the capability registry; hardcoded dispatch
   tables are non-conformant (spec-chat-prompt-pipeline §2.7). Coverage scopes to the installed
   capability set, making three install modes first-class: chat-core (clean abstention on data
   questions), + data platform (generic answering), + domain packs (curated answering).

## Consequences

- **Positive.** A base install answers data questions the day one table lands in gold; domain
  packs shrink from N hand-written functions to N declarations + few bespoke verbs; the
  generic executor is tested once, centrally (the broken-since-written class disappears);
  install modes degrade to honest abstention instead of breakage.
- **Costs.** The four generic verbs + filter grammar + declaration compiler are new platform
  surface with real security obligations (tier filtering, no SQL injection by construction);
  the serving/extraction work must replace the hardcoded dispatch with discovery; existing
  consumer verbs migrate opportunistically (declaration-backed where expressible), not big-bang.
- **Risks.** A generic verb is a wider query surface than curated verbs — mitigated by the
  restricted grammar, schema validation, tier fail-closed, ADR-114 gating, and read-only
  declaration. Aggregates over semantically dirty columns can mislead — mitigated by the
  provenance envelope (`method`, `rows`) and pack declarations carrying curation where it
  matters.
- **Migration.** Phase 1: the four generic verbs + envelope + tests (platform). Phase 2:
  declaration compiler + pack registration seam. Phase 3: chat extraction consumes discovery
  (retiring the consumer serving shim's dispatch table). Consumer-layer docs track their own
  alignment in their repos.
