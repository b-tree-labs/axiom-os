# ADR-166: Program membership is a principal; feeders attribute to it

**Status:** Accepted (2026-10-06)
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (the composed CLERK), [ADR-162](adr-162-the-watcher-primitive.md) (the watcher primitive the feeders instantiate), [ADR-165](adr-165-program-self-update-and-per-consumer-change-detection.md) (the self-update core the feeders write into), [ADR-020](adr-020-federation-identity-and-relationships.md) (`@name:context`), `docs/prds/prd-program.md` (R4, R9, R13), `docs/specs/spec-program.md`

## Context

The capture feeders (ADR-162 instances for GitLab/GitHub) ingest tracker
activity and reconcile it into the program data file (ADR-165). That forces a
question ADR-161/162/165 left open: **who does a tracker action belong to?**

A tracker knows seats — a GitLab username, a GitHub login. The program knows
principals (`@name:context`, ADR-020). The two are not the same, and the gap is
load-bearing: a contributor may have no account on the tracker the program uses
(a student, a partner, an agent principal), yet they own real work. If
membership were a tracker seat, that person would be invisible — the exact
"silent gap" prd-program R11/Goals forbids. The naïve alternative, assigning
their items to whoever *does* have a seat, silently rewrites ownership.

The decision has to stand before the feeders are trusted, because the feeders'
whole job is to attribute external activity, and an attribution model chosen
per-feeder would diverge immediately.

## Decision

**Program membership is a principal, carried in `people[]`. A person may
declare an optional `accounts` map** — `{gitlab: <user>|null, github:
<user>|null, …}` — whose keys are the deployment's account systems (not a
closed set) and whose values are a username or an explicit `null` ("no account
on that system"). The map is validated for shape and carried losslessly; it is
how a feeder crosses from a seat to a principal, in both directions.

**Feeders attribute, they do not re-own.** A tracker action by `npl436` is
attributed to the principal whose `accounts.gitlab == "npl436"` (reverse
lookup). A schedule item's **real owner stays the principal named in the data
file** and stays authoritative — the feeder overlays live tracker facts into a
namespaced `tracker` block and records attributed activity, but never
overwrites `owner`, the committed dates, or `status`.

**The proxy-assignee rule.** For a tracker-bound item whose real owner has no
account on the system, the *intended* assignee — what a later posting phase
would set on the tracker — is:

1. the **lane lead** (`lanes[].lead`) when the lead has an account, else
2. the **deputy** (`program.deputy`) when the deputy has one, else
3. **none** (no one in the chain can be assigned).

The real owner is named in every case; the proxy is only who the tracker item
would point at. It is **computed and recorded** on the item (`assignment`),
never posted to the tracker in this phase.

**A missing account is an onboarding finding, not a silent gap.** A
tracker-bound item whose owner has no account produces one `account_missing`
finding per owner, carried on the drift/`changes` surface (tracked
`pending`), beside the capture-half mirror/orphan findings. People with no
tracker account read their status through the node surfaces (ADR-165) — the
feeders never gate membership on a tracker account.

## Options considered

- **Membership is a tracker seat.** Simple, and wrong: anyone without a seat
  is invisible, and the program's own people list stops being the source of
  truth. Rejected against R11/Goals.
- **Attribute by seat and reassign ownership to a seat-holder.** Silently
  rewrites who owns the work — the failure mode the wrong-owner bug
  (ADR-041/042 lineage) taught us to refuse. Rejected.
- **Require every member to hold an account before they can own work.**
  Blocks the partner/student/agent case the platform exists to serve, and
  makes onboarding a precondition rather than a tracked task. Rejected in
  favour of the proxy rule plus an `account_missing` finding.
- **Membership is a principal with an account map (chosen).** Membership and
  attribution are separate concerns joined by a small, explicit, reversible
  map; the proxy rule keeps the tracker assignable without ever losing the
  real owner.

## Consequences

- The data-file schema gains `people[].accounts` (validated, lossless) and
  reads `lanes[].lead` / `program.deputy` for the proxy chain — all deployment
  data, no vendor or domain vocabulary.
- Attribution is bidirectional and total: every seat resolves to a principal
  or to "unknown account" (never a guess), and every principal's account (or
  its absence) is explicit.
- The proxy assignee is a recorded intent, so the later posting phase has a
  computed target to apply and an audit of why — without this phase ever
  writing to a tracker.
- `account_missing` joins the capture-half drift findings, so onboarding gaps
  surface continuously on the same `drift`/`changes` surface as everything
  else, rather than as a one-off audit.
- The model is feeder-agnostic: a future wiki or transcript feeder attributes
  through the same map, and a new account system is a new key, not a new rule.
