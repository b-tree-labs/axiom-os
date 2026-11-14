# PRD: Needs you

**Status:** Accepted (2026-11-10); section 11 added 2026-11-12 (owner decisions)
**Decision:** [ADR-253](../adrs/adr-253-an-ask-is-what-needs-a-person-needs-you-is-its-queue.md). Builds on MVP-21 and MVP-25 in [spec-webapp-buildout-mvp.md](../specs/spec-webapp-buildout-mvp.md), the shell and landing in the web app design (attention bar, D11 "only what you can act on", the signing card D5), and [prd-axiom-notifications.md](prd-axiom-notifications.md) §5.12.

## 1) Elevator Pitch

Everything that needs you, from a person or an agent, in one place, with what was already
done and the choice you have to make, and nothing else. Wherever you are, the same item
reaches you, and acting on it anywhere settles it everywhere.

## 2) Problem / Opportunity

Agents propose, ask and get stuck; incidents need a decision; drafts need a signature;
obligations come due. Today each reaches a person its own way: a chat button, an email, a
Log tray, a CLERK reminder, or not at all. One night produced eight raw emails about one
cause, and the same week an agent's approval request sat in a channel nobody was watching.
The design already names the place, Needs you, and the notification design never pointed
at it.

## 3) Goals & Success Metrics

- A person finds everything they must act on in one queue: no ask exists outside it.
- Nothing in it is unactionable: every row has a working primary action (D11).
- The most frequent ask is at most one click from landing (MVP-21-A2).
- An ask resolved in one place is resolved everywhere within seconds, and stops reminding.
- Notifications per resolved ask fall: one notification, then reminders only while open.

## 4) Key Users / Personas

Everyone signed in sees their own asks. Common cases by archetype: an operator signs and
co-signs; a site manager approves promotions, publications and agent actions; an admin
fixes what the agents could not (node health, a stuck feed); an experimenter answers an
agent's question about a request; an agent's accountable person decides what it asked.

## 5) Scope — Key Capabilities (MVP)

**5.1 The ask.** One record per thing that needs a person: for whom (person or role), from
whom (actor, and an agent's accountable person), one plain sentence, a link to the thing,
what was already tried, the choices, a due time or consequence, and its history. Kinds:
*Approve* (an agent's or a person's request), *Sign* (a draft, on the signing card), *Fix*
(an incident the agents could not clear), *Answer* (an agent blocked on a question),
*Due* (an obligation or follow-up), *Decide* (an intake or "Ask…" request).

**5.2 The chip.** The attention bar's "Needs you" chip appears only when the viewer has an
open ask, with the count. Nothing shows when there is nothing to do.

**5.3 The page: an inbox.** Opening the chip shows an inbox, list on the left and the open
ask on the right:

- **List.** One row per ask: what is needed in a few words, from whom, how long it has
  waited or when it is due, and a kind glyph with a word (colour never alone). Grouped:
  *Now* (blocking or urgent), *Due soon* (next 14 days, MVP-21), *Held* (by you, with the
  time it returns). Earlier decided asks sit behind one disclosure.
- **Detail.** The ask's sentence, what was already tried, the thing itself inline (the
  incident's affected subjects, the draft on the signing card, the agent's question with
  its context), and the three choices.
- **Keyboard.** Next and previous, and the signing card's keys: the primary action, H to
  hold, ? to ask (D5). Holding asks for a time and an optional note.
- **Phone.** The list, then the detail full screen; the same three choices.

**5.4 Three choices, everywhere.** Primary (*Approve*, *Sign*, *Fix*, *Answer*) · Hold ·
Ask, on the page, in email (signed one-time links that open the ask in a signed-in
session), on Teams and Slack (buttons), and in chat ("what needs me", then "approve it").
Signing opens the signing card; it never happens from a link or a chat button (D10). "Stop
telling me about this kind" is in the "…" menu.

**5.5 Notifications.** A new ask notifies by the person's Account › Notifications
(channels, quiet hours, what counts as Needs you) and its urgency; reminders repeat while
it is open and stop when it is resolved or held. An FYI (something healed, a run
finished) never enters Needs you; it goes to the daily digest.

**5.6 Agents.** An agent raises an ask with what it tried and its accountable person. It
cannot resolve its own ask and never signs. Site › Automate shows, per agent, the asks it
is waiting on; Team's agent profile shows the same.

**5.7 Sources.** The approval gate, the incident stage (ADR-252), attest drafts,
obligations and CLERK follow-ups, and "Ask…" (MVP-25) all raise asks through one call.

## 6) Non-Functional / Constraints

The queue shows only the viewer's own and their roles' asks (MVP-21-A1), selected by
scope, never loaded and then filtered. Every choice is recorded with who made it. An ask's
content is data, never instruction to an agent that reads it. Generic in Axiom; a product
adds kinds (a co-signature, a missed console check that notifies and never acts) and a
site configures roles and routing.

## 7) Timeline (high level)

1. MVP-21 as written, on the ask store: the chip, the inbox page, Due and Held drafts,
   approvals from the gate.
2. Incidents become *Fix* asks (ADR-249 phase 3 folds in here); email, Teams and chat
   carry the three choices.
3. Agents' *Answer* asks, Site › Automate's waiting view, Team profiles.
4. Sign and Co-sign asks with the signing card (P3).

## 8) Risks & Open Questions

- **Role asks.** When an ask goes to a role, who sees it, and does one person taking it
  remove it from the others? Proposed: everyone in the role sees it until one person acts
  or holds it; then it shows as taken by them.
- **Reminder cadence** per urgency, and whether quiet hours ever yield to urgent asks.
- **Inbox reference.** The layout follows the inbox pattern the owner liked in a
  colleague's harness; the design section should be drawn from it before build.
- **Where an agent's question is answered:** in the ask's detail, or by opening chat with
  the agent and the ask attached.

## 9) Acceptance & Rollout

- **NY-A1.** Every source in 5.7 raises an ask, and no source notifies a person about
  something needing action without one.
- **NY-A2.** Resolving an ask from email, Teams, chat or the page resolves it everywhere
  and stops its reminders.
- **NY-A3.** No row in Needs you lacks a working primary action; an FYI never appears.
- **NY-A4.** An agent cannot resolve an ask it raised; a sign ask can only be resolved on
  the signing card.
- **NY-A5.** MVP-21-A1 and MVP-21-A2 hold.

Behind the `needs_you` flag (MVP-21).

## 10) Contacts & Links

ADR-253, ADR-252, ADR-249, ADR-236, ADR-142; spec MVP-21, MVP-25; web app design: shell,
landing and personal layer, people and agents, Site › Automate.

## 11) Amendments, 2026-11-12

Owner decisions, 2026-11-12, made with the approved Control screens (appkit
[`docs/design/proposed-2026-11-12/site-control/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-12/site-control), screens `b-needs` and `b-access`). Direct links: [README.md](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/README.md), [gallery.html](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/gallery.html), [view23.html](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/proposed-2026-11-12/site-control/view23.html). GitHub shows the HTML as source; clone the repository and open the file to view it. They
win over the sections above where they differ.

**11.1 An ask's shape on a list.** A title, a state with how long it has waited (glyph and word),
one plain sentence, and one action. Nothing else on the row: who may act is already settled by the
ask being in this person's queue, and the proposed step, what was tried and the history are in
the ask's detail (5.3). The first ask, the worst and oldest, carries the view's one filled primary
action; the others are secondary. With nothing waiting, the view is one sentence ("Nothing is
waiting for you."), one hint and one next step, with no counts about the system.

**11.2 The chip at zero.** 5.2 holds: the chip is not rendered when nothing is waiting. The
as-built chip renders "Needs you 0"; that is a defect ([appkit #163](https://github.com/b-tree-labs/axiom-appkit/issues/163)).

**11.3 The facet on Control.** Until the inbox page (5.3) is built, Site › Automate keeps its
Needs you facet, and it reads the same ask records as the chip, selected by the same scope, so
the two counts cannot differ. When the inbox page exists, the facet's place is decided again.

**11.4 The access request is an ask.** A person on a no-access page who requests access raises a
*Decide* ask for the person or role that grants it (5.1, 5.7 "Ask…"). The requester may send one
reminder per 24 hours and may withdraw the request. If the granter declines, their reason is
shown to the requester as written, and the requester may ask again at once, with a note. The
requirement is R8 of [prd-access-and-onboarding.md](prd-access-and-onboarding.md); the mechanism
is Axiom #1867.

Acceptance:

- **NY-A6.** A list row carries a title, a state with its wait, one sentence and one action, and
  exactly one row's action is the filled primary.
- **NY-A7.** With no open ask the chip is absent, and the Control facet's count equals the count
  of the viewer's open asks raised by agents on that site.
- **NY-A8.** An access request appears in the granter's queue as a Decide ask; a second reminder
  inside 24 hours is refused; a decline shows the granter's reason to the requester unchanged.

## 12) A silent agent and a changed type are asks (2026-11-12)

**Approved by the owner, 2026-11-12**, as an input to the attention-signalling design (#1912);
it adds no indicator (appkit [`docs/design/proposed-2026-11-13/people-agents-places/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/proposed-2026-11-13/people-agents-places); web PRD §21.3.2; #1931).

Two things were told only by a line on the Control page, so a person who does not open that page
never learned of them:

- **An agent that has gone silent** raises one ask (Fix) for the person who answers for it: one
  per cause, with "Open the agent", cleared when the agent reports again.
- **A type update that would let an agent do more** raises one ask (Decide) for that person, with
  "Review the change".

The lines on Control become views of those asks. Where and how the count is shown is #1912's to
decide. The Needs you facet on Control still leaves when the inbox page exists (section 11).
