# Product Requirements: Steer

**Product / Feature:** Steer — the agent-activity surface

**Owner:** Platform surfaces • **Status:** Draft • **Last updated:** 2026-09-28

**Decision record:** [ADR-137](../adrs/adr-137-steer-is-the-agent-activity-verb.md)
**Design:** [docs/assets/design/steer/](../assets/design/steer/README.md) — eight artboards, committed

---

## 0) Design

The screens are in [docs/assets/design/steer/](../assets/design/steer/README.md),
committed beside this document so a requirement cites a file rather than a link.
Each capability in section 5 names the artboard that specifies it.

| Artboard | What it settles |
|---|---|
| [`Main.dc.html`](../assets/design/steer/Main.dc.html) | The stream, its facets, liveness, coverage disclosure, the glance gauges, the in-row conversation, the drawer edge and the reveals |
| [`Case.dc.html`](../assets/design/steer/Case.dc.html) | One case: derivation, consequence gate, the decide flow, standing conditions, Discuss, the payload toggle |
| [`Agent.dc.html`](../assets/design/steer/Agent.dc.html) | An agent's profile: every skill, how it arrived, when, and what it may do alone |
| [`Drawer.dc.html`](../assets/design/steer/Drawer.dc.html) | An agent's activity: log, timeline, per-agent search, and talking to it |
| [`Dials.dc.html`](../assets/design/steer/Dials.dc.html) | Tuning: four dials, and teaching by conversation with its read-back |
| [`Sources.dc.html`](../assets/design/steer/Sources.dc.html) | Origin filtering and what each source permits |
| [`States.dc.html`](../assets/design/steer/States.dc.html) | The states in which this surface could lie, and what each must render instead |
| [`Rail.dc.html`](../assets/design/steer/Rail.dc.html) | The nav options, the artifact verb's label, and the route-id question |

Two of these are specifications rather than screens. `States.dc.html` is the
acceptance evidence for every requirement about absence, and `Rail.dc.html`
records the options ADR-137 chose between.

## 1) Elevator Pitch

Steer is the one place a person can see what their agents are doing, what they
did, what they produced, and what needs a human decision, and can change how an
agent behaves without leaving the page.

## 2) Problem / Opportunity

- The platform runs agents continuously and shows none of it. Open cases are
  visible; runs, logs, outcomes and liveness are not.
- Graduated autonomy ships without a window. The gates decide what an agent may
  do alone, and nothing reports what they let through.
- An agent that has gone quiet is indistinguishable from an agent with nothing
  to do. Today both render as an absence, and the reassuring reading wins.
- Guidance a person gives evaporates. A decision is recorded, but the reasoning
  behind it does not reach the next matching case.
- Competitively this is the visible half of the oversight story. The mechanism
  exists; there is no surface that demonstrates it.

## 3) Goals & Success Metrics

**Primary goal:** make continuous agent work supervisable by a person who looks
a couple of times a week, not by one watching a console.

Success metrics:

- **Reporting completeness** — every run by a capability that declares a skill
  spec appears in the stream. Target 100%, measured as a test, not a dashboard.
- **Source symmetry** — harnesses we can observe, against harnesses we can send
  guidance to. Today roughly seven against thirteen. The gap is displayed; the
  target is to close it, and closing it is memory-programme work.
- **Implementation reuse** — number of ingestion paths, origin models and
  serving gates. Target one each. More than one is a defect regardless of how
  well it works.
- **Skill-spec coverage** — capabilities able to report, from 58 of 117 today.
  Target a monotonic ratchet; the count is displayed on the surface and may
  never be hidden.
- **No silent-health states** — zero renderings in which a dark agent, a stopped
  agent and an idle agent are indistinguishable. Enforced by tests over the
  empty, one-row, dark, failed and false-success states.
- **Return cadence** — sessions per person per week. The number is the honest
  test of whether the glance content is worth glancing at.
- **Decision latency** — median age of a case when it is decided, and the age of
  the oldest waiting case, both visible on the surface.
- **Guidance reuse** — proportion of cases resolved by citing a standing
  condition rather than by fresh judgement.

## 4) Key Users / Personas

- **The accountable person.** Looks twice a week, wants to know what changed,
  what is waiting, and whether anything went wrong. Technical, time-poor, and
  the one on the hook for a harm-class decision.
- **The operator of a single agent.** Has one agent that matters to them, wants
  its history, wants to ask it why it did something, and wants to slow it down
  or quiet it without filing a change.
- **The maintainer.** Needs to know which capabilities cannot report, and why a
  given run reached a person rather than resolving itself.
- **The person with agents in several harnesses.** Runs sessions in other tools
  and wants one place that shows all of them, with the differences stated rather
  than smoothed over.
- **An agent.** Reads the same record over MCP. Nothing on this surface may be
  visible to a person and unavailable to a capability, or vice versa.

## 5) Scope — Key Capabilities (MVP)

1. **One faceted run stream** — All, Needs you, Running, Done over a single
   chronological list. Acceptance: a run moves between facets as its state
   changes without moving between screens. Design: `Main.dc.html`.
   History and Recents are untouched by this and stay in the rail: they key on
   the session, Steer keys on the agent, and both read the same turns.
2. **Liveness that distinguishes silence from idleness** — an agent that has
   missed its declared reports is marked, counted, and visually distinct from
   one that is idle or stopped. Acceptance: a dark agent whose last run
   succeeded still renders as dark. Design: `Main.dc.html`, `States.dc.html`.
3. **Coverage disclosure** — the count of capabilities that cannot report is
   always shown. Acceptance: at zero it becomes a positive claim of
   completeness, and at any other value it states that the list is partial. Design: `Main.dc.html`, `States.dc.html`.
4. **In-row conversation** — each run expands to its log, what it cannot
   establish, and a composer scoped to that agent and that run. It grows from
   a summary height to most of the page. Acceptance: the conversation is
   reachable without navigating away from the stream. Design: `Main.dc.html`.
5. **The agent drawer** — one agent's whole history, a timeline selector with
   convenient ranges, and search within that agent. Acceptance: a closed drawer
   is discoverable from the page edge. Design: `Drawer.dc.html`.
6. **Four behaviour dials** — Run, Cadence, Ceiling, Attention. Each change
   carries a reason in the person's words and an expiry, and is itself recorded
   as a run. Acceptance: no dial setting permits acting alone on a harm-class
   finding, and the unreachable position is shown rather than omitted. Design: `Dials.dc.html`.
7. **The derivation, as data** — inputs, rule, an independent check, what it
   cannot establish, and the numeric comparison where there was one. Acceptance:
   the page, the terminal, an export and a capability read the same payload, and
   none can state something the others cannot. Design: `Case.dc.html`.
8. **Glance content that moves** — since-you-last-looked, the handled-alone
   trend, and fleet rhythm. Acceptance: nothing occupies glance space unless it
   changes between typical visits. Design: `Main.dc.html`.
9. **Incremental revelation** — contestable numbers carry an affordance that
   reveals their meaning in place. Acceptance: no reveal navigates away, and one
   is open at a time. Design: `Main.dc.html`.
10. **Breadcrumbs** — on pages with real depth, per the portfolio convention. A
    drawer carries its own header instead. Design: `Case.dc.html`, `Agent.dc.html`.
11. **Teaching by conversation** — a composer on the agent's own page whose
    placeholder teaches the three instruction shapes. Acceptance: the agent
    reads back the rule, the skill-spec version, any new tool binding, whether
    the ceiling moves, and the expiry, and nothing is wired until approved. An
    instruction that would let it act alone on a harm-class finding is refused,
    not narrowed. Design: `Dials.dc.html`, `Agent.dc.html`.
13. **Agent-scoped conversation excerpts** — an agent's history may be drawn
    from sessions that several agents took part in. Acceptance: an excerpt is an
    exchange and never a bare turn, it says it is an excerpt, it attributes every
    participant by principal, and it links to the whole session in Recents.
    Design: `Drawer.dc.html`.
14. **The agent profile** — every skill an agent has, how it arrived (shipped,
    granted or taught), when, and what it may do alone, beside the dated history
    of grants and instructions. Acceptance: a skill that declares no spec is
    marked on the agent's own page, so the fleet-wide gap is attributable.
    Design: `Agent.dc.html`.
12. **Source filtering and the capability matrix** — runs are filterable by the
    harness that produced them, and what a person may do is drawn per source.
    Acceptance: a control that would do nothing for a source is not rendered for
    it; a source that is blocked for want of a credential reads as blocked, not
    as quiet; and a source reached only by write-back says so on the row. Design: `Sources.dc.html`.

15. **Human decisions are signed, and agent actions say who authorised them** —
    a decision taken in the decide flow, an approval of a held action, and a
    dial change that moves the Ceiling are each signed as an attestation
    ([ADR-142](../adrs/adr-142-attestation-is-the-record-of-accountable-human-acts.md)):
    in the platform books `cases`, `approvals` and `autonomy` respectively
    (a Ceiling raised toward Auto signs `delegated`; lowered, `revoked`). The
    decide flow is the confirmation presentation
    ([ADR-144](../adrs/adr-144-a-person-confirms-exactly-what-is-recorded.md)),
    in the one decision anatomy — Primary (sign) · Hold · Ask, keys `a` / `h`
    / `?`. A run that executed on a person's authority shows `authorised_by`
    with the signer's name at the time and a link to the signed record.
    Acceptance: an automatic approval is never rendered as "signed by", and
    every run executed after a human approval links to exactly one signed
    record.

**Out of scope for MVP:** per-rule thresholds, custom schedules, priority
weights and retry policy, which are agent configuration rather than oversight.
Cross-account or team-wide streams, which need a federation decision first.
Creating or cloning an agent, and a conversational query over memory — both ride
machinery this PRD scopes and neither is needed to ship it.

**Explicitly not built here:** any ingestion path for foreign sessions, any
per-harness view, and any second session store. Foreign runs arrive through the
portable-memory absorb path and are filtered by the origin coordinate it already
stamps. Building a parallel mechanism is the failure mode this scope exists to
avoid.

## 6) Non-Functional / Constraints

- **Substrate.** The appkit shell, the `--theme-*` token canon, and the existing
  mount and serving pattern. No bespoke shell, palette or serving arrangement.
- **The payload is the contract.** Every rendering is a projection of the same
  data. A surface that can say something the payload cannot is a defect.
- **Accessibility.** Real controls throughout; no interactive handler on a
  non-interactive element. Text contrast at 4.5:1. Anything that must be told
  apart differs in lightness and carries a label, never hue alone and never a
  bare glyph.
- **Charting.** Any truncated axis states its range on the surface. Nothing is
  inferred at render time that would move when the window changes.
- **Absence.** Reported in kinds — nothing waiting, an agent silent, a
  capability structurally unable to report — never collapsed into one empty
  state.
- **Identity.** Principals appear as `@name:context`. The serving gate's
  existing refusals apply unchanged; this surface adds no read path around them.

## 7) Timeline (high level)

- **Phase 1 — the stream.** Facets, liveness, coverage disclosure, the row
  expansion with its log and limits. The read-only half.
- **Phase 2 — the record.** The case view, the derivation payload, decision
  capture with a verbatim reason and standing conditions with expiry.
- **Phase 3 — the conversation.** In-row composer, the agent drawer, timeline
  and per-agent search.
- **Phase 4 — the dials.** Run, Cadence, Ceiling, Attention, with suppression
  disclosure and dial changes recorded as runs.

Each phase ships something usable on its own; none is a prerequisite refactor.

## 8) Risks & Open Questions

- **The surface renders half the fleet and looks complete.** Mitigation: the
  coverage count is a permanent element, and closing the skill-spec gap is
  tracked as a ratchet rather than a background chore.
- **A quiet dial becomes policy nobody chose.** Mitigation: every dial carries
  an expiry, a non-expiring setting is marked as such on the agent's row, and a
  dial that suppresses shows what it suppressed.
- **"Be more proactive" conflates two axes.** Running more often is cadence and
  is cheap; acting more without asking is ceiling and changes what happens while
  nobody is watching. They must never share a control.
- **Glance content decays into decoration.** Mitigation: anything that does not
  change between typical visits is removed from glance space and demoted to a
  banner or a reveal.
- **Open question — scope of a stream.** Per-principal or per-account, and what
  a second person sees of the first one's guidance. Conservative behaviour holds
  until decided.
- **Open question — dial propagation.** Whether a dial set on one node reaches a
  declared peer, or stays local.
- **Open question — how much context an excerpt carries.** The exchange is the
  floor. Whether an agent-scoped view shows the turns around it, and how a third
  participant is rendered inside someone else's history, is undecided.
- **Open question — the trigger for "since you last looked."** Requires a
  recorded visit, which the shell does not currently keep.
- **Teaching writes to an agent's capabilities.** A sentence becomes a skill-spec
  version and possibly a tool binding. Mitigation: the proposal states every
  change before anything is wired, a translation that cannot be expressed as a
  concrete revertible diff is refused rather than approximated, and the surface
  may never apply a change it did not show.
- **Steering a harness we cannot observe.** Four widely used ones accept
  guidance and return nothing, so a person can change behaviour and never see
  whether it took. Mitigation: the row says so. The real fix is absorb readers,
  which is net-new memory-programme work.
- **Open question — what a run stream shows of work done under another
  account.** The serving gate already refuses cross-account reads by default, so
  the conservative behaviour holds until decided.

## 9) Acceptance & Rollout

- **Sign-off:** platform surfaces owner for the design, and a visual review on
  the reviewer's own machine before any merge, per the standing rule for
  highly visual features.
- **Rollout:** behind the existing nav composition, so a consumer opts in by
  taking the base verb. `#/decide` aliases to `#/steer` from the first release
  and stays.
- **Rollback criteria:** any state in which the surface renders as healthy while
  an agent is dark, or in which the coverage count is absent, is a stop-ship.

## 10) Contacts & Links

- Product and engineering: platform surfaces
- Decision record: [ADR-137](../adrs/adr-137-steer-is-the-agent-activity-verb.md)
- Related: [ADR-123](../adrs/adr-123-receipts-surface-architecture.md),
  [ADR-134](../adrs/adr-134-severity-carries-consequence.md),
  [ADR-135](../adrs/adr-135-graduated-autonomy.md),
  [ADR-136](../adrs/adr-136-uncertainty-is-a-platform-primitive.md)
