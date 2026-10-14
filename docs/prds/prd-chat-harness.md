# Product Requirements: Axiom Chat Harness

**Product / Feature:** Axiom chat as a trustworthy coding and operations harness: always shows where answers
come from, enforces where content may go, and offers the session commands users expect from the leading
harnesses, built on what the platform already implements.

**Owner:** Ben Booth (B-Tree Labs)   •   **Status:** Draft   •   **Last updated:** 2026-10-06

**Related:** ADR-168; [spec-chat-model-picker.md](../specs/spec-chat-model-picker.md);
[spec-chat-slash-commands.md](../specs/spec-chat-slash-commands.md); ADR-054; ADR-030

---

## 1) Elevator Pitch
Whatever model answers you, Axiom chat says where it is running, will not send controlled content somewhere it
may not go, and gives you the commands you already know, with provenance, approvals and audit that other
harnesses do not have.

## 2) Problem / Opportunity
- Users bound to a site node, or who switch models, cannot tell whether they are talking to a local model, a
  cloud provider, or a remote node, and cannot tell which retrieval store is in use.
- Switching models in the terminal chat bypasses export-control routing today (found by reading the code).
- Axiom chat lacks `/where`, `/copy`, `/export`, `/tools`, `/memory`, `/mcp`, `/stats`, `/diff`, `/undo`,
  `/fork`, `/plan`, `/ask`, `/review` and `/init`; `/compact` is advertised but is a stub.
- Opportunity: the platform already implements routing tiers, citation provenance, a durable approval queue,
  a session store and an action audit chain. Wiring them into these commands makes each one better than the
  usual version, not merely present.

## 3) Goals & Success Metrics
- Primary goal: no user is ever unsure where a reply came from or where their content may go.
- Metrics:
  - 100% of replies carry an origin stamp that matches the provider actually used (conformance test).
  - 0 successful switches to a provider whose tier does not admit the session's content class.
  - `/where`, `/status`, the status indicator, the stamp and `axiom_chat__where` agree on every field (one source, tested).
  - No advertised command is a stub; every command has a conformance test.

## 4) Key Users / Personas
- **Colleague bound to a site node:** needs to know what they are talking to and what leaves their machine.
- **Operator working with controlled material:** needs enforcement, not only labels.
- **Developer using chat as a coding harness:** expects the familiar session commands.
- **Agent calling chat tools through MCP:** needs to ask the same question a person asks.

## 5) Scope: Key Capabilities (requirements)

Status key: **Exists** (wired in chat today), **Wire** (implemented elsewhere, not wired into chat), **New**
(no foundation). Where Axiom is better, the platform capability it uses is named; competitor behaviour is as
we understand it and is not re-verified here.

### 5.1 Placement and enforcement
| ID | Requirement | Status | Better because |
|---|---|---|---|
| P1 | Every provider has a `placement` (`local`, `cloud`, `remote-node`) and a `host` for remote | New | Derived from existing settings; overridable |
| P2 | An always-visible indicator from startup (before the first turn) shows model, placement and host, remote in a distinct colour; retrieval source shown separately | New | Retrieval and generation can differ, and we say so |
| P3 | Every reply is stamped with the provider actually used | New | Reads the real selection; node shim supplies true model and host, streamed too |
| P4 | `/model`, `@provider` and `/m` refuse a target whose `routing_tier` does not admit the session's content class | Wire | Routing tiers and the export-control classifier exist; the override path skips them today |
| P5 | MCP tool `axiom_chat__where` returns the same report as `/where` | New | One shared function |

### 5.2 Commands
| ID | Command | Requirement | Status | Better because |
|---|---|---|---|---|
| C1 | `/model` | Interactive picker; rows show placement and host; switch confirms placement; tier check (P4) | Exists (list and switch); New (picker, placement) | Enforcement, not just selection |
| C2 | `/where` | Provider, model, placement, host, tier, retrieval source | New | Same report as the indicator |
| C3 | `/status` | Lead with the where block | Exists | Already includes tier, fallback and retrieval line |
| C4 | `/stats` | Per-turn latency, tokens, tool calls, cost; unknown prices shown as unpriced, never $0 | Exists (`/usage`); Wire (latency) | Honest about missing prices |
| C5 | `/compact` | Real summarisation, keeps citations, writes a session summary to memory | New (stub today) | Memory ledger exists; chat does not write turns to it today |
| C6 | `/copy` | Copy last reply with its stamp and a secret scan first | Wire (clipboard helper exists) | Secret scan exists in the memory layer |
| C7 | `/export` | Write the transcript with stamps; refuse or redact on secret detection | New | Session store and secret scan exist |
| C8 | `/sources` | Show citations and the grounded-or-invented verdict for the last reply | Wire (verdict is computed, never rendered) | Provenance gate exists; nobody renders its verdict |
| C9 | `/tools` | List tools with each tool's permission state and whether it mutates | Wire (tool table and permissions exist) | Shows approval state, not just names |
| C10 | `/mcp` | Show the MCP server surface and which harnesses are configured; state plainly that chat cannot call external MCP servers | Wire (server side only) | No false impression of a client |
| C11 | `/memory` | Search and show memory through the serving gate | Wire (chat has no recall index) | Gate excludes vault content by construction |
| C12 | `/plan`, `/ask` | Switch interaction mode by command, not only Shift+Tab; stated guarantee: tools are not offered to the model | Exists (modes); New (commands) | Honest about what the guarantee is |
| C13 | `/permissions` | Fix the advertised "revoke" text to match behaviour; show the held-for-human queue | Exists | The durable hold queue is a differentiator |
| C14 | `/review`, `/init` | Ship as bundled prompt-template commands; `/init` writes the project instruction file between the managed markers | Exists (user-command mechanism) | Instruction-file write-back already exists |
| C15 | `/fork` | Copy a session under a new id, preserving owner scope | New | Session store owner checks apply |
| C16 | `/diff`, `/undo` | Per-edit pre-image snapshot, recorded in the action ledger so an undo is itself audited | New (no snapshot exists; file writes go straight to disk) | Ledger has an undo reference slot; not populated for writes today |
| C17 | Fix `/alerts` in the plain CLI (advertised, not dispatched), `/help` and the registry in one list | Exists (defects) | |

## 6) Non-Functional / Constraints
- One source of truth for placement; no surface computes it separately.
- Non-TTY and piped sessions suppress the indicator but still emit stamps in structured output.
- A node that predates the shim change is shown as `remote-node (unverified)`; never guessed.
- Domain-agnostic: no consumer vocabulary in Axiom documents or code.

## 7) Timeline (high level)
- Phase 1: P1 to P5, C1 to C3, C17 (visibility and enforcement; includes the shim change as a site release).
- Phase 2: C4 to C12 (commands built on implemented machinery).
- Phase 3: C13 to C16 (`/fork`, `/diff`, `/undo` need new infrastructure).

## 8) Risks & Open Questions
- Closing the override bypass changes behaviour for existing users (message explains; documented path).
- The cost table is stale and returns $0 for unknown models; C4 must fix this first.
- Open: whether binding to a node should also route generation to it (deferred; ADR-168 labels the current behaviour).
- Open: the permitted path to override a tier refusal, if any (decide by Phase 1 review).

## 9) Acceptance & Rollout
- Sign-off: Ben Booth. Rollout per phase; each phase ships with its tests and its documentation.

## 10) Contacts & Links
- ADR-168; spec-chat-slash-commands; spec-chat-model-picker; spec-model-routing.
