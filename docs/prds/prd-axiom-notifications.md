# PRD: `axiom.notifications` — Multi-Channel Notification Primitive (HERALD)

**Status:** Draft (2026-05-30)
**Owner:** Benjamin Booth
**Companion ADR:** [ADR-055](../adrs/adr-055-unified-governance-fabric.md)
**Companion Spec:** [spec-governance-fabric.md](../specs/spec-governance-fabric.md) §3 (connector shape), §4 (receipts), §5.4 (dispatch API), §8.3 (notifications schema)
**Primitive class:** AEOS built-in extension (`axiom.extensions.builtins.notifications`)
**Agent:** HERALD (Generator + Sensor)
**Tracking issue:** [axiom-os#278](https://github.com/b-tree-labs/axiom-os/issues/278)
**Blocked by:** [`prd-axiom-vault.md`](prd-axiom-vault.md) Phase 2 (OAuth flows for channel-adapter authentication)

---

## 1. Elevator Pitch

HERALD gives every agent on the platform *presence and voice* across the channels humans actually use — inbox, mobile push, email, Slack, Teams, Discord, SMS — and gives every recipient a unified inbox where notifications from any agent on any cohort thread together. Every notification carries provenance, respects classification, routes through capability tokens (not raw vendor credentials), graduates from proposal to autonomous per RACI, and produces a delivery receipt that's queryable as a memory fragment. Reply tracking is first-class: when Jim replies to an alert from Slack, HERALD threads it back to the originating agent's context. No peer harness has this; we've been putting it off too long; consumer extensions are blocked.

## 2. Problem / Opportunity

### What's broken today

- **SMTP-only outbound.** Today the only platform-level notification channel is SMTP email. Anything else (Slack, Teams, Discord, push, SMS) lives in extension-specific code, badly, or doesn't exist.
- **No unified inbox.** A user using `axi chat`, Claude Code, Cursor, mobile, and email simultaneously sees notifications scattered across each tool's surface. No coherent "what notifications am I tracking right now."
- **No classification routing.** A notification touching CUI / EAR / ITAR / Part 810 content has no way to refuse routing to non-cleared channels. The check would be ad-hoc per send-site if it existed.
- **No delivery receipts.** "Did Jim see the alert?" is unanswerable. Logs say we sent it; nothing says he received it; nothing threads his reply back to the agent that prompted him.
- **No federation.** Cross-cohort notifications ("peer cohort's HERALD pinged our compliance officer") aren't possible. Federation has the message bus; nothing rides it for notifications.
- **No capability discipline.** Slack / Teams / Discord webhooks live as raw secrets in env vars. Compromised connector code can post anywhere, anytime, until manual rotation.
- **TIDY findings have nowhere to go.** Per [[feedback_stale_systemd_unit_lesson]] crash-loop alerting requirement #3: hygiene findings should reach an operator's preferred channel, not a log file. Today they don't.
- **RACI proposals reach the human through whichever surface the user happens to have open.** No preference model; no follow-up; no "Jim hasn't responded in 4h, escalate to RSO."

### Why now

- Per `docs/working/competitive-parity-gaps.md` §Connectors: messaging-outbound is the load-bearing connector gap, flagged since 2026-04, deferred too long.
- Expman (a domain consumer / EM-005 → EM-008) lands as the first major domain extension during 2026-06; every Operator/Compliance persona need is a notification.
- The vault is being built in parallel (`prd-axiom-vault.md`); notifications need it for OAuth tokens. Now is the moment they co-evolve cleanly.
- The mobile track (a separate future PRD) sits on top of HERALD's outbound + inbound. Building HERALD without thinking about mobile guarantees a re-build later.
- The federation primitives are mature enough to make cross-cohort notifications real, not aspirational. ADR-027 + ADR-028 give the visibility and credibility models.

## 3. Goals & Success Metrics

**Primary goal:** Every agent on the platform can reach any recipient through their preferred channel(s), with provenance + classification routing + capability-token authentication + delivery receipts + reply threading, end-to-end. Recipients see one unified inbox across surfaces.

**Success metrics (post-implementation):**

| Metric | Target |
|---|---|
| Channels supported at v1 | ≥ 6 (inbox, email, Slack, Teams, push-APNS, push-FCM); ≥ 9 at v2 (Discord, SMS, Mobile-native) |
| Connector-shape conformance for every channel adapter | 100% (§3 lint enforces) |
| Delivery receipt p95 (HERALD-direct channels: inbox, push) | < 2 s |
| Delivery receipt p95 (queued channels: email, Teams) | < 30 s |
| Classification routing violations under fuzz | 0 |
| Reply threading correctness (vendor-side reply → originating agent context) | ≥ 99% |
| Cross-cohort notification reachability test pass | 100% (peer A pings cohort B's recipient through federation) |
| Inbox unification — recipient sees same notification across `axi chat`, Claude Code, Cursor, mobile | 100% |
| RACI graduation (proposal → autonomous) of recurring notifications after configurable N approvals | Working in fuzz |
| Time from `axi inbox` to a useful summary | < 200 ms cold; < 50 ms warm |

## 4. Key Users / Personas

| Persona | Primary tasks | Pain today |
|---|---|---|
| **Reactor Operator (Jim)** | Receive transition alerts for samples currently in his custody; reply / acknowledge; sometimes route to RSO. | No mobile path; no acknowledge button; no escalation cadence. |
| **Researcher** | Receive predicted-vs-measured drift notifications post-run; receive collaborator @mentions across cohorts. | Email only; cross-cohort impossible. |
| **Compliance officer** | Receive RACI proposals when an agent wants to act autonomously on regulated content; receive quarterly summary digests. | RACI proposals fire to log; no proposal UX; no preference. |
| **Federation operator** | Be reachable by federated peers without exposing their direct contact info. | No protocol; peers email peers directly. |
| **Extension developer** | Add a notification site to my extension; declare the classification ceiling; not write delivery code. | Email-only API; copy-paste from another extension; no delivery receipt. |
| **Solo user** | "Notify me when CI for my PR finishes" — through whichever channel I'm using right now. | RIVET emits the event; nothing routes it usefully. |
| **TIDY** (agent) | Surface hygiene findings to operator. | Logs only. |

## 5. Scope — Key Capabilities

### 5.1 The send API

```python
# axiom.extensions.builtins.notifications.public_api

async def send(
    envelope: ActionEnvelope,
    recipient: PrincipalRef,
    payload: NotificationPayload,
    priority: Priority = Priority.NORMAL,
    delivery_preferences: Optional[ChannelPreferences] = None,
    reply_target: Optional[ConversationRef] = None,
    dedup_key: Optional[str] = None,
) -> NotificationReceipt:
    """Dispatch a notification. Consults authz, retrieves capability via vault,
       routes through the recipient's preferred channels at-or-below the
       envelope's classification, writes a receipt fragment."""

async def acknowledge(
    receipt_id: str,
    actor: Principal,
    response: AcknowledgmentPayload,
) -> None:
    """Mark a notification acknowledged; bubble back to originating agent context."""

def inbox(
    recipient: Principal,
    filter: Optional[InboxFilter] = None,
) -> Iterator[NotificationReceipt]:
    """The unified-inbox query. Returns receipts the recipient may see."""

async def reply(
    receipt_id: str,
    actor: Principal,
    payload: NotificationPayload,
) -> NotificationReceipt:
    """Thread a reply back to the originating agent's context."""
```

**Acceptance:** every method writes a receipt; receipts thread via `reply_target`; fuzz tests verify no bypass.

### 5.2 The channel-adapter capability kind

```toml
[[extension.provides]]
kind = "channel_adapter"
name = "slack"
entry = "axiom_ext_slack.adapter:SlackChannelAdapter"

[extension.provides.channel_adapter]
direction = "bidirectional"                  # or "outbound" / "inbound"
priority_levels = ["low", "normal", "high", "urgent"]
classification_ceiling = "internal"
supports_threading = true
supports_acknowledge = true
delivery_sla_p95_ms = 2000
connector_ref = "slack"                      # references the connector capability in §3 of spec
```

Each channel adapter is its own AEOS extension. v1 adapters land in the axiom-os monorepo; later adapters live in standalone repos (`UT-Computational-NE/axiom-ext-slack` once extracted).

**Acceptance:** the adapter contract is a typed protocol; tests verify each adapter's conformance; lint enforces the manifest fields.

### 5.3 Initial channel adapters at v1

| Adapter | Direction | Classification ceiling | Notes |
|---|---|---|---|
| `inbox` (axi-internal) | Bidirectional | unlimited (defers to recipient's tier) | The platform-native unified inbox; the baseline every send falls back to |
| `email-smtp` | Bidirectional (sends + IMAP poll for replies) | internal | Retrofitted from existing SMTP module to the channel-adapter shape |
| `slack` | Bidirectional | internal | OAuth via vault; per-workspace |
| `teams` | Bidirectional | internal | OAuth via vault; per-tenant |
| `mobile-apns` | Outbound only | internal | Companion to `axi.mobile` (future) |
| `mobile-fcm` | Outbound only | internal | Companion to `axi.mobile` (future) |

At v2:

| Adapter | Direction | Classification ceiling | Notes |
|---|---|---|---|
| `discord` | Bidirectional | community | Per-server bot |
| `twilio-sms` | Outbound only | internal | Per-recipient phone number; high-priority alert lane |
| `pagerduty` | Outbound only | internal | Incident-response lane |
| `webhook-generic` | Outbound only | configurable | Catch-all for "I want a POST when X happens" |

### 5.4 The unified inbox

`axiom.inbox` is a logical surface over notification receipts.

**CLI:**

```bash
axi inbox                                 # list unread, summarized
axi inbox read <receipt-id>               # mark read
axi inbox reply <receipt-id> "<text>"     # thread reply back
axi inbox forward <receipt-id> --to <principal>
axi inbox snooze <receipt-id> --until <when>
axi inbox preferences                     # per-channel per-class preferences
axi inbox mute <intent-pattern>           # silence routine notifications
```

**Inside `axi chat`** — slash commands `/inbox`, `/inbox read N`, autocomplete on @<recipient> to message recipients.

**In MCP harnesses (Claude Code, Cursor)** — `axiom_inbox__list`, `axiom_inbox__read`, `axiom_inbox__reply` tools (per the MCP server's platform-primitive family).

**In mobile** — native UI consuming the inbox API directly.

Every surface reads the same backing fragments; marking-read on one surface marks read everywhere.

**Acceptance:** end-to-end test: send → see in CLI → mark-read → confirm cleared in MCP — across surfaces.

### 5.5 Classification routing

Per spec §4.2 + §5.4, the envelope's classification + the channel adapter's `classification_ceiling` jointly determine reachable channels. An ITAR-classified notification can never route to Slack (ceiling: `internal`); it can route to `inbox` (unlimited within the recipient's tier) and `email-smtp` if the recipient's email is in a compliant tenant.

The routing decision is a verdict — written as part of the receipt — surfacing *why* a channel was selected or skipped. Operators get a readable rationale.

**Acceptance:** classification routing tests cover every level × every adapter; receipts capture the selection rationale; auditors can replay the decision.

### 5.6 Reply threading

When a recipient replies in Slack (or any bidirectional channel), HERALD's listener (a `service` capability) ingests the reply and:

1. Looks up the channel-side thread identifier in `notification.threads`.
2. Finds the originating `ActionEnvelope`.
3. Constructs an `acknowledge` or `reply` action with the originator's principal as actor.
4. Routes the reply back to the originating agent's context as a memory fragment.

The agent that fired the notification sees the reply as a continuation of its conversation.

**Acceptance:** Slack reply → reaches originating agent context in < 5 s; thread identity preserved across N reply exchanges; correctness ≥ 99% in fuzz suite.

### 5.7 RACI graduation for recurring notifications

A scheduled SLA notification ("sample SR-007 in `processing` >24h") fires its first time as a *proposal* to the operator: "May I send this alert pattern automatically going forward?" After N approvals (configurable; default 3), the pattern graduates to autonomous. Denials reset the counter.

The graduation is per-`(originator_intent, recipient, channel)` — the same alert pattern auto-sends to Jim's mobile but still proposes for the RSO's Teams.

**Acceptance:** graduation flow integration test; receipts capture transitions; the operator sees a "graduate" UX in the inbox.

### 5.8 Federation-aware notification

A notification with `recipient = "axiom://example-consortium/principal/@user:example-org"` routes through Vega's federation handshake:

- GUARD's verdict on the outbound action consults peer trust + cohort policy.
- KEEP delegates a capability bound to the peer's principal.
- The remote HERALD receives the action envelope; their GUARD admits or denies.
- The remote HERALD dispatches to the peer's preferred channels.
- Delivery receipt is dual-classified and federated back to the originator.

**Acceptance:** end-to-end federation drill: cohort A's Expman sends a notification to cohort B's compliance officer; receipt reaches cohort A; reply from cohort B threads back.

### 5.9 Mobile-companion surface (forward-pointing)

HERALD is the substrate for the eventual `axi.mobile` track. v1 of HERALD supports the outbound side (push via APNS/FCM); v1 of mobile (a separate future PRD) consumes HERALD's inbox API + reply API for a native mobile experience.

**Out of scope for this PRD:** the mobile app itself; the push-subscription registration UX; mobile-specific UI patterns. v1 ensures HERALD's APIs accommodate them.

## 6. Non-Functional / Constraints

- **Performance** — per §11 of spec.
- **Delivery guarantees** — at-least-once for outbound (dedupe in receipts); exactly-once for ack/reply (dedup_key enforced).
- **Recipient preferences** — operator can configure per-class per-channel preferences (low-priority → email only; urgent → all channels in parallel).
- **Privacy** — recipient's channel addresses (Slack DM, email, phone) live in the vault; never exposed to senders.
- **Federation neutrality** — local sends work without peer reachability.
- **Cross-platform** — inbox + push targets macOS, Linux, Windows; per `[[feedback_cross_platform_support_matrix]]`.
- **Backward compat** — the existing SMTP module continues to work in parallel during the migration to the channel-adapter shape.

## 7. Timeline (high level)

| Phase | Scope | Target |
|---|---|---|
| Phase 0 | This PRD + spec sections + ADR-055 merged | 2026-06 |
| Phase 1 | `send` + `inbox` API; `inbox` + `email-smtp` adapters; first consumer migration (TIDY findings) | 2026-07 |
| Phase 2 | `slack` + `teams` adapters via vault OAuth; classification routing; receipts in data platform Silver | 2026-08 |
| Phase 3 | Reply threading; RACI graduation; Expman operator notifications cutover | 2026-09 |
| Phase 4 | Federation handoff; cross-cohort drill | 2026-10 |
| Phase 5 | `discord`, `twilio-sms`, `pagerduty`, `webhook-generic`; mobile-companion APIs ready for the mobile PRD to consume | 2026-11 |

Each phase ships shippable value: Phase 1 lets TIDY's findings reach a real human; Phase 2 unblocks Expman notifications; Phase 3 lands the operator-loop completion; Phase 4 unlocks federation; Phase 5 broadens channel coverage and prepares for mobile.

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Notification fatigue — recipients overwhelmed | Per-recipient preferences; mute primitive; RACI graduation defaults conservatively |
| Reply threading correctness (vendor-side thread ID models vary) | Per-vendor adapter test suite; documented threading semantics; runbook entries |
| Classification routing forces fallback to inbox; recipient never reads inbox | Inbox-only routing UX surfaces a "no channel match" indicator; operators can configure escalation |
| Cross-cohort notifications get spammed by hostile peers | Trust-graph credibility scoring; the receiver's RACI requires explicit admission of peer notification routes |
| Slack/Teams OAuth refresh failures silently break Phase 2 | Health-check probes per channel; failures surface via TIDY findings |
| Mobile push registration UX (Phase 5+) is hard | Per-platform registration flow; a separate mobile PRD owns the heavy lift |

**Open questions:**

- (Phase 2) Should classification routing escalate or substitute? If ITAR notification can't go to Slack, should it (a) skip Slack and use inbox, (b) escalate to RSO, (c) both? **Default: skip + escalate to a configured fallback recipient.**
- (Phase 3) Reply threading across channel boundaries (recipient replies via email to a Slack-originated notification) — vendor reply tracking won't help. **Default: HERALD attaches a unique thread-token; replies in any channel containing the token thread.**
- (Phase 4) Federation delivery confirmation — how do we know cohort B's HERALD actually delivered? **Default: cohort B's HERALD federates a delivery receipt back; missing receipt after SLA escalates.**
- (Phase 5) Mobile push subscription — per-device or per-principal? **Default: per-principal with multiple subscription tokens; mobile PRD owns details.**

## 9. Acceptance & Rollout

**Sign-off:**
- Engineering: Ben Booth
- Product: Ben Booth (B-Tree Labs)

**Rollout plan:**
1. Phases 0–1 land on `feat/governance-fabric-notifications` branch.
2. Phase 1 cuts 0.27 with `inbox` + `email-smtp` retrofit.
3. Phase 2 cuts 0.28 with Slack + Teams + classification routing.
4. Phase 3 cuts 0.29 with reply threading + Expman cutover.
5. Phase 4 cuts 0.30 with federation handoff.
6. Phase 5 cuts 0.31 with the broader channel set.

**Rollback criteria:**
- Delivery SLA violated for > 5% of high-priority notifications → throttle outbound; escalate via inbox.
- Reply threading correctness drops below 95% → halt cutover of channels relying on it.
- Federation handoff cryptography flaw → halt Phase 4 cutover; security audit re-engagement.

## 10. Contacts & Links

- Product lead: Benjamin Booth — no-reply@axiom-os.ai
- Eng lead: Benjamin Booth
- ADR: [`adr-055-unified-governance-fabric.md`](../adrs/adr-055-unified-governance-fabric.md)
- Spec: [`spec-governance-fabric.md`](../specs/spec-governance-fabric.md) §3, §4, §5.4, §8.3
- Sibling PRDs: [authz](prd-axiom-authz.md), [vault](prd-axiom-vault.md), [schedule](prd-axiom-schedule.md)
- Related — ADR-027 federated memory, ADR-028 trust graph, ADR-045 RACI, ADR-052 DatabaseProvider; `docs/working/competitive-parity-gaps.md`; [[feedback_stale_systemd_unit_lesson]] (the crash-loop alerting consumer); axiom-os#278 (tracking issue)

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
