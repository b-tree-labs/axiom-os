# PRD — Embodied Surfaces

**Owner:** Product • **Status:** Draft • **Last updated:** 2026-09-18
**Related:** [ADR-116](../adrs/adr-116-surface-family-and-coding-harness-posture.md) (surface family + coding-harness posture), [spec-embodied-surfaces](../specs/spec-embodied-surfaces.md) (how), [prd-agents](prd-agents.md) (voice-first agent interaction, identity verification, guardrails — not restated here), ADR-039 (scientific displays), node-foundations subsystem contract §7 (actuator protocols — not restated here), `working/agent-harness-competitive-analysis-2026-09-18.md` (market evidence)

## 1) Elevator pitch

Extend the surface family beyond screens: a **presence surface** (voice /
avatar — full-duplex conversation with an identified human) and an
**actuation surface** (agents that move physical systems — single units to
governed robotic fleets), both inheriting the platform's governance spine so
that speaking to an agent or letting it actuate hardware is as provable,
bounded, and reversible as any CLI action.

## 2) Problem / opportunity

- Operational users don't live in terminals. Hands-busy, eyes-busy
  environments (labs, plant floors, vehicles) need voice-first interaction
  with the *same* agent, memory, and approval chain they get at a keyboard —
  today they get either nothing or an ungoverned consumer assistant.
- Physical automation is arriving faster than its governance. The market
  evidence (competitive analysis §2.5, §7) shows always-on agent adoption
  exploding with security and oversight lagging catastrophically; extending
  that pattern to actuators is unacceptable, and no platform in the field
  offers capability-scoped, receipted, human-gated actuation.
- Fleets multiply the problem: N units under one authority, per-unit safety
  envelopes, cross-site delegation — exactly the shape federation,
  site-scoping, and accountable-human binding already solve for data.

## 3) Goals & success metrics

- Primary: a domain extension can ship a voice surface or an actuation
  surface **without touching core**, by conforming to the surface family
  (spec §5).
- Every actuation flows intent → authority → envelope → receipt with zero
  bypass paths (metric: 100% of issued commands carry a journal entry
  linked to a principal and an envelope verdict; enforced by conformance
  tests, not convention).
- Voice sessions bind to verified identity before any non-read action
  (metric: 0 write-class actions from unverified speakers).
- North-star contribution: receipted autonomous actions/week counts
  embodied actions the day the first actuation surface deploys.

## 4) Key users / personas

- **Operator / technician** — converses hands-free; approves or denies
  agent-proposed actions by voice with the same force as a click.
- **Automation engineer** — authors an actuation extension for their
  hardware against published protocols; never re-implements governance.
- **Responsible authority** — reviews the command journal; answers "what
  moved, who allowed it, under which envelope" in one query.
- **Student / researcher** — the presence surface as tutor/companion,
  inheriting classroom guardrails.

## 5) Scope — key capabilities

1. **Presence surface (voice/avatar).** Full-duplex speech loop on the
   gateway; speaker identity verification gates per prd-agents; barge-in;
   every utterance and agent reply lands as memory fragments; delivery and
   reply-binding ride HERALD (ADR-067). Avatar rendering is a display
   concern layered on the same session (ADR-039 patterns).
2. **Actuation surface (single unit).** Agent-originated intents flow
   through the §7 actuator protocols (AuthorityRegistry, CommandRouter,
   EnvelopeCheck, Interlocks, ActuatorWriter, CommandJournal); RACI-gated
   intents pause on the durable ApprovalGate; every command receipt is a
   memory fragment.
3. **Fleet governance.** Many units, one accountable authority: per-unit
   envelopes, site-scoped visibility, fleet-wide halt as a first-class
   verb, delegation across organizational boundaries via federation with
   accountable-human propagation.
4. **Surface-family conformance.** The published checklist + test base that
   makes an extension a "surface" (spec §5) — applied equally to existing
   CLI/web/chat surfaces and these new ones.

## 6) Non-goals

- **Not a coding tool, ever** (ADR-116): no IDE, no coding terminal.
  External coding harnesses — including OpenCode — are partners served via
  AGENTS.md/context sync, MCP, ACP, and memory adapters, with support
  tracked as a product surface in its own right.
- Not a robotics middleware: motion planning, kinematics, perception, and
  real-time control loops belong to the domain extension and its hardware
  stack. Axiom governs the *command path*, not the control loop.
- Not a general telephony/meeting product; presence targets operational
  interaction with the platform's own agents.
- No direct-hardware writes outside envelope + interlock gating — there is
  no "raw mode."

## 7) Constraints & dependencies

- Safety-adjacent writes remain human-in-the-loop by default (platform
  invariant); autonomy graduates only through RACI tiers with durable
  approvals.
- Latency: presence needs streaming-first gateway paths; actuation needs
  the deterministic gates to run outside the LLM loop (the LLM proposes,
  deterministic layers dispose — established platform doctrine).
- Depends on: subsystem contract §7 protocols (node-foundations), HERALD
  inbound (ADR-067), governance fabric (ADR-055), site scoping, federation.

## 8) Open questions

- Fleet-wide halt semantics across federated cohorts (single verb vs
  per-cohort policy) — needs its own ADR when fleet work starts.
- Avatar embodiment scope for v1 (audio-only vs rendered) — decide at
  presence-surface kickoff.
- Whether surface-family conformance becomes an AEOS conformance level or
  stays a platform-side checklist (interacts with ADR-032 donation scope).
