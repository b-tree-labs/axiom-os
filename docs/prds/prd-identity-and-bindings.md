# PRD: Axiom Identity & Bindings — external-account binding layer

**Product / Feature:** The post-install, steady-state surface that binds external accounts (vendor AI services, OS users, federated peers' principals) to the axi principal established at install. Adds `axi identity` verb-set; extends `IdentityProvider` extension kind from `prd-identity-acquisition.md` with claim-binding semantics.

**Owner:** Axiom Platform   •   **Status:** Draft (design only; this PRD does not ship code)   •   **Last updated:** 2026-05-14

**Related (foundational — do NOT duplicate; reference and extend):**

- **ADR-035** (Human-Principal Binding) — defines `accountable_human_id` + `delegation_chain` on every fragment. This PRD *consumes* that contract; every binding produces an audit-trail record bound to the master principal.
- **`prd-identity-acquisition.md`** — defines the install-time `discover → display → verify → bind` pipeline, the `IdentityProvider` extension kind, the `did:key:` mapping. This PRD *extends* that pipeline to handle post-install account additions/removals; it does not re-specify the install-time machinery.
- **ADR-026** (Ownership Model) — defines master + peer delegations + the four rights (read/write/delete/delegate). Bindings inherit from the master; a binding is itself a kind of memory fragment subject to ownership.
- **ADR-020 / 022 / 024** (Federation identity roots, membership separation, root availability + delegation) — defines how *federated peers* establish identity. This PRD addresses *external services* (vendor accounts), which are a peer-class with weaker trust assumptions than federated peers.
- **ADR-028** (Trust Graph) — bindings carry a per-binding trust score; vendors-as-peers slot into the existing EigenTrust-shaped graph.
- **ADR-027** (Federated Memory) — bindings can be federation-shared subject to visibility + classification.
- **`spec-classification-boundary.md`** — bindings may themselves be CUI / EAR / ITAR / Part 810 sensitive (an analyst's vendor account is often disclosable).

**Cross-references (use-case consumers):**

- `prd-cross-surface-memory.md` (sibling, drafting) — names the integration *patterns*; this PRD provides the binding *substrate*.
- `prd-cross-tool-memory.md` — per-product adapters use the binding layer to resolve which `axi` principal owns a given session.
- `prd-memory.md` §6 differentiators — the "cryptographic provenance" claim depends on accountable_human_id resolving to a real verified identity. Bindings extend this resolution to external surfaces.

---

## 1) Elevator Pitch

A human has one Axiom identity, many vendor accounts, multiple devices, and shifting affiliations over a career. Axiom binds each external account to a single axi principal at the moment it enters the user's workflow, records every binding as an audit-trail-grade fragment, and lets federation, ownership, classification, and accountability all resolve through one identity model. Memory follows the human, not the account.

## 2) Problem / Opportunity

Identity in Axiom today is partially established. ADR-035 makes `accountable_human_id` mandatory on every fragment. `prd-identity-acquisition.md` makes install-time owner-field acquisition correct-by-construction. ADR-026 makes ownership transferable. But three holes remain:

1. **No model for external-account bindings.** When the user ingests a Codex transcript (`tool=codex`) or a Claude Code transcript (`tool=claude-code`), nothing in the substrate says "the OpenAI account `someone@gmail.com` is the local user". The `principal_id` is set by the local config and trusted. The vendor account is in `content.tool` but unbound. A downstream auditor asking *"who really wrote that turn, the human or someone using the human's OS account?"* gets answered by social convention, not architecture.

2. **No model for cross-vendor identity coherence.** A user moves between ChatGPT (OpenAI account), Codex (same OpenAI account, different surface), Claude Code (Anthropic account), JetBrains AI (a third account), and OpenCode (multi-provider, multiple vendor accounts). Each is the same person. The substrate has no mechanism that says "these five accounts are one human." Memory aggregation, federation visibility decisions, retention sweeps, and cryptographic-erasure forget-events all need that coherence and currently don't have it.

3. **No persona model.** A single human routinely has multiple intentional identities (work `user@example.org`, personal `personal@example.com`, research `b.booth@b-treeventures.com`). ADR-035 says each fragment has one `accountable_human_id`. The substrate doesn't articulate that one human can have multiple `accountable_human_id`s on purpose, or how to switch between them, or how each persona's external-account bindings stay separate.

The user-visible failure is the one that motivated this PRD: a turn appears in Codex, axiom ingests it, the OpenAI account is `xiaowu@example.com` (a coauthor on the codex session), and `accountable_human_id` is set to `user@example.org` (the local installer). Now the audit trail says Ben wrote a turn Xiaowu actually wrote, because the substrate had no way to know they were different people sharing one OS user.

## 3) Goals & Success Metrics

**Primary goal:** A binding from an external account to an axi principal is correct, attestable, federatable, and revocable; multi-persona is first-class.

**Success metrics (post-implementation):**

1. **External-account coverage:** ≥ 90% of fragments produced by tools with on-disk transcripts (claude-code, codex, opencode, gemini-cli when shipped) have a resolved external-account binding within 24h of the transcript landing.
2. **Binding correctness:** 0 false-attribution incidents in the audit drill (a manufactured fragment whose `accountable_human_id` cannot be traced through a binding back to a real verified human).
3. **Multi-persona ergonomics:** A user with ≥ 2 personas can switch contexts in < 10 seconds via `axi identity use <persona>` and have all subsequent writes carry the right `accountable_human_id`.
4. **Federation safety:** 100% of bindings classified as CUI / EAR / ITAR are gated by `spec-classification-boundary.md` rules at cohort outflow; no leakage in 1k fuzz tests.
5. **Cryptographic-erasure completeness:** A `forget` event on a persona destroys the binding records *and* renders the linked vendor accounts unresolvable in the ledger going forward (per ADR-026 ownership rights).

## 4) Key Users / Personas

| Archetype | Primary tasks | Pain today |
|---|---|---|
| **Solo researcher with multi-affiliation** | Use Codex with the work OpenAI account, ChatGPT with personal, Claude Code on a research grant. | Each tool's transcripts land under one axi principal regardless of which vendor account produced them. Audit trail loses the *which-account* layer. |
| **PI of a regulated cohort** | Verify that grad-student-attributed work was actually done by the grad student, not a shared shell. | Today: trust the OS user. Tomorrow: a binding chain back to the grad student's verified email. |
| **Operator running a federated peer** | Admit a peer cohort whose members are bound to external IdPs. | Existing cohort `identity_policy` (per `prd-identity-acquisition.md`) covers admission; bindings extend to *external account* claims that peer members declare. |
| **AI safety / compliance** | Re-attest historical fragments after a binding's IdP relationship changes (employee changed jobs, vendor account was rotated). | No mechanism today; ADR-035 says `accountable_human_id` is immutable on a fragment, but the binding *interpretation* of that field is mutable. |
| **Extension developer** | Use the `IdentityProvider` extension kind to add a new vendor (e.g., when Anthropic ships a fresh OAuth API). | Has install-time scaffold (`prd-identity-acquisition.md`); needs a *post-install* scaffold this PRD provides. |

## 5) Scope: what this PRD owns

The PRD adds a single concept (`Binding`) and the lifecycle around it. Everything else is referenced from foundational docs.

### 5.1 The Binding record

A `Binding` is a `MemoryFragment` with:

```
cognitive_type    = "core"               # bindings are identity-level
fact_kind         = "identity_binding"
content.principal_id        = <axi principal this binds TO>
content.external_account    = {
    vendor:  "anthropic" | "openai" | "google" | "github" | "google-workspace" | "microsoft" | "opencode-local" | …,
    surface: "web" | "cli" | "ide-plugin" | "api" | "managed",
    identifier: "<vendor-account-id>",       # e.g. an email, an OAuth sub, a tenant id; format per vendor
    display_hint: "<human-readable>"          # for UX; not load-bearing
}
content.verification_level  = "declared" | "oauth_owned" | "vendor_attested"
content.verification_proof  = {…}        # OAuth tokens / DID-attestation / nothing (declared-only)
content.binding_status      = "active" | "revoked"
content.revoked_at          = <ISO8601 | None>
content.persona_tag         = <persona id | None>    # ties to the persona model (§7)
```

**Three verification levels** (no aspiration beyond what's actually obtainable):

1. **`declared`** — user typed it in. The cheapest binding. Bound to local trust only; no cross-cohort weight.
2. **`oauth_owned`** — axi executed an OAuth flow against the vendor's identity surface and got back the account identifier the vendor confirms. Most accurate level achievable for vendors with OAuth (Anthropic, OpenAI, Google, GitHub, Microsoft).
3. **`vendor_attested`** — the vendor signed an attestation that this account belongs to the axi principal (e.g., via OIDC `sub` claim with the vendor's signing key in our trust root). Highest level. Rare today; future-proof.

### 5.2 Lifecycle verbs

`axi identity` verb-set:

```
axi identity list                         # all bindings for current principal
axi identity bind <vendor>:<account>      # interactive: walks IdentityProvider's OAuth flow if available; declared otherwise
axi identity verify <binding-id>          # promote declared → oauth_owned by running OAuth
axi identity revoke <binding-id>          # mark revoked (does NOT delete past fragments; future writes lose the binding)
axi identity use <persona>                # switch active persona (sets memory.default_principal + binding context)
axi identity personas                     # list configured personas + default persona
axi identity persona create <name> --principal <axi-principal>
axi identity show <binding-id>            # detail incl. verification_level + history
axi identity audit                        # per-fragment binding-resolution report (compliance)
```

### 5.3 Persona model

A **persona** is a named bundle: `{ axi principal_id, default external-account bindings, default visibility scope, default classification stamp }`. One human can have N personas. Each `MemoryFragment` carries one `accountable_human_id` per ADR-035, which is the persona's master principal.

Personas are configured in `~/.axi/identity/personas.toml`:

```toml
[personas.work]
principal_id = "user@example.org"
default_visibility = "team"
default_classification = "unclassified"
binding_refs = ["anthropic:user@example.org", "openai:bbooth-work@openai.com", "github:user-org"]

[personas.personal]
principal_id = "personal@example.com"
default_visibility = "private"
binding_refs = ["anthropic:personal@example.com", "openai:benjamin@personal-email"]
```

`axi identity use work` sets `memory.default_principal=user@example.org` and activates the work bindings; subsequent writes carry that persona's principal + bindings.

### 5.4 Storage

Bindings + personas live in two places by design (the canonical fragment representation is duplicated by a fast-lookup cache):

- **Canonical:** `MemoryFragment` (cognitive_type=core, fact_kind=identity_binding) in the ledger. Authoritative, audit-trailed, federable.
- **Cache:** `~/.axi/identity/bindings.toml` — a flat-file projection of active bindings for fast CLI resolution. Rebuildable from the ledger via `axi identity rebuild-cache`. The cache is *never* the source of truth.

Personas live in `~/.axi/identity/personas.toml` only (configuration, not ledger). Adding a persona is a config write; *using* a persona produces ledger writes carrying that persona's principal.

### 5.5 Binding resolution at write time

Every adapter (per `prd-cross-tool-memory.md`) calls a resolution function before writing a fragment:

```python
def resolve_binding(tool: str, vendor_account_hint: dict | None, active_persona: Persona) -> Binding | None:
    # 1. If hint is provided + a binding matches → use it
    # 2. Else if active_persona has a default binding for tool → use it
    # 3. Else: no binding (write goes through but accountable_human_id falls back to active_persona.principal_id; flag for later resolution)
```

The fallback never fails open the audit chain. It records that no binding was resolved; `axi identity audit` surfaces such fragments for later attribution.

## 6) Relationship to existing identity work: explicit delineation

To enforce the "no-duplicate-content" discipline:

| Concept | Owned by | This PRD says |
|---|---|---|
| `principal_id` (Matrix-style `@name:context`) | `src/axiom/vega/identity/principal.py` | Consumes; never redefines. |
| `accountable_human_id` (mandatory; per ADR-035) | ADR-035 | Consumes; bindings *resolve* this value. |
| `delegation_chain` (agent → human) | ADR-035 | Consumes. |
| Install-time `discover → display → verify → bind` pipeline | `prd-identity-acquisition.md` | Consumes; the post-install flow this PRD adds uses the same pipeline phases with different defaults. |
| `IdentityProvider` extension kind | `prd-identity-acquisition.md` | Consumes; this PRD adds the *vendor* family of providers (Anthropic, OpenAI, Google, GitHub, Microsoft) as instances of that kind. |
| `did:key:...` derived from public_key | `prd-identity-acquisition.md` | Consumes for the axi principal side. Vendor accounts use vendor-native identifiers, not DIDs. |
| Single master + peer delegations + 4 rights | ADR-026 | Consumes; bindings inherit master ownership. |
| Federation identity root + membership separation | ADR-020 / 022 / 024 | Consumes; vendor accounts are NOT roots — they are peer-class entities with declared/oauth/attested verification level. |
| EigenTrust trust graph | ADR-028 | Consumes; per-binding trust score plugs into the graph. |
| Visibility / classification stamps | `spec-federation-policy.md` + `spec-classification-boundary.md` | Consumes; bindings carry visibility + classification (an analyst's vendor account is often itself CUI). |

## 7) Federation + cross-cohort

Bindings are MemoryFragments with classification + visibility. Cross-cohort propagation follows `spec-federation-policy.md` exactly — no new mechanism. Two non-obvious cases:

- **Bound externally, propagated internally.** A grad-student peer's binding to a Google Workspace identity propagates *within* the cohort (PI sees who they are) but not *outside* (downstream peers see the axi principal only).
- **Re-attestation after rotation.** When a vendor account is rotated (employee leaves, account is recreated), prior bindings are not rewritten (immutable fragments). Instead, a new binding is added with `binding_status=active` and the old one is `revoke`-d. Historical fragments keep pointing to the revoked binding; auditors can follow.

## 8) Non-functional / constraints

- **Privacy:** Account identifiers are PII. Bindings carry classification per the human's regime; some are CUI. The cache is local-only by default and never federation-shared without explicit policy opt-in.
- **Pseudonymity:** A user may operate a persona under a pseudonym (`anon-7`); bindings can be `declared` only (no OAuth verification leaks the real identity).
- **Performance:** Binding resolution is on the write hot path; must be < 1ms p95 (cache-resolved).
- **Availability:** Cache loss must be recoverable from the ledger; rebuilding from audit log is part of `axi dr` self-heal.
- **Backwards compatibility:** Existing fragments without bindings are *not* invalid; the audit shows them as "binding-unresolved" and the operator may opt to bulk-resolve via `axi identity backfill`.

## 9) Multi-vendor binding flows (illustrative)

For each vendor, the OAuth flow + the data we capture. (Full implementation table — what vendor extensions in the `IdentityProvider` family must support — lives in `prd-cross-surface-memory.md` §"Per-vendor section"; this PRD declares only the verification levels each must reach.)

| Vendor | Surfaces under one account | Min verification level | Notes |
|---|---|---|---|
| Anthropic | claude.ai web, Claude Code, Anthropic API | `oauth_owned` (Anthropic OAuth ships 2026) | Falls back to `declared` until then |
| OpenAI | chatgpt.com, Codex CLI, OpenAI API | `oauth_owned` (OAuth available today) | |
| Google | gemini.google.com, Gemini CLI, Google API | `oauth_owned` via Google Workspace OIDC | DID derived from OIDC sub |
| GitHub | github.com (referenced for some IDE plugins) | `oauth_owned` | Already an IdentityProvider per `prd-identity-acquisition.md` |
| Microsoft | Copilot products, Microsoft 365 | `oauth_owned` via Microsoft Identity Platform | |
| Open source (OpenCode, Aider, Continue) | Local CLI; multi-provider | `declared` only | These tools have no per-vendor account — they federate through whichever vendor accounts the user has separately bound |

## 10) Open questions

1. **Verification proof storage.** Storing OAuth refresh tokens in MemoryFragments is unacceptable (PII + reuse risk). Two options: (a) tokens live in OS keychain, MemoryFragment carries only a token-handle that resolves locally; (b) tokens are content-classified as `vault` per MIRIX, never federated. Lean: (a) for refresh tokens, (b) for one-time attestation proofs. Decide pre-impl.
2. **Persona switching ergonomics.** Per-shell? Per-session? Per-command? Today `memory.default_principal` is global-process. Personas need at minimum per-shell. Probably per-`axi-process-tree` via env var. Decide pre-impl.
3. **Vendor-attested level scope.** Few vendors today ship the kind of OIDC `sub` attestation that admits direct cohort-trust treatment. Should `vendor_attested` be specified now (forward-compat) or deferred until at least one vendor supports it?
4. **Binding-resolution cache invariants.** What does `axi identity rebuild-cache` guarantee? Probably: byte-identical reconstruction of `~/.axi/identity/bindings.toml` from the audit log over any time window where the master keypair hasn't changed. Confirm pre-impl.
5. **Per-persona ledger or single ledger?** A user with strict work-vs-personal separation may want physically separate ledgers; today personas share one. Open question whether persona = scope or persona = principal-within-scope. Lean: principal-within-scope (one ledger; queries filter by persona). Multi-ledger is a federation question, not a persona question.
6. **Pseudonymity contract.** What axiom guarantees about *not* linking pseudonymous personas to their owner. Probably nothing (the ledger trivially does); user must opt into separate ledgers for true pseudonymity. Document this honestly.

## 11) Timeline

This PRD is **design-only** (per the user's "PRD first; tech spec follows when wire format stabilizes" decision). Sequencing:

| Phase | Deliverable | Window |
|---|---|---|
| Phase 0 (now) | This PRD + `prd-cross-surface-memory.md` + trim of `prd-cross-tool-memory.md`. Design-only, no code. | This branch |
| Phase 1 | Spec — `spec-identity-bindings.md` — wire format for the `identity_binding` fragment shape + cache file format + verification-proof storage. Plus an ADR-038 for the persona model (or extend ADR-035). | Pre-0.18 or alongside |
| Phase 2 | Impl in 0.18 or 0.19 — `axi identity` verb-set + Binding fragment type + binding-resolution function + cache rebuild + the vendor-family `IdentityProvider` extensions for Anthropic/OpenAI/Google/GitHub/Microsoft | Late 0.18 or 0.19 |
| Phase 3 | Per-product adapter rework — `prd-cross-tool-memory.md` adapters consume the resolution function instead of trusting `principal_id` as today. | 0.19 |
| Phase 4 | Federation + classification integration — bindings federate per `spec-federation-policy.md`; `spec-classification-boundary.md` regimes apply to binding content. | 0.20+ |

## 12) Use cases this enables

- **Cross-surface memory** (`prd-cross-surface-memory.md`) — the headline. Memory follows the *human*, not the *account*, even when the human is using different vendor accounts for different surfaces.
- **Cross-tool reach** (`prd-cross-tool-memory.md`) — adapter writes resolve to the right `accountable_human_id` instead of trusting OS user.
- **Audit / compliance** — regulated cohorts can answer "which named human authored this turn?" with a chain from fragment → binding → verified identity → human-of-record.
- **Multi-persona workflows** — work vs personal vs research separation without separate installs.
- **Federation admission** — extends `prd-identity-acquisition.md` admission to per-cohort policies referencing external bindings ("members must have an `oauth_owned` binding to Google Workspace tenant `example.org`").
- **Cryptographic erasure** (per ADR-026 destruction-of-keys) — `axi identity revoke --crypto-erase` destroys the persona's key material; downstream peers receive a tombstone they can't decrypt.

## 13) Acceptance & rollout

- **Acceptance signal:** A research advisor running `axi identity audit` against a 30-day window of grad-student fragments sees ≥ 90% binding-resolved with verification_level ≥ `declared`, ≥ 50% `oauth_owned`, 0 false-attributions in a manual spot-check of 50 random fragments.
- **Rollout:** Bindings ship as opt-in in 0.18 (the install flow does *not* require a binding). 0.19 makes binding-resolution a *write-path warning* when unresolved; 0.20 makes it a *cohort-policy-enforceable requirement* per ADR-035.
- **Backwards compatibility:** All existing fragments continue to validate. `axi identity audit --backfill` proposes bindings for legacy fragments based on heuristics (transcript metadata, OS user, install-time owner field); the operator confirms each before promotion.

## 14) Contacts & links

- Product lead: Benjamin Booth (no-reply@axiom-os.ai)
- Engineering lead: same
- **Foundational reading order for reviewers:** ADR-035 (start here) → `prd-identity-acquisition.md` → ADR-026 → ADR-020/022/024 → this PRD.
- Sibling PRD: `prd-cross-surface-memory.md` (drafting)
- Cross-tool implementation: `prd-cross-tool-memory.md`

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
