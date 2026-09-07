# KEEP — skill surface

**Status:** living document · **Owner:** Benjamin Booth
**Related:** ADR-055 (KEEP as Steward + Governor), ADR-056 (CLI verbs are thin
wrappers over skill functions), `secrets/docs/decisions/adr-001-secrets-vs-keep.md`
(the boundary), ADR-076 (personal credential fabric)

---

## The boundary, first

ADR-001 (secrets) splits two things that look alike and must not merge:

| | `secrets` extension | `vault` extension = **KEEP** |
|---|---|---|
| Purpose | operational credentials + rotation | governance capability tokens |
| API | `get / put / delete / lease / rotate` | `mint / present / revoke / audit` |

Every skill below observes one rule: **KEEP owns policy; `secrets` owns
mechanism.** KEEP asserts that credentials are managed, that rotations took
effect, that posture never silently weakened. It does not scan filesystems or
talk to vendor APIs — it asks `secrets` to, and decides what the answer means.

Folding mechanism into KEEP would put operational-credential handling inside the
capability-token primitive, which is the conflation ADR-001 exists to prevent.

## Why these skills exist

Each one is here because something failed on 2026-08-18/19, not because it
seemed prudent. The pattern across all of them: **the managed system was
healthy, and the thing outside it was not.**

---

## Implemented

### `vault.sweep` — is anything unmanaged?

Calls `secrets.discover` and escalates. Unmanaged material makes the sweep
**fail**, so a heartbeat that finds a loose credential is visibly not-ok rather
than a line in a log.

> **The incident.** Six vendor tokens lived in git remote URLs on a deployed node — one of
> them group-scoped with Owner role and full API scope. `axi secrets
> audit` reported a clean bill the entire time, correctly: the store had never
> heard of them, so no cadence applied and no expiry audit saw them.

Runs on KEEP's heartbeat (hourly). Unmanaged credential material is a standing
condition rather than an event, so the cadence is set to notice within a working
day without re-paging the same finding every few minutes.

---

## Proposed

Ordered by how much each would have saved on the night that produced them.

### `vault.adopt` — bring discovered material under management

One operation for what is currently four manual steps: store the value, remove
the literal from where it was found, rewire the consumer through the credential
helper, and arm rotation.

> **The incident.** Adopting a single token by hand meant rotating it, vaulting
> it, cleaning the remote URL, writing a credential file, and verifying fetch —
> five commands across two machines, any of which could be half-done. A
> half-adopted credential is worse than an unadopted one: it looks managed.

Generalised deliberately: the *consumer rewiring* step is provider-specific
(git credential helper, env file, unit drop-in), so adoption dispatches on the
probe that found it. The probe registry already knows the location kind.

### `vault.rotation.armed` — does every managed credential have a lifecycle?

Assert that each stored credential has a rotation provider and a cadence.
Report the ones that do not.

> **The incident.** One stored credential was armed for autonomous 30-day
> rotation. A group-scoped Owner token for the same host was not armed at all. Nothing compared them, so
> the difference was invisible until someone looked by hand.

### `vault.posture.no_regression` — did a lifecycle operation weaken anything?

After any rotation, compare before and after across **every** posture axis and
refuse a silent downgrade: expiry shortened, scope broadened, role escalated,
assurance lowered.

> **The incident.** One vendor's rotate endpoint defaults to a **7-day** expiry. A
> token expiring 2027-01-14 was rotated and came back expiring in a week — a
> five-month downgrade, returned silently in a success response, which would
> have broken a deployed node's clone the following week.

This is the most generalised of the set on purpose. The specific bug was expiry,
but the rule is *a lifecycle operation must not change posture without saying
so* — and scope-broadening on rotation is the same failure in the dangerous
direction.

### `vault.revocation.proof` — did the revocation actually take?

After rotating, prove the prior value no longer authenticates, via a
provider-supplied probe. A rotation that mints a new credential without
invalidating the old one looks identical to one that did.

> **The incident.** The old value was confirmed dead only because a `git fetch`
> was observed to break. Nothing automated that check, so "rotated" and
> "rotated and revoked" were indistinguishable in the record.

### `vault.consumer.map` — who uses this credential?

Given a managed credential, list every location that references it — so a
rotation knows what it is about to break.

Not a new mechanism: it is `secrets.discover` indexed the other way. The scan
that answers "what is unmanaged?" (fingerprints absent from the store) answers
"who consumes X?" (fingerprints equal to X's) from the same evidence.

> **The incident.** An Owner-scoped group token was rotated without knowing
> every consumer. It happened to be one clone. That was luck, not method.

---

## Design rules these share

**Findings carry fingerprints, never values.** A truncated digest is enough to
recognise the same credential in two places and match it against the store, and
useless to anyone who obtains it — so a KEEP report is safe to paste into a
ticket.

**Verification is a first-class operation.** Three of the five proposed skills
(`rotation.armed`, `posture.no_regression`, `revocation.proof`) do nothing but
check that a previous operation actually took effect. That is the same gap as a
deploy path that fetched happily while the node ran five-week-old code: the
absence of comparison, not the absence of capability.

**Escalate, do not merely report.** A sweep that returns 0 on a finding trains
people to ignore it.
