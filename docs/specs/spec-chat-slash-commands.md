# Spec: Chat Slash Commands and Placement

**Owner:** Ben Booth (B-Tree Labs) • **Status:** Draft • **Last updated:** 2026-10-06
**PRD:** [prd-chat-harness.md](../prds/prd-chat-harness.md) • **Key ADRs:** [ADR-168](../adrs/adr-168-chat-states-where-every-answer-came-from-and-enforces-it.md), ADR-054, ADR-030

## Overview

This spec defines the placement report that every chat surface shares, the tier enforcement on model
switching, and the contract for each slash command. It is written against the code as it is: every statement
about current behaviour comes from reading `src/axiom/extensions/builtins/chat/` and the gateway, and each
requirement carries a status: **Exists**, **Wire** (implemented elsewhere, not wired into chat), or **New**.

## Contracts

### Placement on a provider

```python
Placement = Literal["local", "cloud", "remote-node"]

@dataclass(frozen=True)
class ProviderPlacement:
    placement: Placement
    host: str | None            # required for remote-node; shown to the user
    verified: bool              # False when inferred, or when a node predates the shim change
    source: Literal["setting", "derived"]
```

Derivation when no setting exists: an auto-discovered local server or a loopback endpoint is `local`; a
provider reached through a bound site node is `remote-node`; everything else with a non-loopback endpoint is
`cloud`. `routing_tier` and `requires_vpn` remain separate fields: placement says where it runs, tier says
what content it may receive.

### WhereReport (the single source)

```python
@dataclass(frozen=True)
class WhereReport:
    provider: str; model: str
    placement: ProviderPlacement
    tier: str                    # routing_tier of the active provider
    content_class: str           # the session's classified class
    retrieval: RetrievalWhere    # store kind, host, placement; separate from generation
    override: str | None         # a /model or @provider override in effect
    fallback: list[str]
```

`where_report(agent) -> WhereReport` is the only constructor. The always-visible indicator, `/where`,
`/status`, `/model`, the reply stamp and the MCP tool `axiom_chat__where` call it; none computes placement
itself. A test asserts that all surfaces print identical values for the same state.

### Reply stamp

Each assistant reply ends with one dim line: `↳ <model> · <LOCAL|CLOUD|REMOTE host> · RAG: <store placement>`,
followed by the cost and latency fields of the picker spec §6 when known. It is built from the selection the
turn made (the provider returned by the gateway's selection step), never from `ModelStrategy`, which is not
on the chat path today. Structured output (JSON, SSE) carries the same fields as data, not text.

### Node shim response

The site node's chat shim adds a block beside its retrieval metadata: `x_origin = {model, host, placement}`
reporting the model that actually generated the reply, in streamed replies as a final event. A client that
receives no `x_origin` shows the provider as `remote-node (unverified)`.

### Tier enforcement on switching

`set_provider_override` (used by `/model`, `@provider`, `/m`) must call the same tier check the unconstrained
path uses. Today the override is honored before any tier logic. The rule: refuse when the target's
`routing_tier` does not admit the session's `content_class`; print the reason and the providers that would
be admitted; write an audit record with the requested provider, the content class and the refusal. Any
permitted override path is decided in review and stated here before it is built. The HTTP serving path already
refuses overrides and stays as it is.

### MCP tool

`axiom_chat__where` returns a `WhereReport` as JSON. It is read-only and idempotent.

## Design

### Registration

A command costs four edits today: a handler in `commands.py`, a branch in the plain-CLI dispatcher
(`cli.py`), an entry in `CHAT_META_COMMANDS`, and a line in `cmd_help`. The fullscreen TUI intercepts a few
commands and delegates the rest. `tests/test_command_registration.py` fails if a command is dispatched but not
advertised, but does not check `cmd_help`. This spec requires: one registry that generates completion, the
unknown-command check, and help; and a test that every advertised command has a handler and a conformance
test. Bundled prompt-template commands (`/review`, `/init`) use the existing user-command loader
(`~/.axi/commands/*.md`) and ship in the package.

### Per command

Each row states the machinery read and the failure behaviour. "Unavailable" means print what is missing,
never a plausible value.

| Command | Reads | Output | Unavailable behaviour |
|---|---|---|---|
| `/where` | `where_report` | The report, one block | n/a |
| `/model` | gateway providers, `where_report` | Picker rows `name (model) · PLACEMENT host`; switch confirms placement; refusal per tier rule | Lists providers; says none configured |
| `/status` | existing plus `where_report` | Where block first | n/a |
| `/stats` | `agent.usage`, turn timings | Per turn: latency, tokens, tool calls, cost | A model with no known price shows `unpriced`; today the cost table returns $0 for unknown models and is stale, which must be fixed first |
| `/compact` | session messages, gateway | Summary replaces older turns; citations kept; summary written to memory | Fails with a message, never a stub; the stub that prints "not yet implemented" is removed |
| `/copy` | last reply, clipboard helper, secret scan | Copies the reply and its stamp | Refuses on secret detection and says which pattern |
| `/export` | `SessionStore`, secret scan | Transcript file with stamps | Refuses or redacts on secret detection |
| `/sources` | `scope.last_provenance`, citations | Citations and the grounded or invented verdict | "No retrieval in this turn" |
| `/tools` | `get_all_tools()`, permission store | Each tool with permission state and whether it mutates | Notes that skill tools appear only when `chat.tool_namespaces` is set |
| `/mcp` | MCP server and harness config | Server surface and configured harnesses | States that chat cannot call external MCP servers |
| `/memory` | memory recall behind the serving gate | Search and show | Today chat has no recall index; until wired, says so and offers `axi memory` |
| `/plan`, `/ask` | `set_interaction_mode` | Mode switch | Stated guarantee: tools are not offered to the model; it is not a sandbox |
| `/permissions` | permission store, hold queue | Overrides and held actions | Help text corrected from "revoke" to the implemented `reset` |
| `/review`, `/init` | bundled template files | Run the template through `agent.turn()` | n/a |
| `/fork` | `SessionStore` | New session id with copied messages, owner scope kept | n/a |
| `/diff`, `/undo` | pre-image snapshots, action ledger | Per-edit diff; restore | Not available until snapshots exist |

### `/diff` and `/undo` foundation

`write_file` currently writes with no backup, snapshot or version-control call. These commands require, first, a
pre-image snapshot taken in the write handler and recorded in the action ledger through its undo reference, so
that an undo is itself an audited action. Whether anything currently populates that reference for writes is
unverified. Until this lands, the commands are not registered.

### Defects fixed with this work

- `/compact` prints "not yet implemented" while advertised.
- `/alerts` is advertised and completed but has no branch in the plain-CLI dispatcher.
- The `/permissions` help text says "revoke"; the implemented verb is `reset`.
- `/context` estimates from a fixed 128k budget and character count, not the real window; it must say it is an estimate.

### Test plan

- One test per surface asserting identical `WhereReport` values for a fixed state (including local, cloud and remote providers).
- Tier enforcement: a session classified controlled, then `/model`, `@provider` and `/m` to a cloud provider are refused and audited; negative control with an admitting provider succeeds.
- Reply stamp matches the provider the gateway returned, in streamed and non-streamed replies, with and without `x_origin`.
- Registry: advertised equals dispatched equals helped; no command prints a stub message.
- Per command: success, unavailable behaviour, and secret refusal for `/copy` and `/export`.

## Decisions

- ADR-168: placement is explicit, one report feeds every surface, replies are stamped from the actual selection, switching enforces tier, commands are a contract.

## Open questions

- The permitted path, if any, to override a tier refusal (owner: Ben Booth, decide at Phase 1 review).
- Whether binding to a site node should also route generation to the node (deferred).
- Whether anything populates the action ledger's undo reference for file writes (owner: implementer, first check for `/undo`).
- Cloud-hosted node and loopback tunnels: whether a tunnelled remote counts as `local` by endpoint (owner: implementer; default is `remote-node` when reached through a bound node).
