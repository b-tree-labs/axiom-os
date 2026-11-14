# ADR-001 (secrets) — SecretStore is distinct from KEEP / vault primitive

**Status:** Accepted · **Date:** 2026-05-31 · **Owner:** Benjamin Booth

## Context

The `vault` extension shipped the KEEP primitive on 2026-05-29 (ADR-055
Cut 2): capability-token issuance + presentation + revocation, sitting
under the governance fabric. KEEP is the *only plaintext-credential
site* for outbound calls — the governance design says capabilities (not
raw secrets) cross the wire to consumers.

The DP-1 deploy runbook (`docs/runbooks/dp1-node-deploy.md`) still
carries plaintext-credential surface in several places:

- `axi data install --db-password "$DB_PASSWORD"`
- `~/.axi/credentials/box/state.json` (Box SSO session blob)
- `$DP1_RAG_DSN` env var holds a full Postgres DSN including the password
- Future per-connector credentials

These are *operational secrets* — the platform itself needs them at
install/boot time to wire up databases, queues, and outbound clients.
They are not capability tokens; they are passwords + sessions + DSN
fragments.

Naming the same thing "vault" for both invites the next contributor to
fold them, which breaks the governance separation. We split.

## Decision

Two extensions, two APIs, possibly one backend:

| | **`secrets` extension** (this) | **`vault` extension** (KEEP) |
|---|---|---|
| Purpose | Operational creds + key rotation | Governance capability tokens |
| Lifecycle | Long-lived, rotated | Issued, scoped, expired by policy |
| API | `get / put / delete / lease / rotate` | `mint / present / revoke / audit` |
| Consumer | `data_platform`, `rag`, `connect`, install commands | KEEP outbound, action envelopes (ADR-055) |
| Backend | OpenBao default (factory/provider registry) | OpenBao **may** be the backend, but the API is distinct |

The two are allowed to share OpenBao as a transport — that is operational
deduplication, not API conflation. A consumer reaching for an
operational secret calls `secrets.resolve(SecretRef, ctx)`. A consumer
needing to make a capability-bounded outbound call goes through KEEP.
No consumer goes through both for the same action.

## Consequences

- `secrets/` ships its own SecretStoreProvider registry. Concrete
  providers: `openbao`, `env`, `kubernetes` initially.
- KEEP (vault extension) is **unchanged**. ADR-055 stays the
  governance-fabric reference.
- The `vault` extension's own credential storage (whatever backs
  KEEP-issued tokens) may later be re-pointed at the SecretStore, but
  that is a follow-up — not a SEC-1 dependency.
- A "secrets-vs-vault" lint or doctor check is a candidate for a later
  PR: any code that imports `vault.capability_store` and `secrets`
  in the same module gets flagged for human review.

## Alternatives considered

**Fold operational secrets into KEEP.** Rejected. KEEP's API is
capability-shaped (mint/present/revoke), not credential-shaped
(get/put/rotate). Forcing a Postgres password through `mint` requires
inventing a degenerate capability and loses the audit-stream + lease
semantics that make a real secret store useful at HPC-cluster scale.

**Vendor a SecretStore inside `data_platform`.** Rejected. The same
problem appears in `rag` (embedding-provider tokens), `connect`
(per-source credentials), and future federation outbound — duplicating
the abstraction three times is worse than a built-in extension owning it.
