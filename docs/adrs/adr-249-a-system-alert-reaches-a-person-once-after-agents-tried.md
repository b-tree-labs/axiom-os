# ADR-249: A system alert reaches a person once, after agents tried, written for them, with what they can do

**Status:** Accepted (2026-11-10)
**Related:** ADR-060 (cross-agent event routing), ADR-066 (sender registry), ADR-123 (receipts surface), ADR-179 (signed one-time action links), ADR-185 (chain `unmet`), ADR-196 (follow-ups), ADR-236 (deny, ask, notify, allow), PRD `prd-axiom-notifications.md` §5.10 and §5.12

## Context

On 2026-11-10 a node's owner opened eight emails sent within a minute of each
other. Each was titled `[HIGH] scheduled dispatch dead-lettered: data.refresh`
followed by a Python exception, came from a provider's default
`DoNotReply@<guid>` address, and ended in four lines of metadata. All eight had
one cause: a scheduled job that should never have been scheduled for those
connectors. Nothing had tried to fix it, nothing said what it meant or whether
it mattered, and nothing in the email let the owner do anything about it. It
had arrived the night before as well, and would again the next night.

Every piece needed to do better already exists, and none of it is on this path:

- The scheduler's dead-letter hook calls HERALD's `send` directly, with no
  dedup key and no body. HERALD drops only exact repeats, so eight connectors
  failing for one reason is eight emails.
- `AgentBridge` (ADR-060) was meant to be the single route from bus subjects to
  HERALD, and is never attached outside tests.
- Receipts declare conditions with remedies and offer *fix, hold, acknowledge*
  on a case (ADR-123), but no alert becomes a case, and nothing runs a remedy
  before a person is asked.
- Signed one-time action links exist for software updates only (ADR-179).
- The per-agent sender registry (ADR-066) is never populated, so every message
  comes from the node-wide address.
- A failed skill's structured `unmet` fix (ADR-185) is flattened to a string by
  the scheduler before HERALD sees it.

## Decision

**A system alert is an event, not a message. It becomes one incident per cause;
agents attempt the declared remedy first; a person is told only when that fails
or needs their authority; and what they receive is written for them, with the
actions they can take.**

### D1 — One door

System code publishes an alert on the bus (`alert.<area>.<kind>`, carrying the
structured failure: action, subject, error class, `unmet`, evidence reference).
It never calls `send` itself. One alert desk, owned by HERALD, subscribes. The
scheduler's dead-letter hook, health checks and freshness sweeps are its first
producers. The bus is in-process today; the desk persists what it holds, so a
restart loses no incident.

### D2 — One incident per cause

The desk groups alerts by a fingerprint: the action, the error class, and the
unmet requirement (or the message with identifiers removed). Members arriving
within a hold window (default 10 minutes; 1 minute for `urgent`) join one
incident, which lists the subjects it affects. While an incident is open, a
repeat updates it and sends nothing. A resolved incident that recurs reopens
the same incident, and its history says so.

### D3 — Heal first

Each alert kind maps to a receipts condition with a declared remedy: a
registered capability, run through `invoke_capability` under the owning
agent's authority (RACI tier, ADR-045). The desk asks the owning agent to run
it within a bounded budget (attempts and time) before anyone is told. An
incident the remedy clears is recorded and appears in the daily digest; no one
is interrupted. A person is notified only when there is no remedy, the remedy
failed, or the remedy needs authority the agent does not hold. The message says
what was tried and what happened.

### D4 — Written for a person

The message is written from the incident's structured fields by the node's
model, against a fixed shape: what happened, what it affects, what was already
tried, what the reader can do. It is at most five sentences and contains no
stack traces or paths. The model writes from fields, never from raw log text,
and alert content stays data, not instruction (§5.10). The full record
(evidence, logs, receipts) is on the incident's page, linked, never pasted in.
If no model is available, a deterministic template fills the same shape.

### D5 — Actions, one click each

Every person-facing alert offers, as signed one-time links in email (ADR-179,
generalised) and as buttons on interactive channels:

- **Fix**: run the declared remedy now (when one exists and the reader may run it).
- **Get help**: open a conversation about the incident with the owning agent.
- **Ignore, with a note**: snooze until a chosen time, recording why
  (a follow-up, ADR-196, so it comes back).
- **Stop telling me about this kind**: a per-recipient mute with an expiry.

A link lands on the incident page in a signed-in session, and the choice there
is a confirmation, not a signature (§5.7a). Each choice is recorded on the
incident.

### D6 — Cadence by priority

`urgent` goes as soon as the heal budget is spent; `high` goes after the hold
window; `normal` and `low` go only in the daily digest. A digest is sent even
when quiet, as the receipts digest already is.

### D7 — A sender people recognise

The platform's default sender is an address on the platform's own domain,
named for the agent (for example `PLINTH via Axiom`), through the sender
registry (ADR-066). A site sets its own sender in site configuration. The
provider's default address is never used.

## Options considered

- **Better templates on today's path.** It would make each email readable and
  still send eight of them, untried, with nothing to click. It lost because it
  fixes the wording and none of the behaviour.
- **Digest everything.** It ends the flood but delays the alerts that need a
  person now, and still asks people to act on failures that a declared remedy
  could have cleared. It survives as D6, for `normal` and `low` only.
- **Let the model decide what to send.** Grouping, healing and sending are
  policy, and must be testable and deterministic. The model writes the words
  (D4) and decides none of the routing.
- **Send to the program tracker instead of a person.** Some incidents belong
  there, and a follow-up from "ignore, with a note" is one. A node failing
  tonight still needs someone told tonight.

## Consequences

- One cause produces one message, and most causes produce none.
- Every alert kind needs a declared remedy, or an explicit "none" that the
  message says out loud. Producers carry structured failures (`unmet`) to the
  desk instead of exception strings, so the scheduler stops flattening them.
- The desk is a stateful service. It needs its own tests for grouping, holding,
  reopening and restart, and a zero-gap upgrade test (ADR-182).
- The signed action link becomes a platform primitive, not an update-notice
  feature.
- A site configures its sender. Until it does, the platform default is used.

Follow-up work, in order: (1) the desk with D1, D2 and D6, fed by the scheduler's
dead-letter hook; (2) D4 messages, the incident page and the D7 sender;
(3) D5 actions; (4) D3 remedies for the first alert kinds (dead-lettered jobs:
re-run after the environment check; stale feeds: ask the feed's owner).
