# ADR-124: A credential's truth lives at the issuer, not in our metadata

**Status:** Proposed
**Date:** 2026-09-21

## Context

A stored credential went dead and nothing noticed for roughly six months. The
value in the store belonged to a token that had been deleted or regenerated in
the provider's UI. The first symptom was an HTTP 401 during ordinary work, and
the conclusion drawn from that single failure was that the whole host was
unreachable — while seven other credentials for the same host were healthy.

Every guard that should have caught it reads **our own metadata**:

- the expiry audit reads `expires_at`
- the rotation pass acts on what the audit reports
- the health heartbeat summarises the same

Metadata records what we believed when we wrote it down. It cannot notice that
somebody revoked a token in a web UI, and it cannot supply an `expires_at`
nobody ever entered. Six credentials on this install had none, which made them
invisible to all three guards simultaneously and permanently — no later event
supplies a missing field.

## Decision

**Reconcile against the issuer, and treat the issuer as authoritative.**

1. `axi vault reconcile` asks each credential to describe itself at its issuer
   (`GET /personal_access_tokens/self` for GitLab-family tokens) and compares:
   `orphaned` (issuer does not recognise the value), `revoked`,
   `expiry_drift`, `expiry_unknown_locally`, `ok`.
2. `--apply` records the issuer's expiry locally. **Backfilling an expiry the
   issuer already knows is the single change that would have made this visible
   months earlier** — an unrecorded expiry is not unknowable, it is unasked.
3. Reconciliation joins the vault steward, so the unattended pass includes the
   one check the others structurally cannot perform.
4. `axi vault resolve --host <h> --probe` tests **every** credential for a host.
   A host-keyed credential helper returns one of several; one failing is not the
   host failing, and that sentence is now printed rather than remembered.
5. `axi secrets set` warns when no expiry is recorded, naming what goes blind
   and the command that can fetch the date.

Boundaries:

- **Applying writes metadata only.** Recording a fact the issuer just stated is
  not the same authority as changing what a credential is. No value is written,
  nothing is minted or rotated.
- **An orphan is never repaired by applying.** Writing an expiry onto a value
  the issuer rejects would make a dead credential look healthy — the exact
  failure this ends. It needs a human to mint a replacement.
- **`unreachable` is not `rejected`, and `unknown` is not `dead`.** A network
  blip recorded as an orphaned credential would recreate the original error from
  the other direction.
- **The write still succeeds without an expiry.** Some credentials genuinely do
  not expire, and refusing the store would push people to work around it. It
  must not be *silent*, because silence is what made this cost six months.

## Consequences

**Good.** Drift that no local check could see becomes a routine finding. The
issuer also reports real scopes and the real token name, which makes credential
*purpose* authoritative rather than inferred from notes — the ambiguity that
caused the wrong credential to be used in the first place.

**Cost.** Reconciliation makes one network call per supported credential, so it
is opt-in at the CLI and runs on the steward's hourly cadence rather than on
every command. Providers without an issuer API report `unsupported` rather than
drifted; reporting them as findings would make the report noise, and a noisy
report is one nobody reads.

**Accepted limitation.** Twelve of twenty credentials on this install are
`guided` and cannot be reconciled. For those, the expiry audit remains the only
guard, which is why `no_expiry` is now an escalation there (ADR-122 sibling
work) rather than a silent pass.
