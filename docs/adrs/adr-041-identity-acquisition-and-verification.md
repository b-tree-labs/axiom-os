# ADR-041: Identity Acquisition and Verification at Install Time

**Status:** Proposed (2026-05-02)
**Authors:** Benjamin Booth, Claude
**Related:**
- ADR-020 (federation identity + relationships) — the keypair model this extends
- ADR-022/023/024/025 (federation identity roots, topology, availability, threat model)
- ADR-026 (ownership model — owner field semantics)
- ADR-027 (federated memory — `accountable_human_id` propagates everywhere)
- ADR-028 (trust graph — TOFU + adaptation loop, the model we mirror for first-party identity)
- ADR-031 (extension self-containment — identity providers ship as AEOS extensions)
- ADR-032 (dual-track standards positioning — DID/VC live on the public track)
- ADR-035 (human-principal binding — `accountable_human_id` is mandatory on every fragment)
- ADR-036 (extension runtime surfaces — IdentityProvider is a new runtime surface)
- **ADR-042 (chat-driven corrections + correction-aware retrieval — sister ADR; complementary)**
- `spec-aeos-0.1.md` (AEOS extension contract — adds `kind = "identity_provider"`)
- `spec-federation-policy.md` (cohort manifest carries identity-provider requirements)
- `project_auth_tier_staging.md` (tokens now → OIDC post-Prague → InCommon → PIV)

---

## Context

A typo became load-bearing forever.

`axi federation init` today asks for `Owner identifier (e.g., email):` at a bare `input()` prompt. No defaults. No verification. No SSO. Whatever the operator types becomes:

- The `owner` field on `NodeIdentity` (`src/axiom/vega/federation/identity.py`).
- The `accountable_human_id` stamped on **every** memory fragment via ADR-035.
- The owner field on **every** federation peer record we ever publish.
- The signer-of-record on every signed compute receipt this node ever emits.

A wrong value at install time pollutes every downstream artifact for the lifetime of the node. There is no architectural correction path — only `rm -rf ~/.axi/identity/`, which throws away the keypair and severs every trust relationship we have ever built.

The bug surfaced concretely: a workstation's owner field was discovered to read `wrong.user@example.org` — an email that does not exist, never has, and is not one of the operator's two real institutional addresses. Every cross-cohort fragment that node has ever signed carries this bad attribution. Every federation peer we have advertised to has cached it. The damage is silent and compounding.

This ADR establishes how Axiom acquires, verifies, and binds the human identity at install time so that *the wrong value cannot be silently committed*. Its sister ADR-042 establishes how to correct values that nevertheless turn out wrong over time. Together they are the accountability story for ADR-035: this one for "born right," that one for "made right."

### What's broken today (state of the surface)

1. **Zero defaults.** The prompt does not show what `git config user.email`, `gh auth status`, AWS, macOS Address Book, or any other system source already knows about the operator.
2. **Silent acceptance.** Whatever the operator types is committed. No round-trip to confirm the address is reachable; no signal that the address is well-formed beyond not being empty.
3. **No SSO.** Even at institutions with mature OIDC/InCommon/SAML federations, the operator is asked to retype an email that the institution would happily attest to.
4. **No layered providers.** The institution-specific verification logic (an institutional SSO, Microsoft 365, Google Workspace, Okta, GitHub) has no extension contract to ship into.
5. **No cohort policy hook.** A cohort that requires institution-verified identity (a regulated research program, a credit-bearing course, a classified workstream) cannot enforce that requirement at the membership boundary.
6. **No DID story.** The Ed25519 keypair we already generate per-node IS DID-shape (`did:key:<pubkey>`), but we do not formalize the binding, so we cannot accept verifiable credentials *against* it.
7. **No repair path.** There is no `axi nodes set-owner --verify` flow. The only way out is total identity reset.

### What we are NOT designing here

- **Authentication of sessions.** Tokens, OIDC, InCommon — that's `project_auth_tier_staging.md`. Identity *acquisition* is what gets recorded as the owner; *authentication* is what lets a session act under that owner. Different concerns; this ADR is the former.
- **Correction of already-bad data.** ADR-042 (sister) handles chat-driven corrections + correction-aware retrieval for fragments already in the wild.
- **Authorization.** What an authenticated, verified identity is *permitted* to do is the trust-profile / RACI / classification stack (ADR-028, ADR-035, `spec-federation-policy.md`). This ADR establishes that the identity is real; the policy stack decides what it can do.

---

## Decision

Identity acquisition becomes a layered pipeline with deterministic candidate discovery, mandatory verification on the owner binding, pluggable institutional providers shipped as a new AEOS extension kind, cohort-level enforcement, and a formal DID binding to the existing Ed25519 keypair. Each layer is independently shippable; the contract is what binds them.

### D1 — Candidate discovery: read every plausible source, display all, never silently pick one

Install time runs a `discover()` sweep across every available identity provider and accumulates a list of `CandidateIdentity` records. For the v0 baseline (no institutional providers installed), the built-in `system-sources` provider consults at minimum:

- `git config user.email` and `git config user.name`
- `gh auth status` (active GitHub login, if `gh` CLI present)
- `~/.aws/credentials` default profile (if present; AWS account email is often the operator)
- macOS `dscl . -read /Users/$USER RealName` and Address Book "me" record (Darwin only)
- `getpass.getuser()` + reverse lookup against `/etc/passwd` GECOS field (Linux/Darwin)
- Microsoft Graph "me" if a Teams/Outlook session is signed in (best-effort)
- `~/.gitconfig` `[user]` aliases section
- `$EMAIL`, `$GIT_AUTHOR_EMAIL`, `$GIT_COMMITTER_EMAIL` environment variables

Per `feedback_proactive_ux_minimize_cognitive_load`, the install flow does not silently pick a winner. It **displays all candidates** with their source attribution and prompts a confirm. Default selection is the most-corroborated candidate (appearing in the most sources). Operator overrides with a number, `n` for none-of-the-above (free-form entry), or just hits Enter to accept the default.

This is the load-bearing UX call: **display-and-confirm, not auto-pick.** Auto-pick is what got us here.

### D2 — Verification is mandatory on the owner binding; optional on aliases

Even after the operator picks one, we send a 6-digit verification code to the chosen address. The operator pastes it back. Only verified addresses are written to `identity.json` as the `owner` field.

- **Owner field: verification required.** No exceptions. The owner field stamps every fragment forever; a 30-second round-trip is cheap insurance.
- **Aliases: verification optional.** Additional addresses that are claimed-but-not-verified are stored in `identity.json` under `aliases[]` with `verified: false` and a `verified_at` of `null`. They are usable for display and lookup but cannot be the `accountable_human_id` on a fragment until verified.
- **Air-gapped / no-mail-out environments:** the install flow detects no-network state and offers two fallbacks: (a) skip verification with `--unverified-owner` and a strongly-worded warning that the value is provisional; (b) defer verification by writing an `unverified` flag to `identity.json` and prompting on first network-available run.

The verification mechanism mirrors the SSH-TOFU + fingerprint pattern Axiom already uses for federation peers (`src/axiom/vega/federation/identity.py:fingerprint`). The user-facing pattern is intentionally Signal-style: the operator confirms identity by demonstrating control of a side channel, the side channel is remembered, and subsequent assertions ride on the established binding.

### D3 — IdentityProvider becomes a first-class AEOS extension kind

`spec-aeos-0.1.md` §4 currently lists seven capability kinds (agent, tool, cmd, service, adapter, skill, hook). This ADR adds an eighth: `identity_provider`.

```python
# axiom.identity.providers.contract
from typing import Protocol

class IdentityProvider(Protocol):
    name: str                                   # e.g., "google-workspace", "institutional-sso"
    discovery_method: Literal["passive", "interactive"]
    verification_methods: list[str]              # e.g., ["email_code", "oidc", "saml"]

    def discover(self) -> list[CandidateIdentity]:
        """Return identities this provider observes about the operator.

        Passive providers (system-sources) MUST NOT prompt or open browsers.
        Interactive providers MAY open a browser tab during discover() if
        the operator has opted in to that provider.
        """

    def verify(self, claimed: CandidateIdentity) -> SignedAssertion:
        """Verify the operator controls the claimed identity.

        Returns a SignedAssertion bearing the provider's signature, the
        verified principal, the verification timestamp, and the verification
        method used. Raises VerificationFailed on a wrong code, expired
        challenge, revoked credential, etc.
        """

    def refresh(self, assertion: SignedAssertion) -> SignedAssertion:
        """Re-verify an existing assertion (e.g., OIDC token refresh)."""
```

Concrete providers shipped as separate AEOS extensions per ADR-031:

| Extension | Backend | Audience |
|---|---|---|
| `axiom-identity-system-sources` (built-in) | `git`, `gh`, `dscl`, env | Everyone (always installed) |
| `axiom-identity-google` | Google Workspace OIDC | Most academic institutions |
| `axiom-identity-microsoft` | Microsoft 365 / Azure AD | Corporate / mixed institutions |
| `axiom-identity-okta` | Okta OIDC / SAML | Enterprise SSO |
| `axiom-identity-incommon` | InCommon SAML / Shibboleth | US academic federation (broad) |
| `axiom-identity-institutional-sso` | An institutional account, wraps InCommon | institution-specific (concrete example only — not in core docs) |
| `axiom-identity-github` | GitHub OAuth | Developer-shop case |

The core docs reference "institutional identity providers" generically. UT-EID and Google-Workspace and the rest are concrete extensions — not vocabulary the core spec uses.

### D4 — Cohort-level identity policy lives in the cohort manifest, enforced by the membership gate

Federation cohorts can require specific identity providers. Concretely, the cohort manifest gains an optional `identity_policy` block:

```toml
[cohort.identity_policy]
required_providers = ["incommon"]              # any-of: at least one assertion from this set
forbidden_providers = []                       # never-of: explicit deny
required_verification_methods = ["oidc", "saml"]   # excludes "email_code" only
maximum_assertion_age_days = 365               # forces periodic refresh
```

The cohort coordinator's admission gate (per `spec-federation-policy.md`) checks the candidate member's `SignedAssertion` set against the policy. Members presenting only an unverified or insufficient identity are admitted as `provisional` — read-only on cohort fragments — until they upgrade.

**Where the policy lives is the load-bearing call.** It lives in the cohort manifest because: (a) the cohort owns its own admission semantics; (b) the manifest is already the propagation surface for cohort-wide constraints (visibility horizon, classification ceiling); (c) the policy engine consumes it like any other cohort policy. The policy engine is the *enforcement* point; the manifest is the *declaration*. Both, not either.

### D5 — DIDs are formalized in Phase A; verifiable credentials land in Phase B

The Ed25519 keypair we already generate at install time IS DID-shape. We formalize the binding now:

- `NodeIdentity` gains a `did` field of the form `did:key:z<base58btc-multibase-pubkey>` per the W3C `did:key` method (one-line registry, zero infrastructure).
- The `did` is derived deterministically from the existing `public_key` field. It is not a new secret; it is a new *projection* of the same secret.
- `SignedAssertion` from any IdentityProvider asserts `(institutional_identity → did)`. The DID is the long-term anchor; the institutional identity is the assertion-of-the-day. This means a graduated student's `user@example.org` can become `user@alumni.example.org` without losing the underlying DID — the institution just stops asserting; a new institution starts asserting; the DID persists.

Verifiable Credentials (VCs) per the W3C Verifiable Credentials Data Model land in Phase B. The IdentityProvider contract is designed so a `SignedAssertion` is upgrade-compatible with a VC: same fields, same shape, swap the signature envelope.

**Why ship DIDs now (Phase A) instead of stubbing for Phase B:** the keypair already exists. The derivation is mechanical. Adding the `did` field today costs nothing and avoids a schema migration when VCs land. Skipping it means a future schema bump for what is essentially a one-line computed property today. Per `feedback_no_backward_compat_shims`, ship it clean now.

### D6 — Repair path: `axi nodes set-owner --verify` re-runs the full pipeline

For nodes that already have a wrong owner field (today's state):

```
axi nodes set-owner --verify
```

This command re-runs discover → display → pick → verify against currently-installed providers. On confirm, it:

1. Writes the new value to `identity.json`.
2. Composes a `correction` memory fragment per ADR-042 — a fragment that records `(old_value, new_value, reason, signature)` so the correction itself is auditable.
3. Marks the prior owner value as `superseded_by` in a small per-node `identity_history` log.
4. Does NOT rewrite past fragments — those keep their original `accountable_human_id`. ADR-042's correction-aware retrieval is what makes them queryable under the new identity at read time.

The keypair is preserved. Federation peer relationships are preserved. Trust scores are preserved. Only the human-readable owner binding shifts; the cryptographic identity is unchanged.

For migration of existing-broken nodes: we **do not auto-prompt re-verification on first launch after upgrade** in Phase A. We surface a banner ("identity has not been verified; run `axi nodes set-owner --verify` to confirm or correct") and an `axi doctor` warning. Auto-prompting on every upgrade is over-aggressive and trains users to dismiss prompts. The banner + doctor pattern matches our existing UX for non-blocking advisory state.

In Phase B, when DIDs are widely adopted, we add a one-time `axi nodes attest-owner` interactive flow that runs at the next `axi` invocation after upgrade for nodes whose `identity.json` lacks the `verified_at` timestamp. This is the explicit migration trigger; it fires once per node, not per launch.

### D7 — The accountability handoff: this ADR + ADR-042 are complementary, not overlapping

This ADR establishes the *first-write* discipline: the identity is acquired correctly when first bound to the keypair, period. ADR-042 establishes the *over-time* discipline: when (not if) reality shifts — name change, institution change, typo discovered — the correction flows through chat, lands as a `correction` fragment, and downstream retrievals become aware of the binding update.

Concretely:
- This ADR's `axi nodes set-owner --verify` writes the corrected owner into `identity.json` AND emits the `correction` fragment.
- ADR-042 owns the `correction` fragment schema, the chat surface that produces them, and the read-time retrieval awareness.
- Neither ADR rewrites past fragments. ADR-042's projection layer is what makes pre-correction fragments queryable under post-correction identities.

Both must ship for the accountability story to close. If only this one ships, the past fragments are still wrong with no path to reconcile. If only ADR-042 ships, the install flow keeps producing new wrong fragments to correct.

### D8 — Phasing is honest about Prague

Prague class begins early June 2026 — approximately five weeks from this ADR.

**Prague (today's surface, broken state — ship-as-is):**
- Plain `input()` prompt, no defaults, no verification.
- Instructor pre-issues tokens with a manually-typed owner; the operator does not run `federation init` for the first time at install — they run `axi classroom join <invite>` against an instructor-prepared identity.
- This sidesteps the ADR-041 surface for Prague's specific case but does not fix it.
- A doctor warning ships pointing operators to the post-Prague repair flow.

**Post-Prague Phase A (target: 6 weeks after Prague go-live):**
- D1 (candidate discovery + display) shipped end-to-end.
- D2 (email-code verification) shipped for owner field.
- D5 (DID derivation in `NodeIdentity`) shipped.
- D6 (`axi nodes set-owner --verify` repair flow) shipped.
- `axiom-identity-system-sources` built-in shipped.
- `axiom-identity-google` and `axiom-identity-github` shipped as first external providers.

**Post-Prague Phase B (target: end of Q3 2026):**
- D3 full IdentityProvider extension contract published in AEOS 0.2.
- `axiom-identity-microsoft`, `axiom-identity-okta`, `axiom-identity-incommon` shipped.
- D4 cohort-level policy enforcement wired into `spec-federation-policy.md`.
- Verifiable Credentials substrate adopted for `SignedAssertion`.
- One-time `axi nodes attest-owner` migration trigger ships.

**Phase C (long-horizon, gov/industry):**
- PIV / CAC / FICAM-compliant providers as separate extensions per `project_auth_tier_staging.md` Tier 3.
- Verifier-issued credentials for classified workstreams.

Per `feedback_freeze_foundation_during_delivery`, Phase A is queued *behind* Prague go-live. This ADR records the design decision; the implementation does not begin until the runway plan permits.

### D9 — Conformance: AEOS gates publication of identity providers on contract adherence

Identity providers are a high-trust extension category — they assert "this person controls this address." A misbehaving identity provider can launder bad attributions into the system.

AEOS conformance for `kind = "identity_provider"` requires:

- Gold-level conformance per `spec-aeos-0.1.md` §12.3 (Sigstore signed, behavioral attestation supported, quarantine/recovery ceremony supported).
- Implementation of a conformance test suite (`axiom_tests.identity_providers.IdentityProviderTests`) that exercises: (a) discover idempotency, (b) verify-with-wrong-code rejection, (c) refresh-of-expired-assertion behavior, (d) signature-shape correctness, (e) no-network failure modes.
- Published `publisher_identity` field naming the institutional or vendor signer; the verification flow surfaces this prominently ("verified by Google Workspace, signed by Google LLC").

### D10 — Surfacing in UX: every place the owner is shown, the verification state is shown next to it

Per ADR-035 §D6 (accountability is visible everywhere), the owner field's verification state is visible everywhere it is. Concretely:

- `axi federation status` — owner line shows `user@example.org (verified, InCommon, 2026-06-15)`.
- `axi me` — same shape.
- Any agent message that reveals the operator's accountable-human binding shows the verification provenance on hover/expansion.
- `axi doctor` — flags any unverified owner with severity `warn` and a one-line repair hint.
- The candidate-discovery TUI screen (per spec-identity-acquisition §6) shows source attribution per candidate so the operator sees where each suggestion came from.

Verification state is part of the identity's truth condition, not an internal detail.

### D11 — System-sources provider is built-in and unconditionally installed

Unlike institutional providers (which are opt-in extensions), `axiom-identity-system-sources` ships as a built-in extension per AEOS `builtin = true` semantics. It is unconditionally available because the candidate-discovery sweep would be useless without it.

It is the only identity provider that is built-in. All others (Google, Microsoft, Okta, InCommon, UT-EID, GitHub) are external AEOS extensions installed by the operator or the cohort coordinator's manifest. This honors `feedback_axiom_domain_agnostic` — the core ships generic plumbing; institutional specifics arrive via extensions.

### D12 — No backward-compat shim for the old `_prompt_owner()` path

Per `feedback_no_backward_compat_shims`, when D1+D2 ship, `_prompt_owner()` is deleted. `_cmd_init` calls the new pipeline. There is no fallback path that bypasses verification. Operators on legacy nodes that were initialized under the old flow are routed to D6 (`axi nodes set-owner --verify`); they are not allowed to initialize new nodes under the old flow.

This is a pre-public-launch refactor; we are not maintaining a shim for users who do not exist yet.

---

## Consequences

### Positive

- **The owner field is truthful by construction.** Verification at acquisition makes the bug class that surfaced this ADR architecturally impossible going forward.
- **Cohorts get an admission contract.** "Prague NE-101 cohort accepts only InCommon-verified identities" becomes a one-line manifest entry, not a manual instructor process.
- **Identity ecosystem is open.** Any institution with an OIDC / SAML / SSO surface can ship its own AEOS-conformant provider extension and participate without core changes. UT-EID is just the proof case.
- **DID gives us a self-sovereign anchor.** Operators outlive their institutional affiliations. The DID does too.
- **The repair path exists.** No more "rm -rf the keypair to fix a typo." Identity drift over time is now a first-class operation.
- **AEOS gains a high-value capability kind.** Identity providers are in active demand across the agentic-platform space; shipping a clean contract positions Axiom as the federation-native option.

### Negative / costs

- **New AEOS capability kind.** `spec-aeos-0.1.md` needs a §4.8 amendment. Several follow-on tools (`axi ext lint`, `ExtensionStandardTests`) gain identity-provider awareness.
- **Schema bump on `NodeIdentity`.** `did`, `verified_at`, `verification_provider`, `aliases[]`, `assertions[]` are new fields. Migration helper required. Pre-bump `identity.json` files default to `verified_at: null` and `did: <derived from public_key>` on read.
- **Email-code mailer dependency.** Phase A's verification-by-code requires the install flow to send mail. Built-in implementation uses the operator's local SMTP if available; falls back to a small SMTP relay we operate; falls back to "skip with warning." Adds an operational dependency we did not previously have.
- **Six external extensions to design + build.** Phase A ships 2 (Google, GitHub); Phase B ships 4 (Microsoft, Okta, InCommon, UT-EID). The cumulative engineering cost is real; phased schedule is honest about it.
- **Cohort manifest schema growth.** `identity_policy` block is new; admission gate logic in `spec-federation-policy.md` grows to consume it.

### Risks + mitigations

| Risk | Mitigation |
|---|---|
| Email verification fails for operators in air-gapped or restricted networks | `--unverified-owner` escape hatch with strongly-worded warning; identity is flagged unverified in audit projections; cohort policy can downgrade unverified identities to provisional |
| Operator lies during verification (controls the inbox of someone else's address) | Out of scope for v0; institutional providers (D3) raise the bar by requiring SSO into the institution; cohort policy (D4) refuses unverified identities for high-stakes cohorts |
| Identity provider extension is malicious (asserts identities the operator does not control) | AEOS Gold conformance + Sigstore signature + behavioral attestation per D9; quarantine/recovery ceremony if drift detected; cohort policy can deny specific providers |
| DID method (`did:key`) is not interoperable with institutional VC issuers that prefer `did:web` or `did:ion` | Phase B layers `did:web` over the existing `did:key` (the keypair stays the secret; the DID method is the addressing scheme); cross-method verification is a published operation |
| Phase A repair path used to "set the owner to a different real-but-not-mine person" | `correction` fragment is signed; the new owner must be email-verifiable; cohort policy can require trust-graph corroboration for owner changes on already-trusted nodes |
| Operator does not run `set-owner --verify` after upgrade and lives with a bad owner field forever | Doctor warning + banner per D6; cohort admission gate flags unverified identities at federation-membership boundary; ADR-042 chat-driven correction is the catchall path |
| Verification round-trip latency degrades install UX | 6-digit codes deliver in seconds via standard mail; install flow allows skip-and-defer with a clear narration; the operator never has to wait synchronously |

---

## Compliance gates introduced

- `pytest -m identity_acquisition` (new marker):
  - `discover()` is idempotent and produces well-formed `CandidateIdentity` records.
  - `verify()` rejects wrong codes, expired challenges, and revoked credentials.
  - `SignedAssertion` round-trips through serialization without loss.
  - `NodeIdentity.did` derives deterministically from `public_key`.
  - The repair flow emits a `correction` fragment matching ADR-042's schema.
- `pytest -m identity_provider_conformance` (new marker, run by `axi ext lint` against any extension declaring `kind = "identity_provider"`):
  - Implements the full `IdentityProvider` Protocol.
  - No-network failure modes are well-behaved.
  - Signature shapes verify against the declared `publisher_identity`.
- `axi doctor` gains `identity_verified` and `identity_provider_loaded` checks.

These join the existing `accountability_compliance` (ADR-035) and `memory_compliance` markers as release gates.

---

## Open items

- **Email-code mailer ownership.** Do we operate a small relay (operationally light, single point of failure), require operators to bring their own SMTP (zero ops, friction high), or both behind a flag (likely, but the default matters)?
- **`did:key` vs `did:web` as the default Phase A method.** `did:key` is zero-infrastructure; `did:web` requires a hosted JSON document but interoperates better with VC ecosystems. The decision is whether Axiom ships its own well-known DID resolution endpoint.
- **Cohort-policy enforcement when the cohort is offline.** If the coordinator is down, can a member still be admitted with weaker verification and upgraded later? `spec-federation-policy.md` has analogous "deferred enforcement" patterns; this ADR points at them but does not pick.
- **Provider precedence when multiple providers are installed.** If both Google and Microsoft assert the same operator address, do we collect both assertions or pick one? Defaulting to "collect both" is the right answer; the manifest schema just needs to admit it.
- **Discovery sweep cost on machines with many providers installed.** A Phase B operator with 5 providers may face a 10-second discovery sweep. We bound discovery to 3 seconds per provider with a soft timeout; provider authors note the budget.

---

## The bottom line

Today's install flow lets a typo become a permanent attribution. This ADR replaces silent acceptance with a discover → display → verify → bind → assert pipeline that is correct by construction, plus a repair path for already-broken state, plus an extension contract for the institutional providers that should be doing the verification anyway. The DID binding makes identity portable across the operator's affiliations. The cohort policy hook makes "we only admit verified members" a one-line declaration. ADR-035's accountability promise — no AI action without a named human standing behind it — is only worth what its weakest provenance is. Today, the weakest provenance is the install prompt. This ADR fixes that.

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
