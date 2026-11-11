# ADR-253: An ask is what needs a person; Needs you is its queue; a notification delivers it

**Status:** Accepted (2026-11-10)
**Related:** ADR-249 and ADR-252 (a notification with a cause becomes an incident), ADR-236 (deny, ask, notify, allow), ADR-142 (software proposes, a person signs), ADR-123 (receipts surface), ADR-196 (follow-ups), ADR-179 (signed one-time action links), ADR-211 and ADR-223 (the rail; Control is Site › Automate), PRD `prd-needs-you.md`, spec `spec-webapp-buildout-mvp.md` MVP-21 and MVP-25

## Context

Five things on the platform need a person, and each reaches them its own way or not at all:

| What needs a person | Where it lives today |
|---|---|
| An agent wants to do something and needs a yes (ADR-236 *ask*) | the approval gate; buttons on Slack and Teams |
| A detected cause the agents could not fix (ADR-252) | an incident, delivered as a notification |
| A draft a person must complete and sign (ADR-142) | attest's drafts tray |
| A commitment or obligation coming due | CLERK follow-ups; obligations (key expiry, invitations) |
| A person without permission asks someone who has it (MVP-25) | "Ask…", planned to land in "Needs you" |

The web app design already names the place a person sees these: **Needs you**, a chip
in the attention bar that appears only when something needs this viewer, opening the few
items that do (shell; landing D11; MVP-21). The notification design (ADR-249, ADR-252)
never mentions it, so an agent needing a person was not a notification at all, and a
notification that needs a person had no item to resolve.

## Decision

**One object, an *ask*, is anything that needs a specific person or role to act. Needs you
is the queue of a viewer's open asks. A notification is how an ask, or an FYI, reaches a
person who is not looking at Needs you. Acting on an ask anywhere resolves it everywhere.**

- **D1 — Every source raises an ask through one call.** Approval requests, incidents the
  agents could not fix, drafts to sign, items coming due, and "Ask…" requests all create
  an ask through one platform call; none keeps its own queue for people. Each source keeps
  its own record (the incident, the draft, the obligation); the ask points at it.
- **D2 — An ask says what is needed and what was done.** For whom (a person or a role),
  from whom (the actor, and for an agent its accountable person), one sentence of what is
  needed, a link to the thing itself, what was already tried, the choices, and a due time
  or consequence. Written for a person, with the same rules as ADR-249 D4.
- **D3 — Three choices on every ask, the signing card's.** The primary action named for
  the ask (*Approve*, *Sign*, *Fix*, *Answer*), **Hold** (not now, until a time, with a
  note), **Ask** (talk it through with the agent or a person). These are the signing card's
  Sign · Hold · Ask (D5), and notifications' "Primary · Hold · Ask, no fourth button"
  (spec §3a). "Stop telling me about this kind" is a setting in the "…" menu, not a fourth
  button. Signing happens only on the signing card, never from an email or a chat button
  (D10, PRD §5.7a): a link opens the card.
- **D4 — Needs you shows only asks.** An FYI (something was healed, a run finished, a
  digest) never enters Needs you (D11, "only what you can act on"). It goes to the digest
  or the channel the person chose.
- **D5 — A notification points at its ask.** When an ask is raised, its notification
  follows the person's Account › Notifications settings (channels, quiet hours, what counts
  as Needs you) and its urgency; reminders repeat until the ask is resolved or held.
  Resolving the ask in the app, from an email link, from a Teams button or in chat
  resolves it everywhere, and stops the reminders.
- **D6 — Group by cause.** Asks with one cause are one ask (ADR-252; node health as one
  grouped item, personal-layer decision).
- **D7 — Agents ask; they never decide their own asks.** An agent may raise an ask and may
  act once a person chooses; it may not resolve an ask it raised, and it never signs
  (D10). Site › Automate shows, per agent, the asks it is waiting on: a view of the same
  queue, not a second one.

## Options considered

- **Keep each source's own surface.** Approvals in chat buttons, incidents in email,
  drafts in Log. It lost because a person then has to know where to look, which is the
  problem Needs you exists to solve.
- **Make every notification a Needs you item.** It floods Needs you with things nobody can
  act on, against D11.
- **A separate agent-requests inbox under Control.** It would be the second queue this
  decision rules out; Site › Automate shows the agents' waiting asks as a view instead.

## Consequences

- One store of asks, with one API, read by the attention bar, Site › Automate, Team
  profiles, the phone, CLI (`axi notifications list`) and chat ("what needs me"), MVP-21.
- The approval gate, the incident stage, attest drafts, obligations and follow-ups raise
  asks instead of reaching people directly. ADR-249's phase 3 (the incident page and its
  actions) becomes the ask's detail view for an incident, not a separate page.
- Every channel's buttons and links carry the same three choices.
- Account › Notifications gains "what counts as Needs you" for real, as MVP-21 lists it.
