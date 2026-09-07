# PRD: Identity Acquisition and Verification at Install Time

**Product / Feature:** Identity acquisition pipeline (`axi federation init` + `axi nodes set-owner --verify` + `IdentityProvider` extension kind)

**Owner:** Axiom Platform   •   **Status:** Draft (design only)   •   **Last updated:** 2026-05-02

**Related:** ADR-041 (this PRD's design source), ADR-042 (sister: chat-driven corrections), ADR-035 (human-principal binding), `spec-aeos-0.1.md`, `project_auth_tier_staging.md`

---

## 1) Elevator Pitch

Axiom's install flow stops accepting unverified identity values that pollute every downstream artifact, and starts running a layered discover → display → verify → bind pipeline backed by pluggable institutional providers — so the operator's owner field is true on day one and stays true across affiliation changes.

## 2) Problem / Opportunity

- **Today (broken):** `axi federation init` collects the operator's email at a bare `input()` prompt with no defaults, no verification, no SSO. Whatever the operator types becomes the owner field on `NodeIdentity`, the `accountable_human_id` on every memory fragment per ADR-035, the owner on every federation peer record, and the signer-of-record on every signed compute receipt. A typo becomes load-bearing forever; the only fix today is throwing away the keypair and severing every trust relationship.
- **Concrete bug:** A workstation owner field was discovered to read `user@example.org` — an address that does not exist and is not one of the operator's two real institutional emails. Every cross-cohort fragment that node has signed carries the bad attribution.
- **Why it matters now:** ADR-035 makes accountability load-bearing in the architecture. The accountability promise is only as strong as the weakest provenance step in the chain. The install prompt is currently the weakest step.
- **Who is impacted:** Every Axiom operator at install time; every cohort that needs admission-grade verification; every downstream federation peer that consumes our signed assertions.

## 3) Goals & Success Metrics

- **Primary goal:** Install-time identity is correct-by-construction; over-time identity drift is correctable without identity reset.

- **Success metrics:**
  1. **Bad-owner rate at install: < 1%** of nodes have an unverified owner field 30 days post-install (today: ~unknown, but at least 1 known case in a population of ~5).
  2. **Discovery hit-rate: ≥ 90%** of installs find at least one well-formed candidate from system sources (no free-form typing required).
  3. **Verification round-trip latency: p50 < 30s, p95 < 90s** for email-code verification on a connected machine.
  4. **Repair-flow adoption: 100%** of nodes flagged by `axi doctor` as having unverified owner are corrected within 14 days of the warning surfacing.
  5. **Identity-provider extension count: ≥ 2** shipped at Phase A end (Google, GitHub); ≥ 5 at Phase B end (add Microsoft, Okta, InCommon).
  6. **DID coverage: 100%** of `NodeIdentity` records emit a valid `did:key:...` value derived from `public_key`.
  7. **Cohort-policy adoption: ≥ 1** Phase B cohort admits members under an `identity_policy` declaration.
  8. **AEOS conformance: 100%** of identity-provider extensions ship at Gold conformance per AEOS §12.3.
  9. **Zero compat shims:** the legacy `_prompt_owner()` path is deleted, not retained behind a flag.
  10. **Doctor-warning-to-repair p50: < 24h** for warned operators (measures whether the warning is legible enough to act on).

## 4) Key Users / Personas

- **Operator at first install (P0).** Knows their email. Wants to type as little as possible. Has `git`, often has `gh`, may have a Microsoft 365 or Google Workspace session signed in. Should not need to know what `federation init` is or that an owner field exists.
- **Operator at install on an institution-managed machine (P1).** Their institution has an SSO surface (an institutional account via InCommon, or Google Workspace, or Okta). They expect "sign in with my institution" to be the path of least resistance and are confused when the prompt does not offer it.
- **Cohort coordinator (P1).** Defines who is admitted to a cohort. Wants to declare "this cohort accepts only InCommon-verified identities" once in a manifest, not enforce it manually per member.
- **Operator with an already-broken owner field (P0, today's reality).** Discovered the bad value sometime after install. Wants a one-command repair that does not destroy the keypair or sever federation peer relationships.
- **Operator who changes institutions (P2).** Graduates, transfers, or changes employer. Wants the underlying DID to persist while institutional assertions roll over.
- **Identity-provider extension author (P2).** Wants a clean Protocol contract and conformance test suite to implement against, without reading Axiom-core source.

## 5) Scope — Key Capabilities

### Phase A (target: 6 weeks after Prague go-live)

1. **Candidate discovery (built-in `axiom-identity-system-sources` provider)** — sweeps `git`, `gh`, `~/.aws`, macOS `dscl`, env vars, GECOS; produces a `list[CandidateIdentity]`. *Acceptance:* discovery completes in < 3s on a dev workstation, returns ≥ 1 candidate when any of the standard sources are present.

2. **Display-and-confirm TUI** — shows all candidates with source attribution, defaults to the most-corroborated, accepts numeric pick or `n` for free-form. *Acceptance:* operator can complete the identity step in three keystrokes (one number + Enter, then verification code paste).

3. **Email-code verification (built-in)** — sends a 6-digit code, accepts paste-back with three retries, marks the identity `verified` on success. *Acceptance:* verification round-trip succeeds on a machine with default mail or with the operated relay.

4. **`NodeIdentity.did` derivation (Phase A formalization)** — every new identity emits `did:key:z<multibase-pubkey>`; pre-existing identities backfill the field on next read. *Acceptance:* `axi federation status` shows the DID; round-trips through `to_dict` / load.

5. **Repair flow: `axi nodes set-owner --verify`** — re-runs the full pipeline against currently-installed providers; writes new owner; emits a `correction` fragment per ADR-042's schema; preserves keypair + peer relationships. *Acceptance:* a node with a wrong owner field can be repaired without `rm -rf ~/.axi/identity/`.

6. **Doctor + banner for unverified owners** — `axi doctor` flags `verified_at: null`; one-line banner on `axi federation status` and `axi me` directs to the repair flow. *Acceptance:* doctor exit code reflects the warning; banner is dismissible per session but does not auto-clear.

7. **First two external providers shipped** — `axiom-identity-google` and `axiom-identity-github` published as standalone AEOS extensions, conformance-tested. *Acceptance:* both extensions install via `axi ext install`, integrate into the discovery sweep, complete OIDC verification end-to-end.

### Phase B (target: end of Q3 2026)

8. **AEOS 0.2 amendment** — `kind = "identity_provider"` ratified in `spec-aeos-0.1.md` §4; `IdentityProviderTests` published in `axiom-tests`. *Acceptance:* `axi ext lint` validates an identity-provider extension end-to-end.

9. **Microsoft, Okta, InCommon, UT-EID providers** — four institutional extensions published. *Acceptance:* each completes verification round-trip against a sandbox of its respective IdP.

10. **Cohort-level `identity_policy`** — manifest schema + admission gate per `spec-federation-policy.md`. Provisional-membership downgrade for members not meeting policy. *Acceptance:* a manifest declaring `required_providers = ["incommon"]` admits InCommon-verified members and downgrades others to provisional.

11. **One-time `axi nodes attest-owner` migration trigger** — fires once on first invocation after upgrade for nodes lacking `verified_at`. *Acceptance:* triggers exactly once; records a flag preventing re-fire; remains skippable.

12. **Verifiable Credentials substrate for `SignedAssertion`** — assertion shape upgrades to W3C VC Data Model. *Acceptance:* an institutional VC issued externally verifies through the runtime.

### Phase C (long-horizon)

13. **PIV / CAC / FICAM providers** — gov/industry tier per `project_auth_tier_staging.md` Tier 3. Out of scope for this PRD's current cycle.

## 6) Non-Functional / Constraints

- **Performance:**
  - Discovery sweep < 3s per provider; total install-time discovery < 10s with 3 providers installed.
  - Verification round-trip p95 < 90s on a connected machine.
  - DID derivation O(1), no I/O.
- **Security:**
  - All `SignedAssertion` artifacts cryptographically signed; signature verification is a deterministic primitive (per ADR-029 four-primitives boundary).
  - No verification step bypassable for the owner field; aliases may carry unverified flag.
  - Identity-provider extensions are AEOS Gold conformance per ADR-041 D9.
- **Privacy:**
  - System-sources provider does not transmit any candidate to a remote service; discovery is local-only.
  - Email verification sends only the 6-digit code; no telemetry beyond per-provider's own assertion-issuance protocol.
  - GDPR: candidate identities are processed under consent of the install action; not stored without operator confirmation.
- **Domain-agnostic:** core docs and built-in code never reference any specific domain or institution. An institutional SSO (e.g. InCommon-federated EID) is a concrete extension example; the core recognizes only "institutional identity providers."
- **Platforms:** macOS + Linux Phase A; Windows Phase B (Windows Hello / Azure AD integration).
- **Air-gapped support:** `--unverified-owner` escape hatch + deferred-verification mode; banner pressure to verify on first network-available run.
- **No backward-compat shims** per `feedback_no_backward_compat_shims`: legacy `_prompt_owner()` deleted in Phase A; no flag preserves old behavior.

## 7) Timeline (high level)

- **Now (pre-Prague, design-only):** ADR-041 + ADR-042 + this PRD + spec-identity-acquisition land. No implementation begins per `feedback_freeze_foundation_during_delivery`.
- **Prague window (early June – mid July 2026):** No implementation. Today's broken surface ships as-is for Prague's 12-student cohort; instructor-issued tokens with manually-curated owner field. A doctor warning + post-Prague repair path are the safety net.
- **Phase A (mid-July – early September 2026):** Capabilities 1–7. Targets the post-Prague AEOS queue per `project_post_prague_aeos_queue.md`.
- **Phase B (September – late November 2026):** Capabilities 8–12.
- **Phase C (2027+):** Capability 13 and beyond, gov/industry-driven.

## 8) Risks & Open Questions

| Risk / Question | Mitigation / Resolution path |
|---|---|
| Email-code mailer is a new operational dependency; if our relay is down, all installs degrade. | Default to local SMTP; fall back to operated relay; fall back to deferred-verification mode. Outage is annoying, not fatal. |
| Prague nodes will install under broken flow and accumulate bad attributions in 5 weeks of class. | Pre-Prague: instructor curates the owner field per student manually before class start; treats it as enrollment data; no operator types it raw. Post-Prague: bulk repair via Phase A `set-owner --verify`. |
| Operator runs verification, then loses access to the inbox (graduated, left job). | DID persists. Phase B `attest-owner` flow re-runs verification under a new institutional assertion against the same DID. |
| Cohort policy declares `required_providers = ["incommon"]` but a member's institution is not InCommon-affiliated. | Provisional membership: read-only, banner pressure to upgrade, cohort coordinator can grant exception via signed override. |
| Multiple providers installed — discovery returns 8 candidates and the TUI is overwhelming. | UI collapses to top-3-by-corroboration with "show all" expansion. Per `feedback_drive_dont_enumerate`. |
| Wrong default in candidate-discovery TUI (most-corroborated is not what operator wants). | Operator types one number to override; never bound to the default. Plus the verification round-trip catches "wrong choice picked" before commit. |
| AEOS 0.2 amendment for `kind = "identity_provider"` slips into next-major. | Phase A still ships against the proposed shape; AEOS-0.1 manifests carry the new kind under a permissive root pre-ratification. |
| **Open question:** mailer ownership (operate vs. BYO vs. both). Decide by Phase A start. |
| **Open question:** `did:key` vs. `did:web` Phase A default. Decide alongside DID infrastructure decision. |
| **Open question:** discovery-sweep timeout budget under many providers. Decide alongside Capability 1 implementation. |

## 9) Acceptance & Rollout

- **Sign-off (design):** Ben Booth (Axiom platform owner). Cohort-policy semantics review with whoever signs off on `spec-federation-policy.md` amendments.
- **Sign-off (implementation):** Phase A capability set demoed end-to-end (install fresh node, repair an existing broken node, install a Google provider extension and re-verify against it).
- **Rollout:** Phase A ships behind a `axiom.feature.identity_acquisition_v1` flag for the first two weeks of internal use; flag removed once doctor-flagged repair-flow adoption metric (success criterion #4) clears 80%.
- **Rollback criterion:** if Phase A discovery sweep degrades install latency above p95 = 30s, or if the email-code mailer has > 5% delivery failure, the flag flips off and we ship Phase A2 with the regression fixed before re-enabling.

## 10) Contacts & Links

- Product lead: Benjamin Booth (`no-reply@axiom-os.ai`)
- Eng lead: TBD (post-Prague assignment)
- Spec / design / docs:
  - `docs/adrs/adr-041-identity-acquisition-and-verification.md` — design decisions
  - `docs/adrs/adr-042-chat-driven-corrections.md` — sister ADR (correction over time)
  - `docs/specs/spec-identity-acquisition.md` — technical spec
  - `docs/specs/spec-aeos-0.1.md` §4 — AEOS amendment for `identity_provider` kind
  - `project_auth_tier_staging.md` — staged auth-tier rollout (tokens → OIDC → InCommon → PIV)

---

(Keep this one page; expand in the technical spec for Protocol shapes, sample TUI screens, and the verification-protocol state machine.)
_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
