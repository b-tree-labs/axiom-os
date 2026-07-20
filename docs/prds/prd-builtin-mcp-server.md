# Built-in Root MCP Server — Product Requirements

**Status:** Draft  •  **Owner:** Benjamin Booth  •  **Last updated:** 2026-05-01
**Audience:** Extension authors, peer-harness integrators, operators of Axiom nodes, anyone connecting an external agentic harness (Claude Code, Cursor, Codex, LangChain, CrewAI, Goose, ChatGPT Desktop, Claude.ai, etc.) to an Axiom node.
**Companion ADR:** [`adr-038-builtin-mcp-server.md`](../adrs/adr-038-builtin-mcp-server.md)
**Companion Spec:** [`spec-builtin-mcp-server.md`](../specs/spec-builtin-mcp-server.md)

---

## 1. Elevator Pitch

Every Axiom node ships a single root MCP server that surfaces the full power of the platform — memory composition, federation, RAG, signals, and every opted-in extension capability — through one well-known protocol. Connect any MCP-speaking harness (17+ supported) once and get the whole node; declare `[extension.mcp]` in your manifest and your extension is exposed for free.

## 2. Problem / Opportunity

### What's broken today

- Axiom has *two* MCP touchpoints: a client-config generator (`extensions/mcp_generation.py`) and one hand-written per-extension server (`classroom/mcp_server.py`). Neither gives a peer harness a complete view of the node.
- Extension authors who want MCP exposure must hand-build a server, hand-curate a tool list, and re-serialise existing tool definitions into MCP `Tool` objects. The friction is high enough that nobody does it (only `classroom/` ships one, and only because Prague required it).
- Platform primitives — memory composition, federation peer state, RAG retrieval, signals briefs, node status — are reachable only through `axi` CLI shell-outs. Non-Anthropic harnesses (Codex, LangChain, Goose, ChatGPT Desktop, Claude.ai connectors) can shell out but cannot introspect.
- When an extension is installed/upgraded/removed, previously-generated client configs go stale silently. Refresh requires the user to re-run `axi mcp generate`.

### Why now

1. **Prague-deadline competitive parity.** Every comparator harness in the agentic-platform space speaks MCP natively or consumes MCP servers. Axiom needs one stable, well-documented MCP entry point to be a peer rather than a curiosity.
2. **Extension growth.** As more built-ins land (memory, federation, hygiene, classroom, signals, release, …), the per-extension-server pattern's tax compounds linearly. A single aggregation point is the only design that scales.
3. **Federation seeding.** Prague federation needs students' Axiom nodes to be reachable from their preferred harness (Claude Code, Cursor, ChatGPT Desktop). A consistent single root server is the substrate for that connectivity story.

## 3. Goals & Success Metrics

**Primary goal:** Any peer agentic harness can discover and use the full surface of an Axiom node through one MCP connection — stdio for local, HTTP/SSE for remote — with no per-extension setup ceremony.

**Success metrics (KPIs):**

| Metric | Target |
|---|---|
| Time from `pip install axiom-os-lm` to first successful MCP `list_tools` call from Claude Code | ≤ 60 seconds (single config-write, no hand-editing) |
| Number of platform-primitive tools available on a zero-extension node | ≥ 12 (memory ×3, federation, rag, signals, node, db ×5) — see §5.1 for the db family added per ADR-052 |
| Server-level `instructions` string visible at MCP connect | Present; names ADR-052 + ADR-049 + extension `session_for` pattern so peer harnesses do the right thing without reading the playbook |
| Number of peer harnesses with documented adapter recipes | ≥ 17 (per ADR-038 D7) |
| Mean time from `axi ext install <name>` to surface refresh | ≤ 1 M-O heartbeat (≤ 5 minutes default) |
| Manifest opt-in friction for an extension author with one tool to expose | ≤ 4 lines of TOML |
| Lint failures on extensions missing explicit MCP opt-in/opt-out, post-Phase-2 | 0 (every extension declares intent) |
| MCP surface staleness (cached vs. fresh) detected by M-O drift check on every heartbeat | 100% detection within 2 heartbeats of an install/remove event |
| Aggregation registry test coverage | ≥ 90% line coverage; 100% branch coverage on collision/precedence rules |

## 4. Key Users / Personas

| Persona | Primary tasks | Technical level |
|---|---|---|
| **Extension author** | Declare `[extension.mcp]` in `axiom-extension.toml`; verify their tools surface; write `[[extension.mcp.tool]]` overrides when defaults aren't right. | Python developer, comfortable with TOML and AEOS manifests. |
| **Operator / sysadmin** | Install Axiom; run `axi mcp clients` to wire local harnesses; optionally enable HTTP/SSE for remote access; manage tokens. | Comfortable with CLI + JSON config files. |
| **Peer-harness user (instructor, student, researcher)** | Install Claude Code / Cursor / Goose / etc.; copy-paste one config block; ask the harness to use Axiom tools. | Knows their harness; does not want to learn Axiom internals. |
| **Federation/Vega operator** | Surface federation state to remote harnesses behind principal-bound auth; verify remote-harness identity is enforced. | Phase 5 persona; knows trust-graph semantics. |
| **M-O / hygiene operator** | Pre-approve auto-regen; observe drift events in the M-O dashboard; one-time approval for "regen MCP surface on every install." | Casual operator; mostly hands-off. |

## 5. Scope — Key Capabilities (MVP through Phase 4)

### MVP (Phase 1)
1. **Built-in mcp extension at `extensions/builtins/mcp/`** — AEOS-conformant; flat layout; `axiom-extension.toml` with `builtin = true`. Acceptance: passes `axi ext lint` and `ExtensionStandardTests`.
2. **Runnable stdio server** — `python -m axiom.extensions.builtins.mcp.server` opens a stdio MCP session, advertises tools, responds to `list_tools` and `call_tool`. Acceptance: smoke-test with the `mcp` Python SDK client returns the platform-primitive tool list.
3. **Aggregation registry** — walks installed extensions, reads `[extension.mcp]` (placeholder schema in Phase 1; real in Phase 2), builds a deterministic `MCPSurface` with content-hashed cache at `~/.axiom/mcp/surface.json`. Acceptance: idempotent; same input → same output (modulo timestamps).
4. **Platform-primitive tools** — 7 tools surfaced from platform modules: `axiom_memory__compose`, `axiom_memory__retrieve`, `axiom_federation__list_peers`, `axiom_federation__send`, `axiom_rag__retrieve`, `axiom_signals__brief`, `axiom_node__status`. Acceptance: each callable end-to-end against a zero-extension node.
5. **`axi mcp` CLI noun** — subcommands: `serve` (run stdio), `status`, `regenerate`, `list-tools`, `inspect <tool>`. Acceptance: each subcommand returns expected output; `axi mcp generate` aliased for back-compat.

### Phase 1.5 — Persistence surface (added 2026-05-30 per ADR-052)

The DatabaseProvider primitive (`axiom.infra.db`, ADR-052) added a platform-owned RDBMS surface but no peer-harness window into it. Phase 1.5 closes that:

#### 5.1 New platform-primitive tools — the `axiom_db__*` family

Five tools added to the Phase-1 primitive set; the four read tools land alongside the existing seven, the one write tool is RACI-gated per Phase 4's pre-approval pattern.

| Tool | Purpose | Safety |
|---|---|---|
| `axiom_db__schemas` | List installed extension schemas + their Alembic head revisions + pending counts | Read-only |
| `axiom_db__describe_schema(extension)` | Tables + columns + indexes + foreign keys for an extension's schema | Read-only |
| `axiom_db__migration_status(extension)` | Pending revisions vs head; "behind by N" / "up to date" | Read-only |
| `axiom_db__health` | Connectivity + pool stats + schema count + Postgres version | Read-only |
| `axiom_db__migrate(extension, target?)` | `alembic upgrade` to target revision (default: head); writes | RACI-gated; one-time pre-approve per ADR-045; revocable via `axi raci revoke 'db.migrate.*'` |

Acceptance: each callable end-to-end against a zero-extension node; the four read tools return well-formed responses against an Axiom install with one extension (`expman`) declaring `[database] needs_schema = true`; `axiom_db__migrate` is denied without pre-approval and succeeds with it.

#### 5.2 Server-level `instructions` string

The root MCP server passes an `instructions` block to `Server("axiom-root", instructions=…)` so every connecting client (Claude Code, Cursor, Cline, Goose, ChatGPT Desktop, Claude.ai, Codex, …) sees the ADR-052 pattern at connect:

> *Axiom is an agentic platform with conformant extensions. When working on an extension that needs persistence, use `axiom.infra.db.session_for("<ext>")` — never construct your own engine, never write to `public`, never hardcode `schema=` on tables (per ADR-052). Cross-extension reads ride the data platform (ADR-049), not OLTP joins. Within-extension tenancy is a three-option menu: single-tenant, row-level `tenant_id`, or schema-per-tenant. Use the `axiom_db__*` tools to introspect installed extension schemas.*

Acceptance: a fresh Claude.ai / Cursor / ChatGPT Desktop connection lists the instructions at connect; an `mcp list-tools` smoke test from each harness shows the instructions block in its `serverInfo`.

#### 5.3 Aggregation registry surfaces `[database]` declarations

When the AEOS manifest gains the `[database]` block (Phase 2 / ADR-052 §D7), the aggregation registry surfaces every extension that declared `needs_schema = true` via `axiom_db__schemas` automatically. No per-extension MCP server, no hand-curated tool list. Idempotent + content-hashed like the rest of the surface.

Acceptance: installing an extension with `[database] needs_schema = true` triggers a surface refresh within one M-O heartbeat; `axiom_db__schemas` returns the new schema with the next call.

#### 5.4 `extension-persistence` SKILL.md skill

Ship a SKILL.md skill in the mcp built-in (`extensions/builtins/mcp/skills/extension-persistence/SKILL.md`) that captures the ADR-052 patterns as model-mediated instructions — the copy-paste shape, the five don'ts, the multitenancy menu, the Alembic env. Any agentskills.io-compatible harness can invoke the skill to get the canonical pattern injected.

Acceptance: the skill validates against agentskills.io's SKILL.md schema; `axi ext lint` passes; a Goose / Claude Code session can invoke it by name and receive the full guide.

### Phase 2 (Manifest schema + first three extensions)
6. **`[extension.mcp]` schema in AEOS** — full schema in `spec-aeos-0.1.md` §6; JSON Schema published at `docs/specs/aeos-schema-0.1.json`. Acceptance: schema validation passes for the three pilot extensions.
7. **Lint enforcement** — `axi ext lint` fails any extension that has neither a `[extension.mcp]` block nor a `# mcp: not-applicable — <reason>` comment. Acceptance: lint runs in CI; broken extensions block merge.
8. **Three extensions converted** — `memory`, `signals`, `hygiene` (chosen for diversity: a platform-touching kind, an event-emitting kind, an agent-housing kind; deliberately NOT `classroom` or `rag*` per worktree boundaries). Acceptance: each surfaces its tools through the root MCP server with names matching the new schema.

### Phase 3 (Adapter recipes)
9. **17 harness recipes** — at `docs/working/mcp-harness-adapters/<harness>.md` per ADR-038 D7. Acceptance: each recipe includes install + connect + verify steps + smoke-test command.
10. **`axi mcp clients` writes Tier-1 configs** — for Claude Code, Cursor, Continue, Cline, Windsurf, Cody, Aider, Open Interpreter, Goose, ChatGPT Desktop. Acceptance: command writes the correct config block atomically; preserves user-added entries.
11. **Discoverable index** at `docs/working/mcp-harness-adapters/README.md` — table of supported harnesses, recipe links, last-tested-against version. Acceptance: linked from the mcp built-in's README and `axi mcp clients --list`.

### Phase 4 (CI/CD + runtime adaptation)
12. **`extension.post_install` hook** — mcp built-in subscribes; on every extension install/uninstall/update, regenerates the surface. Acceptance: integration test installs an extension, observes the surface refresh without user action.
13. **M-O drift check** — `_check_mcp_surface_drift()` on every hygiene heartbeat; debounce 2 heartbeats before proposing regen; integrates with existing RACI proposal flow. Acceptance: drift introduced manually (edit a manifest) is detected and proposed within 2 heartbeats.
14. **One-time RACI pre-approval** — operator enables "auto-regen MCP surface on extension change" once; subsequent regens are silent execution. Acceptance: pre-approval persists across restarts; revocation via `axi raci revoke <pattern>` works as documented.

## 6. Non-Functional / Constraints

- **Performance.** Cold stdio handshake ≤ 200 ms on a Workstation profile. `list_tools` response ≤ 50 ms with a fully-cached surface. Surface regen ≤ 500 ms for ≤ 25 extensions. (Today's whole Axiom built-in extension set is 27 extensions; this is a near-term ceiling.)
- **Security.** Stdio transport defaults to local-only (no network listener). HTTP/SSE off by default. Phase-5 token auth uses cryptographically random ≥ 256-bit tokens; revocation list checked on every request. Principal-bound auth honors `@name:context` Matrix-style identities.
- **Auth posture.** v0 = same as today's classroom server (stdio, no auth). Phase 5 adds token + principal modes; remote-HTTP enforcement is *additive* — never weakens local stdio.
- **Compatibility.** Reuses the existing `mcp>=1.0` Python SDK already in `pyproject.toml`'s `[project.optional-dependencies]`. No new pinned dependencies. Compatible with the latest stable MCP protocol revision (≥ 2025-11).
- **Domain neutrality.** Platform tool names + descriptions never reference any specific domain (no nuclear, reactor, classroom, facility wording at the platform layer). Per `feedback_axiom_domain_agnostic`.
- **Platforms.** macOS, Linux, Windows (Python 3.11+). Stdio works everywhere; HTTP/SSE has the same per-platform constraints as the existing `axi serve` paths.
- **Federation neutrality.** A node's MCP surface advertises federation tools (`axiom_federation__list_peers`, `axiom_federation__send`) but never *initiates* federation traffic on behalf of a remote MCP client without explicit principal-bound auth. v0 keeps these tools local-stdio-only by default per ADR-038 D6.
- **No DB mocking in tests** per the project test rule. Use `tmp_axiom_home` fixture + real SQLite for memory tests; real filesystem for surface-cache tests.
- **Backward compat for `mcp_generation.py`.** Module remains importable through one minor version. `axi mcp generate` is aliased to `axi mcp clients --write`. No external breakage.

## 7. Timeline (high level)

| Phase | Scope | Target |
|---|---|---|
| Phase 0 | Design docs (ADR + PRD + Spec) | 2026-05-01 |
| Phase 1 | Scaffolding + platform primitives + stdio | 2026-05-02 → 2026-05-05 |
| Phase 2 | Manifest schema + 3 extensions converted | 2026-05-05 → 2026-05-08 |
| Phase 3 | Adapter recipes (17) + `axi mcp clients` | 2026-05-08 → 2026-05-12 |
| Phase 4 | CI/CD trigger + M-O drift detection | 2026-05-12 → 2026-05-15 |
| Phase 5 | Remote auth (token + principal HTTP modes) | Post-Prague (June+ 2026) |

Phase 5 deliberately falls outside the Prague-go-live runway per `feedback_freeze_foundation_during_delivery`.

## 8. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Extension authors don't opt in; root server stays sparse | Lint enforces explicit opt-in/opt-out; the three Phase-2 conversions become the worked-examples in `axi ext init` templates; documentation in `docs/working/aeos-playbook.md` |
| Platform-primitive tool surface collides with extension-declared tools | Platform always wins on collision; extension-vs-extension collision warns at lint; deterministic ordering via `discovery.list_extensions()` makes surface reproducible |
| Stdio process lifecycle (spawn-per-client) overwhelms a node with 5+ concurrent harnesses | Each peer harness owns its own subprocess; per-process memory is small (no model weights); kernel handles scheduling. If contention emerges, HTTP/SSE single-process mode is the answer (Phase 5) |
| MCP protocol version drift breaks one harness or another | `pyproject.toml` pins `mcp>=1.0`; we test against the latest stable in CI; per-recipe "last-tested-against" metadata flags drift to maintainers |
| M-O drift check creates noisy proposals during rapid install churn | 2-heartbeat debounce + content-hash comparison; once user pre-approves auto-regen, proposals become silent execution |
| Remote HTTP auth (Phase 5) opens an attack surface that v0 doesn't have | HTTP off by default; flag-gated; tokens ≥ 256 bits; revocation list; principal-binding required for any tool touching federation/memory writes |
| Per-extension MCP-server back-compat (classroom) silently breaks | Keep `classroom/mcp_server.py` running for one minor version; deprecation warning at boot; migration documented in classroom's `docs/decisions/` |

**Open questions** (decisions needed before specific phases):

- (Phase 2) Should `[[extension.mcp.tool]]` blocks be allowed to *re-implement* a tool (provide an alternative `entry`) or strictly to *configure* the existing tool? Decision needed before spec freeze.
- (Phase 4) Where does the "auto-regen pre-approval" persistence live? `~/.axiom/raci/preapprovals.json` (consistent with RIVET) or in the mcp built-in's own state? Decision: piggyback on RACI store.
- (Phase 5) Token storage at rest — plaintext JSON, OS keychain, or both? Decision deferred to the Phase-5 spec.

## 9. Acceptance & Rollout

**Sign-off:**
- Engineering: Ben Booth
- Product: Ben Booth (B-Tree Labs)
- Reviewers: Reviewers nominated at PR time

**Rollout plan:**
1. Phase 0: docs reviewed, approved, merged to `feat/builtin-mcp-server`.
2. Phase 1–4: each phase ships end-to-end value (per `feedback_phased_work_must_deliver_per_phase`); milestones reported to Ben after each.
3. Branch merged to main once all four phases are green and standard test suite passes.
4. Tagged release `v0.13.0` per the project's "production from tags" convention (`feedback_production_from_tags`); released to PyPI as `axiom-os-lm v0.13.0`.
5. Phase 5 lands as a separate ADR + PRD update + tag (`v0.14.0` or later).

**Rollback criteria:**
- Aggregation registry crashes in surface generation → revert to last-known-good `surface.json` from cache; M-O escalates.
- A peer harness recipe breaks (regression in upstream harness) → mark recipe stale in the index; downgrade harness in CI matrix.
- M-O drift check generates spurious proposals → emergency disable via `axi raci revoke 'mcp.surface.regen.*'`; root cause investigated before re-enable.

## 10. Contacts & Links

- Product lead: Benjamin Booth — no-reply@axiom-os.ai
- Eng lead: Benjamin Booth
- ADR: [`adr-038-builtin-mcp-server.md`](../adrs/adr-038-builtin-mcp-server.md)
- Spec: [`spec-builtin-mcp-server.md`](../specs/spec-builtin-mcp-server.md)
- AEOS spec (extended): [`spec-aeos-0.1.md`](../specs/spec-aeos-0.1.md) §6, [`spec-aeos-1.0.md`](../specs/spec-aeos-1.0.md) §4.x
- Related — ADR-006 MCP commitment, ADR-031 extension self-containment, ADR-036 runtime surfaces, ADR-049 data-platform boundary (cross-extension reads), ADR-052 database tenancy (the `axiom_db__*` surface from §5.1)

---

_Copyright (c) 2026 B-Tree Ventures, LLC. Apache-2.0 licensed._
