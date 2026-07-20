# Changelog

All notable changes to Axiom are documented in this file.

## [0.37.1] — 2026-07-15 — webgate install-path fix

### Fixed

- **`python-multipart` is now a core dependency.** The `webgate` login router
  declares FastAPI `Form(...)` fields, which FastAPI refuses to serve unless
  `python-multipart` is importable — raising `RuntimeError` at router-build
  time. webgate is a *core* builtin, but the dependency was undeclared and only
  arrived transitively via the `[mcp]` extra, so a plain `pip install
  axiom-os-lm` that mounts the gate crashed (surfaced when a consumer bumped its
  pin to 0.37.0). Same silent-transitive class as the earlier PyJWT/httpx gaps.
  A non-docker install-path test now guards the declaration.

## [0.37.0] — 2026-07-15 — Memory recall hardening

Follow-up to the 0.36.0 memory federation: closes the append→recall gap that
shipped in 0.36.0, makes the optional MCP dependency discoverable, and adds a
one-time backfill for memory recorded before the fix.

### Fixed

- **append→recall was broken** — the default write builders
  (`mcp_server`/`cli` `_build_default_composition`) lacked a `recall_index`, so
  memory recorded via the MCP `axiom_memory_append` tool or `axi memory record`
  landed in the ledger but was never projected into the recall corpus:
  `axiom_memory_recall` served nothing for anything a tool had logged. Both
  builders now carry the `recall_index` that `build_default_serving_service`
  already used. (Ledger reads — `show`/`recent`/`search` — were never affected.)
- **De-flaked the edge-scaling perf guard** — replaced a wall-clock window-ratio
  assertion with a min-of-N comparison so the memory graph test no longer
  reddens CI under load.

### Added

- **`axi memory reindex [--principal <id> | --all]`** — one-time backfill that
  re-projects the ledger into the recall corpus, so fragments recorded before
  the append→recall fix become recallable. Idempotent (the projection is
  disposable per ADR-088 §5); vault is never projected.
- **Actionable `[mcp]`-extra guidance** — importing the memory MCP server
  without the optional `mcp` dependency now raises an install hint
  (`pip install "axiom-os-lm[mcp]"`) instead of a bare `No module named 'mcp'`.

## [0.36.0] — 2026-07-15 — Secret rotation + node-to-node memory federation

Operational secret rotation lands end-to-end — a pluggable SecretStore with an
OpenBao default, autonomous-first rotation, and `axi secrets rotate --force` as
the leaked-key closer — alongside the memory federation that lets two Axiom
nodes sync their ledgers.

### Added

- **SecretStore v1** — the `secrets` extension gains OpenBao as the default
  backend with a fail-closed preflight (never silently degrades to plaintext
  `env` outside dev), an **Azure Key Vault** provider (`azure://<vault>/<name>`),
  and `SecretRef.version` widened to `int | str | None` so opaque/named version
  ids (Azure hex, `latest`) round-trip. Fixes `axi cred|auth|identity` dispatch
  (built-in manifests were missing `builtin = true`).
- **Autonomous-first key rotation** — the overlap-validity model (a rotation is
  a window, not an instant; consumers read the accepted set so a missed flip
  never outages), `Capabilities.rotation` + `resolve_overlap()`, and an **AWS
  Secrets Manager** provider (ADR-080).
- **Rotation-strategy layer** (ADR-095) — `ProviderNativeRotation` (backend
  self-rotates), `HitlRotation` (a human supplies the new value behind a single
  confirm, for vendors whose API can't mint), and `SendGridRotation` (the
  vendor-API mint/revoke pattern), driven by a force/cadence-gated engine with a
  deferred-revoke sweep.
- **`axi secrets rotate <ref> [--force]`** — rotate one secret now; `--force` is
  the leaked-key closer. Never prints the secret value.
- **OpenBao deploy IaC** — Helm chart (local / self-hosted / air-gapped
  profiles) + Terraform; no unseal key or root token ever written to cluster
  storage in sealed mode.
- **Portable cross-harness memory federation** (ADR-087/088) — node-to-node
  transport and a hosted-endpoint runtime so two Axiom nodes sync memory, on top
  of the P0–P4 portability, absorb adapters, and hybrid recall shipped this
  cycle.
- **Data-architecture ADRs** — permission-aware retrieval (ADR-094) and the
  polyglot persistence topology + naming standard (ADR-093).
- **`/v1` uniform-authz** issued API principals; **shareable origin-URL**
  provenance on ingest (ADR-091); a **LaTeX-PDF** press provider via Tectonic
  (ADR-090).

### Notes

- Rotation follow-ons (per-vendor adapter config, Azure/GitLab/LangSmith
  adapters, and the PULSE-driven reconciler with alert-before-expiry) build on
  the shipped contract.

## [0.35.0] — 2026-07-08 — Memory forget (redaction from recall)

`axi memory forget` — the first authorized memory mutation: redact recorded
fragments from recall without erasing the audit trail.

### Added

- **`CompositionService.forget(fragment_ids, requester, agent, reason)`** — the
  single-entry-point mutation. Soft-deletes (tombstones) each fragment the
  requester holds `Right.CONTROL` over (the master always does), so reads and
  recall exclude it while the row + `deletion_reason` are retained for audit and
  a `forget` audit entry is recorded. Denied / unknown ids are partitioned in the
  returned `ForgetResult`, never raised.
- **`axi memory forget`** — thin CLI over the first ADR-056 `memory` skill
  (`memory/skills/forget.py`). Select by explicit fragment id(s) or by
  `--principal` narrowed with `--match`; a full-history purge requires `--all`,
  and `--dry-run` previews. `--reason` lands on the tombstone + audit entry.

### Notes

- Redaction, not erasure: the frozen fragment is never mutated, so the
  immutable-provenance contract holds. Hard crypto-shred and tombstone
  propagation to federated peers remain follow-ons.

## [0.34.0] — 2026-07-08 — HERALD cloud channels + Teams bidirectional

Cloud notification channels for HERALD, plus a bidirectional Teams conversational
path that answers in-thread from the reactor chat agent. Dispatch stays
fail-closed on the classification ceiling; recipients without a channel profile
still land in the inbox rather than silently dropping.

### Added

- **Cloud channel adapters** (`axi notifications`): Azure Communication Services
  SMS + email, AWS SNS SMS, Amazon SES email, Gmail email, and FCM push. Each is
  a self-contained channel with its own credential surface and tests.
- **Teams bidirectional channel** (Bot Framework): `teams_interactive` channel
  posts alerts and parses inbound activities; the `gateway/` conversation seam
  routes a reply to the chat agent and posts the answer back in-thread, with
  reply-bind-back correlation to the originating alert.
- **`gateway/teams_bot`** — fail-closed Bot Framework JWT verification (RS256,
  audience = app id, issuer = Bot Framework) so only Microsoft-signed activities
  reach the agent.
- **`channel_config.py`** — per-channel config resolution from the comms manifest.
- **Teams app manifest example** (`docs/teams-app-manifest.example.json`) with
  resource-specific consent (`ChannelMessage.Read.Group`).

### Changed

- **`send()` dispatch** hardened: recipient-profile fan-out ordering, an explicit
  inbox fallback when no external channel is admitted, and the classification
  ceiling enforced as a platform default (an `internal`-classified alert flows to
  external channels; a higher ceiling fails closed).
- The conversational agent is named **AXI** per AEOS §3.2 (consumers override the
  display name via the `[sender]` block — e.g. "Neut" for the reactor site).

### Dependencies

- `httpx` promoted to a base dependency (cloud channels + Bot Framework REST).
- New `herald-teams` extra pulls `PyJWT` for Bot Framework token verification.

## [0.33.0] — 2026-07-08 — Open WebUI prompt projector

Ships the Open WebUI prompt projector (`commands: project-openwebui`) as
installable code, replacing the hot-copied downstream shell adapter.
## [Unreleased]

### Added

- **PRESS `latex-pdf` generation provider** (ADR-090) — compile an author-owned
  LaTeX project (`.tex` entry, or a dir with `main.tex`) to PDF via **Tectonic**,
  preserving the document's own class/style/bibliography. Distinct from
  `pandoc-pdf` (markdown → branded PDF). A `.tex` source auto-routes; `latex`/`tex`
  formats map to it. Tectonic added to the runtime image. First use compiled a
  real TMLR journal template and surfaced genuine document defects (unrepresentable
  Unicode glyphs, a longtable artifact) a markdown pipeline would have hidden.

## [0.30.0] — 2026-06-01 — Unified Agent Fabric

Substantial release. Three load-bearing ADRs anchor a competitive-differentiating synthesis: standards as composable named skill-bundles, one vendor adapter per vendor, cross-agent event routing through the bus → HERALD bridge.

### ADRs (foundation)

- **ADR-058** Agent Standards Registry — each persona declares named skill-bundles queryable across CLI / A2A / MCP
- **ADR-059** Connector-First Vendor Unification — publishing's duplicate notification stack retired; one M365 Graph adapter serves HERALD email + Calendar + publishing notifications
- **ADR-060** Cross-Agent Event Routing — no agent imports another's notification path; bus → bridge → HERALD

### Added

- **PRESS skill-ification** (per ADR-056): 7 skills (`press.draft`, `press.publish`, `press.scope_for_source`, `press.next_filename`, `press.detect_version`, `press.standards`, `press.do_standard`)
- **3 PRESS standards bundles**: `publish_prd`, `publish_for_review`, `regenerate_versioned`
- **`axi pub standards` + `axi pub do <name>`** verbs (ADR-058)
- **`publishing.* + rivet.notification` bus routes** in `agent_bridge.default_routing()` (ADR-060)
- **`press.publish` emits `publishing.{succeeded,failed,draft_ready}` events** on the bus — first call-site of the bus-routed pattern
- **PRESS persona** updated with the Standards section
- **`axi pub` canonical vocabulary** — TDD-pinned verb-set, single source of truth in `test_cli_vocabulary.py`
- **`generate` → `draft` rename** with deprecation alias (removed in v0.31)
- **PRESS source-scope detection** — generated docs land in the source's repo/worktree (worktree-safe via `git rev-parse --show-toplevel`)
- **PRESS Finder-style non-clobbering output** — `axi pub draft foo.md` ; second run → `foo (1).docx`
- **PRESS Mermaid pre-render** — `\`\`\`mermaid\`\`\`` blocks become PNGs embedded in the docx
- **Connector extension** (PR #381 + ADR-057) — `axi connector add | status | reconnect`; status_store + observability + wizard + reconnect flow as a top-level primitive
- **HERALD-2a outbound channels** — Slack + Mattermost + Email (nested Factory/Provider; SMTP + Resend + 6 plug-in slots) + Teams (Workflows with quality bar) + Twilio SMS
- **Agent-bus → HERALD bridge** — `rivet.* / tidy.* / *.escalation / publishing.* / rivet.notification` events route through HERALD per recipient profile
- **Recipient preferences primitive** — `@bbooth` resolves to `[slack, twilio-sms, email, inbox]` per `(classification, priority)`

### Deprecated

- `publishing/providers/notification/{smtp,terminal}.py` — removed in v0.31 per ADR-059; consumers migrate to bus emission
- `axi pub generate` — alias for `axi pub draft`; removed in v0.31

### Test count

258 publishing + 16 agent_bridge + 213 notifications + 25 connector + ~600 other = ~1100 tests across the affected surfaces; all green.

## [0.29.2] — 2026-06-01 — Unbrick RIVET heartbeat + TIDY watch-the-watcher

(Re-cut from 0.28.2 — parallel keystore session bumped to 0.29.1 in between; PR #321 merged after 0.29.1 was published so 0.29.1 lacks the RIVET fix. Same content, additive bump.)

Critical fix: the 2026-05-30 CLI noun-renaming silently dropped `release heartbeat` from the skill registry. The supervisor kept firing the (wrong) command every 5 minutes for 28 hours; RIVET wrote nothing; no one noticed because there was no skill watching for agent silence.

### Fixed

- `axi release heartbeat` is restored as a registered skill (skills/__init__.py, _SKILLS dict, manifest declaration, cli.py leaf list); manifest `heartbeat_command` set back to `release heartbeat`. Verified: writes a fresh entry on every fire.

### Added

- **`axiom.extensions.builtins.hygiene.heartbeat_liveness`** — TIDY's watch-the-watcher. Walks `~/.axi/agents/*/heartbeat.jsonl`, emits findings (severities: `never_fired`, `stale`, `dead`) for agents that have gone quiet. Includes a regression test against the exact 28-hour scenario.
- Three new RIVET classification patterns:
  - `storage quota`, `failed to createartifact`, `artifact storage` → routes to `infra` (was misclassified as `code` on a consumer-repo storage-quota incident)
  - `popen-gw` + `pytest-of-` co-occurrence → routes to `flake` (catches every parallel-worker test pollution that's been bypassing pre-push for the last week)

### PR

- #321 — RIVET unbrick + TIDY heartbeat_liveness + classification gaps. 45 tests passing.



## [0.28.1] — 2026-05-31 — Extension-author runway: config primitive + TIDY artifact-cleanup

Two small primitives that, together, unblock the next wave of extension authors and keep platform hygiene from gating builds:

- **`axiom.infra.config`** — the watched-configuration primitive per AEOS §2.13–§2.14. Schema + value store + locks + cross-platform filesystem watcher (watchdog with polling fallback) + receipt-emission hook. The architectural answer for both long-running-service deployments (change config without restart) and cloud-class deployments (each fire reads current value). Lock primitive composes with the parallel keystore session — predicate lives here, cryptographic enforcement there. 26 tests + end-to-end smoke. (PR #315)
- **`axiom.extensions.builtins.hygiene.artifact_cleanup`** — TIDY's first provider-agnostic CI-storage hygiene skill. Time cutoff + per-workflow last-N safety net + ADR-045 D6 volume gating. GitHub via `gh api` is the first backend; GitLab + others slot into the `Provider` Protocol without touching the policy layer. Motivated by a 2026-05-30 consumer-repo storage-quota incident. 12 tests. (PR #316)

The 0.28.0 release (parallel keystore session) shipped SEC-2/SEC-3 secret-provider work — openbao, env, kubernetes. 0.28.1 adds the extension-author surface so the two streams compose for the consumer.

### AEOS spec updates

- New §2.13: **State externalization** — agents MUST NOT carry in-memory state on which observable behavior depends. The unifying property long-running-service + cloud deployments demand identically.
- New §2.14: **Configuration is durable + watched + auditable** — schema validation + filesystem watching + receipt emission + lockability + "extensions MUST consume the platform primitive."

### Quickstarts

- `docs/working/extension-authn-quickstart.md` — Austin's 4-line authz pattern
- `docs/working/extension-config-quickstart.md` — Austin's 5-verb config pattern

## [0.25.0] — 2026-05-30 — Governance fabric foundation + GUARD + KEEP (ADR-055)

Lands the foundation of the unified governance fabric per ADR-055 — the
shared substrate every primitive (vault / authz / notifications /
schedule) consumes. Cut 0 (foundation) + Cut 1 (GUARD authz) + Cut 2
(KEEP vault) ship together as the first runnable slice.

### Added

- **`axiom.governance`** module — `ActionEnvelope`, `CapabilityToken`,
  `Verdict`, `Classification` (public/internal/regulated/controlled),
  `ActionIntent` + `IntentPattern` with 24-verb registered ontology
  (per spec §1.3), `ResourceRef` + `ResourcePattern`, `ProvenanceRef`.
  88 unit tests; reuses `axiom.vega.identity.Principal`.
- **`axiom.extensions.builtins.authz`** (GUARD) — `decide(envelope) →
  Verdict` API; declarative rule engine with documented precedence
  (deny > propose > require_capability > permit; higher priority wins
  within disposition); Postgres-backed receipts via ADR-052's
  `session_for('authz')`. 28 tests including the no-bypass property
  parametrized across 5 envelope shapes and 3 integration tests
  against real Postgres.
- **`axiom.extensions.builtins.vault`** (KEEP) — capability lifecycle
  (issue / get / revoke / is_revoked); the `outbound_call(capability,
  request, ctx)` chokepoint that is the ONLY plaintext-credential site
  on the platform per spec §2.3 + §9.2. Builds on the existing
  `axiom.infra.connections.get_credential` chain for Phase 1.
  13 tests including the load-bearing no-credential-leak-in-caller-
  request isolation test.

### Architectural notes

This release is **architecturally** the most significant ship since
ADR-052 (database tenancy). It commits the platform to capability tokens
not raw credentials, classification-aware everything, and federation-
native primitives — the three differentiators that no peer harness
(Claude Code, Cursor, Aider, Mastra, LangGraph, OpenAI Agents) has.

The four primitives are siblings, not silos: vault + authz here, plus
notifications (HERALD) + schedule (PULSE) following in 0.26 + 0.27. The
march tracker at `docs/working/governance-fabric-march.md` shows the
sequence + Austin support track (which fabric phases unblock which
Expman phases).

### Consumer integration for downstream extensions

```python
from axiom.extensions.builtins.authz import decide, DecideContext
from axiom.extensions.builtins.vault import VaultContext, issue_capability
from axiom.governance import ActionEnvelope, NextAction
from axiom.infra.db import session_for

authz_ctx = DecideContext(session_factory=lambda: session_for("authz"))
vault_ctx = VaultContext(session_factory=lambda: session_for("vault"))

def my_action(envelope: ActionEnvelope) -> None:
    verdict = decide(envelope, authz_ctx)
    if verdict.next_action_for_caller is not NextAction.PROCEED:
        return
    # do the action (vault-mediated if it's outbound HTTP)
```

The domain consumer's EM-005 `transition()` consumes this directly in Phase 1.5.

### Related

- **PRs:** #285 (foundation), #288 (Cut 1 GUARD), #289 (Cut 2 KEEP)
- **PRDs:** prd-axiom-authz, prd-axiom-vault (both stable on main)
- **Spec:** spec-governance-fabric (build-ready substrate)
- **ADR:** adr-055-unified-governance-fabric

## [0.25.0-data-platform] — 2026-05-30 — Generic data-platform installer + skill-as-function (ADR-053)

Lands the platform's runtime skill-invocation contract and the first
extension that uses it end-to-end. The data-platform's CLI surface
moves from agent-named (`axi plinth`) to purpose-named (`axi data`)
per the 2026-05-30 noun-convention decision (CLI nouns are
deterministic platform 'arms and legs'; agent personas are
LLM-character names used in reasoning, not the CLI surface).

### Added

- **ADR-053 — skill-as-function** (`docs/adrs/adr-053-skill-as-function.md`).
  Skills are first-class invocable functions registered through a
  process-local registry; agents + CLI verbs share the same channel;
  bidirectional A2A drops out for free.
- **`axiom.infra.skills`** — `SkillRegistry`, `SkillContext`,
  `SkillResult`. Register-by-callable (eager) or register-by-entry
  (lazy `module:function` binding for manifest-driven discovery).
- **AEOS schema extension** — `kind="skill"` blocks may now declare
  optional `entry` (dotted `module:function`) + `schema` (JSON Schema
  for params). Back-compat: existing markdown-only skills validate
  unchanged.
- **`axi data` CLI surface** (renamed from `axi plinth`):
  - `install` — provision the data-platform on a K8s target. Pure-IaC
    (Terraform → Helm → K3D + `pip install` from PyPI). NO custom
    Dockerfile. Auto-detects kube context; prompts ONLY for source
    binding + creds.
  - `diagnose` — deterministic post-install checks (release exists,
    Deployments Ready, PVC Bound). Invokes `data.troubleshoot` on
    irregularity.
  - `troubleshoot` — PLINTH-persona LLM-reasoning hook for install
    anomalies (deterministic stub today; LLM binding follow-up).
  - `ingest`, `register <name>`, `unregister <name>`,
    `list [resource]`.
- **Generic Helm chart** at
  `src/axiom/extensions/builtins/data_platform/deploy/helm/` —
  shipped INSIDE the extension per ADR-031. Consumer layers
  (a domain consumer, …) supply only values overlays.
- **CLI verb-grammar + noun-convention audit refresh** at
  `docs/working/cli-verb-grammar-audit-2026-05-30.md` — 20 extensions
  with drift, the new noun-convention rule, prioritized migration
  sequence.

### Changed

- `data_platform` extension version 0.2.0 → 0.3.0.
- AEOS manifest schema (`aeos-manifest-0.1.json`) — `ProvidedSkill`
  gains optional `entry` + `schema` fields.

### Breaking — `axi plinth` removed

The `axi plinth` CLI noun is removed in favor of `axi data` (per
`feedback_no_backward_compat_shims`: pre-public-launch we refactor
cleanly, no deprecation aliases). The PLINTH agent persona itself is
unchanged — it's the LLM character used for install-irregularity
reasoning.

Verb renames:

| Was | Now |
|---|---|
| `axi plinth register-connector --name X` | `axi data register X` |
| `axi plinth unregister-connector --name X` | `axi data unregister X` |
| `axi plinth list-connectors` | `axi data list` |
| `axi plinth run-ingest --connector X` | `axi data ingest --connector X` |

### Migrations queued (next PRs)

- `axi tidy` → `axi hygiene` + verb grammar fixes
- `axi rivet` → `axi release` consolidation
- `axi triage` + `axi doctor` → `axi diagnose`
- federation extension 6-noun consolidation
- `axi chaos` audit (identical to `axi security` — likely copy-paste bug)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## [0.24.2] — 2026-05-30 — Promote psycopg2 + alembic to core deps (ADR-052 follow-up)

Caught by a fresh-install smoke against PyPI 0.24.1: `pip install axiom-os-lm` did NOT pull psycopg2 or alembic, so `from axiom.infra.db import session_for` errored at engine construction (`ModuleNotFoundError: No module named 'psycopg2'`). Any new extension developer who followed the EM-005 / ADR-052 / agent-facing-docs route would hit this immediately.

Per ADR-052 the schema-per-extension DatabaseProvider is a **platform-core contract**, not an extra. Moves `psycopg2-binary>=2.9` and `alembic>=1.13` from the `[rag]` / `[signal]` extras into main `dependencies`. The extras still list them (`pip install axiom-os-lm[signal]` keeps working — pip dedupes), but base install now ships the platform's persistence stack.

No code changes; pyproject + CHANGELOG only.

## [0.24.1] — 2026-05-30 — ADR-052: DatabaseProvider primitive (schema-per-extension)

Lands the platform's default DB-tenancy contract: one Postgres per Axiom
install, one Postgres schema per extension, owned end-to-end by the
platform. Extensions consume a single API:

    from axiom.infra.db import session_for

    with session_for("expman") as s:
        s.add(sample); s.commit()

The provider owns the shared Engine+pool (from `AXIOM_DB_URL`), normalizes
the extension name to a safe schema identifier, ensures the schema
exists, and sets `search_path` so unqualified table names resolve to the
extension's own schema. `engine_for()` returns `(shared engine, schema)`
for Alembic env.py wiring.

Aligned with ADR-050 (`tenant` + `site` vocabulary; this ADR provides
the *mechanism* for extension-level isolation; within-extension tenancy
options — single / row-level `tenant_id` / schema-per-tenant — sit on
top) and ADR-049 (cross-extension reads ride the data platform, not
OLTP joins).

### Added

- **`axiom.infra.db`** — `get_engine()`, `ensure_schema()`,
  `session_for()`, `engine_for()`, `normalize_extension_name()`.
  Process-wide shared Engine + pool. ~120 lines.
- **`docs/adrs/adr-052-database-tenancy-schema-per-extension.md`** —
  load-bearing decision (D1–D7); renumbered from 051 due to a parallel-
  session collision with the cross-provider-context ADR.
- Agent-facing docs wired with the schema-per-extension pattern so
  future Claude Code / Cursor / Copilot / Codex / Aider sessions find
  it before they invent something else: `AGENTS.md`,
  `docs/working/extension-developer-guide.md` (new "Step 2.5 — add
  persistence"), `docs/working/aeos-playbook.md` (new "Persistence"
  section + three anti-patterns), `docs/specs/spec-aeos-1.0.md` (new
  §4.x forward-pointer for the `[database]` manifest block).
- **`docs/prds/prd-builtin-mcp-server.md`** §5.1–§5.4 — Phase 1.5
  "Persistence surface": five `axiom_db__*` platform-primitive tools
  (4 read + 1 RACI-gated migrate), server-level `instructions` string,
  aggregation surfacing of the `[database]` manifest block, and an
  `extension-persistence` SKILL.md skill. Build tracked in axiom-os#268.

### Tests

10 unit + 5 integration tests in `tests/infra/test_db.py`. Integration
suite skips gracefully when Postgres isn't reachable.

### Related

- **PR #264** — code + docs
- **Issue #265** — "Extension developer ergonomics" umbrella (Rails-style
  scaffolding wave that sits on top of the primitive)
- **Issue #268** — MCP companion (surface the primitive to peer harnesses)
- **The domain consumer's EM-005 #33** — first consumer (expman extension)

## [0.24.0] — 2026-05-30 — DP-1: Box → bronze → RAG Dagster pipeline

Closes the data-platform's exercise #1 (per project_dp1_build_plan): a
real Box-folder source landing through the v0.22.0 provenance gate
into a content-addressed bronze substrate of record, then chunked +
embedded into the served RAG view. Drivable by Dagster (production)
or PLINTH's `axi plinth run-ingest` skill (gated fallback).

Per ADR-049 (orchestration boundary): Dagster owns the lakehouse path;
PLINTH triggers + applies `guarded_act` (RACI v2 D6); the v0.22.0 gate
is the single chokepoint — bronze gates once, embedder never re-gates.

### Added
- **`data_platform.contracts.FetchedItem`** — frozen dataclass carrying
  content + metadata (id, modified_at, etag, content_type, size,
  source_path, source-specific `extra`) bronze needs for sidecar
  manifests. Replaces `IngestSource.fetch() -> bytes` with
  `-> FetchedItem` (skeleton-stage contract change; zero external
  callers).
- **`data_platform.sources.BoxIngestSource`** — pull-oriented Box-folder
  `IngestSource`. Pluggable `api_client` for testability. Recurses into
  subfolders; client-side watermark filter on `modified_at`; size-
  consistency check on fetched bytes.
- **`data_platform.sources.BoxBrowserApiClient`** — production adapter
  reusing the `publishing/box_browser` Playwright SSO session for the
  read direction the upload-only provider lacks.
- **`data_platform.bronze`** — `BronzeWriter` composes the v0.22.0
  provenance gate (`rag.ingest_router.route_path`) with a sink.
  EXCLUDE writes a decision record only; ALLOW/QUARANTINE land
  content-addressed (sha256) + sidecar. `FilesystemBronzeSink` is the
  lean default + dev backstop (Iceberg sink follow-up).
- **`data_platform.rag_embed.embed_bronze_record`** — bronze → RAG
  adapter. Reads bytes from `record.content_path`, cites via
  `item.source_path`, never re-gates. Honors disposition; embed-failure
  no-upsert per the #7 lesson.
- **`data_platform.orchestration.run_box_to_rag`** — pure-Python one-pass
  driver. Pluggable for Dagster + PLINTH.
- **`data_platform.dagster_app`** — Dagster `Definitions` shim under
  the `[data-platform]` extra: `box_corpus_sensor`, `box_corpus` asset,
  `rag_index_ready` marker, `dp1_box_run_job`. Imports gated for
  contributors without the extra.
- **PLINTH skills + `axi plinth` CLI** —
  `register-connector` / `unregister-connector` / `list-connectors` /
  `run-ingest`. TOML connector registry under
  `$AXI_STATE/plinth/connectors/`. `run-ingest` wraps per-item writes
  in `guarded_act` (ADR-045 D6: reversibility + volume bound + sentinel
  pause). AEOS skill manifest entries.

### Changed
- `data_platform` extension version 0.1.0 → 0.2.0.
- `IngestSource.fetch(item)` return type: `bytes` → `FetchedItem`
  (skeleton-stage change; no external callers).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## [0.22.0] — 2026-05-28 — Durable RAG ingest + provenance gate + corpus audit + availability-aware CLI + multi-forge CI

Adds a durable, resumable document-ingest engine and an export-control-aware
provenance gate to the RAG subsystem (spec-rag-ingest-advanced), an
availability-aware CLI dispatcher (ADR-047), and a forge-agnostic CI-provider
abstraction for RIVET.

### Added
- **`axi rag ingest`** — durable, resumable ingest of large corpora: preflight
  (input scan, destination reachability, capacity estimate that aborts with
  numbers), calibration/ETA measured from the live run, a progress event stream
  (TTY panel + headless JSON), batch-granularity checkpoint/resume, and a SIGINT
  checkpoint-and-exit / double-tap-abort coordinator. Wraps the existing ingest
  engine; no schema change. (Replaces the legacy `ingest` index-alias.)
- **Provenance/artifact router** — per file, decides exclude / quarantine /
  allow(+tier) by source path and artifact type from a `--rules <file.toml>`
  rule set, wired into the ingest path so controlled or proprietary content is
  gated *before it is read*. Rule loader (`load_rules`/`load_rules_file`),
  first-match-wins routing.
- **Safe-by-default guard** — ingest into a shared tier (`rag-org`/
  `rag-community`) refuses without provenance rules unless `--no-rules` (or the
  `AXIOM_RAG_RULES` env var) is set.
- **`axi rag audit`** — audit an existing corpus's source paths against a rule
  set; `--purge` removes EXCLUDE-flagged documents. Finds controlled content
  ingested before the rules existed. (`store.list_document_paths`.)
- **Honest ingest drop reporting** — `IngestStats` distinguishes unchanged /
  unsupported / failed / excluded / quarantined, with per-extension and per-rule
  breakdowns surfaced to the operator. The directory walk now *sees* unsupported
  files instead of pre-filtering them invisibly.
- **Availability-aware CLI dispatcher** (ADR-047) — commands declare capability
  requirements (`requires = ["git"]` in the AEOS manifest); the dispatcher hides
  them from help/completion and refuses to run them with a reason + remedy when a
  dependency is missing, instead of crashing mid-run. New `infra/capabilities`
  (probes for git/gh/glab/gitlab-token), `infra/cli_gating`, and
  `AXI_SHOW_UNAVAILABLE` to reveal hidden commands. `axi release` requires git.
- **git availability helpers + repo-init offer** (`infra/git`, `infra/git_setup`)
  — `git_available`/`is_git_repo`/`is_inside_work_tree`/`init_repo`; `axi release`
  now guards on git + repo presence and offers `git init` (or fails fast in
  automation) instead of crashing on the first git call in a non-repo.
- **Forge-agnostic CI providers for RIVET** (`release/providers`) — pipeline
  status read via a provider detected from the git remote: GitHub (`gh`), GitLab
  (REST, any host, by path or numeric `project_id`), and Gitea/Forgejo. The
  watched-repo list is config-driven (`ci-repos.toml`, `$AXI_CI_REPOS`); a
  missing tool/token degrades to "no signal", never a crash.
- **`axi release --tag-only`** — tag an already-bumped version (e.g. after a
  release PR bumped it) and push, without re-bumping. The push fires the
  publish/mirror sync.

### Changed
- **Embedding-failure durability** — `embed_texts` raises `EmbeddingError` when a
  provider is configured but fails (vs returning `None` only when no provider is
  configured). Ingest no longer commits a document text-only with its checksum
  after a transient embedder/network failure (which would skip it as "unchanged"
  forever) — it retries on re-run. Query-time search degrades to keyword.
- **EC keyword screening** (`ec_screening`) reworked into severity classes
  (controlled/sensitive/none) and demoted to a *secondary* signal behind
  provenance-based routing.

### Notes
- The advanced-ingest live path uses the hardened engine (file-level checksum
  resume). Batch-level checkpoint execution, the federated SSH `--target`, and
  the proof prompt remain on the `spec-rag-ingest-advanced` roadmap.

## [0.21.1] — 2026-05-26 — TIDY remote-delete no longer hangs on credential prompt

### Fixed
- **`safe_git_env` sets `GIT_TERMINAL_PROMPT=0`** — no axiom git subprocess
  can hang waiting on an interactive credential/username prompt; it fails
  fast instead. (A `git push origin --delete` blocked on osxkeychain during
  the 2026-05-26 branch cleanup.)
- **TIDY `branch_prune` deletes GitHub remote refs via `gh api`** (scoped
  token) instead of `git push --delete`, which could block on the HTTPS
  credential prompt. Non-GitHub remotes keep the `git push --delete` path
  (now prompt-disabled, so it fails fast rather than hanging).

## [0.21.0] — 2026-05-26 — RIVET/TIDY boundary + TIDY branch/remote-ref reclamation + RACI D6 graduation-safety

Establishes the RIVET/TIDY role boundary (ADR-046) and closes the
detect-only gap in TIDY's branch hygiene.

### Added
- **TIDY branch/remote-ref prune** (`hygiene/branch_prune.py`): TIDY now
  *executes* merged-branch cleanup — deletes merged local branches and
  merged remote refs, archiving each under `refs/tidy-archive/` first
  (reversible). New `axi tidy branches [--prune] [--remote] [--dry-run]
  [--yes]` + `branch-hygiene` skill.
- **RACI v2 D6 graduation-safety** (ADR-045, now Accepted): the
  `act-then-notify` tier, reversibility-gated graduation, volume/rate
  circuit-breaker, novelty confirmation, digest batching.
- **Action-guard D6 primitives** (`policy/agent_action_guard.py`):
  reversibility gate (refuses irreversible autonomous ops) +
  `volume_mode="confirm"` (over-limit batch downgrades to a prompt).
- **RIVET lifecycle events** (`release/lifecycle_events.py`): emits
  `rivet.pr_merged` / `tag_released` / `ci_recovered` on the EventBus for
  TIDY to consume; RIVET touches no git refs.

### Docs
- ADR-046 (RIVET/TIDY boundary), ADR-045 D6 amendment, and the
  coverage-manifest / action-guard specs + agent roster synced to the new
  capabilities.

## [0.20.0] — 2026-05-22 — axi schedule install (4 backends) + RIVET PR-CI watcher (3 layers) + daemon idempotence

Two feature lines land in this release. The scheduling primitive
grows from render-only to a full multi-backend install pipeline.
RIVET grows a proactive PR-CI watcher with three layers.

### Added — `axi schedule install` apply side, four backends (PR #211 — issue #205)

`axi schedule install --host <host>` lays down host-side schedule
artifacts via four backends:

| Backend | Target | Artifact | Idempotence |
|---|---|---|---|
| `cron` | Linux/Unix with crontab | `<name>.cron` | marker-comment in crontab |
| `launchd` | macOS | `<name>.plist` under `~/Library/LaunchAgents/` | content-equal + already-loaded skip |
| `systemd` | Linux with user-systemd | bundled `<name>.systemd` → `.service` + `.timer` | `_write_if_changed` + daemon-reload |
| `wintasks` | Windows 10/11 via OpenSSH | `<name>.ps1` PowerShell script | `Unregister` before `Register` |

`--backend auto` (default) probes the host (`uname -s`, `command -v
systemctl`, `ver`) and picks. Transports: `SSHRunner` (default) and
`LocalRunner` for `--host localhost`.

Integration smokes against real hosts:
- cron via SSHRunner against a remote Linux host (`RUN_SSH_TESTS=1`)
- launchd against the local macOS box (`RUN_LAUNCHD_TESTS=1`) with
  tmp `LAUNCH_AGENTS_DIR` override
- systemd via SSHRunner against a remote Linux host (`RUN_SSH_TESTS=1`)
- wintasks unit-tests-only (no Windows host in CI)

Plus a full CLI E2E subprocess smoke (`test_cli_e2e_smoke.py`) that
exercises `axi schedule create → install → uninstall` through
`python -m`.

### Added — `axi schedule create` is backend-aware (PR #211 — issue #205)

`axi schedule create` now accepts `--backend
{cron,launchd,systemd,wintasks}` (default `cron`). Routes through
`backend.render()` so create and install share one format. Prior
0.19.0 path had quietly diverged — create wrote cron files without
the `# axi schedule managed:` marker that install required.

`axi schedule list` shows the inferred backend per row.

When `axi schedule install` finds no artifacts for the requested
backend but artifacts exist in OTHER backend formats, it now prints
an actionable error naming the mismatch + suggesting `--backend <X>`
or recreate. `axi schedule uninstall` checks artifact existence
before claiming success.

### Fixed — macOS daemon-registration noise (PR #211 — issue #208)

`LaunchdProvider.install` now compares plist content against the
on-disk file and skips the write when they match. `start()` probes
`launchctl list` and skips load when already loaded. Re-running
`axi install` on an unchanged config fires zero "App Background
Activity" toasts. Same idempotence in the new `LaunchdBackend` for
`axi schedule install`. `RegistrationResult.unchanged` plumbed
through; install CLI suppresses the "Agent services" output block
on fully-idempotent runs.

### Added — chat auto-start provisioned llamafile + free-first fallback (PR #212)

`axi chat` auto-starts a provisioned llamafile when present, and
shows a free-first fallback menu when the user hasn't set up a
provider yet.

### Added — RIVET PR-CI watcher, Layer 1 polling (PR #211 — slice 18)

New `release/pr_check_watcher.py`. RIVET's heartbeat enumerates the
user's open PRs (`gh pr list --author @me`) and fetches per-job
state, classifies failures (`infra` vs `code`), persists last-seen
state at `~/.axi/agents/rivet/pr-checks.json`, emits `StateFlip`
events on terminal-state transitions. Fills the gap that let PR
#211's own Build Wheel billing-block failure fly under the prior
top-level-run-only `ci_monitor._check_github`.

### Added — RIVET responder, Layer 3a notifications + failure reports (PR #214 — slice 19)

`release/pr_check_responder.py` routes `StateFlip` events via
`TerminalNotificationProvider` (stdout + macOS notification center
via pync):

- infra flip → high-urgency "ACTION REQUIRED" notification
- code flip → normal-urgency "FIX NEEDED" + failure-report markdown
  under `~/.axi/agents/rivet/reports/` with actionable next steps
- recovery → low-urgency "recovered" notification

### Added — RIVET auto-close on recovery (PR #215 — slice 20)

`release/pr_check_auto_closer.py` auto-closes stale `🔴 CI failed
on \`refs/pull/<N>/merge\`` issues opened by the github-actions bot
when RIVET sees a recovery flip. Safety defenses: title regex
anchored on the specific PR's merge ref, author must be the
`github-actions` bot, state must be `OPEN`, `RIVET_AUTO_CLOSE_DRY_RUN=1`
for surface-only, `RIVET_AUTO_CLOSE=0` for hard disable. Each close
drops an audit comment.

### Added — `axi rivet close-stale` manual sweep (PRs #215, #216, #217 — slices 21-23)

CLI verb for the existing backlog. Four target modes:

- `--pr <N>` — one PR (only if currently passing)
- `--all-prs` — every PR with stale issues whose state is safe
  (open+passing OR merged+main-passing OR closed-without-merge)
- `--all-main` — main-branch issues (only if main is currently
  passing)
- `--all-tags` — release-tag issues whose tag is reachable from
  main (release was integrated, codebase has moved on)

`--dry-run` previews on all four. Live deployment closed **95**
stale issues across the three target modes this cycle (44 main +
43 PR + 8 tag).

### Fixed — pre-push test flake (PR #211)

`hygiene.git_signals._run` now strips `GIT_*` env vars before
invoking subprocess git. The pre-push hook context was leaking
`GIT_DIR`, breaking 8 hygiene tests across the last three releases
by routing `git ls-files` etc. to the host repo instead of the
test's tmp_path. Plus regression tests that reproduce the leak.

### Documentation / hygiene

Scrubbed an org-specific host name from the scheduling
surface (`src/axiom/cli/scheduling/*` + tests). Test smokes now
require explicit `AXIOM_TEST_HOST` env var; default no longer baked
in. Follow-up issue #213 tracks the broader scrub across other
modules.

### Test count

- pre-push: ~7333 passed at release tip (up from 7152 at v0.19.0)
- 130+ new tests across scheduling backends, CLI E2E smoke, RIVET
  watcher/responder/auto-closer/sweep

### Breaking changes

None. The domain consumer's floor pin can bump `>=0.19.0` → `>=0.20.0`.

## [0.19.0] — 2026-05-19 — Memory sessions + MCP audit-trail fixes + repo-hygiene signals + AEOS defaults + axi schedule

Three PRs across three feature areas land in this release. Headline:
memory provenance grows to include a session dimension, the MCP
audit-trail surface is correctness-fixed, and Axiom gains a proactive
hygiene-signals surface plus AEOS-default + scheduling primitives that
the 2026-05-18/19 self-hosted-node cleanup motivated.

### Added — session-aware memory (PR #197 — `spec-memory §3.7`)

`Provenance` tuple grows from `(T, U, A, R)` to `(T, U, A, R, S)`.
Episodic fragments stay session-bound by default; core / procedural /
resource fragments cross sessions unconditionally; semantic fragments
cross by relevance (phase-1 = no filter). The principle: **things you
DID are scoped to where you did them; things you KNOW are global.**

- `Provenance.session_id: str = ""` — backwards-compatible default;
  legacy v1+v2 fragments decode to `""` and are interpreted as
  cross-session.
- `CompositionService.write(..., session_id=)` auto-resolves from the
  active session when omitted.
- New module `axiom.memory.session`: per-process current-session
  resolution, `~/.axi/sessions/<uuid>.json` registry, auto-name
  `<cwd-basename>-<YYYY-MM-DD-HHMM>`, 4h auto-resume window per
  `(principal, cwd)`. `PYTEST_CURRENT_TEST` auto-disables resolution
  so the test suite doesn't pollute `~/.axi/`.
- `axiom_memory__compose` MCP tool accepts caller's `session_id`
  (cross-vendor clients attribute writes to their session, not the
  MCP server's process session).
- `axiom_memory__retrieve` accepts `scope` (`default` / `strict` /
  `current` / `all` / `session:<id>`) + `session_id`; surfaces
  `session_id` in the provenance block for cross-session attribution
  audits.
- PRD §5 (item 10) + §6 (new bet) + §9 (open question) updated;
  spec-memory §3.3 (tuple grows) + §3.7 (new — id/lifecycle/scope/
  registry/CLI) + §8 (write signature) updated.

### Added — MCP audit-trail bug fixes (PR #197)

Three stranded fixes that landed alongside the session work — the
`(T,U,A,R)` provenance audit was effectively broken on main without
them:

- `fix(mcp)`: `axiom_memory__compose` honors caller-supplied agents +
  resources. Previously, every MCP-written fragment came back with
  `agents=["mcp_root_server"]` regardless of caller — no way to
  distinguish "Claude wrote this from Claude Code" vs "GPT-5 wrote
  this from Codex".
- `fix(mcp)`: `axiom_memory__retrieve` returns full content +
  provenance by default. Prior shape truncated each fragment to
  `summary + fact_kind`, forcing follow-up per-fragment fetches that
  didn't exist as tools yet.
- `fix(mcp)`: `axiom_memory__retrieve` sorts newest-first. Without
  the sort, the registry returned insertion order ascending so
  `limit=N` returned the OLDEST N — exact opposite of every chat
  client's expectation.

Bonus: `scripts/inspect-mcp-memory.py` for quick `(T,U,A,R)` audit
dumps.

### Added — repo-hygiene signals (PR #204 — issue #201)

Seven pure-function signals over file-tree + git state. Each returns
`Finding` objects suitable for `node_health` aggregation; each
`auto_fixable` flag distinguishes TIDY-can-batch-resolve entries from
human-review-only entries.

| Signal | Surfaces |
|---|---|
| `check_stale_branches` | Local branches whose tip is on `origin/main` and aren't checked out anywhere |
| `check_orphan_worktrees` | Worktrees git marks prunable, on already-merged branches, or whose upstream is gone (filters local-only-WIP false positive) |
| `check_dormant_stashes` | Stashes older than the dormancy threshold (default 60d) |
| `check_dup_basenames` | Same filename tracked at multiple paths (filters `__init__.py` etc.) |
| `check_self_similar_dirs` | Paths containing `X/X/` patterns |
| `check_scripts_with_hardcoded_paths` | `scripts/*.sh` containing `$HOME/Projects/...` etc. |
| `check_non_graduated_scaffolds` | Scaffolds tracked by `axi ext init` that haven't graduated and are older than `dormancy_days` (default 14) |

Each signal's motivating case was a real artifact surfaced during the
2026-05-18/19 cleanup audit (25 stale branches on axiom, 8 on
the domain consumer, the self-hosted-node residue chain, etc.).

### Added — AEOS defaults (PR #204 — issue #202.1/.2/.4/.5/.6)

- `axiom.infra.paths.get_agent_output_dir(agent_name)` returns
  `<project_root>/runtime/agent-output/<agent_name>/`. Convention:
  every extension agent writing operational output (heartbeat JSON,
  health reports, cron logs) resolves its write path via this helper.
  Consumers `.gitignore` the single root once and cover every agent
  forever. Motivating case: the domain consumer's `runtime/mo-reports/`
  accumulating 32MB of untracked heartbeat JSON.
- `axi ext init` records each scaffold in
  `<project_root>/.axi/scaffold-graduation.json` (project-local,
  gitignored). Companion `check_non_graduated_scaffolds` flags
  prototypes that sit untouched. Module: `axiom.cli.ext.scaffold_registry`
  with `ScaffoldRecord` / `record_scaffold` / `graduate_scaffold` /
  `list_records` / `list_non_graduated`.
- AEOS scaffold's generated `AGENTS.md` now points new extension
  authors at `get_agent_output_dir`, with a note on the bespoke-path
  failure mode.

### Added — `axi schedule` primitive (PR #204 — issue #203, render side)

- `axi schedule create <name> --host <host> --cron <expr> --command <cmd>`
  writes a portable cron artifact to `deploy/<host>/<name>.cron` with
  `${REPO_DIR}` / `${PROJECT_ROOT}` placeholders. Per-host isolation;
  re-running overwrites. Validation rejects malformed cron and
  unsafe name/host segments.
- `axi schedule list` enumerates all schedule artifacts under
  `deploy/`.
- Apply side (`axi schedule install --host`) tracked as issue #205
  follow-up — SSH + crontab manipulation deserves its own focused PR.

### Fixed — conftest pollution-restoration (PR #200)

`tests/conftest.py`'s session-end guard snapshots `user.name` /
`user.email` at session start and restores them at session end.
Discovered 2026-05-18: if the snapshot was already polluted from a
prior session (e.g. `Test` / `test@example.com` from a hygiene
fixture leak), the guard re-installed the pollution forever — the
conftest itself became the persistence vector.

- `tests/_pollution_guard.is_polluted_snapshot()` recognizes known
  markers: names `Test` / `T` / `tester` / `GLOBAL-LEAK-PROBE` and
  emails `test@example.com` / `t@example.com` / `t@t.test`.
- Guard now **unsets** the local config when the snapshot itself is
  polluted, letting the global identity (`Benjamin Booth`) take over.

### Versions

- axiom-os-lm `0.18.0 → 0.19.0`

### Test counts

- Pre-push: 7097 passed / 220 skipped / 76 deselected
- 115 new tests across PR #197 (37) + PR #200 (19) + PR #204 (59)

### Downstream

- The domain consumer's floor pin: bump `axiom-os-lm>=0.18.0` → `>=0.19.0` to
  pick up the session-aware memory + MCP audit-trail fixes (the
  hygiene signals + AEOS defaults are useful in the domain consumer's own
  development; the schedule primitive is the right home for what
  `scripts/mo-heartbeat.sh` was doing).

## [0.18.0] — 2026-05-18 — Chat surface: resilience, multimodal, slash commands, scidisplay Pillar 1, built-in MCP

Twelve PRs across four feature areas land in this release. Headline:
the chat surface gains parity-level capabilities (slash commands,
prompt fragments, multi-modal vision, per-tool permissions, background
tasks, graceful tool-failure recovery), Axiom ships its first scientific
display pillar (math + code rendering), and the built-in MCP server
exposes platform primitives to MCP clients.

### Added — chat surface

- `feat(chat)` — **user-defined slash commands** (Claude-Code-style).
  Authors drop `.md`/`.txt` files into a prompt-library directory;
  the chat surface registers them as `/name` shortcuts with argument
  templating.
- `feat(chat)` — **user-authored prompt-library fragments**. Reusable
  snippets composable into slash commands or pasted inline.
- `feat(chat)` — **multi-modal image input** (Anthropic + OpenAI
  vision). Attach images via paste / drop / `axi chat --image PATH`;
  the chat loop forwards them to the model with vision-capable
  request shapes.
- `feat(chat)` — **per-tool persisted permissions**. Permission
  prompts now offer "Always allow" / "Always deny" toggles that
  persist across sessions per `(principal, tool)` tuple.
- `feat(chat)` — **`/tasks` slash command** wires the background-task
  surface into chat; long-running ops (test suites, benchmarks,
  ingest) report progress as a separate stream.
- `feat(chat, gateway)` — **resilience pass**. Gateway retries 5xx +
  network errors with jittered exponential backoff; chat handles
  tool failures via a typed taxonomy and auto-retries the recoverable
  classes without losing conversation context.

### Added — scidisplay (Pillar 1)

- `feat(scidisplay)` — **math rendering pipeline** (A1-A5). KaTeX-based
  ` ```math ` fence rendering with three Axiom themes; cached
  per-fragment.
- `feat(scidisplay)` — **chat-surface math integration** (A4). Math
  fences in chat output render inline at terminal-display time.
- `feat(scidisplay)` — **code rendering with three Axiom themes**
  (A9-A11). Syntax-highlighted code blocks; theme matches the
  surrounding chat surface.
- `feat(scidisplay)` — **chat-surface code integration** (A12).
  ADR-039 (scientific displays) lands as the architectural anchor.

### Added — built-in MCP server

- `feat(mcp)` — **built-in MCP server core**: aggregation, server,
  drift detection, CLI surface, subscriber. ADR-038 + PRD + spec +
  18 harness adapter docs.
- `feat(aeos)` — **MCPBlock schema** + manifest-schema lint +
  scaffold default for `axi ext init`.
- `feat(hygiene, signals)` — MCP handlers for hygiene + signals
  extensions; `node_health` drift check exposed to MCP clients.

### Added — memory CLI

- `feat(memory)` — color + markdown rendering helpers in
  `axi memory show`. Frontmatter highlighting, semantic coloring of
  fragment fields, inline markdown for fragment bodies.

### Added — design (no code)

- `prd(identity)` — `prd-identity-and-bindings.md`: external-account
  binding layer + persona model + three verification levels.
- `prd(memory)` — `prd-cross-surface-memory.md`: vocabulary lock,
  per-vendor inbound/outbound matrix, OpenCode interop posture.
- `docs(roadmap)` — 2026-05-14 entry; new identity + cross-surface
  design layer (id-1..id-10) spanning Phase 0 through Phase 4
  federation.

### Versions

- axiom-os-lm `0.17.1 → 0.18.0`
- memory extension unchanged at `0.5.0`
- scidisplay extension at `0.1.0` (first feature release)

### Notes for downstream consumers

- The domain consumer's floor pin: bump `axiom-os-lm>=0.17.1` → `>=0.18.0` to
  pick up the gateway resilience improvements.
- No breaking changes to public APIs. CompositionService,
  MemoryFragment, federation surfaces unchanged.

## [0.17.1] — 2026-05-13 — bench-1: LongMemEval first run

First public-benchmark wiring through the maturation pipeline.
Drives [LongMemEval](https://huggingface.co/datasets/xiaowu0162/LongMemEval)
(Xiao Wu et al. 2024) — 500 long-conversation questions across 5
capabilities. 0.17.1 ships the **runnable harness** + **reproducible
baseline number** on the first 100 questions.

### Added — `axiom.memory.maturation.bench`

- `SyntheticCorpus.small()` — 5-question in-memory fixture (no network)
  covering all 5 LongMemEval capabilities. Used by unit tests + smoke
  runs.
- `load_corpus_from_huggingface()` — direct `hf_hub_download` of
  `xiaowu0162/LongMemEval` (`longmemeval_s` / `longmemeval_m`). The
  LongMemEval files are bare JSON, not standard HF dataset format, so
  the loader bypasses `datasets.load_dataset` to avoid format-detection
  failure.
- `LongMemEvalRunner(configuration="baseline" | "matured", top_k=3)`
  — per-question isolated ledger; ingests haystack sessions as
  `chat_turn` episodes; optionally runs the full maturation pipeline
  (mat-2 → mat-3 → mat-4) when `configuration="matured"`; keyword-
  overlap retrieval over `chat_turn` + `compacted_chat_turn` +
  `semantic_insight`; recall-based scoring with abstention handling.
- `score_answer()` — token-recall of ground-truth in retrieved text,
  stopword-filtered. Deterministic (LLM-judged scoring lands when the
  gateway integration ships).
- CLI: `python -m axiom.memory.maturation.bench.longmemeval --corpus
  {synthetic|huggingface} [--limit N] [--top-k K] [--configuration
  {baseline|matured|both}] [--json]`

### Results — first 100 LongMemEval-S questions

| Configuration | Accuracy | Mean recall | Δ |
|---|---|---|---|
| baseline | **77.0%** (77/100) | **0.730** | — |
| matured | 77.0% (77/100) | 0.730 | +0.0pp |

**Headline:** the substrate + keyword-overlap retrieval recovers ≥ 50%
of ground-truth tokens on **77 of 100** real LongMemEval-S questions.
This is the first defensible "best memory platform" number for Axiom
(per `prd-memory.md §3` axis 3).

**Honest no-differentiation finding:** with *deterministic everything*
(mat-2 importance heuristic + mat-3 token-recurrence reflection + mat-4
length-reduction compaction), the matured ledger doesn't differentiate
from episodic-only under token-overlap retrieval. The full analysis is
in `docs/working/memory-benchmarks-longmemeval-2026-05-13.md`. Short
version: deterministic reflection re-states tokens that already exist
in the source episodes, so a token-overlap retriever sees no new
vocabulary. LLM-driven reflection (deferred from 0.17.0; lands when
the gateway integration ships) is expected to differentiate by
producing synthesis with new vocabulary.

**What the run *does* prove:**

- Substrate scales: 100 questions × 2 configurations end-to-end in
  ~17min on a laptop, with median 54 sessions × 2 turns per question
  ingested into isolated ledgers.
- No regression: the maturation pipeline never destroys retrievable
  content (audit-chain rule from `spec-memory-compaction.md §6.1`
  holds end-to-end).
- Benchmark harness reproducible: deterministic scoring + extractors
  + scorer; byte-identical replay across runs.

### Added — 9 smoke tests

`test_longmemeval_bench.py` covers module surface, synthetic corpus
shape, `score_answer` recall semantics (recovery, abstention, stopword
handling), runner accuracy on the synthetic corpus, no-regression
check (matured ≥ baseline), result.to_dict shape, invalid-configuration
rejection.

### Added — benchmark report

`docs/working/memory-benchmarks-longmemeval-2026-05-13.md` —
full analysis, per-bucket breakdown, reproduction instructions, and
sequencing into 0.18+.

### Versions

- axiom-os-lm `0.17.0 → 0.17.1`
- memory extension unchanged at `0.5.0`

### Deferred to 0.18.x / 0.19

- Full 500-question run (cost: ~88min; this release runs first 100
  for the substantive number — running the remaining 400 doesn't
  change the headline finding without an LLM-driven extractor).
- LLM-driven extractor variants (gateway integration prerequisite).
- LLM-judged scoring (the published LongMemEval methodology; needed
  for direct comparison with published Mem0/Letta/MemGPT numbers).
- bench-2 LoCoMo, bench-3 MemBench.

## [0.17.0] — 2026-05-13 — Memory matures: dream-cycle lifecycle (mat-1..mat-4)

Axiom Memory now matures the way biological memory does. Episodes get
scored for importance, consolidate into semantic facts via the daily
dreaming pass, and compact into summary fragments once their insight
has been captured. The dream-cycle orchestrator coordinates all of it
per scope, gated by per-stage triggers and per-cycle budgets.

This release ships the **minimum-viable maturation pipeline** of the
seven-stage lifecycle defined in `docs/specs/spec-memory-maturation.md`:

- Stage 2: importance scoring (catch-up sweep + deterministic scorer)
- Stage 3: daily consolidation (deterministic reflection extractor)
- Stage 4: summarize compaction (with audit-chain enforcement)

Stages 5 (archive), 6 (forget), and the LLM-driven extractor variants
land in 0.18+ once the gateway integration is wired and a cold-tier
storage backend is selected. See `docs/working/memory-roadmap.md` for
sequencing.

### Added — `axiom.memory.maturation` package

- **`DreamCycleOrchestrator`** (`maturation.dream_cycle`) — coordinates
  registered :class:`StageHandler` instances in canonical
  :data:`STAGE_ORDER` for a scope. Per-cycle budget enforcement
  (calls, tokens, walltime) stops the cycle cleanly at a stage
  boundary; :class:`BudgetExceededError` raised mid-stage marks the
  cycle interrupted. Per-scope cooldown (default 60s) prevents
  thrashing; ``force=True`` on :meth:`run_cycle` bypasses. Each cycle
  writes a ``fact_kind="dream_cycle_metrics"`` fragment to the ledger
  for SCAN/TRIAGE observability.
- **`ImportanceScoringStageHandler`** (`maturation.importance`) —
  stage-2 catch-up sweep that scores episodic fragments lacking an
  importance score. Side-fragment representation
  (``fact_kind="importance_score"``, ``target_fragment_id``) keeps the
  substrate append-only. Default scorer is
  :class:`DeterministicImportanceScorer` (byte-identical heuristic on
  text-length + question marks + presence of assistant response).
- **`ReflectionStageHandler`** (`maturation.reflection`) — stage-3
  daily consolidation. Builds an :class:`EpisodeBatch` from un-
  consumed episodes (newer than the scope's last
  ``reflection_marker``), invokes the extractor, applies the policy
  gate (citation requirement; classification composition: derived
  = max of sources), writes accepted proposals as semantic fragments
  with ``derived_from`` provenance chains, and emits a new marker so
  the next cycle skips already-consumed episodes. Default extractor
  is :class:`DeterministicReflectionExtractor` (recurring-token
  heuristic; byte-identical). Trigger: accumulated importance ≥
  150.0 (Park et al. threshold).
- **`CompactionSummarizeStageHandler`** (`maturation.compaction`) —
  stage-4 summarize cadence. Audit-chain enforcement: only compacts
  episodes whose ``id`` appears in some semantic fragment's
  ``derived_from``. Default summarizer is :class:`DefaultSummarizer`
  (deterministic length-reduction; must reduce ≥ 50%). Each compaction
  emits two side fragments — the compacted_chat_turn summary and the
  supersession record — keeping the substrate append-only.

### Added — three coordinated normative specs

- `docs/specs/spec-memory-maturation.md` — the umbrella. Two
  operations (consolidate vs compact), six stages, multi-scale timing
  (seconds → year), dream cycle, MIRIX 6-type backbone, storage
  tiering, classification monotonicity, per-extension policy profiles
  (default | aggressive | conservative | regulated | custom).
- `docs/specs/spec-memory-reflection.md` — stage 3 narrowly: three
  cadences (daily / weekly / monthly + custom), LLM and deterministic
  extractor kinds as peers, citation requirement, policy gate. The
  daily pass ships here; weekly themes + monthly identity (`semantic
  → core`) follow.
- `docs/specs/spec-memory-compaction.md` — stages 4-6: summarize /
  archive / forget. Audit-chain enforcement; cryptographic erasure
  per ADR-026; the tombstone-event channel that reflection
  subscribes to; cohort cold-tier federation; TIDY as principal
  operator, WARDEN as auditor.

### Tests

49 new tests across four files cover the maturation MVP:

- `test_dream_cycle_orchestrator.py` (16) — module surface, Stage
  enum, canonical ordering, registration semantics, cycle execution
  (empty / pending / triggered / skipped), budget enforcement, cooldown,
  cycle-metrics fragment.
- `test_importance_scoring.py` (14) — module surface, deterministic
  scoring properties (range, idempotency, ordering), side-fragment
  write, handler pending/idempotency/scope filtering, end-to-end via
  orchestrator.
- `test_reflection.py` (12) — module surface, deterministic extractor
  byte-identical replay, handler trigger at importance threshold,
  semantic fragment write with `derived_from`, reflection-marker,
  citation gate rejection, end-to-end via orchestrator.
- `test_compaction.py` (11) — module surface, default summarizer
  length reduction + determinism, audit-chain enforcement (skip
  without semantic; compact with semantic), supersession side
  fragment, idempotency, end-to-end via orchestrator.

Full memory ext + axiom.memory regression: 165 passed, 4 skipped, 0
failed.

### Versions

- axiom-os-lm `0.16.0 → 0.17.0`
- memory extension `0.4.0 → 0.5.0`

### What's deliberately *not* in this release

- LLM-driven extractors (deterministic only this round; gateway
  integration deferred)
- Stage 5 (archive) — needs cold-tier storage backend selection
- Stage 6 (forget) — needs cryptographic erasure key-management
- Weekly + monthly reflection cadences — daily only
- Cross-scope reflection
- `axi memory dream` CLI surface — orchestrator is library-only this
  round
- bench-1 LongMemEval first run — deferred to 0.17.x (see roadmap)

## [0.16.0] — 2026-05-11 — Codex CLI transcript ingest (cross-tool memory adapter #2)

### Added — codex adapter for `axi memory ingest`

Second canonical per-tool transcript parser, joining claude-code in the
cross-tool memory substrate. Closes the silent gap where Codex CLI
sessions sat unfolded on disk despite the Codex MCP registration shipped
in 0.15.x.

- New `parse_codex_jsonl()` in `axiom.memory.session_capture` — parses
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` into turn-pair dicts.
  Drops `role=developer` records (system permissions / instructions);
  collapses *consecutive* same-role records (streamed assistant output;
  auto-injected env context + user prompt) into single segments; emits
  one turn pair per (user-segment → assistant-segment) transition.
- New `ingest_codex_jsonl()` — full ingest path with idempotency on
  stable `source_uuid = codex:<session_id>:turn-<idx>` derived from
  `session_meta.id` + turn index. Re-running on the same transcript is
  a no-op; running on a transcript that grew in place writes only the
  new turn pairs. Mirrors the `ingest_claude_code_jsonl` contract.
- `axi memory ingest <path> --tool codex` dispatches to the new parser
  via `KNOWN_TOOL_PARSERS`. CLI surface unchanged.
- 16 new unit tests covering parse contract, pairing semantics
  (consecutive-record collapse, developer-role drop, function_call
  ignore, malformed-line tolerance), idempotency, dispatch, and
  metadata extraction. Verified end-to-end against six real user-host
  codex transcripts (13 turn pairs total).

### Fixed — `axiom.infra.git.run_git` no longer walks up to parent repos

The shared `run_git` helper passed `cwd=repo_root` but didn't constrain
git's upward search, so callers handing it a path lacking a `.git/`
(e.g. a `tmp_path` in tests, or a project subdirectory under a larger
workspace repo) silently got state from the nearest ancestor repository.
That vector produced the 2026-05-04 and 2026-05-11 tester-pollution
incidents — the worktree's HEAD moved during test runs because production
code called via tests reached up and operated on the real workspace repo.

`run_git` now sets `GIT_CEILING_DIRECTORIES=<repo_root>` in the subprocess
env. If no `.git/` is present at `repo_root` or below, git returns its
normal "not a git repository" error rather than searching upward.

The same fix applies to three duplicate `_run` helpers in the hygiene
extension that bypass `run_git`:

- `axiom.extensions.builtins.hygiene.worktrees._run`
- `axiom.extensions.builtins.hygiene.drift._run`
- `axiom.extensions.builtins.hygiene.ci_watcher._run`

Each now sets `GIT_CEILING_DIRECTORIES=<cwd>` (or `Path.cwd()` when cwd
is None) before invoking subprocess.

### Fixed — GIT_DIR propagation from git hooks bypassed every protection

The pre-push hook's full-suite run still produced 17 failures + 27
errors after the `GIT_CEILING_DIRECTORIES` fixes, even though
standalone pytest runs were clean. Empirical reproduction (running
pytest with ``GIT_DIR=/Users/example/.../axiom-memory-mcp/.git`` in the
parent env) showed the exact same failure pattern as the hook context.

Root cause: ``git push`` invokes pre-push hooks with ``GIT_DIR`` (and
peer ``GIT_*`` vars) set in the environment. ``GIT_DIR`` short-circuits
all git repo discovery — it overrides ``cwd=``, ``-C <path>``, and
``GIT_CEILING_DIRECTORIES``. Every subprocess we spawned for git
inherited ``GIT_DIR`` from ``os.environ.copy()``, so each test silently
operated on the host repo instead of its ``tmp_path``-scoped one.

Fix: strip every ``GIT_*`` key from the inherited env before adding our
isolation-specific keys. Applied to:

- `axiom.extensions.builtins.hygiene._git_isolation.git_isolated_env`
  (the canonical test helper)
- `axiom.infra.git.run_git`
- `axiom.extensions.builtins.hygiene.{worktrees,drift,ci_watcher}._run`
- `axiom.cli.ext.commands.publish._in_git_repo` and `_git_tags` (new
  shared `_git_env_for` helper)

Verification: pytest run with ``GIT_DIR`` artificially set in the
parent env — 6700 passed, 204 skipped, 0 failed, 0 errors, HEAD
unchanged. This is the test that the standalone runs could not
exercise but the pre-push hook does by virtue of being invoked from
``git push``.

A systemic audit of the remaining production sites that invoke git via
subprocess.run (12+ files outside the helpers above) is queued as
follow-up. Current tests pass with the helpers fixed; remaining sites
are latent rather than active vulnerabilities, but the same
strip-`GIT_*` pattern should apply to each.

### Fixed — consolidated all git-subprocess sites onto `safe_git_env`

The systemic audit landed: every production `subprocess.run(["git", ...])`
call site now sets `env=safe_git_env(<cwd>)`. New canonical helper
`axiom.infra.git.safe_git_env(repo_root=None) -> dict[str, str]`
(public, exported) returns an env dict with every `GIT_*` key stripped
plus, when `repo_root` is supplied, `GIT_CEILING_DIRECTORIES` set to
its resolved path.

Refactored to use the shared helper:

- `axiom.infra.git.run_git` (was duplicating the strip + ceiling logic)
- `axiom.extensions.builtins.hygiene._git_isolation.git_isolated_env`
- `axiom.extensions.builtins.hygiene.{worktrees,drift,ci_watcher}._run`
- `axiom.cli.ext.commands.publish._in_git_repo` + `_git_tags`

Newly fixed sites (22 across 14 production files): the previously
latent vulnerabilities listed in the audit. Includes:

- `axiom.agents.pipeline.repo_state._default_runner`
- `axiom.cli.ext.commands.migrate._is_inside_git_worktree`, `_move`
- `axiom.extensions.builtins.classroom.cli` (git config --global probe)
- `axiom.extensions.builtins.diagnostics.tools._exec_git_commit_fix`
  (7 sites)
- `axiom.extensions.builtins.hygiene.agents.tidy.discover._git_head`
- `axiom.extensions.builtins.hygiene.cli` (3 sites)
- `axiom.extensions.builtins.publishing.scripts.publish.get_commit_sha`,
  `count_commits_since`
- `axiom.extensions.builtins.release.cli._git`
- `axiom.extensions.builtins.review.tools.diff.local_diff`
- `axiom.extensions.builtins.update.cli` (9 sites)
- `axiom.extensions.builtins.update.version_check` (4 sites)
- `axiom.rag.personal._git_log_text`
- `axiom.setup.probe` (2 sites)

Non-git subprocess sites (kubectl, docker, pip, pytest, ruff, pandoc,
grep, k3d) intentionally left alone — `safe_git_env` is specifically
for git's environment-variable surface and would be misleading on
other commands.

Verification: 6700 passed / 0 failed / 0 errors in *both* standalone
and `GIT_DIR`-set-in-parent (simulated pre-push hook) contexts. The
hook-simulated check is the new ground truth — prior fixes passed
standalone but the hook context exposed each missed vector.

### Fixed — `test_concurrent_reads_dont_block_each_other` threshold

Bumped the concurrent-reads timing assertion from `< 1.0s` to
`< 3.0s`. The test asserts that 3 multiprocessing readers complete
concurrently (not serially). Standalone the test runs in ~0.4s, but
late in the 6700-test full sweep system load pushes it to ~1.0–1.5s
even with the locks behaving correctly. 3.0s still catches a serial
regression (which would be 3+ second-scale process-startup overhead)
without false-positive-ing on load. Not caused by the safe_git_env
work — pre-existing flakiness surfaced by running it inside the full
sweep.

### Notes

- Codex MCP-side registration already shipped in 0.15.x (writes
  `~/.codex/config.toml` via the per-tool registrar protocol). 0.16.0
  completes the round-trip: registered Codex sessions can now be folded
  into the per-principal ledger via the ingest path.
- No breaking changes; additive to the dispatch surface. `opencode`,
  `gemini`, and `chatgpt-desktop` remain stubs with contributor-pointer
  `NotImplementedError`.
- Memory extension manifest bumped 0.3.0 → 0.4.0.
- Validates the per-tool adapter pattern that the remaining cross-tool
  Tier-2 work (jetbrains, vscode-copilot, gemini, chatgpt-desktop) will
  replicate per `docs/working/memory-roadmap.md`.

## [0.15.1] — 2026-05-08 — Re-release of 0.15.0 content (PyPI 0.15.0 was a phantom)

PyPI's `axiom-os-lm 0.15.0` (published 2026-05-06) was an accidental release built from an orphan version-bump commit (`dba60dd1`) that never reached `main`. It contained essentially only the version number change. The real 0.15.0 content — cross-tool memory MCP foundation, post-Prague hardening, and the agent rename — landed on `main` in PR #171 (2026-05-08) and ships as **0.15.1**.

The PyPI 0.15.0 release stays in place (immutable, can't be replaced); 0.15.1 is the canonical version users should install. Same surface as the [0.15.0] section below; treat that section as the change-log for 0.15.1's content.

## [0.15.0] — 2026-05-08 (PyPI release built from PR #171 — phantom on PyPI; superseded by 0.15.1) — Cross-tool memory MCP foundation + post-Prague tight-cut hardening

### Added — axiom-memory MCP server (per-extension topology)

The shared cross-tool, cross-session memory substrate. Every LLM tool the user
touches (Claude Code, Codex, Gemini, OpenCode, axi chat, future tools) writes
to and reads from the same per-principal ledger via a common path.

- New `axiom-memory` MCP server at `axiom.extensions.builtins.memory.mcp_server`
  exposing read tools (`axiom_memory_show`, `_recent`, `_search`) and write
  tools (`axiom_memory_append`). Server `instructions` field carries the
  cross-vendor model-discipline prompt for tools without on-disk session logs.
- New `axiom.memory.session_capture.record_session_turn()` — the common write
  path the MCP, CLI, and (eventual) `axi chat` all converge on. Provenance
  encodes originating tool + model in the agents set so cross-tool queries can
  scope by origin.
- `axi memory record [--principal] [--tool] [--user-input] [--assistant-output]`
  for shell-driven writes. `--json-stdin` reads a JSON event for hooks /
  automation.
- `axi memory ingest <path>` folds Claude Code session JSONL transcripts into
  the ledger via the common path. Idempotent on `content.extra.source_uuid`.
  `--watch` polling mode supports incremental ingest as the transcript grows.
- `axi memory ingest --tool <name>` dispatches by parser. claude-code is
  canonical; opencode / gemini / chatgpt-desktop are stubs raising
  NotImplementedError with a contributor pointer.
- Memory extension manifest declares the MCP via `[mcp_servers.axiom-memory]`
  so `axi ext mcp --target claude_code` aggregates it alongside other
  extensions.

### Added — post-Prague memory hardening (tight cut)

Items pulled forward from `project_axi_memory_failure_modes_and_selfheal` to
close the highest-leverage silent-dysfunction classes before Prague:

- **Default principal pin** via `memory.default_principal` setting. CLI and
  MCP fall back to the pin when callers omit `--principal` / `principal_id`,
  closing the cross-identity footgun (e.g. canonical UT email vs harness-
  provided account email).
- **Per-tool registrar protocol** in `axiom.extensions.builtins.memory.register_mcp`.
  `TOOL_REGISTRARS` maps tool name → `ToolRegistrar` (detect + register +
  is_registered). `axi memory register-mcp --all` walks every detected tool;
  `--tool <name>` for a single tool. New Codex registrar (writes
  `~/.codex/config.toml` via tomlkit, idempotent). Gemini and OpenCode are
  stubs.
- **Heartbeat fragment + freshness check.** `axi memory heartbeat` writes a
  periodic `fact_kind=heartbeat` fragment via the common path. `axi memory
  heartbeat-install` lays down a launchd plist
  (`~/Library/LaunchAgents/com.axiom.memory.heartbeat.plist`) for hourly
  cadence; `heartbeat-uninstall` reverses it. `axi dr` flags missing/stale
  heartbeats: ≤60min OK, 60–120min WARN, >120min ERROR.
- **`axi dr` principal-pin reconciliation.** Samples the 25 most-recent
  fragments and WARNs when recent writes used a principal other than the
  pinned default — catches drift before users wonder why memory is "empty."
- **`axi dr` MCP-registration check** (resolves symlinks so `python` ≡
  `python3.14` in the same venv don't false-positive as stale).

### Renamed — agents (IP remediation, was Unreleased before 0.15.0)

Renamed agents to remove Pixar/WALL-E derivation:
- WALL-E → AXI (the chat/orchestrator agent)
- EVE → SCAN
- M-O → TIDY
- BURN-E → RIVET
- D-FIB → TRIAGE
- PR-T → PRESS
- CURI-O → CURIO (dash dropped)
- CHALK-E → CHALKE (dash dropped)
- V-EGA → WARDEN

CLI nouns updated where they matched old agent names. PyPI script entry points
`walle` and `wall-e` removed (agent identity unified with the `axi` binary).
No backwards aliases — pre-public, no external users to deprecate.

### Notes

- Tests: 423 passed / 4 skipped in the targeted memory + cli + classroom sweep.
- 49 new tests across the post-Prague tight-cut items.
- Out of scope (still post-Prague queue): per-tool MCP liveness probe, file-
  watch on tool configs, audit-log → SQLite replay, RACI escalation surface,
  privacy knobs, opt-in install wizard, onboarding seed fragment, Linux
  systemd-timer install.

---

## [Pre-0.15.0 history — previously labeled Unreleased] — 2026-05-04 — Agent rename (IP remediation)

Renamed agents to remove Pixar/WALL-E derivation:
- WALL-E → AXI (the chat/orchestrator agent)
- EVE → SCAN
- M-O → TIDY
- BURN-E → RIVET
- D-FIB → TRIAGE
- PR-T → PRESS
- CURI-O → CURIO (dash dropped)
- CHALK-E → CHALKE (dash dropped)
- V-EGA → WARDEN

CLI nouns updated where they matched old agent names. PyPI script entry points `walle`
and `wall-e` removed (agent identity unified with the `axi` binary). No backwards
aliases — pre-public, no external users to deprecate.
