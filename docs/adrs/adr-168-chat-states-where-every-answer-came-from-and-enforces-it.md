# ADR-168: Chat states where every answer came from, and enforces where it may go

**Status:** Proposed (2026-10-06)
**Related:** ADR-054 (LLM tier policy), ADR-030 (federated inference), ADR-071 (LLM as a core primitive),
[spec-chat-model-picker.md](../specs/spec-chat-model-picker.md) (amended),
[spec-chat-slash-commands.md](../specs/spec-chat-slash-commands.md) (new),
[prd-chat-harness.md](../prds/prd-chat-harness.md) (new)

## Context

People who use Axiom chat cannot tell whether a reply came from a model on their own machine, a cloud
provider, or a remote site node they are bound to. Reading the code (not the specs) shows why:

- A configured LLM provider has no placement field. "Local" is inferred from an empty API-key setting, the
  auto-discovered local server, or the `routing_tier` and `requires_vpn` fields.
- The persistent status line shows the model name only, and only after the first turn. `/model` lists names
  without an endpoint. Only `/status` mentions tier.
- Replies carry no origin marker. The picker spec designed one, but it was never built. A site node's chat
  shim reports its model as a fixed alias and adds retrieval metadata, never the generating model or host,
  and its streamed replies carry no metadata at all.
- Binding to a site node selects the retrieval store, not the generating model, so "bound to the node" can
  mean the node's documents answered by a different model. The `/planes` report states configuration only and
  opens no connection.
- **`/model <provider>` bypasses export-control routing.** The provider override is honored before any tier
  logic, so a user holding controlled content can switch to a cloud provider. The HTTP serving path refuses the
  override; the terminal chat does not. No test covers controlled content together with an override.

The leading coding harnesses all offer `/model` and a family of session commands. Matching them is table
stakes. What Axiom can add is what the platform already implements: provider routing tiers, citation
provenance, a durable human-approval queue, a session store, and an action audit chain.

## Decision

1. **Placement is an explicit property of every provider:** `local`, `cloud` or `remote-node`, with a `host`
   for the last. It is derived from existing settings so current configurations keep working, and a setting
   can override it.
2. **One report, many surfaces.** A single `WhereReport` (provider, model, placement, host, tier, retrieval
   source and its placement) is the only source for the always-visible status indicator, `/where`, `/status`,
   `/model`, the reply stamp, and the MCP tool `axiom_chat__where`. Surfaces cannot disagree because they
   share one function.
3. **Every reply is stamped with the provider actually used,** read from the selection the turn really made.
   The stamp does not depend on `ModelStrategy` or auto mode, which are not on the chat path today. A site
   node's chat shim returns the real model, host and placement, in streamed replies too, so the client prints
   something true.
4. **`/model` enforces the routing tier.** A switch (and `@provider` and `/m` per-prompt forms) is refused
   when the target's `routing_tier` does not admit the content class already in the session, with the reason
   stated. This closes the bypass above. The refusal is audited.
5. **Slash commands are a contract, not a list.** Each command is specified with the machinery it reads, a
   status of exists, wire or new, and a conformance test. A command with no foundation is not shipped as a stub.
6. **Honesty over parity.** A command reports what it cannot do (chat cannot call external MCP servers;
   a model price is unknown) instead of printing a plausible value.

## Options considered

- **Label only, do not enforce.** Cheaper, but it leaves a known export-control bypass. Rejected.
- **Stamp from `ModelStrategy`.** The strategy layer is not on the chat path; a stamp derived from it would
  describe a prediction, not the turn. Rejected; the stamp reads the actual selection.
- **Route generation to the node when bound.** A separate, larger decision about what binding means. Deferred;
  this ADR makes the current behaviour visible without changing it.
- **Copy the other harnesses' commands as they are.** Reaches parity and no more, and several (`/diff`,
  `/undo`) have no foundation here today. Rejected as the whole plan.

## Consequences

- Users can always see, and an agent can always ask, where a reply came from.
- Closing the override bypass changes behaviour for anyone who relied on it; the refusal message explains and
  names the permitted path.
- The node chat shim changes (a site release), and the client must tolerate nodes that predate it by
  reporting placement as unverified rather than guessing.
- New work is sequenced by what exists: visibility and enforcement first, commands that need new
  infrastructure (`/diff`, `/undo`, `/fork`) last.
