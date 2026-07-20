# PRD: `axiom.mcp_gateway` — Capability-Gated MCP

**Status:** Draft (2026-05-31)
**Owner:** Benjamin Booth
**Companion ADR:** [ADR-055](../adrs/adr-055-unified-governance-fabric.md) (governance fabric umbrella)
**Companion Spec:** [spec-governance-fabric.md](../specs/spec-governance-fabric.md) §1 envelope, §3 connector shape, §4 receipts
**Primitive class:** AEOS built-in extension (`axiom.extensions.builtins.mcp_gateway`)
**Agent:** GATEWAY (Adapter + Sensor) — wraps every MCP tool call in an action envelope
**Sibling:** `axi keep` (capabilities), `axi audit` (verdicts), `axi notifications` (RACI proposals)

---

## 1. Elevator Pitch

`axi mcp wrap <server>` takes any existing MCP server — `mcp-server-github`, `mcp-server-postgres`, `mcp-server-slack`, your custom MCP — and produces a capability-gated drop-in replacement. Every tool call from any harness (Claude Code, Cursor, Aider, Cline, OpenHands, Goose, hermes, OpenClaw, custom) is wrapped in an `ActionEnvelope`, decided by GUARD, witnessed by a receipt, routable by classification, and revocable per-(agent, tool, resource) without killing processes. Harnesses don't change. MCP servers don't change. The operator gains a single policy + audit + revocation plane across every agent action in their org.

## 2. Problem / Opportunity

### What's broken today

- **No policy across MCP servers.** Each server enforces what its author wrote — typically nothing beyond "is the token valid." A coding agent given an MCP-server-GitHub token has god-mode the moment it speaks the protocol.
- **No shared audit trail.** Each server logs in its own format, in its own location. "What did the agent do to our codebase yesterday" is log archaeology across N servers.
- **No revocation granularity.** To take a capability away from one agent on one tool you rotate the token, which kills every agent using it. Real incident response cannot operate this way.
- **No classification routing.** A tool call touching CUI or 10 CFR 810 material has no platform mechanism to refuse routing through a non-cleared MCP server.
- **No federation.** Two organizations whose agents want to interact through MCP cannot establish trust beyond "we exchanged tokens by email." Bounded, time-limited cross-org tool access has no protocol.
- **No proposal/graduation.** A novel tool call has no path to "ask the human once, then graduate to autonomous after N approvals." Each harness rolls its own (badly) or doesn't try.

### Why now

- MCP is the de facto agent-tool protocol; install base is large and growing. The wrapping pattern works **without changing MCP itself** — we leverage Anthropic's standard.
- Every governance primitive needed already shipped: GUARD (axi audit), KEEP (capability tokens), HERALD (proposal routing), PULSE (rotation), the action envelope and receipt schema (spec §1/§4).
- The federation primitives (ADR-027 cohorts + ADR-028 trust graph) are mature enough to back A2A-style cross-org tool use.
- No competing harness ships this; the moment one team has it, the build-vs-buy math for every team after favors adopting it.

## 3. Goals & Success Metrics

**Primary goal:** Any organization running any MCP-speaking agent harness can wrap their MCP server stack in one command, gain capability-bounded tool calls + queryable audit + granular revocation, and operate the resulting plane without per-server policy code.

**Success metrics (post-implementation):**

| Metric | Target |
|---|---|
| `axi mcp wrap <server>` produces a working gateway with no MCP-server source change | 100% across the 10 most-installed MCP servers at v1 |
| Policy decision overhead added per tool call (p99 cached) | < 8 ms |
| Policy decision overhead (p99 cold, novel call class) | < 60 ms |
| Receipt coverage — every tool call produces a verdict receipt | 100% by construction (lint enforces) |
| Granular revocation latency — "revoke agent X's capability on tool Y" to next blocked call | < 1 second |
| Cross-harness coverage at v2 | Claude Code, Cursor, Aider, Cline, OpenHands, Goose, hermes, OpenClaw, custom |
| Federation handshake — peer cohort agent gains scoped capability against our MCP | end-to-end demo for two cohorts |

## 4. Key Users / Personas

- **Platform/IT operator** ("Dana") — runs the agent plane for an org of 50–5,000 engineers; needs policy + audit + revocation across every harness in use.
- **Agent harness consumer** ("Engineer Eve") — writes prompts, doesn't care what's under the MCP socket; benefits from "agent can't accidentally push to prod" by virtue of policy.
- **Compliance officer** ("Auditor Aiyana") — needs SOC2 / HIPAA / FedRAMP-defensible records of what every agent touched, under whose authority, with what classification.
- **Incident responder** ("SRE Sam") — needs to revoke a capability from one agent on one tool within seconds, without killing the agent process or the harness.
- **Federation peer** ("Vendor Vega") — wants to send a bounded support agent into a customer's environment to triage; needs cross-cohort capability + audit chain on both sides.

## 5. Scope — Key Capabilities

### 5.1 The wrap command

```
axi mcp wrap <server-name> [--policy policy.toml] [--mode inline|sidecar|proxy]
                            [--cohort <cohort-id>] [--classification-ceiling internal]
```

`<server-name>` is either a binary on PATH (`mcp-server-github`) or a registry kind from the installed extensions. The wrap produces a gateway endpoint speaking MCP on the front and consulting GUARD on the back. Three deployment modes:

- **inline** — gateway runs in the same process as the harness's MCP client; tool calls pass through a local function.
- **sidecar** — gateway runs as a separate process on the same host; the harness points at the sidecar's socket.
- **proxy** — gateway runs centrally (e.g. in a K8s pod); many harnesses share one gateway.

**Acceptance:** the top 10 MCP servers (github / gitlab / postgres / mysql / slack / sentry / linear / brave-search / filesystem / shell) all wrap cleanly with no source change and a stock `policy.toml`.

### 5.2 Policy compilation

`policy.toml` declares per-tool per-resource per-classification rules. Compiled at wrap time into AUTHZ `Policy` rows. A minimal policy:

```toml
[[rule]]
name = "github-read-only-by-default"
tool = "github.*"
resource = "github://*"
disposition = "permit"
verbs = ["get_*", "list_*", "search_*"]

[[rule]]
name = "github-writes-propose-to-human"
tool = "github.*"
verbs = ["create_*", "update_*", "merge_*", "delete_*"]
disposition = "propose"

[[rule]]
name = "no-force-push-ever"
tool = "github.push"
match = "force == true"
disposition = "deny"
priority = 100
```

**Acceptance:** policy compiles, every rule round-trips to/from the `authz.policies` table, the `axi mcp policy lint` command catches conflicts + unreachable rules.

### 5.3 Capability presentation

Every wrapped tool call carries a KEEP-issued capability token in the envelope. The token states `(actor, intent_scope, resource_scope, classification_ceiling, ttl, max_uses)`. The gateway:

1. Reads the incoming MCP tool call.
2. Loads or mints the actor's capability for this call class (KEEP).
3. Builds an `ActionEnvelope`.
4. Calls `authz.decide(envelope)`.
5. Routes per `verdict.next_action_for_caller` — proceed / abort / propose / await human.
6. On proceed, forwards to the underlying MCP server; on the response path, writes a receipt fragment.

**Acceptance:** the `no_action_without_authz` lint passes against the gateway codebase; fuzz tests confirm no path forwards to the underlying server without a decide() call.

### 5.4 Revocation surface

```
axi mcp revoke --actor @agent:claude-code-foo --tool github.push --reason "incident-1234"
axi mcp revoke --capability-id cap-abc123
axi mcp revoke --cohort vendor-x --all   # incident response
```

Revocation is **synchronous** at the gateway — the next call from the revoked (actor, tool) tuple is denied within the time it takes for the gateway to read the revocation row (sub-second in v1). No process restart required.

**Acceptance:** revoke command + next-call denial confirmed in < 1 second p99 in distributed-mode tests; revocation receipts queryable via `axi audit`.

### 5.5 Classification routing

A tool call inherits classification from the envelope's `context_fragment_id`, the targeted resource (`resource.classification`), and the calling actor's role. The gateway refuses to forward an envelope whose classification exceeds the underlying MCP server's declared `classification_ceiling`. The ceiling is set per-server at wrap time.

This is the same helper PULSE + HERALD consume; built once in `axiom.governance.classification`.

**Acceptance:** wrapping a public-tier server with `--classification-ceiling internal` followed by a CUI-tagged call returns `deny` with the correct rationale; the rationale is reproducible via `axi audit explain`.

### 5.6 Federation: cross-cohort capability-bound tool use

A peer cohort's KEEP mints a capability bound to `(verb, resource_pattern, classification_ceiling, ttl, max_uses)` signed under their cohort key. Our WARDEN verifies the signature + trust score; our GUARD decides under our cohort policy + the presented capability; our gateway forwards to the underlying MCP server; receipts land in both cohorts' audit streams.

**Acceptance:** demo D2 — vendor cohort's support agent gains 1-hour read-only capability on incidents in our project, files findings, both sides hold matching receipt chains.

### 5.7 RACI graduation for novel tool calls

A tool + resource pair never authorized by an actor before defaults to `propose_to_human`. HERALD routes the proposal to the operator's preferred channel; the human approves or denies; after N approvals (graduation threshold) the verdict becomes `permit` without prompting. Denials reset the counter.

This is the same graduation surface authz already exposes; the gateway is its consumer.

**Acceptance:** novel tool call returns `propose_to_human` once, then after configured N approvals the same actor + tool + resource class returns `permit`; denial resets.

### 5.8 Observability surface

```
axi mcp tail --since 10m                # live tool-call stream
axi mcp stats --by tool --since 24h     # per-tool call/deny/propose counts
axi mcp explain <receipt-id>            # delegates to axi audit explain
```

The `tail` output is the operator's debugging window: every envelope + verdict + capability + forwarded response, redacted per classification.

**Acceptance:** the tail stream renders verbatim from the same receipt fragments `axi audit list` queries; redaction is enforced (a CUI-classified response body is replaced with a placeholder + receipt id for the cleared operator to fetch separately).

### 5.9 Receipt-bound responses

The gateway returns to the harness a tuple of `(tool_response, receipt_fragment_id)`. Harnesses unaware of the receipt id treat it as no-op metadata; AEOS-aware harnesses link the receipt into their own memory store so downstream actions can name their provenance parent. This is how a chain of agent actions assembles a queryable provenance graph spanning multiple harnesses.

**Acceptance:** an end-to-end test wires Claude Code → wrapped GitHub MCP → wrapped Postgres MCP and proves `axi audit chain <last-receipt>` walks back to the human prompt that started it.

## 6. Non-Functional / Constraints

- **MCP protocol fidelity.** The gateway speaks MCP correctly; non-AEOS-aware harnesses do not observe behavioral changes other than added latency and denied calls.
- **Latency budget.** p99 8 ms cached, 60 ms cold (§3 metrics).
- **No new process privileges.** The gateway runs at the same trust level as the underlying MCP server it wraps; it does not elevate.
- **Backpressure.** RACI proposals queue per actor; if HERALD is unreachable, the gateway returns `await_human` and the harness handles UX (default: surface as tool error with retry hint).
- **Sealed-state honesty.** If GUARD or KEEP is unreachable, the gateway fails closed by default with a clear error; an operator-set `--fail-open-for-class read.*` allows operating in degraded mode on a documented subset.

## 7. Timeline (high level)

| Phase | Scope | Target |
|---|---|---|
| Phase 0 | This PRD + tech spec | 2026-06 |
| Phase 1 | `axi mcp wrap` + inline mode + policy compilation + receipt writing | 2026-07 |
| Phase 2 | Sidecar + proxy modes; revocation surface; tail stream | 2026-07 → 2026-08 |
| Phase 3 | Top-10 MCP-server interop matrix + the per-server policy.toml templates | 2026-08 |
| Phase 4 | Classification routing + RACI graduation hookup | 2026-08 → 2026-09 |
| Phase 5 | Federation handshake (cross-cohort capability via WARDEN) | 2026-09 → 2026-10 |
| Phase 6 | Public reference deployment + adoption playbook | 2026-10 → 2026-11 |

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Latency overhead disqualifies for tight-loop tool use | Aggressive verdict caching; p99 monitored every phase; document the budget |
| Policy authoring is too hard for typical operators | Ship per-server templates (top-10 MCP servers) with sane defaults; `axi keep mint` from English (forward-looking) becomes the everyday path |
| MCP protocol drift | Treat MCP as upstream; reflect changes downstream within one minor release |
| Wrapping breaks edge-case servers that depend on undocumented protocol behavior | Per-server adapter shims; tracked in the interop matrix |
| Federation handshake fails on real-world clock skew / network partitions | Trust-graph + capability TTL semantics tested under fault injection in Phase 5 |

**Open questions:**

- Should the gateway expose its own MCP server surface (so harnesses see "the axiom gateway" as a single MCP server enumerating wrapped tools), or stay one gateway per wrapped server? The former is more ergonomic; the latter is closer to the protocol. Likely both, selected at wrap time.
- How does the gateway handle long-running streaming tool responses (e.g. shell exec)? Receipt-per-chunk or receipt-per-stream-with-summary?
- Sidecar discovery — service mesh integration vs operator-configured socket paths.

## 9. Acceptance & Rollout

**Sign-off:**
- Engineering: Benjamin Booth
- Product: Benjamin Booth

**Rollout plan:**
1. Phase 0–1 land on `feat/mcp-gateway` branch; Phase 1 cuts a minor.
2. Phase 2–3 each cut a minor; per-server interop tests gate the cut.
3. Phase 4 cuts a minor co-released with the classification-routing helper used by HERALD.
4. Phase 5 cuts a minor co-released with WARDEN's federation handshake work.

**Rollback criteria:**
- Latency p99 degrades > 2× target → throttle policy evaluation + cache verdict more aggressively + alert.
- Any path forwards to the underlying MCP server without a decide() call → emergency revert + post-mortem.

## 10. Contacts & Links

- Spec: `spec-governance-fabric.md` (envelope, receipts, decide API)
- AUTHZ engine: shipped in axiom 0.27.0 — `axi audit list / show / chain / causes / graduation / explain / lint / healthcheck`
- KEEP capabilities: shipped in axiom 0.26.x (capability storage); outbound-call wrapping + minting UX in flight
- HERALD: spec in flight
- PULSE: spec in flight (rotation-driven capabilities)
