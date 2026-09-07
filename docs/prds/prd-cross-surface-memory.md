# PRD: Cross-Surface Memory — Within-Vendor Continuity & Multi-Provider Multiplexers

**Product / Feature:** The integration layer that makes one human's memory continuous as they move between *surfaces of the same vendor* (ChatGPT web ↔ Codex CLI; Claude web ↔ Claude Code; Gemini web ↔ Antigravity IDE) and across *multi-provider client multiplexers* (OpenCode, Aider, Continue). Sister doc to `prd-cross-tool-memory.md` (about cross-*client* integration paths) and consumer of `prd-identity-and-bindings.md` (the binding substrate).

**Owner:** Axiom Platform   •   **Status:** Draft (design only; this PRD does not ship code)   •   **Last updated:** 2026-05-14

**Related (foundational — do NOT duplicate; reference and extend):**

- **`prd-cross-tool-memory.md`** — names the **three integration paths** (MCP-native, transcript-ingest backstop, federated peer) and the **per-adapter AEOS pattern**. This PRD consumes those paths; it does not re-specify them.
- **`prd-identity-and-bindings.md`** (sibling, drafting) — names the binding layer (per-vendor-account → axi principal). This PRD consumes that resolution; without it, multi-account multi-surface memory has no way to coalesce by human.
- **`prd-memory.md`** — the substrate; `CompositionService`, MIRIX 6-type taxonomy, RPE, classification-aware retention. This PRD is one *integration scope* feeding that substrate.
- **`prd-prompt-registry.md`** — outbound surface injections (system-prompt augmentation, MCP `instructions`) live as registry templates.
- **ADR-026 / ADR-027 / ADR-028 / ADR-035** — ownership, federation, trust graph, accountability. The cross-surface flow re-uses these; no new federation primitives.
- **`spec-aeos-0.1.md`** — every per-surface adapter is an AEOS extension.

---

## 1) Elevator Pitch

A human's mental model is one continuous self across every place they interact with AI. The wire reality is fragmented: each vendor's surfaces (web chat, desktop, CLI, IDE plugin, mobile) maintain *separate* memory state, even when they share an account. ChatGPT's web memory doesn't reach Codex CLI on the same OpenAI account; Claude's web project doesn't propagate to Claude Code; Antigravity's IDE state stays in Antigravity. Axiom is the missing connective tissue: every surface either writes through Axiom's substrate or is read by Axiom's transcript-ingest backstop, and every surface receives an outbound injection of relevant memory at session start. One ledger, many surfaces, continuous self.

## 2) Problem / Opportunity

`prd-cross-tool-memory.md` answered: "how does Axiom *connect* to each external tool (Claude Code, Codex, JetBrains)?" That PRD's three integration paths assume one tool maps to one memory horizon. Field reality has at least three additional structural issues that paper doesn't address:

1. **Within-vendor surface fragmentation.** A user moves between ChatGPT (web), Codex (CLI), and OpenAI API custom apps within a single workday and a single OpenAI account. ChatGPT's web memory tool persists facts the model learns in the browser session. Codex CLI on the same account *cannot read those facts* — they live behind OpenAI's web product, not the user-facing surface. The user perceives this as a memory failure of Axiom; in fact it's a vendor structural choice that Axiom must compensate for. Same with Claude (web project context ⊄ Claude Code; Claude Code's `~/.claude/projects/` ⊄ web). Same with Gemini (web personalization ⊄ Gemini CLI / Antigravity IDE).

2. **Inbound vs outbound asymmetry.** For most vendor surfaces, *inbound* (vendor → axiom) is achievable through on-disk transcripts, optional provider memory APIs, or browser-side extensions. *Outbound* (axiom → vendor) is harder. Most surfaces have no MCP host, no instructions slot, no native injection point; only the conversation. So the wire shape is asymmetric by design, and the PRD must spell out the contract.

3. **Multi-provider multiplexer clients.** A growing class of clients (OpenCode, Aider, Continue, custom CLIs) are *vendor-agnostic*: one product routes to N providers via user-configured keys. Axiom integration for these is structurally different from per-vendor adapters: the *client* is one MCP host that emits transcripts on disk; the *provider* layer is hidden behind the client's routing config, and the binding question (which OpenAI account did this turn use?) requires hint propagation through the client. Without explicit vocabulary, this is a confusion magnet.

`prd-cross-tool-memory.md` PRD §11 promised "reconciliation with adjacent specs"; this PRD takes that scope and lifts it out, because it's substantive enough to need its own design treatment. The cross-tool PRD will then be trimmed (separate work) to reference this one.

## 3) Goals & Success Metrics

**Primary goal:** A single human's memory horizon is **continuous across surfaces of any vendor** they use and **routed correctly through multi-provider clients**, with explicit inbound + outbound contracts per surface.

**Success metrics:**

1. **Within-vendor continuity coverage.** For each tier-1 vendor (Anthropic, OpenAI, Google), ≥ 90% of fragments produced on *any* surface are recoverable via the cross-vendor query on *any other* surface within 24h.
2. **Inbound coverage per vendor.** Each tier-1 vendor's transcript-ingest backstop covers ≥ 95% of substantive sessions; the inbound failure mode is "no transcript on disk," not "transcript on disk but ignored."
3. **Outbound presence per surface.** Each tier-1 surface that supports any injection mechanism (MCP, system prompt, instructions, hook) receives the resume-hydration result on session start, < 200 ms p95 per `prd-cross-tool-memory.md` (no regression).
4. **Multi-provider client routing.** When a user's OpenCode session uses two providers in one workday (e.g., Anthropic + OpenAI), the ledger fragments carry the correct provider-account binding ≥ 95% of the time (best-effort: depends on client cooperation).
5. **Vendor-account binding accuracy.** When `prd-identity-and-bindings.md` is enabled, ≥ 99% of fragments emitted by within-vendor adapters resolve to a binding (level ≥ declared); ≥ 50% reach `oauth_owned`. (Tracked jointly with the bindings PRD.)
6. **Vocabulary stickiness.** Internal docs + AEOS adapter manifests use the locked vocabulary (§4.1) consistently after this PRD lands, measured by a `lint-vocab` script run on `docs/` + `axiom-extension.toml` files.

## 4) Scope: what this PRD owns

This PRD owns three things: **vocabulary**, the **inbound/outbound contract per surface**, and the **OpenCode-class multiplexer policy**. Everything mechanical (adapter scaffold, daemon, hooks, RPE intent) belongs to `prd-cross-tool-memory.md` and is referenced here.

### 4.1 Vocabulary (locked)

| Term | Definition | Examples |
|---|---|---|
| **Vendor** | The corporate entity owning a family of AI products. The unit of API + account + billing. | Anthropic; OpenAI; Google; Microsoft; xAI; Mistral. |
| **Surface** | The user-facing medium where a product is consumed. | web, desktop, cli, ide-plugin, mobile, api-direct, embedded. |
| **Product** | A `(vendor, surface, function)` tuple sold/distributed as a named thing. The unit a per-product adapter targets. | `(Anthropic, web, chat)` = claude.ai; `(Anthropic, cli, coding)` = Claude Code; `(OpenAI, web, chat)` = chatgpt.com; `(OpenAI, cli, coding)` = Codex CLI; `(Google, ide-plugin, coding)` = Antigravity. |
| **Provider** | A *routable backend* a client can call — almost always a `(vendor, api)` pair. The unit of OAuth + the unit a multi-provider client lists in its config. | `anthropic`, `openai`, `google-ai-studio`, `azure-openai`, `bedrock`. |
| **Surface family** | The set of all Products under one Vendor. Cross-surface memory is *intra-family* continuity. | Anthropic family = {claude.ai, Claude desktop, Claude Code, Anthropic API direct}. |
| **Multiplexer client** | A user-facing client product that talks to multiple providers, selected per-turn from user config. | OpenCode, Aider, Continue, axi-chat, neut-chat. |
| **Vendor account** | The user's identity at one vendor. Bound to an axi principal per `prd-identity-and-bindings.md`. Cross-vendor coherence depends on bindings. | `user@example.org` at Anthropic; same email at OpenAI = *different* vendor accounts unless bound. |

The vocabulary is intentionally narrower than common usage (which often blurs "provider" and "vendor"). The narrowness pays off: "the OpenAI provider routes to chatgpt.com when called from the web surface and to api.openai.com when called from a multiplexer client" is a precise statement after this locking.

### 4.2 Inbound vs Outbound contract (per surface)

For each `(vendor, surface)` cell:

- **Inbound:** how the surface's content reaches Axiom. One of: `mcp-native` (the surface is an MCP host and Axiom registers as a server), `transcript-ingest` (the surface writes a parseable transcript to disk), `provider-memory-bridge` (the vendor exposes an API for the memory state itself), or `none` (opaque to Axiom).
- **Outbound:** how Axiom-stored memory reaches the surface at session start / mid-session. One of: `mcp-instructions` (the surface respects MCP `instructions` field via prompt registry per `prd-cross-tool-memory.md §5.7`), `system-prompt-augmentation` (Axiom prepends content to the surface's system prompt — requires client cooperation), `hook` (the surface fires a SessionStart hook Axiom can register against), `manual-paste` (the user copy-pastes — explicit fallback), or `none`.

The cell value is a single string like `mcp-native | mcp-instructions` (inbound | outbound). The §6 per-vendor matrix populates every cell.

### 4.3 Three integration patterns

**Pattern A — Surface-pair continuity (within one vendor).** Two surfaces of the same vendor sharing one account. The hard parts: vendor accounts are often shared but per-surface memory state is not; OAuth scopes differ per surface. Solution: Axiom is the ledger of record; both surfaces contribute inbound; both receive outbound where feasible. Cross-surface coherence is by binding (per `prd-identity-and-bindings.md`).

**Pattern B — Cross-vendor continuity (across vendors).** A persona's memory unified across N vendors. The hard parts: each vendor account is independently bound; no cross-vendor identity primitive exists. Solution: the persona model in `prd-identity-and-bindings.md` is the unifier. Axiom-side queries scope by persona, not by vendor.

**Pattern C — Provider-routing-aware integration (multiplexer clients).** A single client product (OpenCode, Aider) reaches Axiom; the *provider* of each turn must be propagated. The hard parts: the client knows the provider (it routed), but the client must expose that info in the transcript or via MCP metadata. Solution: per-client contract; multiplexer client adapters use the `binding hint` mechanism (`prd-identity-and-bindings.md §5.5`).

### 4.4 OpenCode-class multiplexer policy

The user-locked posture (per the prior conversation):

1. **Schema alignment.** Axiom's `MemoryFragment` JSON shape, RPE intent vocabulary, and AEOS adapter manifest format track upstream OpenCode formats *where they exist*. When OpenCode introduces a schema for transcript metadata, Axiom contributes a compatible shape rather than inventing parallel structure. When Axiom moves first, we publish the schema in a way OpenCode can adopt.

2. **MCP host compatibility.** Axiom-Memory MCP works inside OpenCode's MCP host the same way it works inside Claude Code's. No OpenCode-specific code path; OpenCode is "just another MCP host" architecturally.

3. **Upstream registry contribution.** Per-provider AEOS adapters (anthropic, openai, google) are published in a form OpenCode can consume — the same vendor-account binding metadata, the same transcript-ingest hints. If OpenCode runs a registry, Axiom contributes adapter manifests upstream.

4. **NOT "preferred client" framing.** Axiom's docs do not position OpenCode (or any multiplexer client) as the preferred / recommended client. Axiom is provider-agnostic and client-agnostic. The reason to be friendly: ally vs ecosystem competition is the right relationship, but pinning a "preferred" client creates lock-in that contradicts the substrate-not-tool positioning. **Excluded from scope:** OpenCode-specific developer-onboarding doc, mutual-funnel marketing assets, exclusive optimizations.

## 5) Relationship to adjacent docs: explicit delineation

| Concern | Lives in | This PRD says |
|---|---|---|
| Three integration paths (MCP-native / transcript-ingest / federated peer) | `prd-cross-tool-memory.md §5` | Consumes; never re-specifies. |
| Per-tool AEOS adapter pattern (`axi-memory-adapter-{tool}`) | `prd-cross-tool-memory.md §5.1` | Consumes; per-vendor adapters follow the same scaffold. |
| L2 ingest daemon | `prd-cross-tool-memory.md §5.2` | Consumes; daemon watches every surface's transcript path identically. |
| Resume-on-start hook | `prd-cross-tool-memory.md §5.3` | Consumes; outbound surfaces with hook support use this primitive. |
| Provider memory bridge (Anthropic memory tool, etc.) | `prd-cross-tool-memory.md §5.5` | Consumes; this PRD adds the **per-vendor matrix** that tracks which bridges exist. |
| External-account → axi principal mapping | `prd-identity-and-bindings.md` | Consumes; binding resolution gates this PRD's cross-surface coalescing. |
| Persona model | `prd-identity-and-bindings.md §5.3` | Consumes; Pattern B (cross-vendor) is the persona's primary use case. |
| `accountable_human_id` mandate | ADR-035 | Consumes; bindings resolve the value at write time. |
| MIRIX cognitive-type tagging | `prd-cross-tool-memory.md §5.6` | Consumes; surface-derived fragments tag the same way regardless of vendor. |
| Outbound injection via MCP instructions | `prd-cross-tool-memory.md §5.7` + `prd-prompt-registry.md` | Consumes; per-vendor templates live in the prompt registry. |

## 6) Per-vendor surface matrix

For each tier-1 vendor: surfaces, vendor-account semantics, inbound/outbound contract, known limits. Cells are populated as best understood at 2026-05-14; the matrix is **expected to drift** and should be re-attested per release.

### 6.1 Anthropic

| Surface | Product | Vendor account scope | Inbound | Outbound | Notes |
|---|---|---|---|---|---|
| web | claude.ai | Anthropic account | `transcript-ingest` (browser extension future; `none` today) | `manual-paste` today; `system-prompt-augmentation` when project-system-prompt API stabilizes | Project context is per-project; not visible to non-Anthropic surfaces today. |
| desktop | Claude desktop app | Anthropic account | `transcript-ingest` (app log path) | `mcp-instructions` (desktop hosts MCP) | Aligned with claude.ai's project state. |
| cli | Claude Code | Anthropic account | `transcript-ingest` (`~/.claude/projects/*.jsonl`) — already shipped | `mcp-instructions` + `hook` (SessionStart) — already shipped | Reference implementation for the cross-tool path. |
| api-direct | Anthropic API + Anthropic memory tool | Anthropic account | `provider-memory-bridge` (when stable) | `system-prompt-augmentation` via custom apps | Anthropic memory tool is the inbound bridge target. |

### 6.2 OpenAI

| Surface | Product | Vendor account scope | Inbound | Outbound | Notes |
|---|---|---|---|---|---|
| web | chatgpt.com (ChatGPT memory) | OpenAI account | `none` (server-side memory is opaque) | `manual-paste` | ChatGPT memory is intentionally walled per OpenAI policy. Document the limit honestly. |
| desktop | ChatGPT macOS/Windows app | OpenAI account | `none` today | `manual-paste` | Same opacity as web. |
| cli | Codex CLI | OpenAI account | `transcript-ingest` (`~/.codex/sessions/*.jsonl`) — shipped 0.16.0 | `mcp-instructions` (Codex CLI MCP host) | Codex CLI is the highest-leverage OpenAI surface for inbound. |
| ide-plugin | (various, VS Code Copilot uses different provider routing internally) | mixed | `none` today | `none` | VS Code Copilot is structurally an Azure-OpenAI provider, not OpenAI surface. |
| api-direct | OpenAI API direct | OpenAI account | `provider-memory-bridge` (none today — no managed-memory product) | `system-prompt-augmentation` via custom apps | Custom apps wire Axiom via system-prompt injection. |

### 6.3 Google

| Surface | Product | Vendor account scope | Inbound | Outbound | Notes |
|---|---|---|---|---|---|
| web | gemini.google.com | Google Workspace / consumer Google | `none` (server-side personalization opaque) | `manual-paste` | |
| desktop | Gemini desktop / Workspace integrations | Google Workspace | `none` | `none` today | |
| cli | Gemini CLI | Google account | `transcript-ingest` (file path TBD) | `mcp-instructions` (host capability TBD) | Adapter stubbed; not yet validated. |
| ide-plugin | Antigravity (Google IDE) | Google account | `transcript-ingest` (file path TBD) | `mcp-instructions` | Adapter stubbed; not yet validated. |
| api-direct | Gemini API direct | Google account | `provider-memory-bridge` (none today) | `system-prompt-augmentation` | |

### 6.4 Microsoft + others

| Vendor | Surfaces | Inbound posture | Outbound posture | Notes |
|---|---|---|---|---|
| Microsoft | Copilot (Word/Outlook/etc.), VS Code Copilot, Azure OpenAI | mostly `none` today (Copilot product memory is opaque) | `mcp-instructions` where MCP-hosted | VS Code Copilot is an MCP host; Axiom Memory registers there same as Claude Code. |
| xAI | Grok web, Grok API | `transcript-ingest` for web (extension future); none today | `manual-paste` | Lower-tier; deferred. |
| Mistral, Together, Replicate, Groq | API-direct only | N/A (no end-user surface) | N/A | Show up only as providers, not products. |

### 6.5 Open-source / multiplexer clients

| Client | Vendor | How it shows up | Notes |
|---|---|---|---|
| OpenCode | Multi (user-configured) | Single MCP host; transcript-ingest for non-MCP sessions; binding hints from client routing | The §4.4 policy applies in full. |
| Aider | Multi | `transcript-ingest` (`.aider.chat.history.md`); no MCP host today | Hint propagation via the transcript's per-turn provider marker. |
| Continue | Multi | MCP-host roadmap unclear; `transcript-ingest` per-IDE | Per-IDE binding hint. |
| axi-chat | N/A (Axiom-native) | Direct CompositionService writes; no adapter needed | Inside-out; not really a multiplexer in the same sense. |
| neut-chat | N/A (domain-consumer-native) | Direct CompositionService writes | Inside-out. |

## 7) Non-functional / constraints

- **Honesty about opacity.** Every cell marked `none` is documented as such on the user-facing reference page. Axiom does *not* claim cross-surface continuity for opaque surfaces; the user is told plainly where the gaps are.
- **Drift attestation.** Each vendor's row is re-attested at each Axiom release. If a vendor ships a new surface or a new API, the matrix is updated and the test suite covers the new surface before announcing.
- **Privacy isolation.** Vendor-bound bindings carry the binding's classification (per `prd-identity-and-bindings.md §7`). Surface-bound transcripts inherit the binding's classification. Sensitive surfaces (e.g., a CUI-tagged Codex session) do not federate cross-cohort without explicit policy opt-in.
- **Provider-routing fidelity.** A multiplexer client adapter that loses provider information on the transcript path (e.g., writes "model=claude-3-5-sonnet" but not "provider=anthropic") is a structural failure of that adapter — Axiom marks such fragments `binding_status=unresolved` and surfaces in `axi identity audit`.
- **Vocabulary lint.** A `scripts/lint-vocab.py` validator (deliverable in this PRD's spec phase) checks docs + manifest files for the locked vocabulary (vendor / surface / product / provider). Goal: keep the conceptual model from drifting after this lock.

## 8) Open questions

1. **Web-surface inbound.** Browser-extension-based transcript capture for the web surfaces (claude.ai, chatgpt.com, gemini.google.com) is technically feasible but politically charged (vendor ToS reads tend to discourage scraping their own UI). Decide: ship Axiom-branded extensions? Document third-party extensions that work? Discourage? Decision deferred to post-impl per-vendor review.
2. **System-prompt augmentation outbound.** For custom apps + API-direct surfaces, Axiom can prepend retrieved memory to the user's system prompt. Two API-shape options: (a) Axiom serves a `GET /system-prompt-context` HTTP endpoint the custom app fetches; (b) Axiom ships an SDK shim the custom app imports. Lean: (a) for portability; revisit when first custom-app integration is real.
3. **Provider-routing hint format.** Multiplexer clients currently emit provider info inconsistently. We should publish a small spec ("Axiom multiplexer provenance hints") that multiplexer clients can implement to give us cleaner hints. Owned by `spec-cross-tool-memory.md` (does not exist yet — landed alongside this PRD's impl phase).
4. **Per-surface RPE intents.** Should we have surface-specialized resume intents (e.g., `resume.codex_cli_repo_context` vs `resume.claude_code_session`) or a single `resume.task_context` for all? Lean: single intent + per-surface adapter shapes the result. Revisit if surface-specific signal proves load-bearing.
5. **When does cross-vendor coalescing become a federation question?** Today a persona is a local-ledger construct. When two laptops with different bindings represent the same persona, ADR-027 federation handles propagation. But cross-vendor IS *still* one persona on one device — does it need any federation primitive at all, or stays a single-ledger query concern? Lean: single-ledger; bindings make the join key explicit.

## 9) Timeline

| Phase | Deliverable | Window |
|---|---|---|
| Phase 0 (now) | This PRD + `prd-identity-and-bindings.md` + trim of `prd-cross-tool-memory.md`. Design-only, no code. | This branch |
| Phase 1 | Spec — `spec-cross-surface-memory.md` (the vocabulary, the matrix, the multiplexer-provenance hints) + complementary `spec-identity-bindings.md`. | Pre-0.18 |
| Phase 2 | Implementation: Codex MCP-instructions outbound + Codex SessionStart hook (within-Anthropic precedent extended to within-OpenAI). Vendor-family identity providers for Anthropic + OpenAI + Google (per `prd-identity-and-bindings.md §11`). | 0.18 or 0.19 |
| Phase 3 | Multiplexer client adapters for OpenCode (first-class) + Aider (best-effort). Vocabulary lint script. Matrix attestation harness. | 0.19 |
| Phase 4 | Browser-extension inbound for tier-1 web surfaces (claude.ai, chatgpt.com, gemini.google.com), governed by vendor-ToS review. | 0.20+ |
| Phase 5 | Custom-app `GET /system-prompt-context` HTTP endpoint + SDK shim; outbound for API-direct surfaces. | 0.20+ |

## 10) Acceptance & rollout

- **Acceptance signal:** A user with a persona bound to OpenAI + Anthropic + Google accounts uses ChatGPT (web), Codex CLI (terminal), Claude Code (terminal), and Antigravity (IDE) in one workday. At the end of the day, `axi memory search` against the persona returns substantive fragments from at least 3 of the 4 surfaces. `axi identity audit` shows ≥ 95% binding resolution.
- **Rollout:** Per-vendor opt-in. Each vendor row of the matrix ships independently. The matrix attestation harness runs in CI at each release; a vendor row whose attestation fails blocks the release of that vendor's adapter.
- **Backwards compatibility:** Existing Claude Code + Codex transcript-ingest paths are unchanged. Per-vendor identity providers ship as additive `IdentityProvider` extensions; absence of a binding falls back to current behavior (write succeeds, `accountable_human_id` from active persona).

## 11) Risks

| Risk | Mitigation |
|---|---|
| Vendor ToS evolves to block transcript capture (browser extension or on-disk parsing) | Per-vendor legal review at each phase; documented opt-out per matrix cell; honesty about opacity is itself the mitigation. |
| Multiplexer-client schema drift outpaces our adapter | Publish a stable multiplexer-provenance hint spec; version the adapters; CI matrix attestation. |
| Outbound injection (system-prompt-augmentation) leaks across personas | Per-persona scope at hydrate time; classification gates per `spec-classification-boundary.md`; opt-in per surface. |
| "Cross-surface" branding gets confused with "cross-tool" in onboarding | The vocabulary lint and onboarding doc revision land in Phase 1; the two PRDs cross-reference one another explicitly. |
| Surface taxonomy itself becomes obsolete (a new modality lands not fitting web/desktop/cli/ide-plugin/mobile/api) | Surface enum is extensible; matrix accommodates new surface kinds without schema migration. |

## 12) Contacts & links

- Product lead: Benjamin Booth (no-reply@axiom-os.ai)
- Engineering lead: same
- **Reviewer reading order:** `prd-cross-tool-memory.md` (foundational integration paths) → `prd-identity-and-bindings.md` (binding substrate) → this PRD → vendor-specific extension PRDs as they land.
- Sibling PRDs: `prd-cross-tool-memory.md`, `prd-identity-and-bindings.md`
- Tech spec (deferred): `docs/specs/spec-cross-surface-memory.md` — lands when wire format stabilizes per user's "PRD first" lean.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
