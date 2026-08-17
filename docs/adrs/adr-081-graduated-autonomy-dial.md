# ADR-081: Graduated Autonomy Dial — Ship OFF, Graduate Up via an Onboarding Interview

**Status:** Proposed (2026-07-08)
**Refines:** ADR-028 (trust graph), ADR-043/045 (RACI graduated autonomy), ADR-055 (governance fabric)
**Builds on:** ADR-035 (human-principal binding), the per-agent consent model, the Background Service (agents/background_service.py)
**Related:** launchd/notification hygiene (quiet-on-idempotent, #208)

---

## Context

Axiom is starting to land on colleagues' laptops. Today there is **no master
control over autonomous execution**, and the nearest thing — the per-agent
consent file — **fails open**: on a fresh install "undecided" means *dispatch
everything*. So once `axi install` writes the OS timer, the daemon agents
(PRESS/RIVET/TIDY every 5 min, TRIAGE every 10 min) run on their heartbeats
without an explicit yes. That is the wrong default for software a new operator
is meeting for the first time.

Ben's requirement: an easy, obvious on/off for heartbeat agents, **installed OFF
by default**, and beyond on/off a **graduated dial** ("All off … All on") with
named in-between stops — some sub-scopable (e.g. comms bots all vs. per-channel)
— where a node is configured through an **interactive interview** that unlocks
capability in proportion to the operator's familiarity and trust.

A survey of the field (Claude Code, Codex, Cursor, Cline, Roo Code, Aider,
Devin, Copilot coding agent, AutoGPT, LangGraph, Letta, Mem0) shows two mature
control shapes — a global **mode enum** and **per-category allowlists** — plus
recurring guardrails (circuit breakers, allowlists-over-denylists, human
checkpoints). None ships an ordered dial of *named tiers scoped by capability
class*, none treats background/heartbeat or per-channel comms or memory-write as
a first-class governed tier, and none defaults fully OFF and graduates up via an
onboarding interview. That gap is the opportunity; the mature shapes are the
idioms we should reuse so the basics feel familiar.

## Decision

### 1. Two orthogonal knobs (the Codex idiom: sandbox × approval)

- **`autonomy.level`** — *what runs autonomously* (the dial below).
- **`raci.trust`** (existing, 1–5) — *how eagerly actions auto-approve within
  what the level unlocks*.

Above both sits **`autonomy.enabled`** (bool, ships `False`) — the master "big
off switch". `enabled = false` ⇒ nothing autonomous, regardless of level. This
is the familiar, unmissable basic control, and it is **already shipped** (see
Increment 1).

### 2. The dial — ordered, cumulative, capability-class per stop

Each level includes everything below it; levels are ordered by increasing blast
radius and decreasing reversibility, not by tool type. A **deny floor** and
**step-up-at-consequence** hold in every tier and cannot be dialed away (the
Claude Code hook-`deny`-beats-bypass lesson; our "safety floors under LLM
judgment" and "identity is a posture, step up only at consequence" positions).

| Level | Name | Unlocks | Enforced at (exists today) |
|---|---|---|---|
| 0 | **Observe** *(install default)* | Nothing autonomous — read, answer, retrieve, propose plans; every mutation asks | master gate |
| 1 | **Remember** | Memory write / consolidation (internal, reversible) | memory write path + `memory/heartbeat_install` |
| 2 | **Self-heal** | Auto-restart + health heartbeats, Axiom's own services only | `infra/services.py` KeepAlive/Restart + TIDY |
| 3 | **Schedule** | Scheduled/heartbeat jobs unattended in policy windows; read+memory+notify only | `background_service.dispatch_due_agents`, PULSE `engine.tick` |
| 4 | **Converse** | Comms-channel bots post/respond — scopable all **or** per-channel allowlist | HERALD `send` + channel registry |
| 5 | **Operate** | Fully autonomous multi-step agents, cross-surface mutations | `policy/agent_action_guard.guarded_act` (per-action floor) |

### 3. Familiar idioms for the basics, novelty only where it earns its keep

Do familiar things in familiar ways so a driver coming from another harness is
not surprised by ours:

- **Named modes**, not bare numbers, at the surface — `off` (=L0), and level
  aliases so `axi autonomy status` reads like Claude Code's mode line. L0 is
  our "plan mode for the whole platform".
- **Per-category override table** beneath the dial (the Cline/Roo idiom): the
  existing per-agent consent file, **flipped from fail-open to fail-closed**,
  becomes the escape hatch for operators who want, say, memory + one comms
  channel without the intervening tiers. The ordered dial is the recommended
  path and the default; the table is the power-user affordance.
- **Circuit breakers** (the Cline/Roo/AutoGPT idiom): max autonomous
  actions / cost per window, enforced under every tier.
- **Allowlists over denylists** (the Cursor 2025 lesson): comms scoping and
  command gating are allow-based; deny floors are separate and non-negotiable.
- **Checkpoints at consequence** (the Devin idiom): high-consequence actions
  (spend, external send, destructive ops) step up for approval even at L5.

The novelty we keep: the *ordered named-tier dial scoped by class*, memory and
background/heartbeat and per-channel comms as first-class tiers, and default-OFF
graduated via interview.

### 4. Set by an onboarding interview (a new configure agent)

There is no installer/configure agent today; setup is a deterministic wizard.
We add a **configure/onboarding agent** (functional name TBD) that, on install,
sits at L0 and interviews the operator, unlocking one class at a time and
**recording who approved each step-up** (the operational face of ADR-035 +
progressive trust). It reuses the existing `questionnaire/engine.py` QAEngine
(today wired only to the classroom). Tracked separately.

### 5. Enforcement — one gate per surface, all pre-existing

`autonomy.enabled=false` (or `level` below a class's threshold) is checked at:
install (`register_all_daemon_agents` — no OS timer written), runtime
(`background_service.dispatch_due_agents` — nothing dispatched), memory writes
(L1), HERALD send + channel registry (L4, per-channel), PULSE `engine.tick`
(L3), and `agent_action_guard.guarded_act` (L5 floor). No new orchestration
layer; the switch composes the bones we already have.

## Consequences

- **+** Safe by default: a fresh install does nothing autonomous until an
  operator opts in. Also removes the macOS pop-up at install for free — at L0
  no launchd/systemd unit is written, so the OS "Background Items" toast never
  fires (see the launchd-hygiene work).
- **+** Basics feel familiar (mode line, per-category toggles, circuit breakers,
  checkpoints); the differentiated surface (ordered class-scoped dial, memory /
  background / per-channel comms as tiers, interview-graduated) is where we lead.
- **+** Reuses trust graph, RACI, governance tokens, consent, and the action
  guard — no parallel machinery.
- **−** The per-agent consent default flips fail-open → fail-closed; upgrading
  installs that relied on the old implicit-on behavior must opt in. Acceptable:
  the whole point is that implicit-on was wrong.
- **−** A dormant timer from a pre-flip install stays until the next
  register/teardown pass; the runtime gate neuters it meanwhile.

## Increments

1. **Shipped (this ADR's first cut):** `autonomy.enabled` (default `False`) +
   install and runtime gates + tests. The master off switch, safe by default.
2. `autonomy.level` dial + capability-class tags on agent manifests + the
   per-class override table (consent flipped fail-closed) + circuit breakers.
3. The configure/onboarding interview agent that sets both knobs and records
   approvals.
4. launchd/notification hygiene: back-port the #208 idempotence guard to the
   memory-heartbeat writer; route hardcoded plist labels through `get_branding()`.

## Open questions

- Name for the configure/onboarding agent (functional, no mascot names).
- Exact class thresholds per level, and whether `raci.trust` collapses into the
  dial or stays a separate knob (leaning separate — it answers a different
  question).
- Whether "branded, good-looking" desktop notifications are worth a signed
  `.app` bundle / a new HERALD desktop channel, or whether quiet-and-rare is
  enough (leaning the latter for now).
