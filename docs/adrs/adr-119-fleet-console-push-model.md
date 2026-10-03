# ADR-119: Fleet Console — Push-Model Aggregation over Sovereign Nodes

**Status:** Accepted (2026-09-21)

## Context

Operating even one node today means hand-checking canary outcomes, backup
validations, service health, and deployed versions across surfaces that do
not aggregate: `axi status` is single-node, `axi nodes status` pulls over
SSH, `HeartbeatDaemon` answers die with the process, and canary
attestations stop at a local file sink. The company plan's cloud section
names the fix: a fleet console (item 1) and release channel management
(item 2), dogfood-first.

Three doctrines already bind any design:

- **Edges push out; nothing reaches in** (ADR-106, the ingest sink's
  documented contract). A console that polls nodes is disqualified.
- **Reports must match reality** (`docs/working/goal-reports-match-reality.md`):
  a green indicator that cannot fail, or that renders a claim rather than
  an observed effect, is worse than no indicator.
- **The platform says site/tenant, never a domain term** (ADR-050); a
  console is a tenant's view of its own nodes, not a global directory
  (site topology stays P2P by default — the console is opt-in).

Much of the machinery exists unwired: a signed report envelope with
quarantine-on-ingest (`findings/finding.py`, `federation/digest.py`,
`federation/receive.py`) that has no HTTP front door; a complete canary
attestation model with only a local gossip sink (`federation/canary.py`);
backup skills that already emit evidence-shaped results; per-tenant
scoping halves on both the serving (`infra/site_scope.py`) and ingest
(`ingest_sink/tenancy.py`) sides; and the `kind = "api"` contribution
mechanism.

## Decision

1. **A new purpose-named extension `fleet` owns the console.** Federation
   remains the peer-to-peer protocol layer; the console is a serving-side
   aggregation concern with its own store, surfaces, and tenancy. The CLI
   noun is `fleet` (`axi nodes` remains federation's peer-list view; its
   SSH pull becomes a diagnostic fallback, not the model).
2. **Ingestion is push-only.** Nodes push signed reports to one
   authenticated front door, `POST /api/v1/fleet/reports`, contributed via
   the standard api-kind mechanism. The console never opens a connection
   toward a node. Report kinds v1: `heartbeat`, `service_health`,
   `backup`, `backup_validate`, `canary`, `versions`.
3. **The envelope is the existing signed Finding/Digest**, with the
   `node-attestation` signature role. Phase 1 authenticates the transport
   with a per-node bearer credential whose principal context binds the
   site — **site is derived from the credential, never the payload**
   (the ingest-sink tenancy rule). Phase 2 adds Ed25519 envelope
   verification via the existing `verify_digest`, closing the class of
   dev-stub signature checks.
4. **Effect-checked rendering is an invariant, not a style.** A row
   renders green only when it cites an observed-effect field (a dump's
   `size_bytes > 0` and `restore_live == PASS`; a job's `finished_at`;
   contact-derived `last_seen`). A claim without evidence renders as
   UNPROVEN (distinct from FAILED — the backup-validate WARN/FAIL split
   generalized). Silence is failure: a node or report kind stale past 3×
   its declared cadence renders stale, never last-known-green. Every
   effect check ships with a test proving it can fail.
5. **Storage is `session_for("fleet")`** (ADR-052), append-only report
   log plus a latest-per-(node, kind) projection. The console stores
   pushed copies; it does not re-own producers' records (`ScheduleFireLog`
   stays the job-outcome record of truth on the node).
6. **Release channels are modeled centrally as declared pins** —
   `(scope, package, channel, declared_version, source)` — reconciled
   against node-reported deployed versions from `versions` reports. Drift
   is a computed view, not a stored flag. The existing
   `VersionDirective` store feeds declared state; nodes' deployed state
   arrives only by push.

## Consequences

- The console works across NAT/egress-only sites by construction, and a
  compromised console cannot reach into any node — it holds no inbound
  credentials.
- Freshness depends on nodes pushing; the staleness rule (silence is
  failure) is what makes that honest. A node that opts out simply never
  appears — opt-in is per node, recorded as its push configuration.
- Two liveness meanings coexist: federation's `last_seen` (peer contact)
  and the console's report freshness. They must never be merged into one
  field; the console displays its own and may display federation's,
  labeled.
- Phase 1's bearer-only authentication accepts spoofed *content* from a
  node whose credential leaks; envelope signatures (phase 2) bound that
  blast radius. Accepted for the dogfood tenancy, tracked as a named gap.
- A second job-outcome table is deliberately not created; if the console
  ever needs full fire history, it ingests pushed `ScheduleFireLog`
  excerpts as reports.

## References

PRD: `docs/prds/prd-fleet-console.md`. Spec: `docs/specs/spec-fleet-console.md`.
Doctrine: ADR-106 (push ingest), ADR-050 (site/tenant vocabulary), ADR-052
(schema-per-extension), ADR-037 (signed state propagation, Proposed),
`docs/prds/prd-canary-nodes.md` (silence-is-failure, push-never-pull),
`docs/working/goal-reports-match-reality.md` (effect-checked reporting).
