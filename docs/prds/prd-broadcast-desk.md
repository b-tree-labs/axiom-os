# PRD: The Broadcast Desk (program feature 7 — killer artifact #3)

**Status:** Living. **Spec:** `spec-broadcast-desk.md`.

## Problem

Results that nobody hears about didn't happen. Today the "tell people"
step is manual, repetitive, and lossy: progress notes hand-written into
trackers, update emails composed from memory, posts drafted from
recollection — or, more often, skipped. Changelogs exist but they are
prose claims nobody verifies and nobody reads. Meanwhile the platform
now KNOWS what actually happened, with receipts. The gap is one desk
where the period's verified results become communications — composed
per audience, with the human's voice injected, reviewed in one place,
and dispatched through every subscribed channel.

## Users

- The founder/lead broadcasting to a team channel, a program tracker,
  an update list, a public feed — without living in five compose boxes.
- Subscribers (a teammate, a partner, a stakeholder) choosing a
  channel and a cadence and getting *results*, not noise.
- The guerilla loop: receipt-backed posts drafted where the receipts
  live.

## Requirements

### R1 — Sources are receipts, not recollection
A dispatch draws from the period's ledger: session cards, shipped
merges, fired schedules with outcomes, fleet state changes, drift
verdicts. **Only VERIFIED/receipted results auto-include; UNPROVEN
items appear only flagged as such.** The desk enforces
claims-vs-reality on the way OUT the door — that is its difference
from every changelog and newsletter tool alive.

### R2 — One desk, many channels
Audiences map to channels via the connect fabric (team chat, email,
tracker comments, webhooks; a public feed is just another channel).
Each audience gets a composition appropriate to it (a tracker comment
is not a newsletter), from one underlying result set. Subscriber
preferences (channel, cadence, topics) are honored per audience.

### R3 — The human's voice, injectable in place
Every dispatch is drafted (the platform's voice-drafting capability,
in the operator's registered voice) with **injection always available**
during the window: the human adds their view, reorders, cuts — inline,
in the one desk. Injection is the opportunity, not a precondition
(R4); dispatches record whether the pen was used.

### R4 — Autonomous by default, with the OPPORTUNITY for review
The desk runs end-to-end autonomously (founder refinement 2026-09-21):
compose → preflight WINDOW → send. The window is the HITL opportunity,
not a gate — each audience sets its window (zero for trusted internal
channels; hours for external ones), during which the human may inject,
hold, or kill; absent action, it dispatches on schedule. Everything in
its window renders on the one preflight screen and on Today. Two hard
exceptions stand: outbound must still pass the gate (R5 is not a
window), and public-social drafts remain founder-posted per the
campaign's standing rule. Dispatch is receipted per channel (what went
where, when, and whether a human touched it).

### R5 — Outbound passes the gate
Every dispatch passes the serving/classification gate before leaving:
site rules, classification ceilings, restricted-content screens. A
results digest must be structurally incapable of leaking what the
serving face would refuse to serve.

### R6 — Cadence is the subscriber's, content is the period's
Daily/weekly/on-event cadences per audience; a period with nothing
verified sends NOTHING (an empty dispatch is noise; silence is the
honest message). No streak-filling.

## Non-requirements (v1)

- No public social auto-posting (drafts export for the founder to
  post; the campaign's founder-posts-everything rule stands).
- No inbound replies/analytics beyond delivery receipts (later).
- No per-recipient personalization beyond audience-level composition.

## Red-team

**Steelman.** Closes the loop the whole program opened: verified
results become verified communications; it automates two rituals we
already perform manually (tracker sync, receipt-post drafting); every
piece exists (ledger sources, synthesizer/changelog machinery, voice
drafting, HERALD + connect channels, RACI review, serving gate); and
it is the rare feature that is simultaneously internal-productivity
and marketing infrastructure.

**Strawman.** (1) "Another digest to ignore" — automated updates are
spam with a nicer name. (2) Voice authenticity: a drafted "founder
voice" that reads synthetic damages trust worse than silence.
(3) Leak risk: an automated outbound channel is a data-exfiltration
surface with a scheduler. (4) One desk becomes one bottleneck: if
review piles up, dispatches rot in preflight.

**Design responses.** (1)→R1+R6: receipts-only content and
silence-over-filler make it structurally unlike a digest; subscribers
opt into topics. (2)→R3: injection is the workflow, not an override —
the draft exists to be edited, and dispatches record human
involvement per R4. (3)→R5 (the serving gate is the same code that
refuses restricted serving) + external-defaults-to-review.
(4)→per-audience auto-dispatch for low-stakes channels + staleness
rendering on the desk itself (a dispatch stuck in preflight >N days
goes STALE, visibly — the desk eats its own taxonomy).

## Success criteria / kill test

Success: our own weekly program tracker comment and team update are
produced BY the desk (the ritual this session performed by hand),
with at least one founder-voice injection each; a public-feed draft
lands in the campaign flow.
Kill: if after 30 days our own dispatches are mostly auto-sent with
zero injections and zero reads (delivery receipts flat), the desk is
a scheduler wearing a product's clothes — fold it back into HERALD
notifications.
