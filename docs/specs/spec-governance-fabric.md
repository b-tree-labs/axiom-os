# Tech Spec: Unified Governance Fabric

**Status:** Draft (2026-05-30)
**Implements:** [ADR-055](../adrs/adr-055-unified-governance-fabric.md)
**Consumer PRDs:** [`prd-axiom-authz.md`](../prds/prd-axiom-authz.md), [`prd-axiom-vault.md`](../prds/prd-axiom-vault.md), [`prd-axiom-notifications.md`](../prds/prd-axiom-notifications.md), [`prd-axiom-schedule.md`](../prds/prd-axiom-schedule.md)
**Audience:** Engineers building or extending the four primitives; extension authors consuming them; security reviewers.

This spec is the build-ready substrate. It is the single document every primitive PRD references for the action envelope, the capability-token model, the connector shape, the receipt format, the cross-primitive consult pattern, and the federation hop. Each primitive's PRD specifies what *its* surface looks like; this spec specifies *the shared bones underneath*.

---

## 1. The action envelope

### 1.1 Definition

Every action that crosses a trust, classification, or ownership boundary on the platform carries an `ActionEnvelope`:

```python
# axiom.governance.envelope
@dataclass(frozen=True)
class ActionEnvelope:
    actor:               Principal          # who is acting (Matrix-style @name:context)
    capability:          CapabilityToken    # what the actor is authorized to do
    classification:      Classification     # the data-tier label of the resource
    context:             CompositionContext # memory context this action runs under
    provenance_parent:   ProvenanceRef      # which prior fragment caused this action
    federation_origin:   Optional[PeerId]   # if forwarded by a federated peer
    intent:              ActionIntent       # what the actor wants to do (verb)
    resource:            ResourceRef        # what they want to act on
    deadline:            Optional[datetime] # for time-bounded actions
    dedup_key:           str                # for idempotency
```

The envelope is constructed once at the boundary, immutable across the action's lifecycle, and serialized into receipt fragments verbatim.

### 1.2 Field semantics

**`actor`** — A `Principal` per ADR-026. Names a human or an agent. Agents acting on behalf of a human carry both: `actor = agent_principal`, `actor.on_behalf_of = human_principal`. Federation: a peer-forwarded action carries `actor = peer's principal` and `federation_origin = peer_id`.

**`capability`** — A signed, scoped, time-limited token issued by `axiom.vault`. See §3. Tokens are presented to every action; expired or revoked tokens fail-fast at the boundary.

**`classification`** — A label from `spec-classification-boundary.md`. Levels: `public`, `internal`, `regulated` (CUI / EAR), `controlled` (ITAR / Part 810). The classification is *of the resource being acted on*, not of the actor.

**`context`** — A `CompositionContext` per the memory composition service. Actions inherit the right provenance attribution + memory visibility from this context.

**`provenance_parent`** — The fragment that motivated this action. Required for every action; the value `None` is reserved for synthetic boot-time actions only.

**`federation_origin`** — If `None`, the action originated locally. If set, the action was forwarded by the named peer, and the peer's trust score (ADR-028) gates the action's autonomy ceiling.

**`intent`** — A verb from the registered action ontology. See §1.3.

**`resource`** — Typed reference to the resource. Examples: `ExtensionRef("expman")`, `FragmentRef("memory://…")`, `ChannelRef("slack://…")`, `EndpointRef("https://api.openai.com/v1/chat/completions")`. Resources have a classification that's computed from their content + path; the resource's classification is what populates the envelope's `classification` field.

**`deadline`** — For time-bounded actions (a scheduled job firing, a notification with a TTL). Actions that miss their deadline are not retried beyond the deadline; the receipt records the miss.

**`dedup_key`** — A deterministic key derived from `(actor, intent, resource, content_hash)`. Two actions with the same `dedup_key` are at-most-once-executed in a sliding window per §6.

### 1.3 The intent ontology

Actions name themselves with verbs from a registered ontology, not free-form strings. The verb namespace is namespaced per primitive:

```
authz.permit             authz.deny              authz.propose
vault.issue_capability   vault.rotate_secret     vault.revoke_capability   vault.read_secret
notification.send        notification.deliver    notification.receive
schedule.fire            schedule.skip           schedule.retry            schedule.dead_letter
data_platform.read_silver  data_platform.publish_gold
extension.invoke_tool    extension.invoke_cmd    extension.transition_state
federation.forward       federation.admit_peer   federation.share_fragment
```

New verbs are added by amendment to this spec. The lint refuses an `ActionEnvelope` with an unregistered `intent`.

### 1.4 Construction sites

Three sites legitimately construct envelopes:

1. **The user-input boundary** — a CLI invocation, a chat turn, a service request. The envelope's `actor` is the user; `capability` is a session-scope token; `provenance_parent` is the conversation fragment.
2. **The agent-action boundary** — an agent firing a tool. The envelope's `actor` is the agent; `capability` is the agent's scoped token; `provenance_parent` is the agent's most recent fragment.
3. **The federation-inbound boundary** — a peer forwards an action. The envelope is constructed from the peer's signed payload; `federation_origin` is set; `capability` is the cross-federation token the peer presented.

Every other site that needs an envelope **receives one** from upstream — it does not construct one. The lint catches direct envelope construction outside these three sites.

---

## 2. Capability tokens

### 2.1 Token structure

```python
# axiom.governance.capability
@dataclass(frozen=True)
class CapabilityToken:
    id:               str                  # uuidv7
    issuer:           Principal            # the vault that issued it
    subject:          Principal            # who may present it (the actor)
    intent_pattern:   IntentPattern        # which verbs it permits (e.g. "notification.send.*")
    resource_pattern: ResourcePattern      # which resources (e.g. "channel://slack/team-rsc/#alerts")
    classification_ceiling: Classification # max classification the token may act on
    not_before:       datetime
    not_after:        datetime
    delegation_depth: int                  # how many further delegations are allowed (0 = leaf)
    parent_capability: Optional[str]       # if delegated, the parent token's id
    signature:        Signature            # signed by issuer's key
```

Tokens are cryptographically bound to their issuer (signature verification on every presentation), scoped to a verb + resource pattern (no broad authority), classification-ceiling-enforced (cannot escalate above what was granted), and time-bounded (refusing late presentation).

### 2.2 Lifecycle

1. **Issuance** (`vault.issue_capability`) — caller presents an existing `CapabilityToken` permitting issuance (or, for the bootstrap case, the human's hardware-attested install identity); vault returns a new token narrower than the parent.
2. **Presentation** — every action presents its `capability` at the boundary. Validators check signature + expiry + intent match + resource match + classification ceiling.
3. **Delegation** — a holder of a token with `delegation_depth > 0` can re-issue narrower tokens (lower depth, same-or-narrower scope, same-or-earlier expiry, same-or-lower classification). Cryptographic verification chains parent → child.
4. **Revocation** (`vault.revoke_capability`) — the issuer publishes a revocation record (or a federated peer's GUARD publishes one on its own authority for its-issued tokens). Active sessions are notified within one heartbeat; pending presentations are denied on next check.
5. **Rotation** — when an underlying secret rotates (an OAuth refresh-token cycle, a KMS-rotated symmetric key), all child capability tokens auto-re-issue with the same scope under the new secret. The capability identity is stable; the underlying-secret identity is what rotates.

### 2.3 Capability vs raw credential

The fabric *never* exposes a raw credential (an API key, an OAuth access token, a database password) to a calling agent. The agent presents a capability; the vault dereferences it to the underlying credential and performs the action on the agent's behalf. Implementation: every outbound HTTP call routes through `axiom.vault.outbound_call(capability_token, request)`; the vault is the only process that ever holds the cleartext credential.

Per ADR-055 D3, this is *the* differentiator vs peer harnesses. The implementation discipline holds it.

### 2.4 Federation hop

Cross-cohort capability presentation: a peer presents a token issued by their KEEP, validated against the local trust graph (ADR-028 score must exceed a per-resource threshold), classification-checked against the local `cohort_policy`, and admitted with a transitive identity chain (`subject = peer.principal.on_behalf_of = local.equivalent`).

---

## 3. The connector shape (AEOS-registered capability kind)

### 3.1 Manifest declaration

An extension that integrates with an external service declares a `connector` capability:

```toml
[[extension.provides]]
kind = "connector"
name = "slack"
entry = "axiom_ext_slack.connector:SlackConnector"

[[extension.provides.connector]]
oauth = { flow = "authorization_code", scopes = ["chat:write", "channels:read"] }
vault_binding = "kv:slack/team/<workspace_id>"
rate_limit = { rps = 1, burst = 50, per_endpoint = true }
retry = { max_attempts = 5, backoff = "exponential", jitter = 0.25 }
mcp_surface = { tools = ["send_message", "list_channels"] }
provenance_stamp = { fragment_kind = "external_action", tier = "internal" }
classification_ceiling = "internal"
```

The lint refuses publication of a connector that elides any of these fields.

### 3.2 Wire contract

A connector exposes a uniform Python protocol:

```python
class Connector(Protocol):
    async def authorize(self, envelope: ActionEnvelope) -> CapabilityToken:
        """Acquire a fresh capability token via the configured OAuth flow."""

    async def invoke(
        self,
        envelope: ActionEnvelope,
        verb: str,
        arguments: dict[str, Any],
    ) -> InvocationResult:
        """Run an action against the external service with rate-limit + retry."""

    async def revoke(self, capability: CapabilityToken) -> None:
        """Revoke at the external service if possible; the vault also revokes locally."""

    def mcp_tools(self) -> list[Tool]:
        """Return MCP tool definitions for this connector (per spec-aeos §6)."""
```

The platform provides default implementations for rate-limiting, retry, and provenance stamping; connector authors override only the vendor-specific parts.

### 3.3 OAuth flows owned by the vault, not the connector

Per ADR-055 D3 + §2.3: the connector does not store the OAuth refresh token. The connector hands the authorization-code grant to `axiom.vault`, which exchanges it for the refresh token and stores both. The connector receives back a `CapabilityToken` whose presentation triggers the vault's refresh-cycle logic transparently. Connector code never sees a refresh token.

This is what makes "compromised connector code can't exfiltrate your OAuth tokens" a real property and not a hope.

### 3.4 Registered connectors at v1

The first wave of registered connectors (separately-PR'd extensions; this spec just names them):

| Connector | Vendor | Primary use |
|---|---|---|
| `axiom-ext-slack` | Slack | HERALD outbound channel; Expman operator notifications |
| `axiom-ext-teams` | Microsoft Teams | HERALD outbound channel; institutional users |
| `axiom-ext-discord` | Discord | HERALD outbound channel; community cohorts |
| `axiom-ext-email-smtp` | (any SMTP) | HERALD outbound channel (existing surface, retrofitted to the shape) |
| `axiom-ext-mobile-apns` | Apple APNS | HERALD outbound + axiom-mobile reception |
| `axiom-ext-mobile-fcm` | Firebase Cloud Messaging | HERALD outbound + axiom-mobile (Android) |
| `axiom-ext-twilio-sms` | Twilio | HERALD outbound SMS |
| `axiom-ext-google-drive` | Google Drive | Document ingest |
| `axiom-ext-notion` | Notion | Document ingest |
| `axiom-ext-canvas` | Canvas LMS | Keplo classroom integration |
| `axiom-ext-github` | GitHub | RIVET, REV-U surfaces |
| `axiom-ext-gitlab` | GitLab | RIVET, consumer GitLab mirror |
| `axiom-ext-anthropic` | Anthropic | LLM provider (retrofit) |
| `axiom-ext-openai` | OpenAI | LLM provider (retrofit) |

---

## 4. Receipts

### 4.1 Receipt fragment shape

Every action through any of the four primitives produces a receipt:

```python
# A memory fragment, per CompositionService conventions:
{
  "cognitive_type": "procedural",
  "fact_kind":      "action_receipt",
  "content": {
    "envelope":           <ActionEnvelope as JSON>,
    "primitive":          "authz" | "vault" | "notification" | "schedule",
    "verdict":            "permitted" | "denied" | "deferred_to_human",
    "outcome":            "succeeded" | "failed" | "pending" | "expired",
    "effect_fragments":   [<refs to fragments this action produced>],
    "vendor_correlation": "<external service's tracking id, e.g. slack message ts>",
    "latency_ms":         <int>,
    "error":              <error trace if outcome=failed>
  },
  "provenance_parent": <fragment that motivated the action>,
  "classification":    <inherited from the resource>,
  "ownership":         <inherited from the actor>,
  ...
}
```

### 4.2 Receipt tiers

Receipts live in the memory tier appropriate to the resource's classification: an `internal`-classified action's receipt is internal; a `regulated` action's receipt is regulated. Classification of the receipt is **never** lower than the classification of the resource.

### 4.3 The `axi audit` query surface

A new top-level CLI noun (PRD: `prd-axiom-authz.md` §5) ships with the authz primitive:

```bash
axi audit list --since 7d --primitive notification --actor @jim:example-org
axi audit show <receipt-fragment-id>
axi audit chain <receipt-fragment-id>          # walks provenance_parent backward
axi audit causes <fragment-id>                 # find receipts whose effect_fragments include this
```

Backed by a `governance_fabric_silver` Gold-layer view (D9 of ADR-055) that lifts receipts from each primitive's Postgres schema into the data platform.

### 4.4 Federation visibility of receipts

A receipt's federation visibility follows its classification per ADR-027. Cross-cohort audit ("did peer X's HERALD actually deliver the alert?") works when the receipt's classification permits cross-cohort visibility.

---

## 5. The cross-primitive consult pattern

How the four primitives consult each other without coupling.

### 5.1 The decision protocol

Every primitive that needs to act calls into `axiom.authz` first:

```python
# axiom.authz public API
def decide(envelope: ActionEnvelope) -> Verdict:
    """Returns 'permit' / 'deny' / 'propose_to_human' (RACI) / 'rate_limit' / 'expired_capability'.

    The verdict is itself stamped as an authz_receipt fragment for audit.
    """
```

The verdict is the single source of truth. A primitive that proceeds after `authz.decide` returns `deny` (or fails to call it at all) is a platform-discipline violation; tests enforce this via a static-analysis check (`no_action_without_authz`) over every primitive's call graph.

### 5.2 Token retrieval

Primitives needing a capability for an action call:

```python
# axiom.vault public API
def get_capability(
    actor: Principal,
    intent: ActionIntent,
    resource: ResourceRef,
    classification: Classification,
) -> CapabilityToken:
    """Returns a fresh-or-cached capability; auto-renews if near expiry."""
```

The vault may internally negotiate OAuth refresh, mint a new token from a parent capability, or fetch a stored short-lived token. Callers don't know which.

### 5.3 Outbound action

Primitives delivering to an external service call:

```python
# axiom.vault public API
async def outbound_call(
    capability: CapabilityToken,
    request: HttpRequest,
) -> HttpResponse:
    """Routes the request, attaching the underlying credential the capability dereferences to."""
```

`outbound_call` is the *only* place in the codebase that holds plaintext credentials; static analysis enforces this.

### 5.4 Notification dispatch

```python
# axiom.notifications public API
async def send(
    envelope: ActionEnvelope,
    recipient: PrincipalRef,
    channel: ChannelRef,
    payload: NotificationPayload,
) -> NotificationReceipt:
    """Dispatches with classification routing + delivery-receipt tracking."""
```

`send` internally consults `authz.decide`, retrieves the appropriate capability from `vault.get_capability`, and dispatches through the channel's connector. The receipt is a fragment.

### 5.5 Schedule firing

```python
# axiom.schedule public API
def register(
    envelope: ActionEnvelope,
    cadence: Cadence,
    action: CallableRef,
) -> ScheduleId:
    """Register a recurring action under the given envelope."""

def fire(schedule_id: ScheduleId) -> ScheduleReceipt:
    """Called by PULSE; constructs the firing envelope (inheriting from registration),
       consults authz, executes the action, writes the receipt."""
```

---

## 6. Idempotency, retries, and dead-lettering

### 6.1 Idempotency

Every action carries a `dedup_key` (§1.2). Each primitive maintains a sliding window (default 24h, per-action-class configurable) keyed by `(primitive, actor, dedup_key)`. Re-presentation within the window returns the prior receipt verbatim — no re-execution.

The implementation is a `dedup_log` table per primitive's schema, queried under the same `session_for(<ext>)` ADR-052 contract.

### 6.2 Retries

Failed actions retry per the connector's `retry` declaration (§3.1). Each retry attempt updates the existing receipt (`outcome: pending`, `attempt: N`); the final receipt is a single fragment, not N fragments.

### 6.3 Dead-letter

After max attempts, the receipt's `outcome` becomes `failed` and a `dead_letter` fragment is emitted. Dead-letter fragments are surfaced to TIDY's hygiene findings and to the operator's inbox via HERALD.

### 6.4 At-least-once vs at-most-once

The fabric is **at-least-once by default** for outbound (a Slack send may double-fire if the receipt write fails after the API call), **at-most-once for scheduled jobs** (idempotency window catches double-fires), and **exactly-once for vault operations** (cryptographic single-use nonces on the secret-mutation primitives).

---

## 7. Federation hop semantics

### 7.1 Outbound hop

A locally-originated action targeting a federated peer:

1. Envelope constructed locally.
2. `authz.decide` consults federation visibility for the resource (ADR-027) and the peer's trust score (ADR-028).
3. Capability is *re-issued* by the local vault as a federation-hop token (signed by the local KEEP, subject = peer's principal, intent + resource constrained to what's being forwarded).
4. The peer receives a signed action payload; their GUARD validates against their cohort policy.
5. Their KEEP exchanges the federation-hop token for a local-equivalent capability.
6. The action executes; the receipt is dual-classified (originator + executor) and federated back.

### 7.2 Inbound hop

A peer-originated action targeting our cohort:

1. Inbound payload is signature-verified against the peer's known root key (ADR-022).
2. `authz.decide` enforces our `cohort_policy`'s admission rules.
3. The peer's capability is converted to a local capability via §2.4.
4. Action runs locally; receipt is provenance-stamped with `federation_origin = peer`.

### 7.3 Trust score impact

A peer with a falling trust score (per ADR-028) sees their inbound actions progressively denied: at score 1.0 they are fully autonomous; below 0.7 their actions require explicit human approval per the RACI flow; below 0.3 their actions are denied at the GUARD boundary.

---

## 8. The Postgres schemas

Each primitive owns its own Postgres schema per ADR-052. Cross-primitive reads ride the data platform per ADR-049.

### 8.1 `authz` schema

```sql
authz.verdicts            -- every decide() call's receipt
authz.policies            -- per-resource per-intent rules
authz.delegations         -- ownership delegations from ADR-026 (synced)
```

### 8.2 `vault` schema

```sql
vault.capabilities        -- live tokens (id, subject, intent, resource, expiry, issuer_signature)
vault.parents             -- delegation chain
vault.revocations         -- revocation records
vault.secrets             -- encrypted secret-at-rest (NEVER plaintext)
vault.oauth_state         -- OAuth refresh tokens (encrypted)
vault.rotation_log        -- rotation history
```

Secrets are encrypted with an at-rest envelope key derived from the host's hardware-attested identity (TPM 2.0 / Secure Enclave / Windows TPM where available; PBKDF2-bracketed env-secret fallback otherwise). The PRD details key-hierarchy.

### 8.3 `notification` schema

```sql
notification.outbound     -- send() invocations + delivery state
notification.inbox        -- received notifications per recipient
notification.threads      -- threading state for reply tracking
notification.channels     -- channel registry per principal
notification.preferences  -- per-recipient per-channel preferences
```

### 8.4 `schedule` schema

```sql
schedule.registrations    -- registered schedules
schedule.firings          -- every fire() invocation's receipt
schedule.dead_letter      -- exhausted retries
schedule.locks            -- distributed lock for at-most-once execution
```

---

## 9. The static analysis discipline

Per §5.1 and §2.3, two static-analysis rules are load-bearing:

### 9.1 `no_action_without_authz`

Every public function declared in a primitive's `__init__.py` that takes an `ActionEnvelope` (or constructs one) must call `axiom.authz.decide` before performing any work. The rule walks the AST of the function body checking that:

- `axiom.authz.decide(envelope)` is called.
- The result is checked (`if verdict.is_permitted()` or equivalent).
- The function does not proceed past a `deny` or `propose_to_human` verdict.

CI fails the build on violation.

### 9.2 `no_credential_outside_vault`

The literal patterns matching credential shapes (`api_key`, `bearer_token`, `oauth_access_token`, `password`, `secret_key`, `private_key`, `client_secret`) are forbidden as function-local variables or struct fields outside of `axiom.vault.*` modules.

The rule has an allowlist for *named credential reference* (`KeyRef`, `CredentialRef`) which is the typed name for a pointer that the vault dereferences. References are fine; literals are not.

CI fails the build on violation.

---

## 10. Migration of existing call sites

This spec is additive — existing code keeps working. Migrating an existing call site to consume the envelope is mechanical:

1. **Identify the boundary** — where does the action enter the call site? (CLI, agent, federation inbound.)
2. **Construct the envelope at the boundary** — populate from the available context (user CLI invocation → known actor + classification of the working memory; agent firing → agent's principal + agent's working context).
3. **Consult `authz.decide`** — replace ad-hoc `if user.is_admin:` checks with verdict consumption.
4. **Route credentials through the vault** — replace direct `os.environ["API_KEY"]` reads with `vault.get_capability(...)` + `vault.outbound_call(...)`.
5. **Emit a receipt** — return the receipt to upstream callers.

A migration order is enumerated in [`prd-axiom-authz.md` §5.5](../prds/prd-axiom-authz.md) for the highest-leverage existing call sites (federation peer admission, RAG retrieval, classroom course-fork, LLM provider calls).

---

## 11. Performance + scale targets

- **`authz.decide` latency** — p99 < 5 ms cached, p99 < 50 ms cold. The decision is the hot path of every action; it must not become a perf bottleneck.
- **`vault.outbound_call` overhead** — < 10 ms added latency vs direct vendor call (the vault is mostly look-up + signature attach).
- **Capability token verification** — p99 < 1 ms (Ed25519 signature check + expiry check + cached revocation-status lookup).
- **Receipt write throughput** — > 1000 receipts/sec/node (uses ADR-052's shared engine + connection pool).
- **Schedule firing precision** — ≤ 5 s drift at hourly cadence; ≤ 30 s drift at daily cadence (per [PULSE PRD §6](../prds/prd-axiom-schedule.md)).
- **Notification delivery p95** — < 2 s for HERALD-direct channels (inbox, push); < 30 s for queued channels (email batch, Teams webhook); SLOs per channel-adapter PRD.

---

## 12. Open questions

These are deliberately left open for the PRDs to resolve:

- **Vault encryption hierarchy** — TPM-bound vs Secure Enclave vs cross-platform symmetric: how do we keep one envelope key model across macOS / Linux / Windows? (PRD: `prd-axiom-vault.md` §6)
- **OAuth flow ownership** — does the connector author present the authorization flow (we redirect to their custom URL), or does the vault present a standardized OAuth UX (one branded "Connect Slack" page)? (PRD: `prd-axiom-vault.md` §5.4)
- **Schedule cluster mode** — multi-node deployments: how does PULSE coordinate so a schedule fires exactly once across the cluster? Postgres advisory locks vs Raft vs leader election? (PRD: `prd-axiom-schedule.md` §6)
- **Notification reply threading** — how does HERALD correlate inbound Slack replies with the outbound thread that prompted them, when the vendor's thread identity model differs? (PRD: `prd-axiom-notifications.md` §6)
- **RACI graduation thresholds defaults** — what's the default "N successful firings before autonomous" for schedules / notifications / vault grants? (PRD: each)

---

## 13. Cross-references

- **ADR-055** — the architectural commitment this spec implements
- **ADR-026** — ownership (the rights model capabilities inherit)
- **ADR-027** — federated memory (visibility semantics receipts inherit)
- **ADR-028** — trust graph (peer credibility scoring)
- **ADR-035** — human-principal binding (`accountable_human_id` chain)
- **ADR-045** — RACI graduation (the autonomy ladder)
- **ADR-049** — data platform boundary (cross-primitive reads via Silver/Gold)
- **ADR-052** — database tenancy (each primitive's schema)
- **`spec-classification-boundary.md`** — the classification labels
- **`spec-aeos-1.0.md`** — the AEOS standard the primitives conform to
- **`prd-identity-and-bindings.md`** — external-account binding (the vault consumes this for capability subject resolution)
- **`docs/working/competitive-parity-gaps.md`** — the gap-tracking entries this fabric closes
