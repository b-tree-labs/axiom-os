# ADR-252: One notification path: a notification with a cause becomes an incident

**Status:** Accepted (2026-11-10)
**Supersedes:** [ADR-249](adr-249-a-system-alert-reaches-a-person-once-after-agents-tried.md) D1 (the separate alert entry point). ADR-249 D2 to D7 stand, applied as below.
**Related:** ADR-060 (cross-agent event routing), PRD `prd-axiom-notifications.md` §5.12

## Context

ADR-249 described a system alert as "an event, not a message", published on its
own subject to an alert desk. Built that way, HERALD had two front doors:
`send()` for messages and `publish_alert()` for alerts. Delivery, channels,
preferences, the inbox and receipts were already shared, but two entry points
invite two vocabularies, two kinds of record, and code that has to decide which
one it is calling. "Alert" also drifts toward meaning "urgent notification",
which is the wrong axis: priority is already a field of every notification.

The owner asked the question directly: how is an alert different from a
high-priority notification, and why have two?

## Decision

**There is one notification path. What ADR-249 calls an alert is a notification
that names a cause.**

- `notifications.send()` takes an optional `cause`: the detected condition (what
  failed, its error class, the unmet requirement, a declared remedy). With a
  cause, the notification passes through HERALD's incident stage, which groups
  it with others of the same cause, tries the remedy, and decides when a person
  hears (ADR-249 D2, D3, D6). Without a cause it is delivered as written.
- The distinction is where a notification comes from, not how urgent it is. A
  condition the system detected is grouped, healed and held. A message from or
  for a person (a reply, a reminder, a request for approval) is never rewritten,
  grouped or "fixed".
- Priority stays orthogonal. A low-priority notification with a cause goes to
  the digest; an urgent one without a cause is sent at once.
- "Alert" is not a noun in the code. A notification with a cause has an
  incident; the table is `incidents`, the module `notifications/incidents/`.
- What ADR-249 gives people applies to every notification: the message shape and
  template fallback (D4), the actions (D5) and a recognisable sender (D7). A
  reminder can offer "snooze with a note" exactly as an incident does.

## Options considered

- **Keep `publish_alert` beside `send`.** It already shared delivery, so it
  looked harmless. It lost because two entry points are where two systems start,
  and every producer would have to choose.
- **Make "alert" a priority level.** It conflates urgency with origin: a stale
  feed is low priority and still deserves grouping and a fix attempt, and an
  urgent approval request must never be grouped.

## Consequences

- Producers call `send(..., cause=Cause(...))`. `publish_alert` and `Alert` are
  not built.
- `send()` returns without delivering when a cause is given; the incident stage
  delivers later through the same internals, without the cause.
- The incident stage, its store and its digest are part of HERALD, with HERALD's
  tests and HERALD's upgrade test.
