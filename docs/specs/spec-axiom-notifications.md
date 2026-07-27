# Tech Spec: `axiom.notifications` — HERALD

**Status:** Draft (2026-05-31)
**Implements:** [`prd-axiom-notifications.md`](../prds/prd-axiom-notifications.md)
**Substrate:** [`spec-governance-fabric.md`](spec-governance-fabric.md) §1 envelope, §2 capability tokens, §3 connectors, §4 receipts, §5.4 dispatch API, §8.3 schema
**ADRs locked in this spec:** ADR-001 (channel-adapter as 8th AEOS kind), ADR-002 (centralized classification routing), ADR-003 (OAuth via vault + capability-token wrapping) — all live under `src/axiom/extensions/builtins/notifications/docs/decisions/`.
**Companion ADR:** [ADR-055](../adrs/adr-055-unified-governance-fabric.md)
**Audience:** Engineers implementing HERALD-1/2/3, channel-adapter authors, security reviewers.

The PRD answers *why*. This spec is *how*. It pins the storage model, the adapter shape, reply-threading webhook topology, the classification-routing site, the OAuth ownership split, federation hop semantics, the typed send API, the receipt DDL, the v1 channel set, the performance benchmarks, and the explicitly deferred questions. Everything not pinned here is intentionally Phase-2.

---

## §1. Storage model — per-recipient row in Postgres, projection to bronze later

**Decision.** The unified inbox is **a per-recipient row in `notifications.notifications_inbox` (schema-per-extension per ADR-052)**, with the canonical receipt living in `notifications.delivery_receipts`. A separate event-log projection into the data-platform bronze tier ships in HERALD-2 for analytics; it is **not** the source of truth.

### Justification

- **Single source of truth at the OLTP layer.** Every read surface (CLI, MCP, chat, mobile) hits the same row; "mark read in one surface ⇒ visible everywhere" falls out of the database, not a sync protocol.
- **Per-recipient row, not per-send row.** A single `send()` to three recipients writes three inbox rows and one delivery-receipt row. The receipt is per-dispatch; the inbox is per-recipient-per-receipt. This is what makes `axi inbox` a `WHERE recipient = ?` query and nothing more.
- **Schema-per-extension (ADR-052).** Lives at `notifications.*`. Never written via the data platform (ADR-049 reads only).
- **Bronze projection is HERALD-2.** Analytics ("most-acked patterns by class", "channel reach by hour") is bronze-tier work; the OLTP rows are the warm cache plus the authoritative store.

### Schema (full DDL is in §8)

```
notifications.notifications_inbox     — per-recipient row, surfaces the unified inbox
notifications.delivery_receipts       — per-dispatch row, one per send() invocation
notifications.threads                 — correlation_id ↔ vendor thread mapping
notifications.channel_preferences     — per-recipient per-class channel preferences
notifications.channel_registry        — recipient → channel-address bindings (via vault refs)
notifications.dedup_log               — sliding-window idempotency (per fabric §6.1)
```

### Lifecycle

```mermaid
flowchart TD
    A[Agent calls send envelope, recipient, payload]:::call --> B[HERALD: classification route check §4]:::herald
    B --> C[GUARD authz.decide envelope]:::guard
    C -->|permit| D[KEEP vault.get_capability for channel]:::keep
    D --> E[Channel adapter dispatch]:::adapter
    E --> F[Write delivery_receipt row + N inbox rows + thread row if bidirectional]:::db
    F --> G[Receipt fragment to CompositionService §4 of fabric]:::frag
    classDef call fill:#1f3a5f,color:#ffffff,stroke:#0f1f3a
    classDef herald fill:#2a4d7a,color:#ffffff,stroke:#0f1f3a
    classDef guard fill:#5a3a8a,color:#ffffff,stroke:#2a1a4a
    classDef keep fill:#3a6a4a,color:#ffffff,stroke:#1a3a2a
    classDef adapter fill:#7a5a2a,color:#ffffff,stroke:#3a2a0a
    classDef db fill:#4a4a4a,color:#ffffff,stroke:#1a1a1a
    classDef frag fill:#2a6a6a,color:#ffffff,stroke:#0a3a3a
```

---

## §2. The channel-adapter capability shape

**Decision.** Channel adapters are a **new AEOS capability kind `channel_adapter` (the 8th kind)**, not a subtype of `adapter` and not a synonym of `connector`. A `connector` (fabric §3) is the lower-level vendor-integration capability that owns OAuth + rate-limit + retry; a `channel_adapter` is a HERALD-aware capability that *uses* a connector and additionally declares delivery semantics (direction, priority levels, classification ceiling, threading support, ack support).

### Why a new kind, not a subtype of `adapter`

- `adapter` (in current AEOS) carries no delivery contract. A storage adapter and a messaging adapter share nothing operationally.
- HERALD's lint refuses to dispatch through a capability that doesn't declare a `classification_ceiling`. Re-using `adapter` would either bloat that contract or under-specify HERALD's contract.
- Reuse of `connector`'s OAuth + rate-limit machinery is by composition (`connector_ref`), not by kind inheritance.

### Manifest shape (provider-driven)

```toml
[[extension.provides]]
kind = "channel_adapter"
name = "slack"
entry = "axiom_ext_slack.adapter:SlackChannelAdapterProvider"

[extension.provides.channel_adapter]
direction              = "bidirectional"        # outbound | inbound | bidirectional
priority_levels        = ["low", "normal", "high", "urgent"]
classification_ceiling = "internal"             # max envelope.classification this adapter accepts
supports_threading     = true
supports_acknowledge   = true
delivery_sla_p95_ms    = 2000
connector_ref          = "slack"                # composes with the §3 connector capability
webhook_path           = "/herald/webhook/slack" # mounts under HERALD's webhook router; see §3
provenance_stamp = { fragment_kind = "notification_dispatch", tier = "internal" }
```

### `axi ext lint --strict` enforces

For every `kind = "channel_adapter"` entry:

1. `direction` is one of the three literals.
2. `priority_levels` is non-empty and a subset of the registered priority ontology.
3. `classification_ceiling` parses via `axiom.governance.Classification.from_str`.
4. `supports_threading` is `true` ⇒ `webhook_path` is present.
5. `connector_ref` resolves to an installed `kind = "connector"` capability.
6. The entry's Python target implements `ChannelAdapterProvider` (the AEOS provider-pattern, see §7 + the scaffold).
7. `provenance_stamp.tier` ≤ `classification_ceiling` (you cannot stamp a receipt at a tier the adapter can't carry).

The lint runs in CI for every adapter package and on `axi ext install`.

---

## §3. Reply-threading webhook architecture

**Decision.** HERALD mounts **one HTTP endpoint per adapter family** (`/herald/webhook/slack`, `/herald/webhook/email`, `/herald/webhook/teams`, …). The correlation identifier is baked into the **outbound message at dispatch time** (Slack `metadata.event_payload.correlation_id`, RFC-2822 `Message-ID` with embedded HMAC token for email, Teams adaptive-card `data.correlation_id`). Inbound replies are matched against `notifications.threads`, lifted back into the originating envelope's context via the action envelope's `provenance_parent` chain (HERALD reconstructs a *reply* envelope whose `provenance_parent` is the original receipt fragment).

### Topology

```mermaid
flowchart TD
    subgraph Send[Outbound send]
        S1[send invoked]:::call --> S2[HERALD mints correlation_id, signs with cohort key]:::herald
        S2 --> S3[Channel adapter embeds correlation_id in vendor message]:::adapter
        S3 --> S4[Adapter dispatches to vendor API]:::vendor
        S4 --> S5[Write threads row correlation_id, vendor_thread_id, receipt_id]:::db
    end
    subgraph Reply[Inbound reply]
        R1[Vendor POSTs to herald webhook slack]:::vendor --> R2[HERALD verifies vendor signature]:::herald
        R2 --> R3[Extract correlation_id; lookup threads]:::db
        R3 --> R4[Reconstruct reply envelope; provenance_parent equals original receipt]:::herald
        R4 --> R5[CompositionService writes reply fragment in originator context]:::frag
        R5 --> R6[Originating agent sees reply in next composition turn]:::agent
    end
    classDef call fill:#1f3a5f,color:#ffffff,stroke:#0f1f3a
    classDef herald fill:#2a4d7a,color:#ffffff,stroke:#0f1f3a
    classDef adapter fill:#7a5a2a,color:#ffffff,stroke:#3a2a0a
    classDef vendor fill:#5a5a5a,color:#ffffff,stroke:#1a1a1a
    classDef db fill:#4a4a4a,color:#ffffff,stroke:#1a1a1a
    classDef frag fill:#2a6a6a,color:#ffffff,stroke:#0a3a3a
    classDef agent fill:#5a3a8a,color:#ffffff,stroke:#2a1a4a
```

### Deployment topology

- **Self-hosted node** — webhooks land on a NodePort behind nginx reverse-proxy; the cohort's public hostname maps `/herald/*` to the HERALD service. TLS terminates at nginx; HERALD verifies the per-vendor signature *after* TLS.
- **HPC EC tenant** — webhooks land on the in-tenant ingress controller; same per-vendor signature verification. EC-tier adapters must declare `classification_ceiling = "controlled"`; the ingress path is per-tenant-isolated.
- **Local dev** — `axi notifications webhook serve` opens a tunneled port (via `cloudflared` or `ngrok` depending on availability); the tunnel URL is registered to the vendor app for the dev session and torn down on exit.

### Correlation-id discipline

- Correlation IDs are 128-bit, cohort-key-HMAC'd, and embedded in a vendor-appropriate location. They never appear in user-visible payload.
- A `threads` row is the only authoritative correlation-id ⇒ original-receipt mapping. The vendor's own `thread_ts` / `In-Reply-To` is stored alongside but is **not** load-bearing for correctness (vendors lose/mangle threading; HERALD's correlation-id is what survives).
- Out-of-channel replies (operator forwards an email-originated alert into Slack manually) are handled in HERALD-3 via a `notifications.threads.cross_channel` flag; deferred per §11.

---

## §4. Classification routing — centralized in HERALD, adapters opaque

**Decision.** Classification routing happens **before adapter selection, inside HERALD**, using `axiom.governance.classification.classification_lte`. An adapter never sees an envelope whose `classification > adapter.classification_ceiling`. HERALD refuses to dispatch CUI / EAR / ITAR / Part 810 envelopes over `INTERNAL`-or-lower-ceiling channels, and emits a routing-verdict fragment with the rationale even on the refusal path.

### Site

```python
# inside notifications.send (canonical site, no per-adapter duplication)
from axiom.governance.classification import classification_lte

candidates = [a for a in resolved_adapters
              if classification_lte(envelope.classification, a.classification_ceiling)]
if not candidates:
    return _deny(receipt, reason="no_channel_at_or_below_classification")
```

### Why centralized

- One site, one audit surface. Adapter authors cannot accidentally widen the ceiling.
- The helper lives in `axiom.governance.classification` (already implemented, used by PULSE+authz). HERALD imports it; PULSE imports it; they cannot drift.
- Fuzz testing covers a single function call site, not N per-adapter implementations.

### Behaviour on no-match

- Inbox is **always** available (its ceiling is `CONTROLLED`, i.e. unlimited within the recipient's tier; see PRD §5.3).
- `send()` falls back to inbox-only and writes the rationale into the receipt.
- An optional `fallback_escalation_principal` recipient-preference (Phase 3) triggers an escalation envelope.

### Receipt rationale

The delivery receipt's `routing_rationale` JSON column records the per-candidate decision: `[{adapter: "slack", admitted: false, reason: "ceiling=internal < envelope=controlled"}, {adapter: "inbox", admitted: true, ...}]`. Auditors replay the verdict.

---

## §5. OAuth flow ownership — HERALD dances, vault holds, capability tokens wrap

**Decision.** HERALD owns the OAuth flow (it knows which channels exist and what their flows look like). The vault (KEEP, SEC-1 secrets extension) **stores the tokens at rest**. Capability tokens issued by KEEP wrap every outbound call so adapter code never sees a raw OAuth token.

### Split

| Responsibility | Owner |
|---|---|
| Discover OAuth flow shape (authz code vs client-credentials vs device-code) | HERALD (per channel adapter declaration) |
| Conduct the redirect / device-code exchange UI | HERALD (`axi notifications channels authorize <channel>`) |
| Encrypt + persist refresh + access tokens | **KEEP** (vault, via SEC-1 secrets `SecretBackendProvider`) |
| Refresh on expiry | KEEP (transparent to adapter; capability presents → KEEP refreshes if needed) |
| Hand a usable scoped credential to the adapter | KEEP via `CapabilityToken` (the adapter `invoke` call carries the cap; KEEP's `outbound_call` resolves the cap → cleartext at exactly one site, fabric §5.3) |
| Revoke at vendor + locally | KEEP, on revocation event |

### Why this split

- KEEP is already the *only* plaintext-credential site (fabric §5.3). Putting OAuth tokens anywhere else breaks the static-analysis invariant.
- HERALD knows the channel ontology; KEEP doesn't and shouldn't. KEEP exposes `store_secret(name, classification, backend)` and `outbound_call(capability, request)`; HERALD composes them.
- Capability tokens enable per-(actor × intent × resource) scoping. A Slack workspace token wrapped in a capability scoped to `notification.send` + `resource = slack://workspace/T123/*` can never be repurposed by compromised adapter code.

### References

- SEC-1 secrets extension: `src/axiom/extensions/builtins/secrets/providers/` (provider-driven backends: env/settings/file → OS keychain → HashiCorp / AWS / 1Password).
- KEEP `capability_store`: `src/axiom/extensions/builtins/vault/capability_store.py`.
- The capability lifecycle is fabric §2.

---

## §6. Federation-aware notification

**Decision.** A peer cohort's HERALD sends to a recipient in our cohort by presenting a **capability token issued by their KEEP**, scoped to `notification.send` + the recipient's `axiom://` principal. Our HERALD verifies the cap via the trust graph (ADR-028) and the cohort policy (ADR-027), then dispatches under our own cohort's outbound capability. Trust score gates: ≥ 0.7 autonomous, 0.3–0.7 RACI-deferred, < 0.3 denied at GUARD.

### Outbound (we send to a federated recipient)

```mermaid
flowchart TD
    A[Local agent calls send to axiom://peer/principal/@nick]:::call --> B[HERALD: federation_origin none, recipient is peer]:::herald
    B --> C[KEEP issues federation-hop capability, signed by our cohort key]:::keep
    C --> D[A2A forward to peer HERALD with cap + signed envelope]:::vega
    D --> E[Peer GUARD admits per cohort policy + ADR-028 trust]:::peer
    E --> F[Peer HERALD dispatches to channel; writes receipt; federates receipt back]:::peer
    F --> G[Our receipt updated with peer outcome]:::db
    classDef call fill:#1f3a5f,color:#ffffff,stroke:#0f1f3a
    classDef herald fill:#2a4d7a,color:#ffffff,stroke:#0f1f3a
    classDef keep fill:#3a6a4a,color:#ffffff,stroke:#1a3a2a
    classDef vega fill:#7a3a3a,color:#ffffff,stroke:#3a0a0a
    classDef peer fill:#5a5a8a,color:#ffffff,stroke:#1a1a4a
    classDef db fill:#4a4a4a,color:#ffffff,stroke:#1a1a1a
```

### Inbound (peer sends to us)

1. A2A inbound envelope arrives; Vega verifies peer signature against the cohort registry.
2. GUARD's `decide()` consults ADR-028 trust + ADR-027 cohort admission. The peer's capability token is converted to a local capability per fabric §7.2.
3. HERALD applies the same §4 classification routing **at our cohort's ceilings**, not the peer's. A peer's `INTERNAL` send into our `CONTROLLED` recipient may still be inbox-only if our channels don't admit it.
4. Receipt dual-classified (originator = peer, executor = us); federated back if the peer's cohort visibility admits it.

### Trust score impact

Identical to fabric §7.3. HERALD does no trust math itself; it consults `decide()` and respects the verdict.

---

## §7. The send API — typed signature

**Decision.** The canonical Python signature is:

```python
# axiom.extensions.builtins.notifications.send (public façade)
from axiom.governance import ActionEnvelope, Classification, Principal

async def send(
    envelope: ActionEnvelope,
    recipient: Principal | str,                   # str = axiom://-form for federation
    payload: NotificationPayload,
    *,
    priority: Priority = Priority.NORMAL,
    channel_prefs: ChannelPreferences | None = None,
    reply_target: ReceiptRef | None = None,
    dedup_key: str | None = None,
) -> DeliveryReceipt:
    """Dispatch a notification through the recipient's preferred channels
    at-or-below the envelope's classification.

    Internally: classification route (§4) → authz.decide → vault.get_capability
    per admitted channel → adapter.send → write receipts + inbox rows."""
```

`DeliveryReceipt` is the typed return; the underlying memory fragment is written via CompositionService. `dedup_key` defaults to `hash(envelope.dedup_key + recipient + payload.content_hash)`.

### Manifest declaration site (intent registration)

Extensions that *originate* notifications declare their intents:

```toml
[[notifications.intent]]
name                    = "expman.transition_alert"  # NEVER appears in axiom/ — example only
classification_default  = "internal"
priority_default        = "high"
acks_required           = true
description             = "Sample-transition state alert to custody operator."
```

HERALD validates declared intents at extension install time. Unknown intents at `send()` time produce a warning but do not block (consistent with fabric §1.1 permissive-dev + lint-in-CI posture).

### Channel-adapter provider/runtime split (mirrors secrets PR #296)

```python
class ChannelAdapterProvider(ProviderBase):
    """Factory. Advertises Capabilities + builds runtime adapters."""
    def capabilities(self) -> ChannelCapabilities: ...
    def build(self, config: ChannelAdapterConfig) -> ChannelAdapter: ...

class ChannelAdapter(Protocol):
    """Runtime client. Stateless across sends; receives capability per call."""
    async def send(
        self,
        envelope: ActionEnvelope,
        capability: CapabilityToken,
        recipient_address: str,
        payload: NotificationPayload,
    ) -> AdapterDispatchResult: ...

    async def on_webhook(self, request: WebhookRequest) -> WebhookReply: ...
```

Providers register with `ChannelAdapterRegistry`; the registry resolves at `send()` time. Adding a new channel is a new package + import-time registration, zero platform-code change.

---

## §8. Receipt schema (DDL)

```sql
-- notifications.delivery_receipts — per-dispatch row
CREATE TABLE notifications.delivery_receipts (
    id                       text PRIMARY KEY,
    envelope_json            jsonb NOT NULL,
    intent                   text NOT NULL,
    actor                    text NOT NULL,
    recipient                text NOT NULL,
    classification           text NOT NULL,
    priority                 text NOT NULL,
    channel_selected         text,                -- null on full deny
    outcome                  text NOT NULL,       -- pending|succeeded|failed|denied|expired
    vendor_correlation       text,                -- e.g. slack message ts
    correlation_id           text NOT NULL,       -- our HMAC'd id; powers §3 threading
    routing_rationale        jsonb,               -- per-candidate decision (§4)
    latency_ms               integer,
    error                    text,
    fragment_ref             text,                -- pointer to CompositionService fragment
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_delivery_receipts_recipient ON notifications.delivery_receipts (recipient, created_at DESC);
CREATE INDEX ix_delivery_receipts_correlation ON notifications.delivery_receipts (correlation_id);

-- notifications.notifications_inbox — per-recipient projection
CREATE TABLE notifications.notifications_inbox (
    id              text PRIMARY KEY,
    receipt_id      text NOT NULL REFERENCES notifications.delivery_receipts(id) ON DELETE CASCADE,
    recipient       text NOT NULL,
    classification  text NOT NULL,
    priority        text NOT NULL,
    summary         text NOT NULL,
    body_ref        text,                        -- pointer to fragment if body is large
    read_at         timestamptz,
    acknowledged_at timestamptz,
    snoozed_until   timestamptz,
    muted           boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_inbox_recipient_unread ON notifications.notifications_inbox (recipient, created_at DESC)
    WHERE read_at IS NULL;

-- notifications.threads — correlation_id ↔ vendor thread mapping (§3)
CREATE TABLE notifications.threads (
    correlation_id     text PRIMARY KEY,
    receipt_id         text NOT NULL REFERENCES notifications.delivery_receipts(id),
    channel            text NOT NULL,
    vendor_thread_id   text,
    cross_channel      boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- notifications.channel_preferences — per-recipient per-class preferences
CREATE TABLE notifications.channel_preferences (
    recipient        text NOT NULL,
    classification   text NOT NULL,
    priority         text NOT NULL,
    ordered_channels jsonb NOT NULL,            -- ["slack","email","inbox"]
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (recipient, classification, priority)
);

-- notifications.channel_registry — recipient → channel addresses (vault refs)
CREATE TABLE notifications.channel_registry (
    recipient      text NOT NULL,
    channel        text NOT NULL,
    address_ref    text NOT NULL,                -- vault.secret_refs name (NEVER plaintext)
    classification text NOT NULL,                -- max classification this address may receive
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (recipient, channel)
);

-- notifications.dedup_log — sliding-window per fabric §6.1
CREATE TABLE notifications.dedup_log (
    primitive    text NOT NULL DEFAULT 'notifications',
    actor        text NOT NULL,
    dedup_key    text NOT NULL,
    receipt_id   text NOT NULL,
    expires_at   timestamptz NOT NULL,
    PRIMARY KEY (primitive, actor, dedup_key)
);
```

### Relationship to the receipt fragment store

Every `delivery_receipts.id` has exactly one CompositionService fragment (`fragment_ref` column). The fragment carries the spec §4 shape from `spec-governance-fabric.md`. Querying `axi audit` (authz primitive) pulls fragments; querying `axi notifications list` pulls inbox rows; both reconcile via `receipt_id`.

---

## §9. Initial channels at v1

| Adapter | Status at SEC-1 | Status at HERALD-2 | Direction | Ceiling |
|---|---|---|---|---|
| `inbox` | ✅ ships | ✅ | bidirectional | `CONTROLLED` |
| `email-smtp` | scaffolded protocol only | ✅ ships | bidirectional (IMAP poll for replies) | `INTERNAL` |
| `slack` | scaffolded protocol only | ✅ ships | bidirectional | `INTERNAL` |

Deferred to v2 per PRD §5.3: Teams, Discord, Twilio-SMS, APNS/FCM push, PagerDuty, generic webhook.

### Gating criterion for v2 adapters

A channel adapter graduates from v2-list to v1-list only if **all four** are true:

1. A registered consumer extension declares an intent that targets it.
2. The `ChannelAdapterProvider` lint passes in CI on a real package, not a fixture.
3. Federation drill (peer cohort dispatches through our HERALD via that channel) passes.
4. Operational runbook exists for the channel's auth + webhook setup.

Until then the adapter lives behind `[notifications.channels.<name>] enabled = false` in install config and `axi notifications channels list` displays it as `disabled (v2)`.

---

## §10. Non-functional targets + benchmark plan

From PRD §3:

| Target | Bench |
|---|---|
| Inbox-channel delivery p95 < 2 s | `tests/benchmarks/test_inbox_dispatch_p95.py` — 10K concurrent sends, single-process, in-memory provider |
| Email/Teams (queued) p95 < 30 s | `tests/benchmarks/test_queued_dispatch_p95.py` — exercises the queue tail with a synthetic 2 s vendor RTT |
| Classification routing zero violations under fuzz | `tests/fuzz/test_classification_routing.py` — Hypothesis: random (envelope, adapter) pairs; assert `classification_lte` invariant |
| Reply threading correctness ≥ 99% | `tests/fuzz/test_reply_threading.py` — round-trip correlation IDs through synthetic vendor responses with jitter + reordering |
| Cross-cohort reachability 100% | `tests/integration/test_federation_dispatch.py` — two-cohort harness, peer A → peer B's recipient |
| Inbox query < 200 ms cold / < 50 ms warm | `tests/benchmarks/test_inbox_query_p95.py` — pgbench-style |

Benchmarks land alongside the HERALD-2 + HERALD-3 cuts; SEC-1 ships only the inbox bench + the routing fuzz (both target SEC-1-scope behaviour).

---

## §11. Open questions (explicit deferrals)

| # | Question | Defer to |
|---|---|---|
| Q1 | Cross-channel reply lifting (operator forwards an email-originated alert into Slack manually; does the Slack reply still thread?) | HERALD-3 (`threads.cross_channel`) |
| Q2 | Per-device push subscription registration (multiple iOS devices per principal) | mobile PRD; HERALD-4 |
| Q3 | Quiet-hours model (per-recipient time-window suppression with priority overrides) | HERALD-3 |
| Q4 | Channel-failure escalation policy (Slack 5xx for 5 min → auto-fallback ladder vs page) | HERALD-3 |
| Q5 | RACI graduation curve shape (3 approvals → autonomous is default; should it be class-dependent?) | HERALD-3 + ADR-045 follow-up |
| Q6 | Federation receipt-back SLA (peer cohort's HERALD must federate a receipt within N seconds or we escalate) | HERALD-4 federation drill |

Anything not in this list is either pinned above or out of scope for HERALD entirely.

---

## §12. Cross-references

- `spec-governance-fabric.md` §1 (envelope), §2 (capabilities), §3 (connectors), §4 (receipts), §5.4 (dispatch), §7 (federation), §8.3 (schema)
- `prd-axiom-notifications.md` — the why
- `prd-axiom-authz.md` §5 — `axi audit` over HERALD's receipts
- `prd-axiom-vault.md` Phase 2 — OAuth-token storage (blocking dependency)
- `spec-classification-boundary.md` — tier definitions consumed by §4
- ADR-027 (federated memory), ADR-028 (trust graph), ADR-045 (RACI), ADR-052 (schema-per-extension), ADR-055 (governance fabric), ADR-056 (skill-fn CLI layering)
- Scaffold ADRs (extension-local): `src/axiom/extensions/builtins/notifications/docs/decisions/adr-001-*.md`, `-002-*.md`, `-003-*.md`

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
