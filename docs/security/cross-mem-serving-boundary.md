# cross-mem serving boundary — security contract & threat model

**Status:** Draft (ships with ADR-087 implementation; review gate from P3)
**Owner:** Ben Booth
**Decisions:** ADR-087 D7 (serving boundary), D8 (adapters/write-back), D9 (export)
**Audience:** security reviewers approving any new consumer of a user's memory
store, and implementers of the serving layer.

## 1. The core hazard: policy flattening

Inside Axiom a fragment carries enforceable structure: `visibility`,
`classification`, cognitive type (`vault` = programmatic secrets), ownership
and rights (ADR-026), and origin coordinate (`SourceOrigin`, ADR-087 D1). The
moment memory is served to a harness it becomes **plain text in a prompt**.
The receiving harness cannot enforce Axiom policy (it has no vocabulary for
it), prompt text is maximally leaky (transcripts on disk, vendor cloud sync,
telemetry, model echo), and — the amplifier — **the harness's own auto-memory
extractor re-memorizes injected text**. One over-served fragment can be
paraphrased into a vendor-owned store Axiom can never reach, and re-surfaced
indefinitely. A serving leak is irreversible *and self-replicating*.

Failure-mode asymmetry drives the posture: a false negative costs one query
some recall; a false positive is permanent exfiltration. Therefore:
**fail closed. Doubt → deny.**

## 2. The boundary contract

- **One door out.** All serving passes a single gate in the Axiom serving
  layer — after retrieval, before text serialization. Never implemented in a
  per-harness adapter; never delegated to the caller. (Symmetric with the
  write invariant: one door in via `CompositionService`.)
- **Doubt → deny**, enumerated. Deny when:
  1. a fragment's `visibility`/`classification` label is missing or unknown
     (unlabeled ≠ public);
  2. the policy engine errors or is unreachable (a raising policy source is
     *unavailable*, never *healthy*);
  3. the consumer's identity/entitlement cannot be resolved;
  4. the fragment is `vault` — **unconditional**: no configuration, no
     trusted-consumer exemption. A credential in prompt text is already an
     incident.
- **Consumer coordinate.** Entitlement is evaluated against
  `(harness, account, model-endpoint/deployment-tier)`:
  - *Cross-account:* fragments with a given origin/ownership account serve
    only to consumers authenticated to a compatible coordinate. Work and
    personal memory never blend in either direction.
  - *Deployment tier:* a locally hosted model and a remote third-party API are
    different exposure domains. Content restricted to a local/controlled tier
    never rides a prompt to a remote endpoint.
- **Storage-domain principle.** Serve only what is acceptable to persist in
  the consumer's **least-controlled store** — its transcripts, its auto-memory
  extractor, its cloud sync — not merely the immediate prompt.
- **Minimum-necessary serving.** Tight top-k, tier pre-filtered at retrieval
  (the `rag-memory` chunk contract carries `visibility`/`classification` for
  exactly this), sized to the destination context.

## 3. Co-residency threat model (user has their own RAG / other sources)

| Threat | Vector | Mitigations |
|---|---|---|
| Prompt-injection exfiltration | A poisoned document from a co-resident corpus instructs the model to echo memory context | Minimum-necessary serving; `vault` categorically absent (nothing catastrophic is echo-able); labeled provenance blocks so leaks are attributable in audit |
| Transcript-indexing echo | The user's RAG indexes chat history; injected memory text lands in their vector store | Storage-domain principle (§2) — the gate already assumed this copy would exist; cooperative exclusion marker on injected blocks (helpful, never load-bearing) |
| Extractor paraphrase loop | Harness auto-memory re-learns injected text; absorb re-imports our own words | `SourceOrigin` idempotency key + vector near-duplicate tier catch paraphrase; session injection ledger tags what was served |
| Foreign-store push | "Just push fragments into my vector DB" | **No-push rule:** never. Pushed content escapes per-request policy evaluation, survives entitlement changes, and is unreachable by `forget()`. Serving is query-time only; the sole sanctioned exit is the explicit export ceremony (ADR-087 D9) |

## 4. Absorb-side rules (inbound security)

- Adapters are **read-only** against harness-native stores; app-owned
  databases are never written (round-trip through the authored file layer).
- Absorbed content is untrusted input: it is data in the ledger, never
  instructions to the platform.
- Secrets discovered during absorption are routed to `vault` (and thereby
  become unservable), not stored as plain fragments.
- Write-back (sync) touches only authored instruction files, only at session
  boundaries — a mid-session rewrite is both a cache regression and an
  integrity surprise for the running session.

## 5. Export & migration security

- Bundles are signed; the manifest carries content hashes and schema versions.
- Re-homing requires the ADR-026 dual-signature ceremony (outgoing consent +
  incoming acceptance) and re-signing under the destination node key.
- `vault` content is excluded from bundles unless explicitly opted in, and
  then only re-encrypted for the destination — never in plaintext, never by
  default, never silently.
- Import is idempotent (exact-tier dedup), so a replayed bundle cannot
  duplicate or overwrite state.

## 6. Reviewer checklist (enable a new consumer coordinate)

1. Consumer coordinate fully resolved — harness, account, deployment tier?
2. Which corpora/types can this coordinate ever receive? (`vault`: never;
   verify the conformance suite runs against this transport.)
3. Where does served text persist in the consumer's domain (transcripts,
   auto-memory, cloud)? Is every fragment class served acceptable there?
4. Cross-account predicates configured for this user's account set?
5. Gate conformance suite green: vault-never, unlabeled-deny, error-deny,
   cross-account-deny, deployment-tier-deny.
6. Audit: served-fragment logging enabled with provenance labels?

## 7. Residual risks (accepted, disclosed)

- The model itself may echo served text into its reply; the gate bounds *what*
  can be echoed, not *whether*.
- Cooperative markers (transcript-exclusion hints) depend on consumer
  behavior; they are hygiene, never a control.
- Absorbed sources may themselves contain data the user did not expect to
  centralize; per-source extraction and `forget()` remain the remedy.

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
