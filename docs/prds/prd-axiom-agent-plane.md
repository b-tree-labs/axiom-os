# PRD: `axiom.agent_plane` — Cross-Harness Governance, Observability, and Capability UX

**Status:** Draft (2026-05-31)
**Owner:** Benjamin Booth
**Companion ADR:** [ADR-055](../adrs/adr-055-unified-governance-fabric.md)
**Companion Specs:** [spec-governance-fabric.md](../specs/spec-governance-fabric.md), [prd-axiom-mcp-gateway.md](prd-axiom-mcp-gateway.md)
**Primitive class:** AEOS built-in extension (`axiom.extensions.builtins.agent_plane`) + UI surface
**Agent:** PLANE (Steward + Sensor + Generator) — the operator-facing face of the governance fabric

---

## 1. Elevator Pitch

The Agent Plane is the single operator surface for *every* agent action across *every* harness in an organization. One inbox for proposals across Claude Code, Cursor, Aider, Cline, OpenHands, Goose, hermes, OpenClaw, custom harnesses. One audit stream queryable by classification, intent, actor, cohort. One capability marketplace where a person — or another agent — mints time-bounded, scope-bounded, count-bounded action tokens in plain English. One trust graph spanning organizations. Built on the same envelope + receipt + capability primitives every other Axiom extension consumes, so it composes rather than replaces.

## 2. Problem / Opportunity

### What's broken today

- **No cross-harness operator surface.** A company running multiple agent harnesses has multiple operator dashboards, multiple log formats, multiple notification queues, multiple permission models. The IT/platform team operates a fractured fleet by hand.
- **No capability-grant UX.** When a person wants to delegate a bounded action to an agent ("run the deploy once, then expire"), the actual mechanism is "share an API key" or "give the agent a long-lived token." Bounded delegation has no first-class UX anywhere.
- **No agent-to-agent capability passing.** Agent A wants Agent B to do one step on its behalf. Today: A shares its own credentials. Or invents an ad-hoc handshake. The provenance chain is unrecoverable.
- **No multi-tenant agent ops at scale.** An org with 200+ engineers running agent harnesses has no per-team policy, no shared audit, no per-(team, tool, resource) revocation surface.
- **No time-travel debugging.** "Why did the agent do this yesterday" is unreproducible. Each harness's logs are insufficient on their own; nothing reconstructs the cross-harness chain that led to the decision.
- **No federation across organizations.** Two companies whose agents want to interact safely — vendor support, regulator oversight, peer research collaboration — have no protocol-level path. Every integration is a 6-month MOU + manual data sharing.

### Why now

- The wrap-MCP capability (`prd-axiom-mcp-gateway`) instruments every tool call with an envelope + receipt; the Agent Plane is the consumer of that stream that makes operating across harnesses tractable.
- Every governance primitive needed is shipped or specced: GUARD verdicts + audit, KEEP capabilities, HERALD notifications, PULSE schedules, WARDEN federation handshake.
- Cross-harness portability is the unsolved problem for the platform/IT layer; harnesses themselves are commoditizing.
- The federation primitives (ADR-027 + ADR-028) are mature enough to make cross-org agent interaction real, not aspirational. No other harness ships this.

## 3. Goals & Success Metrics

**Primary goal:** A platform operator running 5+ agent harnesses across an org of 100+ engineers can govern, observe, audit, and federate every agent action from one surface, without changing the harnesses themselves.

**Success metrics (post-implementation):**

| Metric | Target |
|---|---|
| Harnesses observable from one operator surface | 8+ at v1 (Claude Code, Cursor, Aider, Cline, OpenHands, Goose, hermes, OpenClaw) |
| Per-team policy granularity | (team, tool, resource_pattern, classification, time_window) |
| Capability mint UX — plain English → typed capability | end-to-end demo working at v1; auditor-review-ready receipt at v2 |
| Agent-to-agent capability passing with provenance chain | end-to-end demo at v1 |
| Federation handshake — cross-cohort agent interaction | end-to-end demo (two cohorts) at v2 |
| Cross-harness chain reconstruction depth | 10+ hops walked correctly via `axi audit chain` |
| Compliance-export — "every agent action touching CUI last 30 days" as a signed JSON record | < 30 seconds for a 30-day window |

## 4. Key Users / Personas

- **Platform operator** ("Dana") — runs the agent plane for the org; needs the one-surface governance + audit + revocation.
- **IT/security admin** ("Ravi") — needs role-based policy across teams + audit-defensibility for compliance reviews.
- **Capability-granter** ("Manager Mei") — needs to delegate a bounded action to an agent or human in plain English ("let @bobbi-bot rerun the deploy if CI passes — 2 hour TTL").
- **Compliance officer** ("Auditor Aiyana") — needs queryable, signed records of every action across every harness, exportable as a SOC2/HIPAA/FedRAMP defensible artifact.
- **Incident responder** ("SRE Sam") — needs granular revocation + cross-harness blast-radius visibility within seconds of an incident.
- **Federation peer** ("Vendor Vega") — wants their support agent to triage a customer incident under a bounded capability the customer issued.
- **Researcher** ("Lab Lakshmi") — wants to share bounded compute + data access across collaborating labs via federated capabilities.

## 5. Scope — Key Capabilities

### 5.1 The unified inbox

```
axi plane inbox [--actor @me] [--state pending|approved|denied]
                [--since 24h] [--cohort *] [--classification *]
```

Every RACI-proposal across every harness lands in one inbox. Threading by `(intent_class, resource, actor)`. Replies via inbox → HERALD-routed back through whichever channel the originating harness was using. The same fragment-store contract HERALD uses; the inbox is just a typed view.

Surfaces at v1: terminal (`axi plane inbox`), Slack adapter (HERALD-2), inbox web UI (a future Axiom UI extension); v2: mobile push.

**Acceptance:** an operator approves a proposal from Slack and the originating Claude Code session sees the decision within HERALD's delivery-receipt p95.

### 5.2 Capability mint UX — plain English → typed capability

```
axi keep mint --capability-from "let @bobbi-bot rerun the deploy if CI passes for 2 hours"
```

The LLM translates the English to a typed capability:

```toml
[capability]
actor          = "@bobbi-bot:internal"
intent_scope   = "deploy.rerun"
resource_scope = "ci://project-x/pipeline/*"
classification_ceiling = "internal"
ttl_seconds    = 7200
max_uses       = 1
condition      = "ci.status == 'passed'"
```

The operator sees the typed form for review before KEEP signs. The signed capability returns as a presentable token + a receipt fragment id. Slack-bot shape, web UI, CLI — same call.

**Acceptance:** auditors can be shown the English request, the typed capability, the operator's approval, the signed token, the receipt fragment, and every presentation of the token as one queryable chain.

### 5.3 Capability marketplace

Capabilities are shareable artifacts. A marketplace surface (CLI at v1, Slack-bot at v1.5, web UI at v2):

```
axi keep share <capability-id> --with @alice:org
axi keep redeem <token>                   # one-shot acceptance
axi keep delegate <capability-id> --to @writer-agent --max-uses 3
```

Capabilities compose: a capability for "review PR #1234" can spawn a sub-capability for "comment on PR #1234" with strictly-narrower scope. The provenance chain proves the delegation; the classification ceiling is monotonically tightened.

**Acceptance:** demo D4 — `/axi-grant "let @bobbi-bot rerun deploy if CI passes, 2hr TTL"` in Slack mints the typed capability, posts the receipt, bobbi-bot redeems on the next CI green, action lands, audit chain queryable.

### 5.4 Cross-harness observability

```
axi plane tail [--harness *] [--actor *] [--classification *]
axi plane stats --by harness,tool --since 24h
axi plane replay <run-id>                  # time-travel
```

Live tail of every action across every running harness session in the org. Stats by harness, tool, classification, denial rate, proposal rate. `replay` reconstructs the chain of envelopes + memory snapshots + verdicts that produced an action — including counterfactuals ("what if this rule had been active?").

Surfaces: CLI at v1; OpenTelemetry export at v1 (every receipt becomes a span); web UI at v2.

**Acceptance:** demo D6 — investigate a bad agent decision, walk the chain back to the human prompt, identify the rule + graduation state + classification at decision time, all in < 30 seconds.

### 5.5 Multi-tenant per-team policy

```
axi plane policy attach --team eng --policy eng-policy.toml --priority 10
axi plane policy attach --team security --policy security-policy.toml --priority 5
axi plane policy list --actor @alice:eng
```

Per-team `Policy` rows tagged with team membership. The same precedence + graduation machinery AUTHZ already uses; the agent plane is the team-shaped consumer view. Team membership resolves from an org identity source (Okta / Google Workspace / Microsoft Entra / static TOML at v1).

**Acceptance:** demo D5 — Cursor sessions for 200 engineers operate under team-scoped policy without per-session configuration; revoke command targeted at one team's tool surface lands in < 1 second.

### 5.6 Cross-organizational federation

```
axi plane federate --cohort vendor-x --trust-score 0.85
axi plane delegate-capability --cohort vendor-x \
                              --intent incident.triage \
                              --resource sentry://project-x/* \
                              --classification-ceiling internal \
                              --ttl 3600 --max-uses 100
```

Issuing a delegation to a peer cohort produces a capability signed under our cohort key. WARDEN handles the cross-cohort signature + trust-score verification; GUARD honors the delegation on inbound; receipts land in both cohorts.

**Acceptance:** demo D2 + D7 — peer agents act in our environment under bounded capability; both cohorts hold matching receipt chains; pulling the capability mid-incident revokes within < 1 second.

### 5.7 Compliance export

```
axi plane export --since 30d --classification CUI --signed --format soc2|hipaa|fedramp
```

Produces a signed JSON record of every action touching the queried classification in the given window, ready to hand to an auditor. The signature is a chain over the receipt fragment ids + the issuing principal + timestamps; verifiable without re-running the policy engine.

**Acceptance:** demo D3 — auditor receives a signed export, verifies the chain offline, asks `axi audit explain <id>` for any single decision, gets a human-readable narrative. Total time: < 5 minutes from request to answer.

### 5.8 On-call capability degradation (PULSE-driven)

```toml
[[plane.rotation]]
name = "primary-on-call"
actor_pattern = "@primary:sre"
capability_id_pattern = "cap-sre-baseline"
schedule = "weekly:mon-fri 09:00-17:00 PT"
escalate_to = "@secondary:sre"
escalation_ttl_seconds = 7200
```

When the primary is off-shift, PULSE auto-mints an upgraded capability for the secondary scoped to a configurable window; capabilities decay back to baseline automatically. The rotation TOML compiles to PULSE schedule rows + capability templates.

**Acceptance:** demo D8 — secondary's capabilities expand at the scheduled time, decay back after the configured TTL, every escalation captured as a receipt.

### 5.9 Receipt-as-portable-proof (forward-leaning)

Every receipt is a signed claim of `(actor, intent, resource, classification, decision, verdict, capability_chain, timestamps)`. Receipts compose. A pay-per-action billing surface, an agent-to-agent payment protocol, a cross-org SLA-enforcement layer — none of these are in scope for the Agent Plane v1, but the *receipt format* is the primitive that makes them possible later. Ship the format correctly and the ecosystem catches up.

**Acceptance:** the receipt schema is published as a typed spec; an independent verifier can validate a signed receipt offline.

## 6. Non-Functional / Constraints

- **Cross-harness fidelity.** The plane does not require harness source changes; AEOS-aware harnesses gain richer integration, but non-aware harnesses still get policy + audit via the MCP gateway.
- **Operator UX latency.** Inbox refresh, capability mint, revoke all complete in < 1 second p99 for in-org operations.
- **Signed-export determinism.** Two exports of the same query produce byte-identical signed records (modulo the signature timestamp).
- **Privacy posture.** The receipt fragments contain envelope contents which may include sensitive material; classification routing applies to the inbox + tail + replay surfaces.
- **Backwards compatibility.** Receipt + capability schemas use a versioned envelope; schema migrations land via `axi plane migrate` co-released with each minor.

## 7. Timeline (high level)

| Phase | Scope | Target |
|---|---|---|
| Phase 0 | This PRD + tech spec | 2026-06 |
| Phase 1 | Inbox + capability mint UX (CLI) + multi-tenant policy | 2026-07 |
| Phase 2 | Cross-harness tail + stats + OpenTelemetry export | 2026-08 |
| Phase 3 | Capability marketplace + composability + delegation | 2026-08 → 2026-09 |
| Phase 4 | Federation + cross-cohort capability + signed compliance export | 2026-09 → 2026-10 |
| Phase 5 | Web UI + Slack-bot adapter | 2026-10 → 2026-11 |
| Phase 6 | On-call capability degradation (PULSE-driven) + receipt-format publication | 2026-11 → 2026-12 |

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| English-to-capability translation produces dangerously wrong types | Operator review gate is mandatory before signing; type-checker rejects unconstrained scopes; gradual training surface (deny + reword) instead of denial-with-no-feedback |
| Federation handshake fails on real-world trust-graph edge cases | Conservative defaults (deny on any signature ambiguity); WARDEN test suite covers the documented failure modes |
| Compliance export grows unbounded as receipt corpus accumulates | Tiered export — recent + signed-summary vs full-historical-archive — both verifiable |
| Plain English capability-mint UX is too lossy / non-deterministic | Pair every English mint with the typed form shown back to the requester for inline correction; preserve both in the receipt |
| Operator inbox overwhelms the human | Categorize by intent class; auto-collapse by graduation; per-recipient preferences via HERALD |

**Open questions:**

- Per-team identity source resolution — Okta / Google Workspace / Microsoft Entra / static TOML? Probably all four as adapters in v1, behind an `axiom.org_identity` provider registry mirroring the secret-store + database providers.
- The capability marketplace's UX — does a capability look like a Slack message, a CLI token, a QR code, an email? Likely all four; the receipt is the canonical form.
- Receipt-format publication — when do we publish AEOS-receipt as a portable open standard separately from the rest of AEOS? Probably as adoption signal warrants; reading ADR-032's dual-track guidance.

## 9. Acceptance & Rollout

**Sign-off:**
- Engineering: Benjamin Booth
- Product: Benjamin Booth

**Rollout plan:**
1. Phase 0–1 land on `feat/agent-plane` branch; Phase 1 cuts a minor.
2. Phase 2 cuts a minor co-released with the MCP gateway Phase 1 — the tail consumes the gateway's receipts.
3. Phase 3 cuts a minor co-released with KEEP's mint UX + outbound-call wrapping work.
4. Phase 4 cuts a minor co-released with WARDEN federation Phase 4.
5. Phase 5 cuts a minor co-released with HERALD Phase 2 (Slack + web adapters).

**Rollback criteria:**
- English-to-capability translation produces a verified-wrong type that an operator approves and KEEP signs → halt the English mint UX, require typed-only minting, post-mortem before reopening.
- Cross-cohort federation handshake produces an unrecoverable verdict mismatch → revert WARDEN integration to read-only mode; alert peer cohorts.

## 10. Contacts & Links

- MCP gateway PRD: this PRD's twin; the gateway emits the receipts the plane consumes
- AUTHZ: shipped in axiom 0.27.0
- KEEP: capability storage shipped; outbound + minting UX in flight
- HERALD + PULSE: specs in flight
- WARDEN: federation handshake; spec in flight
- ADR-027 / 028 / 055 / 056
