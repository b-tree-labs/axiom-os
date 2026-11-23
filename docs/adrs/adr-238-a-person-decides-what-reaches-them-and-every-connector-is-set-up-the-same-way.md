# ADR-238: A person decides what reaches them, and every connector is set up the same way

**Status:** Proposed (2026-11-09)
**Related:** ADR-057 (connector as a top-level primitive), ADR-059 (connector-first vendor unification), ADR-067 (HERALD inbound gateway), ADR-068 (connector detection), ADR-072 and ADR-073 (capabilities projected to MCP), ADR-110 (connector identity profiles), ADR-178 (the site record), ADR-182 (never down from our own causes), [Axiom notifications spec](../specs/spec-axiom-notifications.md)

## Context

A production node had a failing nightly job for days, and a delivery check that
reported failure every morning. Neither reached a person. The alerts were
addressed to `@operator`, no one had a recipient profile, and the only record
was an inbox nobody opened. The same inbox shows one failing job repeated
several times within a minute.

The parts exist. HERALD ships inbox, email, Slack, Mattermost, Teams, SMS and
push adapters, recipient profiles and a per-class preference table. What is
missing is a model in which those parts add up to a person being in control:

- A publisher has to name a recipient, so a monitor must know who is on duty.
- A shared channel webhook reaches a room. It gives no individual any control.
- Setup has several paths: the `connect` extension, the `connector` extension,
  `axi notifications setup`, and per-vendor skills. ADR-110 proposes one model
  for a configured app and is not settled.
- An address can be saved without anything proving that a message gets there.
- The questions an agent harness should be able to answer, such as "what am I
  subscribed to" and "tell me when this feed stops", have no common tools.

## Decision

### D1. Three things, kept separate

| Layer | Answers | Controlled by |
|---|---|---|
| **Connector** | How the system reaches a service | A site admin, or a person for a personal connector |
| **Endpoint** | Where exactly someone is reached | The person or the role that owns it |
| **Subscription** | What is wanted, by which endpoint, and when | The person or the role that owns it |

An endpoint is owned by a person or by a role. A team's group email address and
a shared chat channel are endpoints owned by a role.

### D2. Publishers emit events and never name people

An event carries a topic, a severity, a scope (such as a site) and a stable
incident key. Routing decides who receives it. `@operator` and similar handles
are roles, resolved to current holders through the RACI model.

### D3. People have maximum control, above one floor

A person chooses topics, endpoints, quiet hours, digest or immediate delivery,
and can pause. The floor applies only to a role's critical events: the holder
chooses how a critical event reaches them, and cannot silence it entirely. An
unacknowledged critical event escalates to the role's backup and to its group
endpoint. A role with no reachable holder is a health failure, not an empty
inbox.

Authorization and classification are not preferences. A person only receives
events within their scope. An event classified above an endpoint's ceiling
arrives as a link to the inbox, with no content.

### D4. One incident, one message

Repeats of an incident key update the open incident. A person is told again
only on a change of severity, on escalation, or on recovery.

### D5. Every connector implements one lifecycle

`detect`, `connect`, `verify`, `status`, `revoke`.

`connect` uses the easiest route the service allows: sign-in, a pasted value,
or a site-wide credential. `verify` delivers a real test and confirms it, and a
connector or endpoint is not active until it has passed. `status` reports who
owns the credential, when it was last proven, and whether it needs reconnecting.
This settles ADR-110's open question of the common base: channels, storage,
calendar and directory connectors use this lifecycle.

### D6. One skill set, every surface

The web app, CLI, chat and MCP call the same skills. Harnesses connect over MCP
with the person's own key. A person may change their own endpoints and
subscriptions; changing another person's requires a policy decision and is
audited.

The skills are `connector.{add,verify,status,revoke}` and
`notifications.{endpoint_add,endpoint_verify,subscribe,unsubscribe,pause,list,ack}`.

### D7. A harness is where a person reads and acts, not where they are paged

A harness only sees an event while it is open. Harnesses list, explain and
acknowledge. Critical delivery goes to an endpoint that can reach a person who
is away, such as chat, email or SMS. Each supported harness has a conformance
test for connecting, subscribing, listing and acknowledging.

### D8. Build for the least common denominator

Every install has to reach a person with nothing but a phone number and an email
address. Inbox, email and SMS are the baseline channels, and they are not tied to
any organization's tenant or apps. Workplace chat, such as Teams or Slack, is an
optional connector that a site configures. No core behaviour, default or floor
may depend on one.

An SMS carries the event's title, severity and a link. It carries no data.

### D9. `@operator` becomes a role that resolves to people

`@operator` is today a default string in the data platform's alert helper. Nothing
assigns it, and it reaches only an inbox. It becomes a role in the site record
(ADR-178). The install names a holder and a backup, each with a verified endpoint
that does not depend on a site's chat system.

### D10. Every notification can be acted on

A notification states what happened and what to do, and it offers the anatomy of
the notifications spec on every channel: a primary action when one exists, Hold,
and Ask. Ask opens a conversation with the owning agent, already holding the
incident's context, and the conversation stays open until the incident closes.

A link is a signed, one-time, expiring reference to one incident. It opens a page;
it does not perform the action, because mail scanners open links on their own.
Acknowledge and snooze complete on that page. Anything that changes a system
requires a signed-in person and a plan they approved (ADR-144). Replies by email,
SMS keyword or chat button are the same actions over another transport, with
SMS never able to approve a change.

**One code path.** A notification action is a capability, invoked through
`invoke_capability` with its own surface name, exactly as the CLI, MCP, chat and
agents invoke one. It adds no action handlers, permission checks or audit records
of its own. A change that needs approval uses the platform's single approval path,
which ADR-236 settles, and notifications add no gate of their own. A conformance
test runs one action from every surface and requires the same result, permission
decision and audit record.

A link needs a surface the recipient can reach. An install must publish its
notification links from a public or otherwise reachable address, or say so.

## Options considered

**Keep routing in each monitor.** Rejected: every monitor would need a roster,
and a change of duty would need a code change.

**Shared channels only.** Rejected: simplest to set up, but no individual
control, and everyone sees every alert until they stop reading.

**Let people mute anything.** Rejected: it reproduces the failure that prompted
this decision.

**Treat a harness as a delivery channel.** Rejected: delivery could not be
assured.

## Consequences

- Monitors become simpler, and they lose the ability to choose a recipient.
- Each site must name a holder and a backup for its operating role. The doctor
  check fails until it has.
- Existing `recipient set` profiles become endpoints and subscriptions.
- `axi notifications setup` and the legacy connect wizard fold into the common
  lifecycle. There are no aliases.
- Quiet hours and failure fallback, deferred in the notifications spec, are
  decided here at the level of behaviour; their details remain with that spec.

## Build order

1. Events with topics and incident keys; collapsing repeats.
2. Endpoints, subscriptions, the floor and escalation.
   Actionable links and the Ask conversation.
3. The connector lifecycle for email and chat, then the web settings page.
4. MCP skills and per-harness conformance tests.
5. Remaining connector families; removal of the older paths.
