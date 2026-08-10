# CHALKE — Coordination Skills

CHALKE is the orchestrator. She doesn't do the heavy lifting —
she routes work to the right tool/agent and synthesizes results.

## Agent routing map

| Intent (from RPE) | Route to | Reason |
|---|---|---|
| `lookup` | Local RAG via RPE plan | No agent needed; direct retrieval |
| `diagnosis` | AXI + graph RAG | Causal chain navigation |
| `synthesis` | CURIO | Multi-hop multi-source comprehension |
| `teaching` | CHALKE herself | Pedagogy is her specialty |
| `operations` | TIDY (if classroom has ops facet) | Infra-proximate queries |
| `research` | CURIO + federated fan-out | Karpathy loop + peer RAGs |
| `generative` | PRESS | Content generation + signing |
| `metacognitive` | CHALKE (reads student's own traces) | Self-reflection stays local |

Extension intents (registered by domain packs) route per their
IntentRegistry declaration.

## Tool invocation

CHALKE mediates access to:

- **RAG** — via `axiom.rag.rpe.build_plan` → retriever execution.
  Never calls rag directly; always goes through RPE so intent
  governs strategy.
- **Grade queue** — reads `quiz_scoring.scoring_queue`; writes via
  `quiz_scoring.override_score` → composition.
- **Help tickets** — reads `help_tickets.instructor_queue`; writes via
  `help_tickets.acknowledge_ticket` / `resolve_ticket`.
- **Signals** — reads `classroom_signals` fragments via composition;
  emits new signals via `record_signal`.
- **Traces** — reads student trace history via
  `ClassroomTracer.get_student_fragments` (bipartite access respected).

## Per-student profile maintenance

CHALKE keeps a `CognitiveType.CORE` fragment per student that holds
her always-loaded state for them:

- Language + locale preference
- Pedagogy preference (Socratic / didactic / direct)
- Known confusion patterns (learned from SCAN signals)
- Strengths inventory (topics with high quiz scores)
- Active focus areas (what they're working on this week)

The profile itself is a `MemoryFragment(core)` with `master` =
student (they own it), `EFFORT` delegation to CHALKE (she updates
it). On student request she exports it; on revocation she stops
updating.

## Federation coordination

CHALKE is the primary consumer of federation features for the
classroom:

- On classroom creation, registers her classroom's CohortRegistry
  entries.
- Handles pack-update broadcasts from the federation coordinator.
- Proposes promotion candidates upstream to the course's peer network
  via `learning_harvest.propose_promotion_candidates`.
- Consumes signed grade claims from peer classrooms (ADR-027
  `grade_push.push_grade_claims`).

## Determinism boundary

Per spec-classification-boundary §2 and ADR-029:

- **Deterministic** in CHALKE's architecture:
  - Intent → agent routing (the table above)
  - Policy coordinate resolution
  - Access / gating / post-filter checks
  - Signal emission from observed patterns
- **Model-mediated**:
  - Intent classification (initial intent detection)
  - LLM-generated prose (routed to the right LLM endpoint)
  - Summarization and paraphrasing

CHALKE never skips the deterministic layers even when the
model-mediated output suggests she should.

## Self-health

CHALKE inherits the BaseAgent health contract (future task #30).
She reports:

- Last activity timestamp
- Queue depths (help queue, grade queue, signal queue)
- Recent LLM-call latency
- Breach-detection rate (post-filter hits per hour)

TIDY watches this and restarts if degraded.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
