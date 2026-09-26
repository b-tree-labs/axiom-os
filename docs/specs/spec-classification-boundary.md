# Classification Boundary: Unclassified / Classified / Export-Controlled Handling

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-04-15
**Scope:** Axiom framework and its domain consumers; applies wherever federation touches government, national-lab, or defense-adjacent deployments.
**Related:** ADR-020 (identity layers), ADR-022 (root identity + membership), ADR-023 (topology — pending), ADR-025 (threat model — pending), `spec-security.md §2 Trust Model`, `spec-rag-architecture.md` (three-tier content model).

---

## 1. Why This Spec Exists

A domain consumer (e.g. a nuclear-engineering consumer) may target workflows where **export
control** (ITAR, EAR, NRC 10 CFR Part 810) is a day-1 constraint
and **classified handling** (SECRET, TOP SECRET, SCI) is a
credible near-horizon requirement for government partners and
national labs. Making classification handling an afterthought
invalidates the platform for DoE labs, DoD collaborators, and
regulated industry.

This spec enumerates the scenarios the design must handle and
states the invariants every implementation must preserve. It does
NOT attempt to certify Axiom for any particular classification
regime — formal accreditation is a separate, per-deployment
process. It DOES preclude design choices that would block
accreditation.

**Three distinct regimes covered:**

- **Classification** (SECRET / TOP SECRET / SCI compartments).
  US government hierarchy with compartmentalized access based on
  individual clearance plus need-to-know.
- **Export control** (ITAR, EAR, 10 CFR Part 810).
  Nationality-based access restrictions on specific technologies;
  independent of classification and can apply to unclassified
  content.
- **Restricted / proprietary** (corporate confidential,
  institutional IP).
  Contract-governed access; applies within industry consortiums
  and institutional boundaries.

These regimes overlap in some cases (classified content is
usually export-controlled) but are independently enforced. A
piece of content may be unclassified-but-EAR-controlled;
EAR-controlled-but-ITAR-free; classified-SECRET-but-not-SCI.
Access decisions check all applicable regimes.

---

## 2. Core Model Extensions

The existing three-tier content model (public / restricted /
export_controlled from `spec-rag-architecture.md`) is the
unclassified baseline. This spec extends it.

### 2.1 Content Classification (property of content)

Every content object carries a **classification stamp** with
components:

```
classification:
  level: "unclassified" | "cui" | "secret" | "top_secret"
  compartments: ["NOFORN", "SI", "TK", ...]         # SCI markings
  export_control:
    itar: bool                                       # ITAR-controlled
    ear:                                              # EAR categories
      categories: ["0E982", ...]
      authorized_nationalities: ["US", ...]          # null = no nat'l restriction
    part_810:                                         # NRC 810
      applicable: bool
      specific_authorization: Optional[str]
  proprietary:
    restricted: bool
    license: Optional[str]                           # contract ref
  original_classifier: "@officer:agency"
  classification_date: ISO-8601
  declassification_date: Optional[ISO-8601]
```

Unclassified public content has all fields at their permissive
default. The stamp is a cryptographic attestation produced by an
**original classifier** (a principal with authority to classify);
it travels with the content through every system that handles it.

### 2.2 Principal Clearance (property of principal)

Every principal may hold **clearance attestations** separate from
their identity root and affiliation:

```
clearance:
  subject: "@person:agency"
  level: "unclassified" | "cui" | "secret" | "top_secret"
  compartments: ["NOFORN", ...]
  nationality: "US"                                  # for export control
  granted_by: "@security-officer:agency"
  granted_at: ISO-8601
  expires_at: ISO-8601                               # periodic re-adjudication
  revoked_at: Optional[ISO-8601]
  need_to_know_scopes: list[scope_id]                # specific projects/tasks
```

Clearance is NEVER part of identity. It is a signed attestation
from a clearance-granting authority, with a short TTL (≤ 5 years
typical, shorter for some compartments), and must be re-verified
periodically. **Identity is permanent; clearance is not.**

Non-US nationality is carried explicitly for export-control
checks, independent of classified clearance.

### 2.3 Federation Domain Classification

Every federation declares a **domain classification** — the
maximum level of content it is authorized to handle:

```
federation:
  id: "ut-ne-research-spring-2026"
  domain: "unclassified"    # or "cui", "secret", "top_secret"
  compartments_allowed: []
  export_regime:
    itar_permitted: false
    ear_categories: []
    foreign_nationals: "permitted"   # or "restricted", "prohibited"
    part_810_technologies: []
  cross_domain_transfer_policy: "none"  # or "up_only", "with_review"
```

**A federation cannot handle content whose classification exceeds
its domain.** This is the primary deterministic gate: a
Secret-stamped document cannot enter an unclassified federation,
ever, regardless of who requests it.

---

## 3. Scenarios

Each scenario enumerates what must work, what must NOT happen,
and the deterministic invariants enforced.

### S1. Single-domain unclassified federation

Most classrooms and research collaborations. All content is
unclassified and EAR-unrestricted (or the federation is flagged
as export-restricted and foreign nationals are explicitly
controlled). Standard operation.

**Invariants:**
- No content with `classification.level != "unclassified"` may
  enter this federation.
- If the federation's export regime restricts foreign nationals,
  principals with non-US nationality are gated per EAR/ITAR
  rules.

### S2. Single-domain classified federation

DoE or DoD-adjacent collaboration. All content and all
participants cleared to the federation's domain level.

**Invariants:**
- Every principal must present a valid, unexpired clearance
  attestation at or above the federation's domain level, plus
  every required compartment.
- Every content object within the federation carries a
  classification stamp ≤ federation's domain.
- Any model-mediated operation (LLM call) must use a model
  certified for that classification (e.g. on-premise LLM,
  never a cloud API call).
- Network path must be classification-appropriate (SIPRNet for
  SECRET, JWICS for TS/SCI).

### S3. Mixed-domain node (laptop with unclass + classified contexts)

A user has an unclassified workspace on their personal laptop
AND, separately, a classified workspace on a certified system.
The identity model (ADR-020 contexts) already handles this: each
context has isolated state, its own corpus, its own keys.

**Invariants:**
- Context switching is an explicit, deterministic user action
  (`axi context switch <name>`). NEVER inferred.
- No cross-context corpus access. The classified context does
  not see the unclassified context's content, and vice versa.
- No cross-context agent state leakage. Agents running in the
  unclassified context have no access to memory, cache, or
  signal streams from the classified context.
- Separate federation memberships per context — an identity's
  membership in an unclass federation does NOT imply membership
  in any classified federation.

### S4. Cross-domain transfer (unclass → classified "up-transfer")

A researcher identifies a published unclassified paper relevant
to classified work and wants to bring it into a classified
workspace. Generally permitted but requires explicit action.

**Flow:**
1. User invokes `axi content import --from unclass --to secret
   --source <path>`.
2. The destination classified system verifies the import request
   against a local allowlist (importing from the open internet
   vs from a curated unclass corpus vs from an authorized peer
   may have different policies).
3. On import, the content's classification stamp is updated to
   reflect the NEW environment's floor; original provenance is
   retained in audit.
4. Transfer is logged to the classified system's audit chain.

**Invariants:**
- Up-transfer never uses a network path going from higher to
  lower security. The flow is PUSH from unclass side (write to
  transfer media or one-way diode), PULL from classified side.
- Content classification stamp is **NEW on the classified side**
  (original stamp preserved as provenance metadata, not as
  authority).

### S5. Cross-domain transfer (classified → unclass "down-transfer")

A formally declassified document (FOIA release, scheduled
declassification, specific authority action) needs to move down.
This is ALWAYS a review gate with multiple approvals.

**Flow:**
1. Declassification officer signs a declassification attestation
   referencing the document ID and the authority under which it
   declassifies.
2. Content's classification stamp is rewritten to unclassified
   (or CUI, etc.) with `declassified_by` + `declassified_at`
   provenance.
3. Down-transfer across systems uses the same air-gapped
   physical-transfer pattern as up-transfer, in reverse.
4. Destination unclass system re-ingests as unclassified content
   with declassification provenance attached.

**Invariants:**
- No automatic declassification. Every down-transfer is human-
  authorized by a clearance-appropriate officer.
- Agents NEVER initiate a down-transfer. An agent may IDENTIFY a
  candidate (model-mediated classification suggestion) but the
  deterministic gate is the officer's signed attestation.
- The originating classified system's audit log records the
  full declassification action.

### S6. Export-controlled content in an unclass federation

Content is unclassified but EAR-controlled (e.g. specific
technology transfer rules apply). Federation may permit it with
nationality-based gating.

**Invariants:**
- Content's EAR attestation enumerates authorized nationalities.
- Federation membership for principals with non-authorized
  nationality does NOT grant content access — the content gate
  checks nationality against the stamp regardless of membership.
- An authorization decision is logged per access with both
  identity and nationality recorded.

### S7. Foreign-national access

A researcher with non-US nationality participates in a federation
that includes some export-controlled content. They can access
non-EAR content freely; they cannot access EAR-restricted content
without specific license; they may or may not be allowed to see
metadata ABOUT restricted content depending on the regime.

**Invariants:**
- Nationality is a signed identity attestation (part of the
  identity root's claims), verified by an authority (for example,
  an institutional HR office or issuing government).
- Every export-gated access checks nationality against the
  content's authorization list deterministically. LLM judgment
  never overrides this.

### S8. Air-gapped classified enclaves

A classified federation has no internet reachability. Install,
update, and federation manifest exchange all happen via
controlled paths (SIPRNet, JWICS, physical media).

**Requirements for Axiom design:**
- Offline install bundles: pinned wheel + dependency tarball +
  signed manifest verifiable without network.
- Update channel: detached signed package archive. Operator
  carries it to the air-gapped system and runs a local install;
  `axi update --bundle <path>` must work.
- Federation manifest delta sync: must function with a
  one-way-diode or sneakernet; no assumption of bidirectional
  realtime channel.
- LLM deployment: local-only (no API calls). Bonsai LM or an
  accredited local model.

### S9. Clearance revocation

A principal's clearance is revoked (personnel action, security
incident, routine re-adjudication failure). The principal's
identity survives; their clearance attestation is revoked and
must no longer grant access.

**Flow:**
1. Security officer signs a revocation notice.
2. Revocation propagates via the federation's revocation
   channel (same pattern as key revocation — signed, monotonic,
   TTL'd).
3. All nodes in the classified federation must refuse the
   principal's subsequent requests within a policy-defined
   propagation window.

**Invariants:**
- Revocation propagation is deterministic: once a node has
  received the revocation, refusal is not optional.
- Pre-revocation actions are audited but NOT retroactively
  invalidated — the audit chain records what the principal did
  while cleared.

### S10. Spillage response

Classified content accidentally lands in an unclassified
channel. Detection may be manual report, automated pattern
match, or LLM-flagged candidate.

**Flow:**
1. Incident is reported through the classification regime's
   spillage procedures (NOT via the compromised channel).
2. All copies of the spilled content in unclass systems are
   identified and purged (including RAG indexes, agent memory,
   audit-preserved chains).
3. The unclass system undergoes remediation per the governing
   regime's rules.
4. The classified system's audit captures the spillage and
   remediation.

**Design implications:**
- Content purge must work cleanly — no "soft delete" with
  residual references.
- Audit chains can record spillage metadata WITHOUT storing the
  spilled content itself (spillage metadata is unclassified; the
  actual content is not).

### S11. Compartmentalization within a single level

Two SECRET compartments (e.g. SECRET//SI and SECRET//TK) share
classification level but not access rights. A principal cleared
for SI does NOT automatically see TK.

**Invariants:**
- Compartment membership is a deterministic check against the
  principal's clearance attestation, per-compartment.
- A federation may span multiple compartments; participants see
  only content for compartments they're cleared into.
- Cross-compartment content movement within the same level is
  treated like cross-domain for transfer purposes.

### S12. Model-mediated operations in classified environments

LLM use in classified environments has additional constraints
beyond those in `spec-security.md §2`:

- **Only accredited models.** Cloud LLM APIs (Anthropic, OpenAI,
  etc.) cannot see classified data. Bonsai LM or another
  on-premise/accredited model is used.
- **Classification-preserving prompt engineering.** A prompt
  sent to an LLM carries the classification of its highest-
  classified content. The response inherits that classification.
- **No cross-domain LLM calls.** An LLM instance serving a
  classified context cannot be shared with an unclass context
  even on the same node.
- **Explicit disables.** Some workflows that use LLMs for
  classification *itself* (e.g. "is this document SECRET?") are
  inappropriate — classification decisions are human-original-
  classifier decisions, not LLM suggestions. LLM may flag
  candidates for human review; never authorize.

### S13. Export-control in industry consortiums (without classified)

A multi-company consortium (no government involvement) is still
subject to export control. ITAR and EAR apply independently of
classification.

**Invariants:**
- Consortium federation carries an export regime declaration.
- Nationality-based access gates apply per S6 and S7.
- No classified clearance required; export control is a
  separate regime.

---

## 4. Invariants Summary (All Regimes)

These must hold across every scenario:

1. **Classification is deterministic.** No LLM output ever grants
   or denies classified access. Gates are cryptographic-
   attestation checks backed by signed stamps and clearances.
2. **Classification stamps travel with content.** Content without
   a stamp is treated as the lowest trusted level until stamped.
3. **Domain never exceeds floor.** A federation, node, or context
   never handles content whose level exceeds its declared domain.
4. **Identity is separate from clearance.** An identity without
   clearance cannot see classified content, even if they are a
   federation member.
5. **Cross-domain transfer is explicit and audited.** Up-transfer
   is permitted with policy; down-transfer is an officer-signed
   declassification action.
6. **Clearance is perishable.** Short TTL, periodic
   re-adjudication, revocation propagation.
7. **Compartmentalization composes.** Level + compartments must
   both check. Missing either fails access.
8. **Spillage response purges.** Content removal must be
   complete; "soft delete" is not acceptable at classified
   boundaries.
9. **Air-gap is a real operating mode.** Design assumes it from
   day 1 even before any classified deployment exists.
10. **Model-mediated operations respect domain.** LLM instances
    are scoped to a single domain and never cross.

---

## 5. Domain-Consumer-Specific Concerns

A domain consumer (e.g. a nuclear-engineering consumer) builds on
Axiom for a regulated domain. An export-control regime (e.g. NRC
10 CFR Part 810) may always be applicable:

- Technology transfer is regulated even when unclassified.
- Foreign-national participation in research depends on specific
  Part 810 authorizations.
- Some domain data is dual-use (defense +
  civilian), requiring both EAR and ITAR checks.

A federated-learning partnership with a gov-lab counterparty can
introduce rules more restrictive than the deploying org's alone.
Per ADR-022, cross-sector bridges honor the most-restrictive
side's rules; the gov-lab side's Part 810 obligations apply globally
when the two orgs are federated.

For classified work (currently aspirational but credibly near
horizon for some partners), the design must not preclude future
accreditation. This spec's invariants are the preclusion test.

---

## 6. Non-Goals

- **This spec does not certify or accredit Axiom for any
  classification regime.** Accreditation requires per-deployment
  assessment (IATT, ATO, or equivalent) against the governing
  regime's controls (NIST 800-53, CNSSI 1253, etc.).
- **This spec does not enumerate every control** required by
  each regime. It enumerates the scenarios the design must
  handle and the invariants it must preserve.
- **This spec does not implement classified handling.**
  Implementation is a substantial and separate body of work,
  scoped per-deployment. Today's axiom is targeting
  unclass + export-controlled first.

---

## 7. Implementation Staging

Aligned with the "deliver value per phase" rule:

**Stage 1 (now, shipping):** unclassified + EAR/ITAR export
control + Part 810. Three-tier content model
(public/restricted/export_controlled). Nationality-gated access.
Foreign-national handling for domain-consumer research. No classified.

**Stage 2 (near-horizon):** CUI (Controlled Unclassified
Information) handling. Federal contractor requirements.
NIST 800-171 alignment.

**Stage 3 (horizon):** SECRET deployment in a dedicated
accredited enclave. Single-domain classified federation.
Air-gapped operation as a deployment mode.

**Stage 4 (horizon+):** TOP SECRET/SCI, compartmentalization,
cross-domain transfer workflow.

Each stage is independently valuable: stage 1 is everything
needed to federate across universities and with INL for
export-controlled research. Stage 2 adds federal contractor
viability. Stage 3 unlocks one kind of classified partnership.
Stage 4 is the "full classified" capability.

---

## 8. Related Documents

- ADR-022 — federation identity authority (multi-sector,
  including government)
- ADR-023 (pending) — federation topology; cross-sector bridges
  honor most-restrictive side
- ADR-025 (pending) — federation threat model; will incorporate
  classification-specific threats
- `spec-security.md §2` — deterministic/model-mediated framework
  this spec extends for classified environments
- `spec-rag-architecture.md` — three-tier content model (this
  spec's Stage-1 baseline)
- `prd-federation.md §17` — install/upgrade scenarios (air-gapped
  install is listed there)
- `project_federation_authority_scope` memory — session context
  for this work
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
