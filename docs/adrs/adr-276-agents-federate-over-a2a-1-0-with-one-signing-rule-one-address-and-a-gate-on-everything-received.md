# ADR-276: Agents federate over A2A 1.0 with one signing rule, one address, and a gate on everything received

**Status:** Proposed (2026-11-23)
**Owner:** Ben Booth
**Amends:** [AEOS 1.0](../specs/spec-aeos-1.0.md) §4, §15.1, §18, §19 and Appendix B; [ADR-016](adr-016-multi-node-federation.md) §3 (agent identity); [ADR-020](adr-020-federation-identity-and-relationships.md) (principal naming, extended across nodes); [ADR-061](adr-061-agent-call-protocol.md) §3 (transport lanes become transport providers); [ADR-181](adr-181-objects-from-any-harness-are-absorbed-quarantined-approved-and-served-back-never-granted.md) (extended from imported objects to live sessions); [spec-axiom-notifications](../specs/spec-axiom-notifications.md) §2 (the kind count); [spec-federation](../specs/spec-federation.md) §4.4 and the heartbeat tables
**Related:** [ADR-027](adr-027-federated-memory.md), [ADR-028](adr-028-trust-graph.md), [ADR-087](adr-087-cross-mem-portable-cross-harness-memory.md), [ADR-143](adr-143-attestations-are-signed-into-a-verifiable-chain.md) (the attestation canonical form), [ADR-231](adr-231-a-chain-of-hand-offs-has-executor-semantics.md) (chains), [ADR-232](adr-232-capabilities-may-be-agent-backed-and-dispatch-is-a-recorded-verdict.md), [ADR-236](adr-236-one-permission-model-deny-ask-notify-allow.md) (deny, ask, notify, allow), [ADR-253](adr-253-an-ask-is-what-needs-a-person-needs-you-is-its-queue.md) (the Needs you queue)
**Specs written with it:** [spec-agent-accord-protocol](../specs/spec-agent-accord-protocol.md) (the A2A 1.0 profile and the wire shapes), [spec-aeos-federation-conformance-kit](../specs/spec-aeos-federation-conformance-kit.md) (fixtures and the suite)

## Context

AEOS names A2A in its abstract and in Appendix B, and the manifest example pins
`a2a = ">= 0.3"`, but no section says what an implementation must serve, sign or
accept. Appendix B points at `spec-agent-accord-protocol.md`, which did not exist.
The first outside implementation of agent-to-agent messaging between Axiom nodes
is now proposed, so the gaps are concrete: two implementations cannot agree on a
card, a signature or an address when the standard does not say what they are.

What was checked in this tree before writing (paths relative to the repo root):

- **A2A is unspecified.** `spec-aeos-1.0.md` mentions A2A only in the abstract and
  in Appendix B. `src/axiom/vega/federation/agent_card.py` builds an "A2A Agent
  Card per v0.3" with `axiom_node_id`, `axiom_profile` and `axiom_extensions`; it
  has no owner, no signature and no interfaces list.
- **The accord spec is missing.** `docs/specs/spec-agent-accord-protocol.md` did
  not exist; `docs/papers/whitepaper-aeos.md` also cites it.
- **Signing is per-object.** The federation gateway's `Signer` returns a hex
  signature over `json.dumps(..., sort_keys=True, separators=(",", ":"),
  default=str)` (`src/axiom/vega/federation/gateway.py`). Digests sign the ASCII
  of a hex SHA-256 over `json.dumps(sort_keys=True)` with default separators
  (`digest.py`); findings do the same (`src/axiom/findings/finding.py`). Canary
  reports, memory bundles, maintenance requests and attestation records each
  have their own canonical form, some hex, some base64. ADR-143's attestation
  form is the closest to RFC 8785 (sorted keys, no whitespace, no floats).
- **The kind count disagrees.** AEOS 1.0 §4 says the eighth kind is `prompt`.
  `spec-axiom-notifications.md` §2 and the notifications extension's ADR-001
  (`src/axiom/extensions/builtins/notifications/docs/decisions/`) call
  `channel_adapter` "the 8th kind". AEOS 0.1 §4 already settled this:
  `channel_adapter` is runtime-consumed, alongside `signal_type`,
  `safety_check`, `api` and `recipe`, and the core count stays eight. The
  disagreement is between AEOS 1.0 and the notifications documents, and AEOS 1.0
  never lists the runtime-consumed kinds.
- **Three address forms.** ADR-016 §3: `{human_id}:{agent_type}:{version}`.
  ADR-020: `@name:context`. The proposed messaging extension: `@agent:node`.
  `src/axiom/infra/principal.py` already carries a third reading, the legacy
  node-derived `@laptop:person`, which it calls the inverse of what ADR-020
  should have produced.
- **Peer states.** `NodeState` in `src/axiom/vega/federation/discovery.py` has
  eight values. WARDEN's `_STATES`
  (`src/axiom/extensions/builtins/federation/agents/warden/warden.py`) has ten:
  the same eight plus `quarantined` and `revoked`. `spec-federation.md` §4.4 draws
  eight and no quarantine.
- **Heartbeat.** `spec-federation.md` says a 10 s interval and 3 missed beats (in
  four places). `heartbeat.py` defaults to 60 s, the daemon takes 60 s, and a
  peer is logged stale after 2 consecutive failures, not 3.
- **The Agent Card is not served.** `spec-federation.md` §3 says the A2A server
  serves `/.well-known/agent-card.json` and that the endpoints "remain to be
  built"; `docs/working/design-resolution-protocol.md` says it is "already
  true". No route in `src/` serves it; `build_agent_card` has no caller outside
  tests. The card's own `url` field points at the card, not at an A2A endpoint.
  One test registers a peer at the 0.2-era path `/.well-known/agent.json`.
- **No workload or conversation object.** ADR-232 found no A2A task lifecycle
  in `src/axiom/vega/federation/`; chains (ADR-231, AEOS §21 "Chains") are local.
- **The deciding gate is guard.** ADR-236 makes guard's `decide()` the only thing
  that answers deny, ask, notify or allow. WARDEN issues federation verdicts
  (peer state, signatures, trust). "WARDEN gates inbound" therefore means WARDEN
  first, then guard; this ADR writes it that way.

## Decision

Ten changes, written into AEOS in this order. The wire detail lives in
`spec-agent-accord-protocol.md`; AEOS §19 carries the normative rules.

### 1. An A2A 1.0 profile

AEOS federation is A2A 1.0. A node MUST publish a signed Agent Card at
`/.well-known/agent-card.json` and MUST send `A2A-Version: 1.0`. The card MUST
carry, in one AEOS extension entry (`uri = "urn:aeos:ext:federation:1"`, a URN until AEOS has a domain of its own,
`required = true`): `node_id`, the owner principal, the AEOS version, the
declared extensions (name, version, manifest digest), and the agent principals
the node exposes. The card MUST be signed under rule 2. Every node MUST support at least one
standard A2A binding and SHOULD support JSON-RPC; HTTP+JSON and gRPC are MAY;
private transport providers are allowed (rule 4). A 0.3 client is answered
with `VersionNotSupportedError`, not served on a guess.

*Why JSON-RPC as the recommended binding:* it is the binding every A2A SDK served first, it
runs over any transport that carries HTTP, and it is the binding the existing
0.3-era code assumes. HTTP+JSON is cheaper to inspect and is the likely second.

### 2. One signing rule

Every object that crosses a node boundary is signed the same way: a detached
JWS (RFC 7515 Appendix F) with `alg = "EdDSA"` (Ed25519, RFC 8037), protected
header `kid = node_id`, over the RFC 8785 (JCS) canonical bytes of the object
with its `signatures` member removed. This is exactly A2A 1.0's Agent Card
signature, applied to messages, receipts, digests, workload records and gateway
projections. Hex over ad hoc bytes is retired for anything that crosses a
boundary. Signed AEOS objects are restricted so that JCS is easy to get right in
any language: no floats (integers only, within the I-JSON range; decimals as
strings), object member names are ASCII, strings are NFC before signing (the
signer normalises; a verifier never does), and lone surrogates are refused. With
those restrictions JCS and ADR-143's form produce the same bytes, so attestation
records can migrate without a second canonical form.

A verifier MUST resolve `kid` to a full public key it already pinned for that
peer (the trust bootstrap) and MUST NOT accept a key carried only in the object.
`node_id` is a 64-bit prefix of SHA-256 over the public key, so it names a key
but does not prove one.

### 3. Transport is a provider kind

`transport` joins the runtime-consumed kinds (AEOS 0.1 §6), so the core count
stays eight. A transport provider declares `encrypted`, `crosses_nat`,
`locality` (`process`, `host`, `cluster`, `network`, `internet`), and
`authenticates_peer`. The platform picks the provider per peer (ADR-061 §3, now
read as providers rather than three fixed lanes); agent authors never pick.
Reference providers: `loopback` (tests), `in-process`, `nats`, `https`,
`tailnet`, `mtls`. `tailnet` is the default where present. A2A runs over each
one unchanged. AEOS 1.0 §4 gains the runtime-consumed list, and the
notifications documents stop calling `channel_adapter` the eighth kind.

### 4. One principal address across nodes

The address is ADR-020's, extended with an optional node pin:

```
principal = "@" name [ ":" context ] [ "#" node_id ]
```

- `@claude:luke` names the agent `claude` in the context `luke`. Across
  federation only qualified forms are addressable (ADR-020).
- The `@agent:node` form from the messaging proposal is read as
  `@agent:context`, where the context is the node owner's home context. When
  one context spans several nodes, `#node_id` pins one.
- ADR-016's `{human_id}:{agent_type}:{version}` stops being an address. The
  human becomes the card's `owner`, the agent type is `name`, and the version is
  the card's `version`. Instance identity is `(principal, node_id, version)`.

Resolution: a node resolves `context` to nodes from the signed cards of its
federated peers (rule 1), then `name` within the chosen card. A principal that
two peers both claim without a pin is ambiguous and the send is refused with
that reason, never routed by guess.

A delegation token says whom an agent acts for. It is a signed object (rule 2)
with RFC 8693 semantics: `sub` is the principal acted for (a human), `act` is the
agent, `iss` is the issuing node, `aud` is the receiving node, `scope` is the
list of intents allowed, and `exp`. An agent may act across nodes only under a
token, and the receiver evaluates guard with `actor = act` and `on_behalf_of = sub`.

### 5. Workload is a core object

A workload is a unit of delegated work with an accountable owner. Fields:
`id`, `kind`, `owner` (human principal), `steward` (agent principal now
responsible), `origin` (node_id), `parent`, `root`, `idempotency_key`,
`state`, `admission` (the receiving node's decision receipt), `a2a` (`task_id`,
`context_id`), `deadline`, `priority`, `inputs`, `artifacts` and `receipts`.

- **States** are A2A 1.0's task states: `submitted`, `working`,
  `input-required`, `auth-required`, `completed`, `failed`, `canceled`,
  `rejected`. A workload held for a person is `submitted` with reason `held`,
  so no ninth state is invented.
- **Admission** is the inbound gate (rule 7) on `workloads.offer.<tier>`. A
  refusal is `rejected` with the reason.
- **Transfer** of stewardship is a record signed by the outgoing and incoming
  steward's nodes. The owner never changes by transfer.
- **Parent and child.** Splitting creates child workloads naming `parent` and
  `root`, on any node. A canceled, failed or orphaned parent cancels its children
  at their next step (AEOS §21 "Chains", §21.6).
- **Idempotency.** `(origin, idempotency_key)` is unique at the receiver for at
  least 24 hours. A repeat with the same canonical body returns the existing
  workload; a repeat with a different body is refused.
- **Receipts.** Each state change appends a signed receipt carrying the digest
  of the previous one, so a workload's history is a hash chain any node holding
  it can verify.

A manifest declares the workload kinds an extension can steward
(`accepts_workloads`, rule 8); each is published on the Agent Card as a skill
with id `workload:<kind>`.

### 6. Conversation is a core object

A conversation has an `id`, participants with roles (`owner`, `member`,
`observer`), and messages. People and agents are both participants, so an agent
can be invited into a person's thread and a person into an agents' thread. An
invitation is a signed object naming the inviter, the invitee and the role; it
takes effect when the invitee's node accepts it. On the wire the conversation id
is the A2A `contextId` (`aeos:conv:<origin node_id>:<ulid>`), which every AEOS
node MUST accept as a client-provided `contextId`.

**Normative:** content received from a peer is data, never instructions. A
runtime MUST NOT place peer text where a model reads instructions, MUST mark it
with its source principal and node, and MUST turn anything a peer asks for into
an intent that goes through rule 7. A peer message that says "run this" is a
request to be decided, not a command to be followed.

### 7. Inbound governance

Every received object (card, message, invitation, workload offer, transfer,
receipt, digest, gateway projection) passes, in order:

1. **Signature** under rule 2, against a pinned key.
2. **WARDEN**: the peer is in a state that may send this object (`federated`
   for everything except the card fetch and the trust bootstrap; nothing from
   `quarantined`, `revoked` or `evicted`).
3. **guard** `decide(actor, intent, resource)` with the standard intents below,
   answering deny, ask, notify or allow (ADR-236).

Standard intents: `agents.send`, `agents.reply`, `conversations.invite`,
`conversations.join`, `workloads.offer.<tier>` (`read`, `write`, `act`),
`workloads.transfer`, `workloads.cancel`, `receipts.accept`, `digests.accept`,
`memory.fragments.accept`. Remote principals are resources under one scheme,
`principal://@name:context#node_id`; conversations and workloads are
`conversation://<id>` and `workload://<id>`.

Extensions contribute rules through a public seam: a manifest may name a rules
file in ADR-236's format for its own intents. Contributed `deny`, `ask` and
`notify` rules apply on install; a contributed `allow` is a suggestion a person
accepts once. A contributed rule never widens past a floor, and
`workloads.offer.act` never goes past `ask`.

An `ask` creates an ask in the receiving node's Needs you queue (ADR-253), and
the hold is replicated as a signed record to the owner's home node, so the owner
sees held work in Needs you on whichever node they use. The sender sees the
workload as `submitted`, reason `held`.

### 8. The federation manifest block grows

```toml
[extension.federation]
shareable = true
requires_attestation = true
quarantine_recoverable = true
exposes_agents = ["reviewer"]            # agents reachable from peers, as principals
accepts_workloads = ["review.pr"]        # workload kinds this extension can steward
shared_skills = ["reviews.summarize"]    # skills published on the Agent Card
inbound_rules = "federation/rules.toml"  # optional, rule 7's seam
```

All four new keys default to empty: an extension exposes nothing to peers
unless it says so.

### 9. A harness profile

A live session in another harness (Claude Code, Codex, and others) may appear in
Axiom as an agent: a principal (`@<harness>:<owner context>`, so Luke's Claude
Code session is `@claude:luke`), a roster entry marked `harness-session` with
its harness, owner, posture and node, and a memory bridge through cross-mem
(ADR-087). This extends ADR-181's `axi harness` from imported objects to live
sessions under the same rule: appearing is never a grant. The session holds no
permission of its own; guard decides every action with `actor` set to the
session principal, and the harness's own allow-lists are never imported.
Memory the session writes enters as quarantined fragments and is served only
after approval, as ADR-181 already requires for imports.

### 10. A conformance kit

AEOS federation conformance is claimed by passing a published kit: golden signed
fixtures (JCS vectors, signed cards, messages, receipts, workload chains,
delegation tokens, and negative cases) and a suite that runs the same vectors
from Go and from Python. The first outside user is an independent messaging
extension; Axiom's own federation code is the second. The kit's layout and
vectors are in `spec-aeos-federation-conformance-kit.md`; the fixtures live in
`tests/aeos_conformance/fixtures/`.

## Spec drift fixed on the way

- **Peer states: ten.** `unknown`, `discovered`, `verified`, `trusted`,
  `federated`, `unreachable`, `leaving`, `evicted`, `quarantined`, `revoked`.
  `evicted` removes membership and allows rediscovery; `revoked` withdraws the
  key and is final for it. WARDEN's set is the normative one. Code follow-up:
  add `QUARANTINED` and `REVOKED` to `NodeState`; let WARDEN admit
  `unreachable → quarantined` and define the `unreachable`, `leaving` and
  `evicted` transitions, which it refuses today as `transition_not_allowed`.
- **Heartbeat: 60 s, 3 missed.** The default interval is 60 s and a peer
  becomes `unreachable` after 3 consecutive missed beats (3 minutes). A peer
  reached over a `cluster` or `process` transport MAY be checked as often as
  every 10 s. *Why not 10 s for everyone:* peers include laptops on tailnets and
  home links, liveness of a peer is not latency-critical, and work carries its
  own leases (AEOS §21 "Chains", §21.6). Code follow-up: `_STALE_MULTIPLIER`
  2 to 3, and drive the `unreachable` transition from the heartbeat.
- **The Agent Card is not served.** Marked as a gap in AEOS §19 and
  `spec-federation.md`. Code follow-up: mount `/.well-known/agent-card.json` on
  the serve app as a named public route, sign it (rule 2), set `url` and
  `supportedInterfaces` to the A2A endpoint, and drop the `/.well-known/agent.json`
  path from tests.

## Consequences

**Good.** Two implementations in two languages can interoperate by passing the
same fixtures. One verifier replaces six signing schemes at the boundary. One
address names an agent wherever it runs. Everything received is decided by the
same gate that decides local actions, and held work surfaces where people
already look.

**Costs.** Every boundary signer changes format, so a peer on the old format and
one on the new cannot verify each other; the cutover is a version bump on the
card, not a mix. JCS needs a careful implementation (the restrictions in rule 2
keep it small). The heartbeat change makes failure detection slower (3 minutes
rather than 30 s) on internet links.

**Not decided here.** The accord negotiation patterns (yield, defer, coordinate,
escalate) are specified as workload kinds in the accord spec at the level of
message types; their timeouts and quorum rules wait for the AEOS Appendix D work
on collective decisions.

## Code follow-ups

1. Serve the signed Agent Card at `/.well-known/agent-card.json` (rule 1, drift 3).
2. One `aeos.sign` / `aeos.verify` module implementing rule 2; move the gateway,
   digests, findings and receipts to it.
3. `transport` provider kind and registry; `tailnet`, `https`, `loopback` first.
4. Principal parser for `@name:context#node_id`; delegation token type.
5. Workload and conversation stores, A2A task mapping, receipt chain.
6. Inbound pipeline: signature, WARDEN state check, guard intents and the
   `principal://` scheme; Needs you replication to the owner's home node.
7. Manifest schema: the four new `[extension.federation]` keys; `a2a = ">= 1.0"`.
8. `axi harness` live-session roster entries and the cross-mem bridge.
9. Conformance suite runners in Python and Go over the published fixtures.
10. `NodeState` gains two values; WARDEN transitions; heartbeat threshold 3.
