# PRD: The Receipts Surface (feature 0 — fleet console web UI)

**Status:** Living. **ADR:** ADR-123. **Spec:** `docs/specs/spec-receipts-surface.md`.
**Program:** `docs/working/receipts-program-plan-2026-09-21.md` (feature 0 of 0→3).

## Problem

The fleet console answers "is my fleet actually healthy" with receipts
— but only through a CLI and raw JSON. The people who need it at a
glance (an operator starting their day, a lead in an incident, the
founder demoing) get a wall of text. And the program's later features
(work claims, budgets, drift) all need the same visual home; building
it once, beautifully, is the DRY move.

## Positioning (founder, 2026-09-24 — build it AS THIS)
This aspect of Axiom is **Agent Oversight, with resolution**: a more
powerful way of keeping an eye on agents. The loop, not the log:
**observe** (receipts, claims from structure) → **adjudicate**
(VERIFIED/UNPROVEN/CONTRADICTED verdicts) → **resolve** (rerun, hold,
demote, direct — decision windows, actively closing discrepancies) →
**learn** (calibration receipts, graduated authority). Monitoring
products stop at observe; the resolution loop is the product. The
trust-ledger substrate does more (infra, humans, seats — the
agent-less litmus still holds) and quietly powers it all, but the
aspect we build, name, and sell is agent oversight that RESOLVES.
Brief content rule (binds R14): every line is adjudicable or
decidable; verified-as-expected activity compresses to one line or
silence; the band itemizes TRUST DELTAS (authority changing hands),
never activity.

## Users

- **Operator**: opens one page, knows in five seconds whether anything
  needs them, and can read the *evidence* for any non-green state
  without SSH.
- **Skeptic/prospect** (guerilla marketing): a screenshot of this page
  must communicate the thesis unaided — green rows visibly *carry
  receipts*; UNPROVEN is visibly not FAILED.
- **Later features**: Proof-of-Done, budgets, and drift render into
  this shell without new chrome.

## Requirements

### R1 — The board tells the truth at a glance
One page: a card per node (rollup color, site, last contact), expanding
to per-kind rows. Every row shows its status AND its evidence string
face-up — evidence is the product, never a tooltip. Statuses render as
five first-class states (GREEN / UNPROVEN / STALE / FAILED / UNKNOWN);
UNPROVEN is visually distinct from FAILED everywhere (color + icon +
label — never color alone).

### R2 — Staleness is visible before it happens
Each kind shows freshness against its declared cadence (e.g. a quiet
countdown or age bar). A node crossing into STALE changes state without
a reload (poll interval ≤ 60s).

### R3 — Nothing renders that the API didn't say
The page is a pure projection of `GET /api/v1/fleet/status` — same JSON
the CLI shows. No client-side reinterpretation of statuses, no derived
optimism. If the API is unreachable, the page says so and shows nothing
stale-as-fresh.

### R4 — Signature state is surfaced, not hidden
`signature_state: unverified` (until fleet P2) renders as a visible
qualifier on every row — the known gap stays face-up, per program
honesty rules.

### R5 — Appkit-native and tokened
Built from appkit components (existing: AppShell, WorkbenchLayout,
DetailDrawer; net-new in appkit: StatusPill, EvidenceRow, StatTile,
NodeCard). Zero bespoke palettes; every color from `axiom-design-tokens`
per ADR-123. Dark default; readable at phone width.

### R6 — Auth respects the platform seam
Served behind the gate; reads ride the cookie session (ADR-123 D3).
A viewer sees exactly their site scope — the 404-not-403 rule intact.

### R7 — Screenshot-honest
The demo path IS production: screenshots for marketing come from the
live page over the real fleet. A fixture/staging mode may exist for
tests but can never be screenshotted into public material (program
rule; enforced socially, stated here so it is citable).

## Non-requirements (v0)

- No actuation of any kind (no restart/rerun buttons).
- No charts/time-series (arrive with budgets + drift, features 2–3).
- No pin/drift view (fleet P2 delivers the data first).
- No mobile app; responsive web only.

## Red-team (2026-09-21)

**Steelman.** The only surface where the thesis is *visible*; every
later feature renders into it; backend risk is zero (the API is
battle-tested); one honest screenshot does more than any deck; and it
is the demo the Omarchy channel and discovery interviews both need.

**Strawman.** (1) "A dashboard for two nodes" — dashboards are opened
twice and forgotten; the kill question will fail. (2) Our own fleet
renders UNPROVEN-heavy today; an amber-dominated board reads to a cold
viewer as "broken product," and the flagship screenshot backfires.
(3) A web surface on the node is new attack surface for exactly the
audience that will check.

**Design responses (now requirements):**
- **R8 — The default view is "what changed," not a wall.** Landing
  view is a change feed (state transitions with evidence), the board
  one click away. A glanceable surface people *return to* is one that
  answers "anything new?" first. The five-second test (R1) is measured
  against the feed.
- **R9 — Amber must sell, not apologize.** UNPROVEN states carry
  educational microcopy face-up: "UNPROVEN means we refuse to guess.
  Green requires: <the missing evidence, named>." The amber board IS
  the pitch — honesty rendered — and the copy makes that legible to a
  cold viewer. (This converts strawman #2 into the differentiator.)
- **R10 — Strictly read-only, gate-fronted, CSP-pinned.** No mutation
  routes exist in the surface's slice; security posture is stated in
  the README so the audience that checks finds the answer written
  down.

### R13 — Every non-green row carries its next action (founder critique, 2026-09-23)
"How do they take action on an UNPROVEN or STALE assertion?" — a row
that renders judgment and evidence but no action is usable only by the
person who built the system. Every non-green (kind, status) with a
known remedy renders a **next-action line**: who does what, with a
copyable command now and a gated-verb affordance later (K2's
HITL-as-opportunity doctrine; actuation stays out of the surface until
it rides the approval gates). Where the remedy is genuinely unknown,
no action renders — an invented action is worse than none. The
targeting that falls out: the board is the site operator's forensic
drill-down and the cold viewer's proof; the daily front door is the
chat/Today layer (the DefaultApp frame), where receipts surface as
answers and proposals.

### R14 — The brief has a hard attention budget
Needs-you is capped (≤3 items, ranked consequence × window urgency);
everything else compresses to a count WITH A TREND ("14 unproven —
unchanged since Friday"). Deltas, never states: an UNPROVEN that was
UNPROVEN yesterday never re-renders; only transitions surface.
Chronic amber leaves the daily plane entirely and becomes a STOCK,
not a stream — one number, one trend, reviewed on its own cadence
(the unproven-green-audit pattern). Silence is enforced: a period
with nothing verified and nothing transitioning renders nothing.
A brief that can grow unboundedly is a feed, and feeds die.

### R15 — Triage is itself a governed seat
What deserves a Needs-you slot is a decision, so it runs as a
Decision-type seat (the graduated-autonomy primitive, dogfooded on
ourselves): a consequence-threshold RULE decides first; the
operator's holds, approvals, and dismissals are its verdict log; a
model decider may shadow and graduates only when it statistically
beats the rule — wearing a per-seat calibration receipt (ADR-126)
like any other seat. Overwhelm becomes a measured, improving
quantity: if the brief surfaces things the operator dismisses, the
receipts prove it and the seat demotes. As trust in any ACTING seat
grows, its approval windows shrink toward zero and its items leave
the brief entirely — graduated autonomy is the overwhelm valve.

### R16 — The in-harness surface (peer to the web surface)
Developers live in their own harness chat; the platform moves in
with them rather than asking them to visit. The web front door is
the operator's and the team's surface; the developer's surface is
their own harness, instrumented:
- **MCP courier verbs** (`axiom_today`, `axiom_receipt`,
  `axiom_direct`) on the composed node MCP. **Deterministic to the
  words (founder, 2026-09-23 — key):** each verb returns the
  ALREADY-COMPOSED answer — the same evaluator/brief output the CLI
  and web render, receipt citations inline — so the harness agent
  merely relays it. The deterministic projection is the RULE at the
  floor: byte-stable, cacheable, unhallucinatable, near-zero cost.
  Marginal tokens MAY be incurred (founder refinement 2026-09-24),
  but only through a Postrule-governed escalation: the answer path
  is itself a graduated Decision seat — a confidence-thresholded
  deferral decides when a query needs model composition beyond the
  projection, model participation graduates through the gate on the
  seat's own verdict log, and the seat wears a calibration receipt.
  Tokens are spent where the gate has PROVEN they buy answer
  quality; the deterministic floor never stops working. (The third
  dogfood of the primitive, after graduation and brief triage.)
- **Session boundaries**: session-start hook prints the one-line
  brief in their terminal; session-end prints the card verdict and
  link (F1 R6's hooks, promoted to a surface requirement).
  Statusline: live glyph with spend + needs-you count.
- **Context injection**: the day-focus and open contradictions reach
  new sessions through composed context and the instruction-file
  write-back targets (AGENTS.md primary + the rules-file set) — K2's
  directive made mechanical.
- **Both directions, same thread**: absorb adapters let the brief
  cite THEIR sessions precisely ("your 14:02 session claimed X; the
  runner disagreed"); accepting a proposed action opens a session in
  THEIR harness, pre-loaded with the artifact.
- **The installable skill** (`/today`, `/did`, `/receipts`) ships as
  the adoption wedge; per-harness parity via the same MCP seam.

### R17 — The case is Today's unit; the resolver swarm closes it
Receipts correlate into CASES (deterministic first: same entity, same
seam, shared window, known causal edges — one down node is one case,
not five items). A resolver (the case-owner persona) independently
gathers, delegates to specialist sub-agents with per-spawn goals/scope
(F1 R5), and returns ONE Proposed Resolution: diagnosis, plan,
per-step gated actions — a single decision card. Human approval is
optional-vs-required per RACI/ADR-114 policy by case class. The
resolver is itself a session with a session card: its resolution
claims are verified like anyone's, and resolution quality is a
calibration-receipted seat that earns wider autonomy. Receipts remain
the evidence inside cases, one zoom level down.

### R18 — No naked problems (the inbox inversion)
The operator is the APPROVER of processing that already happened,
never the processor. A needs-you item may not surface without its
best proposed resolution and the cost of ignoring attached; an item
nobody (agent included) can propose on goes to the stock/trend, not
the daily plane. With R14's cap, windowed defaults (silence has
defined semantics), and enforced quiet, inbox-zero is the steady
state by construction. If dogfood shows the operator triaging raw
receipts by hand, that is a kill-test violation of R15 and the
receipts will show it.

### R19 — Blast radius is computed; "Unknown" is banned by reframing
Every receipt/case renders downstream impact as reachability over
graphs the platform already maintains: schedules, medallion lineage,
version pins, federation topology, capability grants, subscribers —
and for humans, entity owners (ADR-026) + RACI approvers + desk
audiences. Rendered as a strip (▼ N systems · M schedules · K
people), each element a link. Where no edge exists the surface says
"0 declared dependents" — never Unknown: in a declaration-disciplined
platform the graph IS what is declared, and an undeclared dependency
that breaks is itself a finding. F1's structured observations add
OBSERVED edges automatically; observed-vs-declared edges are
claims-vs-reality applied to the dependency graph. Cost-of-wrong
priors come from the bench's exposure-hours data per claim class.

### R20 — The density grammar
One grammar, four zoom levels holding identical information: STRIP
(glyphs only: entity icon, verdict squares, radius triple, window
ring) → LINE → CARD → PAGE. Entity icons (node ▣ · session ◇ ·
seat ◎ · schedule ⏱ · twin ⧉) and verdict glyphs (✓ ? ⏳ ✕ —) are
identical across terminal, statusline, web, and phone. Numbers
wherever the number is the fact. Never color alone; the
accessibility floor holds at every level; jargon is explained once
in a legend, never per-row.

### R21 — The interaction grammar (the friction sweep, 2026-09-24)
- **Answer before reading**: the surface opens with a verdict strip,
  not prose.
- **One decision anatomy**: every card renders Primary (the
  proposal) · Hold · Ask, same order, same position, never a fourth
  action; overflow lives behind Ask (which opens chat scoped to the
  case).
- **Stable order**: items never move while viewed; arrivals badge,
  they don't insert.
- **Undo over confirm**: receipted, windowed actions execute
  instantly with an undo affordance; confirmation appears ONLY where
  the gate classes the action irreversible/outward — confirmation is
  a property of the action's gate class, not a UI habit.
- **Informed ignoring**: every window states its default in place.
- **The keyboard docket**: j/k move, a approve, h hold, ⏎ open,
  ? ask.
- **The page ends**: a visible terminator; quiet days render one
  line and stop.

## Killer-artifact requirements (founder direction, 2026-09-21)

### R11 — The default experience is "Today," across ALL sessions
The landing view (superseding R8's feed-as-list) is the morning brief:
overnight resident work with receipts, yesterday's session cards
(every session, every harness — the chokepoint sees them all), ONE
number for yesterday's spend, anything DRIFTED/STALE, approvals
waiting. Rendered in the surface, summarized in the terminal on first
open, badged on the desktop glyph. Every entry is a decision or a
receipt — never a summary for its own sake.

### R12 — Today steers: the focus directive
The brief carries a day-focus and PROPAGATES it: resident agents read
it at their next tick; responsive sessions receive it over the agent
bus and acknowledge; new sessions get it in composed context. Focus is
AUTONOMOUS-CAPABLE (founder refinement 2026-09-21): the system may
derive and adopt a proposed focus itself — from receipted state only
(incomplete work, incidents, schedules, drift), never from free model
text — with the human's opportunity to edit or override always one
action away, and a human-set focus instantly superseding. Provenance
is always rendered (who set it: human or system), every focus change
is receipted, and the brief shows exactly who acknowledged — an
unacknowledged agent renders as such (the honesty taxonomy applies to
steering too: "directed" is a claim; "acknowledged" is the effect).

## Success criteria

1. Our own two-node fleet renders; a cold viewer can answer "what's
   wrong and how do we know" for the current UNPROVEN service_health
   rows without any explanation.
2. The first guerilla screenshot post needs zero annotation to land the
   thesis.
3. Feature 1 renders its first task receipt into this shell with no
   new layout work.
