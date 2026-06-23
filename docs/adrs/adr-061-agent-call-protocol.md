# ADR-061 — Agent-call protocol: A2A as substrate for both intra- and inter-authority agent edges

**Status:** Proposed (2026-06-01)
**Owner:** Ben Booth
**Related:** ADR-027 (federated memory + A2A), ADR-028 (trust graph), ADR-029 (federation composition), ADR-056 (skill-fn pattern), DP-AUTH-1 (audit envelope), PRD Event Bus v2.

---

## Context

The portfolio is converging on multiple agent personas — RIVET (CI/CD),
TIDY (hygiene), PLINTH (lakehouse + data), WARDEN (federation trust)
— each owning a skill registry per ADR-056. Real operator workflows
already require these agents to call each other: RIVET wants to ask
PLINTH "is the deploy target healthy?" before pushing a release;
PLINTH wants to ask WARDEN "may I issue this remediation?" against an
EC-classified table; TIDY wants to ask RIVET "is this branch safe to
delete?" before pruning.

Today these edges either don't exist or are ad-hoc Python imports.
That is wrong on three axes:

1. **No audit chain across agent edges.** A RIVET-→-PLINTH call leaves
   no verdict in DP-AUTH-1; the operator can't reconstruct "who asked
   whom, with what evidence, with what authority."
2. **No federation seam.** When the same call needs to cross an
   authority boundary tomorrow (RIVET-at-example-org asking
   PLINTH-at-partner-org), there is no protocol; we would invent A2A
   per-edge.
3. **No graduated autonomy at the edge.** Some agent-call edges should
   silently succeed; others should always ask. There is nowhere to
   express the policy.

A2A (ADR-027) was designed as the federation protocol for cross-
authority memory and inference. The shape — signed envelopes,
verdict-bearing, classification-aware — is exactly what intra-
authority agent calls need. Reusing it collapses the problem.

## Decision

**A2A is the substrate for every agent-to-agent call, intra-authority
and inter-authority. Same envelope. Same verdict chain. Same
classification gate. Transport differs by locality; protocol does not.**

Specifically:

1. **Surface = the agent's `SkillRegistry`** (ADR-056). Inter-agent
   calls invoke registered skill functions; the caller is treated
   exactly as a human invoking the skill — same authz path, same DP-
   AUTH-1 wrap. There is no separate "agent-internal API."

2. **Envelope = the A2A envelope from ADR-027** — signed by the calling
   agent's principal (`@rivet:example-org`, `@plinth:example-org`, …),
   carrying classification, intent, evidence references, and a
   `parent_verdict_id` linking back to the call that triggered this
   one. PLINTH-trace chains naturally form a tree of A2A-envelope
   verdicts the operator can audit later.

3. **Transport selected by locality** — three lanes, one protocol:

   | Edge | Transport |
   |---|---|
   | In-process (same Python runtime) | direct skill-fn invocation, envelope still recorded |
   | Same cluster / same authority | NATS subject (or PG-LISTEN edge tier per Event Bus v2) |
   | Cross-authority | A2A over HTTPS per ADR-027 |

   The caller never picks the transport. The platform picks by the
   target agent's locality, the same way SecretRef picks a provider.

4. **WARDEN gates every cross-agent call.** Every envelope is verdict-
   gated before delivery; WARDEN is the verdict-issuer. Intra-
   authority edges are typically PASS-silent unless the target skill
   is destructive; cross-authority edges always require an explicit
   trust-graph check. Same gate, same shape, both sides.

5. **Graduated autonomy is expressed per call-edge**, not per agent.
   The edge `(caller=RIVET, target_skill=plinth.lakehouse.health)`
   has its own autonomy level. After N successful invocations with
   no operator override, it graduates from `ask` → `notify` →
   `silent`. Edges that produce destructive remediations stay at
   `ask` regardless of count.

6. **Audit chain = the envelope tree**, not a separate log. Operators
   query `axi audit chain --root <verdict-id>` and see the full
   cross-agent call graph that produced an outcome, intra- or inter-
   authority, identically rendered. PLINTH-trace's CausationChain
   structure is one shape of this tree, materialized for the UI.

## Consequences

**Good.**
- One protocol to learn; one audit query that works across every agent
  edge; one place to set autonomy policy.
- Federation is free: the moment we accept the first cross-authority
  agent call, the same envelope, same gate, same audit chain extend
  with no per-edge protocol design.
- DP-AUTH-1 verdicts naturally form trees: a RIVET release-push
  verdict has a child PLINTH health-check verdict, which has a child
  connector token-state verdict, which has a child WARDEN trust check.
  The whole chain is the audit trail.
- PLINTH's trace primitive (per `feedback_plinth_trace_is_the_primitive`)
  is a thin wrapper over a sequence of A2A envelopes; we get the trace
  graph for free from the audit chain.

**Costs.**
- In-process calls pay an envelope-construction overhead they didn't
  before. Acceptable: envelope build is microseconds; tree-traversal
  audit is the value. We benchmark and publish the cost in the
  performance-test baseline (per the brief).
- Every new skill registration must declare its autonomy default and
  whether it's destructive. Mild author burden; pays back at first
  audit.
- WARDEN becomes load-bearing for routine intra-authority traffic.
  We size accordingly; in practice intra-authority verdicts are
  trivial PASSes that cache.

**What we explicitly do NOT do here.**
- No ad-hoc Python imports for agent-to-agent calls. If RIVET wants
  PLINTH's `lakehouse health` skill, it goes through the A2A surface,
  full envelope, even when both run in the same process. The audit
  trail is the value.
- No agent-private API surface. Everything an agent exposes is in its
  SkillRegistry and visible in `axi skills ls`. There is no hidden
  call surface.
- No transport choice exposed to agent authors. They register skills;
  the platform delivers them.

## Open

- Exact envelope schema for the intra-authority lane (sub-set of
  ADR-027's envelope?). To pin in the companion spec.
- Where the autonomy-level state lives durably (per-edge counter +
  outcome history). Likely `authz.edge_autonomy` table.
- Whether the first concrete edge to ship — likely RIVET asking PLINTH
  `lakehouse health` before a release — exercises NATS or stays in-
  process for v0. Suggest in-process for v0 to minimize transport
  risk while establishing the envelope contract; promote to NATS
  in v1.

## How we'd know we did this right

- One audit query (`axi audit chain --root <verdict-id>`) renders any
  outcome's full cross-agent provenance.
- Adding a new agent persona requires zero protocol work — only a
  SkillRegistry registration and an autonomy-policy declaration per
  exposed skill.
- The first cross-authority agent edge ships with no protocol redesign.
- PLINTH-trace's CausationChain visualization is a thin renderer over
  the audit chain tree, not a parallel data structure.

---

_For Ben to review. Next step on approval: write companion
`spec-agent-call-protocol.md` pinning the envelope schema; first
concrete edge is the RIVET-→-PLINTH `lakehouse health` check before
release push, in-process v0._
