# ADR-080: Autonomous-First Key/Secret Rotation

**Status:** Accepted (2026-06-26) · **Amended by ADR-095** (2026-07-15) — the
"Alternatives considered" rejection of a dedicated rotation subsystem is
overturned for the backend-external (vendor-minted) credential case; every
other decision here (overlap-validity, alert-before-expiry, autonomous-first
HITL, the AWS staging-label mapping) stands.
**Deciders:** Benjamin Booth
**Related:** ADR-055 (Unified Governance Fabric / KEEP), ADR-076 (Personal
Credential Fabric), ADR-077 (Local Principal Authentication), the `secrets`
builtin extension (`SecretStoreProvider` / `SecretStore` Protocol), the Box
rotating-refresh-token pattern (`data_platform` source auth) as prior art.

---

## Context

Operational secrets (database passwords, API tokens, OAuth blobs) expire and
must be rotated. The failure mode we have already lived through (Postrule
shared `DASHBOARD_SERVICE_TOKEN`): rotation was a **manual flip** — change the
secret in one place, then race to update every consumer before the old value
stops working. Miss a consumer and you outage.

The requirement is therefore not "support rotation" but: **rotation must never
be a single manual flip that outages if missed.** A consumer still presenting
the previous secret must keep working until it has demonstrably picked up the
new one.

We already have the building blocks:

- A `SecretStoreProvider` / `SecretStore` Protocol seam (`secrets` builtin)
  with `env`, `openbao`, `kubernetes`, and `gcp` providers, capability
  advertisement, and a `resolve()` cross-extension entrypoint.
- A proven rotation-without-outage pattern: the Box **rotating refresh
  token** — a single-use token that is atomically persisted on each refresh,
  with `invalidate()` on failure, so an in-flight rotation never strands the
  caller. That pattern rotates *one* credential held by *one* consumer; this
  ADR generalizes the "old value stays valid through the handover" property to
  *secrets verified by a server against many consumers*.

What is missing: a stated rotation model, an alerting-before-expiry contract,
a schedule/driver, and a cloud-managed provider (AWS) so teams that already
live in AWS get rotation as a config choice rather than a build.

## Decision

### 1. Overlap-validity is the load-bearing rule

A rotation is a **window, not an instant**. During the window a *set* of
secret values is accepted, not a single one:

```
        rotate                          retire-old
  ───────┼───────────── overlap ───────────┼──────────▶ time
  old only        {new, old} accepted        new only
```

Consumers verify an incoming secret against the *accepted set*
(`resolve_overlap()` → `[current, previous]`), never against a single value.
A consumer that has not yet flipped to the new secret stays inside the
accepted set for the whole overlap window, so a missed flip degrades to "rotate
again / alert", never to an outage. The window closes only when the old label
is retired — an explicit, observable, reversible step.

This maps cleanly onto backends that already model it (see §4) and is
synthesized for those that do not (keep the prior value addressable until a
retire step).

### 2. Provider seam — rotation is a capability, not a fork

Rotation stays behind the existing `SecretStore` Protocol. `Capabilities.rotation`
already exists; a backend that drives its own rotation advertises it and
implements `rotate()` (request a rotation now) plus the overlap read. Backends
that cannot (`env`) advertise `rotation=False` and the registry refuses to wire
rotation onto them. No new top-level abstraction; the seam absorbs it.

`resolve_overlap(ref) -> list[Secret]` is added as an **optional** store method
(present where overlap is meaningful). It is additive — existing single-value
`get()` is unchanged, so no consumer breaks.

### 3. Schedule/driver + expiry alerting (autonomous-first, agentic HITL)

- **Driver.** A scheduled task (PULSE cadence) reconciles each managed secret
  against its rotation policy (max-age / explicit schedule). When due, it calls
  `rotate()` (cloud-managed backends) or runs the generate→stage→verify→retire
  sequence (self-driven backends). This is the autonomous default.
- **Alerting before expiry, not after.** The reconciler emits a warning event
  at a lead time before expiry and an error event once inside the danger
  window. Alerts route to the notifications inbox. Expiry surprises are the
  thing we are eliminating.
- **Agentic HITL for the human steps.** Where a step genuinely needs a human
  (approve a production rotation, paste a vendor-issued key the API cannot
  mint), the agent presents a single confirm with full context and wires the
  result back — the human step is one click, never a runbook. The overlap
  window means even a *delayed* human approval does not outage; it only widens
  the window.

### 4. AWS Secrets Manager provider

Ship `aws` as a `SecretStoreProvider` implementing the existing Protocol. AWS
Secrets Manager already implements overlap-validity natively via **staging
labels**: `AWSCURRENT` (new) and `AWSPREVIOUS` (prior, still valid). Our
overlap window *is* AWS's window — `resolve_overlap()` returns
`[AWSCURRENT, AWSPREVIOUS]`, and `rotate()` delegates to SM's own rotation
(Lambda + schedule) rather than rotating in-process. boto3 is an optional dep
(the existing `[storage]` extra already pins it), lazily imported, with a loud
error if missing — mirroring the `gcp` provider's `[secrets-gcp]` handling.

## Consequences

**Positive**
- A missed flip never outages — the prior secret is honored for the whole
  overlap window. This is the requirement, made structural rather than
  procedural.
- No new abstraction: rotation rides the existing provider seam; consumers opt
  in by reading the accepted set instead of a single value.
- Teams on AWS get managed rotation as a config choice; the overlap semantics
  are AWS's own, so we inherit its correctness rather than re-implementing it.
- Autonomous by default, with HITL reduced to a single contextual confirm.

**Negative / trade-offs**
- `SecretRef.version` is an `int`; AWS VersionIds are UUIDs. The AWS provider
  threads VersionId/staging-label selection through the `?version=` / `?stage=`
  *query* and leaves the int `version` field unused. The Protocol's
  int-versioned read contract does not fit cloud backends with opaque version
  ids — flagged for a future `version: str | int` widening if a second such
  backend appears.
- Overlap windows that never retire the old label silently weaken the rotation
  (two live secrets indefinitely). The reconciler must own retirement, and
  alert if a window stays open past policy.
- The schedule/driver and reconciler are specified here but land incrementally;
  this ADR ships the overlap primitive (`resolve_overlap`) and the AWS provider;
  the PULSE-driven driver + alerting follow.

## Alternatives considered

- **Single-value atomic flip with fast consumer push.** Rejected: this is
  exactly the failure mode (race the consumers; miss one; outage).
- **A dedicated rotation subsystem above the secrets seam.** Rejected as
  premature abstraction — rotation is a property of a backend's value set, and
  the `Capabilities.rotation` flag + an optional `resolve_overlap()` express it
  without a new layer.
- **In-process rotation for AWS.** Rejected: SM's Lambda-driven rotation is the
  supported path and already produces the staging-label overlap; re-implementing
  it in-process would duplicate and diverge from AWS's model.
