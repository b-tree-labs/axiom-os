# Product Requirements: Control

> **Name.** This page is **Control**: Site › Automate in the app, route `#/control`, and
> "Control" in search. **Steer is the retired name** (design contract DC-04, ADR-211) and
> appears below only where the 2026-09 text has not been rewritten; read it as Control. The
> file keeps its name so existing links hold.

**Product / Feature:** Control — the agent-activity page

**Owner:** Platform surfaces • **Status:** Draft; the default view, the Scheduled facet, creation, the dials' labels and the held-action card were **approved by the owner on 2026-11-12** (sections "Simple by default" and "The rest of the page") • **Last updated:** 2026-11-12

**Decision record:** [ADR-137](../adrs/adr-137-steer-is-the-agent-activity-verb.md)
**Design:** the approved screens are in appkit, [`docs/design/proposed-2026-11-12/site-control/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-12/site-control)
(approved by the owner, 2026-11-12). Direct links: [README.md](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/README.md), [gallery.html](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/gallery.html), [view23.html](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/view23.html). GitHub shows the HTML as source; clone the repository and open the file to view it. The screens are the design of record and win over the 2026-09
artboards in [docs/assets/design/steer/](../assets/design/steer/README.md), which are superseded
and kept as history.

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
- **Skill-spec coverage** — the share of capabilities able to report. Target a
  monotonic ratchet. The count is read from the capability registry at the
  moment it is shown and is never written into a document or a screen as a
  fixed figure. It is shown to the people who can change it (see "Simple by
  default") and is available to anyone under "About this list".
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
2. **Liveness that distinguishes silence from idleness** *(where it is shown
   was amended 2026-11-12, "Simple by default")* — an agent that has
   missed its declared reports is marked, counted, and visually distinct from
   one that is idle or stopped. Acceptance: a dark agent whose last run
   succeeded still renders as dark. Design: `Main.dc.html`, `States.dc.html`.
3. **Coverage disclosure** *(who sees it was amended 2026-11-12, "Simple by
   default")* — the count of capabilities that cannot report is
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

### Simple by default (2026-11-12)

Owner decision, 2026-11-12, and the origin of design contract rule DC-17. The
page showed everything it knew to everyone, all the time: a full row of
per-agent status when nothing was wrong, a full-width sentence about coverage
that only a maintainer can act on, and identifiers beside every plain title.
The information is right and stays; where it sits changes. This section amends
capabilities 2 and 3 above and wins where they differ.

| Element | Default view | One step away |
|---|---|---|
| Liveness (capability 2) | Nothing while every agent is reporting. When one is silent, a single line, shown to the person who answers for that agent and to site admins, names it, says how long it has been silent, and offers "Open the agent". | "Other agents" on that line opens the strip of the other agents, so no agent is named twice. Anyone can open the whole strip from the page menu (Agents), where each silent agent names who answers for it. |
| Coverage (capability 3) | Nothing for a person who cannot change it, whatever the count. For a person who can (a maintainer of the capabilities), nothing when every capability can report, otherwise a small "Partial list" marker beside the facets. | The marker opens the count, why, the capabilities that cannot report and the action that fixes each one. |
| A run's row | Its state (glyph and word), its plain title and its time. | The agent, the capability id, the log and "Cannot establish", in the expanded row. |
| A held action | What will happen, in plain words, why it is asking, and the choice. | The harm class, the capability id and the parameters, under "Details". Redacted values stay marked as redacted. |

What does not change:

- **A problem is shown to the person who can act on it, with the action.** A
  warning nobody on the screen can do anything about is not shown to them
  (owner decision, 2026-11-12). The page never claims to be complete: its
  heading says it lists what reporting agents do, and the count stays available
  to anyone in the page menu ("About this list") and to the maintainer as a
  mark. The stop-ship rule in section 9 now reads "a maintainer's view lacks
  the mark while coverage is partial", not "the count is absent".
- The page never looks healthy while an agent the person answers for is dark.
- A dark agent whose last run succeeded still shows as dark.
- Every fact that was on the page is still on the page, one step from its mark.

Acceptance:

- **SBD-A1** With every agent reporting and full coverage, the page shows the
  facets and the runs and nothing else above them.
- **SBD-A2** With one agent dark, one line names that agent; opening it shows
  the per-agent strip.
- **SBD-A3** With partial coverage, a person who can change it sees the
  "Partial list" marker, and opening it gives the count, the reason and the
  action for each capability. A person who cannot change it sees no marker and
  no warning; "About this list" in the page menu gives them the count.
- **SBD-A4** A collapsed row carries no identifier; the expanded row carries
  the agent and the capability id.
- **SBD-A5** A held action's default card names no internal class; "Details"
  does.

Decided by the owner, 2026-11-12:

- Coverage is shown to whoever may edit a capability's skill spec.
- A silent agent is shown to its accountable person and to site admins.
- The screen says **silent**; the code's name for the state stays `dark`.
- The crumb is "Site › Automate" and the heading is "Control".

### The rest of the page (2026-11-12)

Approved by the owner on 2026-11-12 with the screens in appkit
[`docs/design/proposed-2026-11-12/site-control/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-12/site-control). Direct links: [README.md](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/README.md), [gallery.html](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/gallery.html), [view23.html](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/view23.html). GitHub shows the HTML as source; clone the repository and open the file to view it. Screen names below are that folder's
(`view23.html?p=<screen>&state=<state>`). The master design never drew any of this; the
inventory of what exists on the node is in that folder's README.

**The Scheduled facet.** Automations (scheduled or triggered runs of a capability, kept by the
scheduler) are a fifth facet of this page, not a page of their own: All · Needs you · Running ·
Done · Scheduled. A row shows the state (scheduled or paused, glyph and word), the name, the
timing in words and the next run. The opened row shows what runs, who does it, who is told, what
needs approval and the last run, with Pause, Edit, Run now and Remove. A paused automation says
who paused it and why. Screens: `b-auto` list, created.

**"+ New".** One create action, "+ New automation", last on the right of the heading row (design
contract DC-07). Its menu lists only kinds that can be created: New automation, New agent (once
the agents phase ships), Ask an agent now, Start from an example. While another action is the
view's filled primary (the silent line's "Open the agent", the first ask's button), "+ New
automation" is a secondary button. A role that cannot create sees "Ask…", naming who can.
Screen: `b-new` menu.

**The launch pad.** With no agents and nothing run, the page is one sentence ("Nothing is running
here yet."), one large "+ New automation", and three starters: an example automation labelled as
an example, "Set up an agent", and "Ask Chat to set it up" (DC-08). No facets and no counts.
Screen: `b-empty` launch.

**Creating an automation.** Describe, Check, Confirm. *Describe*: one field, "What should happen,
and when?", in the person's own words, or "Describe it in Chat instead", which opens the one chat
(DC-06: the field is a form field, not a second chat input). *Check*: the page reads back what it
understood, one line each with "Change": what runs, when (in words, with the next run and the
time zone), who is told, what needs approval; "Try it once now" runs it once without turning it
on; "Show all settings" opens the structured form. *Confirm*: the one signing card, "Sign and turn
on" (DC-10). Nothing runs until it is signed. It then appears first under Scheduled. Requirements
for the scheduler's side are in [prd-axiom-schedule.md](prd-axiom-schedule.md) §5.10. Screens:
`b-auto` start, filled, review, created.

**Creating an agent (a later phase).** The same three steps: its job in plain words; the read-back
(name, what it may do, who answers for it, when it asks a person); "Sign and create". The signer
answers for the agent unless they name another person. An admin, or whoever may grant the
capabilities it asks for, may create one. It ships after automations, because nothing on the node
creates an agent today: see [prd-agents.md](prd-agents.md) "Creating an agent, and the dials".
A new agent appears in the stream as a row, "set up, no job yet", with "Give it an automation".
Screens: `b-agent` start, filled, review, created.

**The dials.** Run, Cadence, Ceiling and Attention (capability 6) live in the agent's Tune tab,
one step from any row, from the silent line and from Team. They never appear on the stream. An
automation's timing is changed with its own Edit, not with a dial. The Ceiling stops read in plain
words and stack as a list; the code's names do not change:

| On screen | In code |
|---|---|
| Only suggest | `propose` |
| Small things that are easy to undo | `noise` |
| Things that cost time or money to redo | `waste` |
| Things that cannot be undone | `loss` |
| Anything that could cause harm: it always asks (shown, never selectable) | no value |

Screen: `b-dials` tune.

**The held-action card.** By default: what will happen, why it is asking, Approve and Reject, and
"You sign your choice on the next step." Details, one step away, holds who asked, the capability,
the harm class and the parameters. **A secret parameter is never listed by name**, redacted or
not: one line, "Uses a stored credential", appears only when the action uses one. Screens:
`b-held` default, details.

**Needs you on this page.** The Needs you facet stays, and reads the same ask records as the
attention-bar chip, so the two counts cannot differ, until the inbox page of
[prd-needs-you.md](prd-needs-you.md) exists. The item's shape is in that PRD. Screens: `b-needs`
empty, two.

**No access.** A person who cannot decide a held action gets the no-access page with a way to
request the role: [prd-access-and-onboarding.md](prd-access-and-onboarding.md) R8. Screens:
`b-access` request, requested, pending, rejected.

Acceptance (continuing SBD-A1 to SBD-A5 above):

- **SBD-A6** The silent line is shown only to the agent's accountable person and site admins,
  with "Open the agent"; anyone else sees no line, and page menu › Agents names who answers for it.
- **SBD-A7** With nothing waiting, the Needs you view is one sentence and one next step, with no
  counts about agents or coverage.
- **SBD-A8** Each ask is a title, a state with its wait, one sentence and one action; the first is
  the view's only filled primary.
- **SBD-A9 to SBD-A12** The no-access page: request; request sent; waiting with a reminder;
  declined earlier with the reason. (R8 in the access PRD carries the detail.)
- **SBD-A13** "About this list" is reachable by every role from the page menu and is not styled as
  a warning.
- **SBD-A14** With no agents and no runs, the page is a launch pad: one sentence, one large create
  action named by its noun, at most three starters, no facets or counts.
- **SBD-A15** "+ New automation" is last on the right of the heading row; its menu lists only
  kinds that can be created; it is not a filled primary while another action is.
- **SBD-A16** Creating an agent or an automation is Describe, Check, Confirm; Check shows in plain
  words what was understood, each line changeable; nothing exists until the signing card is signed.
- **SBD-A17** A new agent names the person who answers for it and when it asks a person, and
  states that harm always asks.
- **SBD-A18** An automation's schedule is shown in words with its next run and time zone; no cron
  expression appears on the default view.
- **SBD-A19** Every automation is listed under Scheduled with its state and next run, and can be
  paused, edited, run now or removed from its opened row.
- **SBD-A20** No dial appears on the stream; all four are in the agent's Tune tab, and the Ceiling
  stops read as in the table above.
- **SBD-A21** No held-action view lists a secret parameter by name; "Uses a stored credential"
  appears only when one is used.
- **SBD-A22** Each act on this page is also a CLI verb, a chat ability and an MCP tool on one
  skill (DC-16).

Build units (none is built by this document):

| Unit | Issue |
|---|---|
| The node tells the page whether the viewer answers for an agent and may edit a spec | Axiom #1877 |
| Schedule skills over chat and MCP, plain-word schedules, try once | Axiom #1878 |
| `agents.create`, signed (after ADR-237) | Axiom #1879 |
| Dial storage, `agents.tune` | Axiom #1880 |
| "Uses a stored credential" in the held-action payload | Axiom #1881 |
| The access request, end to end | Axiom #1867 |
| The simple default view | [appkit #158](https://github.com/b-tree-labs/axiom-appkit/issues/158) |
| The Scheduled facet and automation rows | [appkit #159](https://github.com/b-tree-labs/axiom-appkit/issues/159) |
| "+ New automation", the launch pad, creating an automation | [appkit #160](https://github.com/b-tree-labs/axiom-appkit/issues/160) |
| Creating an agent (later phase) | [appkit #161](https://github.com/b-tree-labs/axiom-appkit/issues/161) |
| The held-action card and Details | [appkit #162](https://github.com/b-tree-labs/axiom-appkit/issues/162) |
| One-sentence asks; no chip at zero | [appkit #163](https://github.com/b-tree-labs/axiom-appkit/issues/163) |
| The four no-access states | [appkit #164](https://github.com/b-tree-labs/axiom-appkit/issues/164) |
| The Ceiling stops in plain words | [appkit #165](https://github.com/b-tree-labs/axiom-appkit/issues/165) |

Not drawn, and still open: the "Ask…" state of "+ New"; "Show all settings"; a triggered
automation's Check step; "Ask an agent now"; the reason-and-end-date step of a dial change.

### One object, three lenses (2026-11-12)

**Approved by the owner, 2026-11-12**, with the screens in appkit [`docs/design/proposed-2026-11-13/people-agents-places/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-13/people-agents-places) (web PRD §21.3.2; design
contract DC-19 and DC-20). Nothing is built. It amends capabilities 1, 4, 5 and 11 and "The rest
of the page".

- **Control is the across-agents view of the one Timeline.** It shows **agent runs and
  automations** for every agent that reports here, and nothing else. Its lead says so once:
  "Agent runs and automations: what the agents that report here are doing, did and will do next."
  The page never says "the run stream" or a bare "runs": a product may have other kinds of run
  (a simulation run, a training run, a pipeline run), and those keep their own page and lane and
  never appear here. If an agent carries out a stage of a pipeline, that stage is an agent run and
  appears here, with "Started by" naming what started it.
- **No list of agents on Control.** The page menu's "Agents" opens Team filtered to agents. The
  one line for a silent agent stays, for the person who answers for it (and becomes a view of an
  ask: Needs you PRD section 12).
- **No chat input on Control** (amends capabilities 4 and 11). The composer inside a run's row
  and the teach composer are removed. An opened agent run offers **"Ask it about this agent
  run"**, which opens the one chat addressed to that agent with the run attached; teaching a rule
  is said there, and the read-back lands in the agent's Tune.
- **"Coming up" is the Scheduled facet's per-agent form.** An automation's home is its row on its
  agent's Activity, under "Coming up". Control's Scheduled facet shows the same rows for every
  agent, by the same component, grouped by agent when the list is long.
- **Everyone or Mine.** Mine is the agent runs and automations of the agents the viewer answers
  for, and what the viewer asked for. In this view a row carries its agent's mark and name, and an
  opened agent run names the automation that started it.
- **The agent drawer** (amends capability 5) is the preview of the agent's page: Activity and
  Tune, "Talk to it", "Open", "More…". The Profile tab's content (capability 14) is the first
  section of Tune. Pausing is one step, the Run dial's, behind every Pause control.
- **Creating an agent is not a Control flow.** "New agent" in the "+ New automation" menu opens
  Team's flow, which ends on the new agent; a new agent is not a row among agent runs.
- **Wording.** Approving a held action is "Approve" or "Reject"; the confirm card follows only for
  an action that cannot be undone, titled by the act (web PRD §19.3.1, DC-21). A dial change is
  recorded with who and why. The attestation underneath (capability 15) is unchanged.

Acceptance: PAO-A3, PAO-A4, PAO-A5, PAO-A8, PAO-A9 in the design's README.

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
- **Rollback criteria** (amended 2026-11-12): any state in which the page looks
  healthy to a person whose agent is silent, or in which a person who may edit
  capability specs sees no "Partial list" mark while coverage is partial, is a
  stop-ship. The route is `#/control`; `#/decide` and `#/steer` land on it.

## 10) Contacts & Links

- Product and engineering: platform surfaces
- Decision record: [ADR-137](../adrs/adr-137-steer-is-the-agent-activity-verb.md)
- Related: [ADR-123](../adrs/adr-123-receipts-surface-architecture.md),
  [ADR-134](../adrs/adr-134-severity-carries-consequence.md),
  [ADR-135](../adrs/adr-135-graduated-autonomy.md),
  [ADR-136](../adrs/adr-136-uncertainty-is-a-platform-primitive.md)
