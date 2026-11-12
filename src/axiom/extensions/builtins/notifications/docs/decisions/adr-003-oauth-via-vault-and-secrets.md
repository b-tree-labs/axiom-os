# ADR-003 — OAuth: HERALD owns the dance, KEEP holds the tokens, capabilities wrap the calls

**Status:** Accepted (2026-05-31)
**Scope:** `axiom.extensions.builtins.notifications` (HERALD)
**Locks:** spec-axiom-notifications §5

## Context

Channel adapters (Slack, Teams, future) need OAuth tokens to call vendor APIs.
The four candidate owners of the OAuth lifecycle:

- **Adapter** holds tokens directly (status quo for SMTP module; rejected for
  any new channel).
- **HERALD** owns everything end-to-end (storage + refresh + outbound call).
- **KEEP** owns everything end-to-end (it doesn't know the channel ontology).
- **Split** — HERALD owns the dance, KEEP holds + refreshes + presents
  capabilities.

## Decision

**Split.** HERALD owns the OAuth flow (it knows which channel needs which
flow). The vault (KEEP, backed by the SEC-1 secrets extension) stores the
tokens at rest and refreshes them on capability use. Adapter code never
sees a raw OAuth token — every outbound call carries a `CapabilityToken`
that KEEP's `outbound_call()` dereferences at exactly one site (fabric §5.3).

## Consequences

- The static-analysis invariant "plaintext credentials only exist inside
  `vault.outbound_call`" stays intact.
- A compromised channel adapter can post within the capability's scope
  (intent + resource pattern + expiry) and nothing more. The capability
  is bound to `notification.send` + the specific channel resource.
- `axi notifications channels authorize <channel>` runs the OAuth dance,
  hands the refresh token to KEEP, and registers a `secret_ref` row.
- HERALD ships a webhook router (`/herald/webhook/<adapter>`) for the
  authorization-code callback; this is the same router used for inbound
  reply threading (spec §3).
- Federation: peer cohorts present capability tokens issued by their KEEP;
  our HERALD verifies via the trust graph + cohort policy (spec §6).

## Alternatives considered

- **Adapter holds tokens** — rejected. Breaks the single-credential-site
  invariant; compromised adapter code can exfiltrate refresh tokens.
- **HERALD owns end-to-end** — rejected. HERALD becomes a second plaintext-
  credential site; static analysis becomes harder.
- **KEEP owns end-to-end** — rejected. KEEP would need to know every
  channel's OAuth shape; that's the wrong layer.

## References

- spec-axiom-notifications §5
- spec-governance-fabric §2 (capability tokens), §5.3 (outbound_call)
- `src/axiom/extensions/builtins/secrets/` (SEC-1 secret backend provider)
- `src/axiom/extensions/builtins/vault/capability_store.py`
