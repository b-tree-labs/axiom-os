# ADR-094 — Permission-aware retrieval: reconcile source ACLs with RAG via externalized, source-agnostic tier policy

**Status:** Proposed · **Date:** 2026-07-13
**Owner:** @ben
**Builds on:** ADR-091 (shareable-URL provenance — `url_for` is the self-enforcing-link mechanism this ADR leans on), ADR-074 (Registry Fabric — `SourceKindProvider` is the per-kind seam), ADR-070 (knowledge architecture — memory authoritative, RAG as unified retrieval), ADR-052 (dependency direction — source specifics stay behind the seam)
**Related:** the Retrieval Policy Engine (RPE); the identity/authz work (role→persona + shadow-mode per-role authorization); ingest classification (EC reject-at-ingest)

## Context

RAG flattens the origin system's permissions. Retrieval serves document *content*
decoupled from the access controls that governed that document in Box, Google Drive,
SharePoint, Confluence, or S3. This is the enterprise-search reconciliation problem, and
it fails in two directions:

- **Over-restrict** — lock retrieval down and useful information is kneecapped; worse, no
  one discovers that a relevant document even *exists*, so no one knows to request access.
- **Over-expose** — index everything as world-readable and restricted content leaks into
  answers for users who should never have seen it.

Three observations bound the problem and keep the solution from becoming the tar pit that
a full ACL reconciliation usually is:

1. **The origin web link is self-enforcing.** An `app.box.com/file/{id}` (or Drive, or
   SharePoint) URL resolves against the *source's own* ACL on click — the source opens the
   file for those with access and shows its native *request-access* flow to those without.
   `SourceKindProvider.url_for` (ADR-091) already produces that link for any source. So
   **showing a link never leaks content**, and it hands us a discover→request-access path
   for free, portably.
2. **The link was never the leak — the injected chunk *content* is.** Today the retriever
   serves every `access_tier = 'public'` chunk's *text* into the answer for any
   authenticated chat user. That exposure is in the **retrieval filter**, not the citation,
   and it exists independently of whether a URL is shown.
3. **Per-file ACL mirroring is the wrong baseline.** Capturing every file's source ACL,
   mapping source identities to platform identities, expanding groups, and chasing drift is
   early-binding (always stale) and heavy. Sensitivity is far more stably expressed at the
   **container** (folder / shared-drive / site / prefix) level, which is coarse and
   human-auditable.

## Decision

Reconcile permissions with **two externalized, source-agnostic policy layers** that ride
the existing seam — no per-file ACL mirror, no per-source code.

1. **Container→tier classification (ingest-time), externalized.** An ordered rule set maps
   each item's **normalized container path** (the path every `SourceKindProvider` already
   emits as `FetchedItem.source_path`) to a sensitivity **tier**, first match wins, with a
   conservative default. Authored as external, hot-editable config — re-tiering a folder is
   a config edit, never a code change or re-deploy. It is **source-agnostic** because it
   matches globs over a normalized path string that Box (folder path), Google Drive (folder
   hierarchy / shared drive), SharePoint (site/library/folder), and S3 (key prefix) all
   produce through the same contract.

2. **Identity→tier authorization (query-time), externalized.** A role/principal →
   allowed-tiers grant table (external config) filters retrieval to the tiers the
   authenticated chat identity may see. This extends the shipped role→persona +
   shadow-mode per-role authorization seam — the shadow log is exactly where this filter
   moves from *logging* to *enforcing*. Source-agnostic.

3. **Gated-but-relevant hits surface existence, not content.** When a retrieved hit's tier
   exceeds the identity's grant, return its **title + why-relevant + the `url_for` link**
   (which leads to the source's native request-access) and **never the chunk text**. This
   is the direct answer to "no one knows enough to request access" — the document becomes
   discoverable and requestable without its content being disclosed.

4. **EC / top tier = reject at ingest.** The most-restrictive classification outcome maps
   to *do not index*: such content is never embedded, so even its existence is hidden. This
   preserves the existing export-control policy (reject, not screen-and-keep) — the rules
   engine simply owns that verdict alongside the tiers.

5. **The seam stays source-agnostic.** The tiering and authz engines operate only on (a) the
   normalized container path every provider emits and (b) `url_for`. No source-specific
   branch lives in the policy engine. An **optional** future provider capability may read a
   source's own coarse sharing signal (folder shared-broadly vs. restricted) to *suggest*
   tiers during onboarding — enrichment, not required, and never a substitute for the
   admin-authored rules.

### Config shape (illustrative — source-neutral, external, hot-editable)

```toml
# access-policy.toml  — lives with the corpus/install config, not in code
default_tier = "restricted"          # conservative default; unmatched containers are gated

[[rule]]                             # ordered — first match wins
container = "/Shared/Literature/**"  # glob over the provider-normalized container path
tier = "public"

[[rule]]
container = "/Operations/**"
tier = "restricted"

[[rule]]
container = "/Legal/Privileged/**"
tier = "reject"                      # never indexed — existence hidden

[grants]                             # identity → visible tiers
role.member    = ["public"]
role.staff     = ["public", "internal"]
role.privileged = ["public", "internal", "restricted"]
```

The **same file** works whether the primary repository is Box, Google Workspace, or
Microsoft 365 — only the `container` globs differ, because they are written against the
normalized path the relevant provider emits. Swapping source kinds changes config, not code.

## Consequences

- **Coverage without the kneecap.** Broadly-shared reference material stays fully useful;
  sensitive containers gate *content* while remaining *discoverable* with a request path.
  The two failure modes are addressed by the same mechanism.
- **Auditable.** A human reads a rules file and a grant table and can sign off on them —
  there is no opaque, drifting ACL graph to reason about.
- **Portable by construction.** A Google-Workspace-primary or M365-primary install expresses
  its policy in the identical schema; the platform ships no source-coupled tiering logic.
- **Drift-tolerant.** Tiers are coarse and container-anchored, so a file moving within a tier
  doesn't break; and the authoritative *open* is always deferred to the source's own
  self-enforcing link, so we never have to be perfectly in sync with source ACLs to be safe.
- **Enforcement seam already exists.** `chunks` already carries `access_tier`,
  `classification`, `allowed_nationalities`, `owner`, `team`; retrieval currently ignores all
  but `access_tier`. This ADR turns that latent taxonomy on, keyed to identity.
- **Out of scope for v1 (deliberately):** per-file ACL mirroring, live per-query source
  permission checks, and cross-system identity federation. These are revisited only if a
  concrete requirement provably exceeds coarse container tiers — the point is to *not* build
  them by default.

## Alternatives considered

- **Early-binding per-file ACL mirror.** Rejected as the baseline — drifts the moment a
  source ACL changes, and forces source→platform identity mapping and group expansion.
- **Late-binding live per-query source checks.** Rejected as the baseline — per-result source
  API calls add latency and rate-limit exposure, and require a per-user source token.
- **Hardcoded per-source tier logic.** Rejected — violates the domain-agnostic seam; tiering
  must be config over a normalized path, not code that knows it is talking to Box.
- **Hide gated documents entirely.** Rejected — it recreates the "no one knows to request
  access" gap; existence + a request path is disclosable for all but the reject tier.
