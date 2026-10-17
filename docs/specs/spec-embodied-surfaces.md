# Spec: Embodied Surfaces

**Owner:** Platform • **Status:** Draft • **Last updated:** 2026-09-18
**PRD:** [prd-embodied-surfaces](../prds/prd-embodied-surfaces.md) • **Key ADRs:** [ADR-116](../adrs/adr-116-surface-family-and-coding-harness-posture.md), ADR-039, ADR-055, ADR-067
**Composes (does not restate):** node-foundations `06-subsystem-contract.md` §7 (actuator protocols) and `08-adr-daq-subsystem.md` (inbound telemetry); `spec-agent-architecture.md`; `prd-agents.md` (voice identity verification); AEOS §4 capability kinds

## Overview

Two new members of the surface family, built entirely from existing
primitives. The presence surface is a streaming conversation loop with a
verified human; the actuation surface is a governed command path to physical
systems. Neither introduces new governance machinery: both are AEOS
extensions whose distinguishing property is *which* existing contracts they
are required to compose, verified by a conformance test base.

```
            ┌────────────── surface family ──────────────┐
   CLI · chat · web kit · mobile · PRESENCE · ACTUATION
            └───────┬─────────────────────┬──────────────┘
                    ▼                     ▼
        identity → policy/RACI → approval gates → action
                    ▼                     ▼
             memory fragments  ←  receipts / journal
```

## Contracts

### Presence surface (voice / avatar)

- **Session:** a presence session is an agent session with a streaming
  transport on the gateway (streaming-first paths; barge-in interrupts
  generation the way Esc interrupts the CLI). One session, one verified
  principal.
- **Identity gate:** no write-class skill dispatch before speaker
  verification succeeds, per the verification ladder prd-agents defines
  (voice ID / badge / SSO). Unverified sessions are read-only and say so.
- **Memory:** each utterance/reply pair appends fragments through
  CompositionService with the standard provenance tuple; a presence session
  is recallable exactly like a chat session.
- **Comms unification:** outbound speech is a HERALD channel; an inbound
  spoken reply binds to the originating ActionEnvelope the same way a Teams
  reply does (ADR-067). Voice is a channel, not a parallel system.
- **Avatar:** rendering is a display concern — a display-kind consuming the
  same session stream (ADR-039 registry patterns); no avatar-specific
  privileges exist.

### Actuation surface

The command path is the §7 protocol chain, unmodified. This spec fixes how
agents enter it and what the platform guarantees around it:

- **Entry:** an agent produces an `Intent`, never a `Command`. The
  deterministic chain — `AuthorityRegistry.authorize` (policy engine) →
  `CommandRouter.route` → `EnvelopeCheck.check` + `Interlocks.evaluate` →
  `ActuatorWriter.write` — runs outside the LLM loop. The LLM proposes;
  deterministic layers dispose.
- **Approval:** intents whose RACI tier requires a human pause on the
  durable ApprovalGate; the approval (or denial), its principal, and its
  channel are part of the command's record. Autonomy graduates per tier,
  never per prompt.
- **Capability binding:** scheduled actuation fires under a vault-issued,
  envelope-scoped capability token (PULSE semantics); interactive actuation
  is bound to the session principal's authority. No path accepts a bare
  credential.
- **Receipts:** every `CommandReceipt` and `CommandJournalEntry` is
  mirrored as a memory fragment, making the journal queryable next to
  everything else the platform remembers. The journal remains the
  append-only encrypted record of authority; the fragment is its shadow
  for recall — journal wins on conflict.
- **Fleet:** a fleet is N actuation extensions under site-scoped visibility
  (deployment ∩ grant bounding, as in the serving surface) with one
  accountable authority per cohort. Cross-org delegation rides federation
  with accountable-human propagation. `halt` is a fleet-wide verb resolved
  before any per-unit routing — its cross-cohort semantics are an open ADR
  (PRD §8).

## Design

Both surfaces are compound AEOS extensions using existing kinds only:
`service` (session/transport, substrate command-path services), `adapter`
(wire-level writers, per §7), `cmd` (operator verbs), `skill` (agent-facing
functions per ADR-056), `hook` (lifecycle). Discovery, manifests, signing,
and testing follow AEOS as for any extension — an embodied surface is not
special to the loader, only to the conformance suite.

## Surface-family conformance (§5)

What makes an extension a *surface* (applied to CLI/chat/web retroactively,
normative for new surfaces). A surface MUST:

1. Bind every action to an identified principal (`@name:context`).
2. Route every effectful operation through the policy engine and, where the
   RACI tier requires, a durable approval gate.
3. Emit receipts as memory fragments under the standard provenance tuple.
4. Honor graduated autonomy — no surface-local bypass of trust profiles.
5. Interrupt cleanly (barge-in / Esc / halt) with the interruption itself
   recorded.
6. Pass the surface conformance test base (extends the AEOS §8 standard
   tests; ships with the family so conformance is provable, not asserted).

## Decisions

- ADR-116 — one family, one spine; no coding-IDE surface; coding harnesses
  served through standards seams.
- ADR-055 — the governance fabric every surface composes.
- ADR-067 — inbound reply-binding this spec reuses for speech.
- Node-foundations §7 — the actuator protocol chain this spec deliberately
  does not fork.

## Open questions

- Conformance ownership: platform checklist vs AEOS conformance level
  (owner: standards track; interacts with ADR-032 donation scope).
- Fleet halt across cohorts (owner: federation; new ADR when scheduled).
- Presence latency budget and local-model fallback for air-gapped sites
  (owner: gateway; measure before specifying).
