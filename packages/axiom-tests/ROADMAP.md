# axiom-tests Roadmap

This file tracks what `axiom-tests` contains today and what reusable infrastructure is planned. Updates land with each minor release.

---

## Purpose and Scope

`axiom-tests` provides **reusable test infrastructure for Axiom-ecosystem extensions**: base classes, shared fixtures, mocks, Hypothesis strategies, validators, and the AEOS conformance pytest plugin.

Extensions **inherit and compose** from this package. They do NOT dump extension-specific tests into it.

### What belongs here

- Base `TestCase` classes extensions inherit (e.g., `ExtensionStandardTests`, `ToolTests`, `AgentTests`, future domain bases)
- Fixtures usable by 2+ extensions (e.g., `mock_llm`, `mock_federation`, `tmp_axiom_home`)
- Mocks of Axiom-core services that extensions plausibly depend on (memory, RAG, gateway, federation)
- Hypothesis strategies for Axiom domain types (principals, manifests, classification stamps)
- Schema validators (AEOS manifest, SKILL.md frontmatter, connection configs)
- The pytest plugin that registers all of the above via a `pytest11` entry point

### What does NOT belong here

- Extension-specific test cases — those live in the extension's own `tests/unit_tests/` or `tests/integration_tests/` per ADR-031
- Integration test fixtures that target only one extension (e.g., classroom-specific Canvas mocks belong in Keplo)
- Test configuration for specific products (Keplo, the domain consumer, Vega) — keep in the product repo
- Research-paper-specific measurement harnesses — separate concern

### Decision criterion

> Would at least two unrelated extensions need this primitive? If yes, axiom-tests. If no, the extension.

When in doubt, build in the extension first. Promote to axiom-tests only when a second extension wants the same primitive.

---

## Released

### 0.1.0 — AEOS conformance (shipped 2026-04-21)

- `ExtensionStandardTests` + per-capability-kind base classes (agent, tool, cmd, service, adapter, skill, hook)
- `mock_llm`, `mock_federation`, `mock_oidc`, `mock_registry`, `tmp_axiom_home`, `manifest_validator`, Hypothesis strategies
- AEOS JSON Schema (`schemas/aeos-manifest-0.1.json`)
- pytest plugin via `pytest11` entry point
- Self-tests: 149 passing, 92% coverage
- Ruff/type-check clean

---

## Planned (in approximate priority order; versions assigned as each lands)

Each planned area is a set of **reusable primitives**, not extension-specific tests. Descriptions emphasize the "base class + fixture" shape.

### Memory primitives

- Base: `MemoryFragmentTests` — fragment creation, ownership, provenance immutability, cognitive-type validation
- Base: `CompositionServiceTests` — composition writes, queries, retention tier transitions
- Fixtures: `mock_composition_service` (in-process), `sample_fragments` (parametrizable), `memory_snapshot` (for diff testing)
- Consumers: any extension writing to memory — Keplo, the domain consumer, signals, synthesis

### RAG + retrieval primitives

- Base: `RetrieverTests` — retrieval result shape, citation linkage, ranking stability
- Base: `RagContextInjectionTests` — verifies retrieved chunks appear in PromptComposer's `retrieved` layer
- Fixtures: `mock_pgvector` (in-memory backend), `sample_corpus` (canned documents), `citation_checker`
- Consumers: any extension using RAG (Keplo tutoring, a domain consumer's domain RAG)

### Gateway primitives

- Base: `LLMGatewayTests` — routing decisions, provider failover, rate limit handling
- Fixtures: `mock_llm_router` (deterministic provider selection), extended `mock_llm` variants (streaming, tool-use, structured-output)
- Consumers: any extension that invokes the gateway (all agent-bearing extensions)

### PromptComposer primitives

- Base: `PromptComposerContractTests` — layer ordering, cache boundary placement, token budget respect
- Fixtures: `mock_composer` (asserts-on-construction), `layer_extractor` (pulls specific layer from composed output)
- Consumers: any extension providing prompt layers (classroom persona, research loops)

### Session primitives

- Base: `ConversationHistoryTests` — sliding-window compaction, summarization correctness, tool-use preservation
- Fixtures: `sample_conversations` (various lengths/patterns), `mock_session_store`
- Consumers: any extension with session state

### Federation primitives (post-Vega extraction)

- Base: `TrustProfileTests`, `ClassificationStampTests`, `RaciGateTests`
- Fixtures: `multi_peer_federation` (3-peer in-memory mesh), `mock_classification_ceiling`
- Consumers: any extension participating in federation or respecting classification boundaries

### Signal pipeline primitives

- Base: `SignalTriageTests`, `SynthesisQueueTests`, `NotificationTemplateTests`
- Fixtures: `mock_triage` (parametrizable rules), `signal_queue_harness`
- Consumers: any extension registering signal types (Keplo, the domain consumer)

### Adapter primitives (expansion beyond OIDC)

- Additional `mock_*` fixtures for new adapter types: Slack, webhook, Canvas LMS, Envoy ExtAuthz, etc.
- Generic `AdapterConnectionTests` extends to any `Adapter` implementation

### Service primitives

- Base: `ServiceLifecycleTests` — start/stop/health_check contracts, restart behavior, port binding
- Fixtures: `isolated_service` (subprocess wrapper), `port_allocator`

### Hook primitives

- Base: `LifecycleHookTests` — event firing, priority ordering, fail_mode behavior
- Fixtures: `hook_event_emitter`, `captured_hook_calls`

---

## Under consideration (not yet on the roadmap)

- Behavioral attestation test infrastructure (waits on AEOS 0.2 attestation features)
- Envoy ExtAuthz integration test harness (once Vega Shape 3 ships)
- DNSSEC federation discovery test fixtures (once Vega Shape 5 ships)
- Conformance-level reporting (Bronze/Silver/Gold) — produces a machine-readable report rather than pass/fail
- Benchmark base classes for performance-sensitive extensions

---

## Update Discipline

- Every minor release updates this file with what landed + what moved from "planned" to "released"
- New scope is considered when an extension author needs a primitive and no ≥2-extension demand exists yet — file an issue, don't preemptively add
- Extension authors who need a private primitive build it in their extension first; promote to axiom-tests only on second-extension demand
- CHANGELOG.md in this package covers releases; this file covers the forward-looking plan

---

## Surfacing This Document

- Referenced in `packages/axiom-tests/README.md`
- Referenced in `axiom/docs/specs/spec-aeos-0.1.md` §8 (Testing Framework) as the expansion plan
- Referenced in `axiom/docs/working/aeos-playbook.md` as the place to check for available primitives
- Quarterly review: when the AEOS quarterly review fires, also check this roadmap for stale plans
