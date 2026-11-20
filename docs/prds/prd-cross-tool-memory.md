# Axiom Memory — Cross-Tool Integration — Product Requirements

**Product:** Axiom Memory's outward integration surface — making external LLM **client tools** (Claude Code, Codex CLI, JetBrains AI plugin, VS Code Copilot, OpenCode, Aider, etc.) first-class consumers of the Axiom Memory substrate. Per the vocabulary lock in `prd-cross-surface-memory.md §4.1`, this PRD owns **client-tool integration paths**; within-vendor surface continuity and multi-provider multiplexer policy live in that sibling doc.
**Owner:** Ben Booth
**Status:** Draft (trimmed 2026-05-14 to delegate cross-surface + multiplexer scope to `prd-cross-surface-memory.md`)
**Created:** 2026-05-11
**Last updated:** 2026-05-14
**Related:**
- `prd-memory.md` — parent PRD; the substrate this surface consumes
- `prd-cross-surface-memory.md` — sibling PRD; owns vocabulary (vendor / surface / product / provider), within-vendor continuity, the per-vendor matrix, and multiplexer policy. **Read alongside this PRD.**
- `prd-identity-and-bindings.md` — sibling PRD; owns external-account → axi principal binding. Cross-tool fragments resolve `accountable_human_id` through this layer.
- `prd-prompt-registry.md` — L3 MCP instructions reuse pattern
- `prd-agents.md` — SCAN / TIDY / TRIAGE memory-observability roles
- `docs/specs/spec-memory.md` — normative tech spec for the substrate
- `docs/working/cross-tool-memory-guarantees-sketch.md` — design sketch this PRD formalizes
- `docs/adrs/adr-026` — single-master + peer delegations (ownership)
- `docs/adrs/adr-027` — federated memory (axiom:// URI, cohort registry, multi-sig)
- `docs/adrs/adr-028` — trust graph (peer reputation)
- `docs/adrs/adr-035` — human-principal binding (`accountable_human_id` mandate)
- `docs/specs/spec-aeos-0.1.md` — Agent Extension Open Standard (seven capability kinds)

---

## 1) Elevator pitch

Any LLM **client tool** a user touches — Claude Code, Codex CLI, JetBrains AI plugin, VS Code Copilot, multi-provider multiplexers like OpenCode — joins the Axiom Memory substrate via one of three integration paths: **MCP-native**, **transcript-ingest backstop**, or **federated peer**. Cross-tool memory becomes a guaranteed property of the workspace, not a per-tool feature. Unexpected host restarts (VS Code reboot, OS update, kernel panic) lose at most bounded context; new sessions resume warm.

**Scope boundary (locked 2026-05-14):** this PRD owns the **per-client adapter mechanics** (parser + service + hook AEOS capabilities) and the **three integration paths** themselves. Within-vendor surface continuity (Claude web ↔ Claude Code; ChatGPT web ↔ Codex CLI; Gemini web ↔ Antigravity), the per-vendor inbound/outbound matrix, and multi-provider multiplexer policy live in `prd-cross-surface-memory.md`. External-account-to-axi-principal binding lives in `prd-identity-and-bindings.md`. Both are prerequisites for this PRD's reach claims.

## 2) Problem / opportunity

A developer working across Claude Code, Codex, JetBrains AI, and one or two web LLMs in a single day operates on N disjoint memories. Each tool has its own conversation history; none share context with the others; provider-side memory (Anthropic memory tool, ChatGPT memory) is walled inside each provider. The user's mental model is "one persistent self across all my tools" — current reality fragments it.

Concrete failures fall out:

- **Crash loss.** A VS Code reboot mid-task kills the active Claude Code session; the new session starts cold with no memory of what was being worked on.
- **Cross-tool blindness.** A decision made in Codex on Tuesday is invisible to Claude Code on Wednesday.
- **Provider lock-in.** Anthropic memory tool persists context only inside Claude; ChatGPT memory only inside ChatGPT. Switching tools means losing context.
- **Silent failure.** When a memory write fails (model never called `append`, ingest daemon stuck, principal mismatched), nothing surfaces it — the gap is invisible until someone notices stale recall.
- **Model-discipline gap.** "Please call `axiom_memory_append` after substantive turns" is a soft instruction; no guarantee.

The opportunity: Axiom Memory's substrate is *already* designed to be the single source of truth across systems (per `prd-memory.md` §6: cryptographic provenance, federation-native, MIRIX taxonomy, classification-aware, sovereign extraction). The missing piece is the integration surface — adapters, ingest daemon, resume hooks, provider bridge — that wires each external tool into it.

## 3) Goals & success metrics

**Two guarantees the integration surface must deliver:**

1. **Active-memory guarantee** — any integrated tool always has a live, registered, writable axiom-memory MCP available to its model loop, *or* a transcript-ingest backstop covering it when the model can't reach MCP.
2. **Restart-safety guarantee** — an unexpected host restart loses at most bounded context: prior turns survive in the ledger, and new sessions resume warm.

**Operational definition of "guaranteed":** strong — eventual ingest with bounded delay (p95 < 5 min once daemon ships). Strict per-turn attestation requires A2A multi-sig and is a Phase 4+ ambition; pragmatic ("no silent gap > 1h") is the floor.

| Goal | Metric | Target |
|---|---|---|
| Active-memory for MCP-native tools | `axi dr` liveness probe pass rate | 100% of registered tools per check |
| Active-memory for transcript-only tools | Ingest lag (newest transcript line → ledger) | p95 < 5 min once daemon ships |
| Restart-safety | Session-resume eval suite (TBD) — % of expected fragments hydrated at new-session start | ≥ 90% on resume eval corpus |
| Silent-failure detection | Heartbeat-freshness check in `axi dr` | flags > 2h as ERROR; SCAN escalates within heartbeat window |
| Cross-tool coverage | Tools with at least one integration path | claude-code (shipped), codex, jetbrains, vscode-copilot, gemini, chatgpt-desktop |
| Provider memory bridge | Anthropic memory tool writes mirrored to axiom as `semantic` fragments | round-trip eval at < 30s lag |

## 4) Key users / personas

Per `prd-memory.md §4`, archetypes only. Cross-tool-specific concrete personas live in tool-specific extension PRDs.

| Archetype | Cross-tool relevance |
|---|---|
| Memory subject | Sees a unified ledger across every LLM tool they touch, not N disjoint histories. Retractions propagate across tools. |
| Scope operator | Configures which tools enroll in their cohort; sets per-tool privacy boundaries. |
| Extension developer | Writes a per-tool adapter as a scaffolded AEOS extension (`axi ext init`); gets ingest, registration, hooks for free. |
| Platform operator | Audits cross-tool ledger health via SCAN; configures privacy and retention per tool. |
| AI safety / compliance | Traces a decision across tool boundaries via a single signed fragment chain. |

## 5) Scope — key capabilities (MVP)

The MVP covers MCP-native and transcript-ingest paths for the six target tools; the federated-peer path (full A2A) is Phase 4+.

1. **Per-tool adapter extensions** — one AEOS extension per tool (`axi-memory-adapter-{tool}`) declaring `adapter` (parser), `service` (daemon role for this tool), and `hook` (resume-on-start where supported) capabilities in `axiom-extension.toml`. claude-code already shipped via `KNOWN_TOOL_PARSERS` dispatch; codex / jetbrains / vscode-copilot / gemini / chatgpt-desktop stubbed.

2. **L2 ingest daemon** — `axi memory daemon` as a launchd/systemd service. Watches each registered tool's transcript paths. Idempotent on `source_uuid`. Emits hourly heartbeat fragment. Per-tool position, lag, and last error stored in the artifact registry.

3. **Resume-on-start hook** — at session start in each MCP-native host that supports a `SessionStart` hook (Claude Code today), inject results of an RPE intent (`resume.task_context` — to be defined) into the model context. Soft fallback: MCP `instructions` (served from the prompt registry) nudges the model to call `axiom_memory_recent` itself.

4. **MCP liveness contract** — `axi dr` opens each registered tool's config and runs a `list_tools` roundtrip to verify the handshake. Flags stale registration with fix hint. Heartbeat-freshness check escalates via SCAN.

5. **Provider memory bridge** — for providers with a model-callable memory surface (Anthropic memory tool, Cursor local files, Codex local store), a proxy/parser mirrors provider writes to axiom as `semantic` fragments and (where API permits) hydrates the provider's surface from axiom on session start. Opaque server-side memories (ChatGPT, Gemini) documented as unaddressable until API opens.

6. **MIRIX type tagging at the write path** — provider mirrors land as `semantic` (extracted facts), sensitive content routes to `vault`, identity facts to `core`, ingested turns remain `episodic`. L0 write path accepts a `cognitive_type` argument; defaults to `episodic` for backward compatibility.

7. **`fragment.classification` optional schema field** — the one genuinely new schema addition. Carries downstream classifier signals (sensitivity, importance, topic tags). Default null; consumed by RPE / policy / retention once a classifier is in official production. See `prd-memory.md §6 — Cross-tool first` for the broader rule.

8. **SCAN extension for memory observability** — always-on agent that consumes heartbeat freshness, principal-pin drift, daemon lag, and audit/SQLite divergence signals from the artifact registry. TIDY runs hygiene (compaction, vacuum, dedup); TRIAGE handles escalation when self-heal can't auto-fix.

**Post-MVP:**

- A2A migration — cohort discovery replaces L1 registrars; subscriptions replace polling; multi-sig provenance enforced via WARDEN. The agent-loop publish event closes the model-discipline gap structurally.
- Ledger replication to a second peer (CRDT-merge under personal cohort).
- Tested rebuild drill (`axi dr --drill`) — monthly SQLite-from-audit reconstruction in temp dir + diff.
- Local write-queue with retry under `~/.axi/queue/` for offline write durability.

## 6) Distinctive bets

| Bet | What it gives us |
|---|---|
| **MCP-native + transcript-ingest backstop** | Two-path coverage: where the model cooperates we win directly; where it doesn't, the daemon folds the on-disk transcript. The model-discipline gap is closed structurally, not by hope. |
| **AEOS adapter pattern** | A new tool integration is a manifest, not a project. `axi ext init <tool>` scaffolds the adapter with `adapter` + `service` + `hook` capabilities. Sigstore-signed releases per AEOS. |
| **Provider memory as federated peer** | Anthropic memory tool, Cursor local store, etc. enroll under ADR-026's delegated-rights model. Axiom stays canonical master; providers become fast local caches with a sync, not competing systems. |
| **Resume via RPE intent + RAG hybrid search** | The hook expresses its hydration need as an RPE intent; ranking is relevance + recency from RAG. Reuses existing primitives; no new ranking system invented. |
| **Heartbeat + SCAN make silent stops loud** | Multi-tier liveness watchdog: MCP heartbeat → daemon check → launchd KeepAlive → notification. Each tier catches the one inside it. No silent loss. |
| **Honest about opaque limits** | ChatGPT memory and Gemini memory have no API access today; we document that limit explicitly rather than claim a false guarantee. |
| **Reuse, not invention** | The work is wiring of existing axiom primitives (CompositionService, MIRIX, RPE, RAG, prompt registry, artifact registry, trust graph, AEOS kinds). Net-new surface = per-tool parsers + one optional schema field. |

## 7) Non-functional / constraints

- **Performance:** Resume hydration < 200 ms p95 at session start. Daemon ingest lag < 5 min p95.
- **Privacy:** Per-tool transcript deny-list evaluated by the 4-scope policy engine (no new policy primitive). Sensitive transcripts route to `vault` scope.
- **Backward compatibility:** Claude Code adapter already ships; no changes to existing fragment schema. New adapters add stubs; existing ledger unaffected. `fragment.classification` is optional/nullable.
- **Observability:** Daemon state (position, lag, last error) and SCAN signals live in the artifact registry (no parallel observability store).
- **Air-gap:** Adapters and daemon work fully offline. A2A federation is optional and orthogonal.
- **Determinism:** Idempotent ingest is keyed on `source_uuid`; re-running an adapter on the same transcript is a no-op.
- **Profiles:** Edge (laptop watcher), Workstation (laptop + desktop CRDT-replicated cohort), Server (later phase, post-A2A).

## 8) Timeline (phases)

> **Authoritative sequencing lives in `docs/working/memory-roadmap.md`.** That doc replaces this §8 when conflicts arise. Treat the table below as a scoped preview of the integration-surface slices within the consolidated roadmap.

| Phase | Window | Deliverable |
|---|---|---|
| **Phase 0** (shipped 2026-05-06/07) | done | claude-code adapter via `KNOWN_TOOL_PARSERS`, MCP server, durable user-scope registrar, foreground `--watch` ingest, idempotent re-ingest on `source_uuid`, `axi dr` registration check, heartbeat installer scaffold (`heartbeat_install.py`) |
| **Phase 1** (current) | 1 wk | This PRD + first new adapter via TDD (codex first — already cross-tool validated). MIRIX cognitive-type tagging at the write path. `axiom_memory_instructions` template in prompt registry. |
| **Phase 2** | 2 wk | L2 ingest daemon as launchd/systemd unit. SCAN consumer of heartbeat freshness + daemon lag. `axi dr` liveness probe of each registered tool. Remaining adapters stubbed with parser contracts. |
| **Phase 3** | 2 wk | Resume-on-start hook for Claude Code (RPE intent `resume.task_context` defined + wired). Anthropic memory-tool proxy MVP (one-way: provider → axiom). |
| **Phase 4** | longer-arc | A2A migration begins — codex enrolled as first A2A peer. Subscriptions replace `--watch` polling. WARDEN multi-sig provenance verification (post-Vega extraction). |
| **Phase 5** | longer-arc | Ledger replication to second peer (CRDT-merge). `axi dr --drill` monthly rebuild test. Local write-queue under `~/.axi/queue/`. |

Phases 4–5 are foundation work; re-evaluate freeze posture per `feedback_freeze_foundation_during_delivery` against whichever deliverable is active at the time.

## 9) Risks & open questions

| Risk | Mitigation |
|---|---|
| JetBrains / VS Code Copilot don't expose a usable transcript or MCP surface | Document the limit; treat as L4 "best-effort" rather than guaranteed. Survey plugin APIs in Phase 2 before committing scope. |
| Provider memory tool API evolves (Anthropic memory tool is new) | Adapter version-pinned to the API; eval suite catches regressions; bridge is one-way until API matures. |
| L2 daemon process dies silently | Multi-tier watchdog: MCP heartbeat → daemon check → launchd KeepAlive → push notification. Fail-loud via SCAN. |
| Cross-tool ledger gets noisy at scale | TIDY hygiene compaction; `fragment.classification` seam enables importance-weighted retention later. |
| Principal mismatch silently splits memory across IDs | SCAN's principal-pin reconcile flags drift; opt-in install wizard pins explicitly at first use. |
| Model-discipline gap persists for opaque tools | Two-path coverage (MCP + transcript ingest); for opaque-server-side providers (ChatGPT, Gemini), document the limit honestly. |
| Daemon scope creep (foreground vs daemon vs `axi chat` startup) | Locked at Phase 2 decision (see open questions). |

**Open questions to lock by Phase 2 start:**

- **Daemon scope.** Per-user launchd/systemd unit only, or also a fallback foreground watcher in `axi chat` startup? (default leaning: both, daemon primary + foreground fallback)
- **Provider-bridge proxy topology.** Single shim MCP fronting both axiom and the provider's memory, or two registered MCPs with a sync agent? (default leaning: two MCPs + sync agent for separation of concerns)
- **Replication topology.** Laptop ↔ desktop only, or include a trusted hosted peer? (default leaning: laptop ↔ desktop first; hosted peer later as a cohort-defined extension)
- **Resume hook host surface.** Which hosts get an active hook in Phase 3? Claude Code first (`SessionStart` confirmed); Codex/JetBrains/VS Code surveys needed.

## 10) Acceptance & rollout

**Sign-off:** Ben Booth (product + eng).

**Engineering gate per phase:**

- **Phase 1:** new adapter ships with full TDD coverage (red → green per slice); ingest of a real transcript verified end-to-end; no regressions in existing test suite; MIRIX type promotion landed without schema migration required.
- **Phase 2:** daemon survives a forced kill + relaunch; heartbeat freshness check fires within window; SCAN escalation reaches operator log.
- **Phase 3:** resume hydration eval suite ≥ 90% on corpus; provider bridge round-trip < 30 s on Anthropic memory-tool reference workflow.

**Rollout:** Per-phase opt-in. Each adapter is enrollable independently via `axi memory register-mcp --tool <t>` (or per-tool registrar). Daemon optional in Phase 2 (foreground `--watch` remains supported). Resume hook opt-in per host in Phase 3.

**Rollback:** Adapters are additive; removing one disables ingest for that tool without affecting others. Daemon disable returns to foreground watcher behavior. Schema additions (`cognitive_type` explicit tagging, `fragment.classification`) default to existing semantics — no migration required.

## 11) Reconciliation with adjacent specs

- **`prd-memory.md`** — this PRD is the *outward integration scope*; `prd-memory.md` is the *substrate scope*. Composition is one-way: the substrate exposes `CompositionService` + ledger; this PRD's adapters write to that surface, the daemon reads transcripts and writes to that surface, hooks read recent and inject at session start.
- **`prd-cross-surface-memory.md`** — owns vocabulary (vendor / surface / product / provider), the within-vendor surface-pair pattern, the per-vendor inbound/outbound matrix, and the multi-provider multiplexer policy (OpenCode-as-ally). This PRD's per-tool adapters are *instances of* the patterns named there; the locked vocabulary applies inside this PRD too. Net: where surfaces of one vendor share an account but maintain separate memory state, the sibling PRD specifies the inbound/outbound contract; this PRD specifies the adapter mechanics that implement it.
- **`prd-identity-and-bindings.md`** — owns external-account → axi principal mapping, persona model, and the binding lifecycle. This PRD's adapters call `resolve_binding()` (per that PRD's §5.5) before write; unresolved fragments flag for backfill rather than blocking the write.
- **`prd-prompt-registry.md`** — L3 MCP `instructions` lives as a template (`axiom_memory_instructions`) in the prompt registry per `prd-prompt-registry.md §Use Cases`. Versioned, eval-able, swappable per tool via the layer-composition model.
- **`prd-agents.md`** — SCAN gains memory-observability jobs (heartbeat freshness, daemon lag, principal-pin reconcile, audit/SQLite divergence). TIDY gains memory hygiene (compaction, vacuum, dedup). TRIAGE handles escalation. No new agents introduced — existing agents extended via their manifest job-list.
- **`docs/working/memory-interop.md`** — superseded where the topic overlaps. Older notes archived under the working/ tree.

## 12) Contacts & links

- Product lead: Benjamin Booth (no-reply@axiom-os.ai)
- Eng lead: same
- Parent PRD: `docs/prds/prd-memory.md`
- Design sketch: `docs/working/cross-tool-memory-guarantees-sketch.md`
- Tech spec (TBD): `docs/specs/spec-cross-tool-memory.md`
- Session checkpoint: `docs/working/session-checkpoint-axiom-memory-mcp.md`

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
