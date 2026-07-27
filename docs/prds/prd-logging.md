# Axiom System Logging PRD

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-19
**Last Updated:** 2026-03-19
**Tech Spec:** [Logging Spec](../tech-specs/spec-logging.md)

**Scope:** System Log taxonomy, EC audit trail, Ops Log relationship, agent
action logging, Phase 1 implementation plan

---

## Executive Summary

Axiom generates machine-produced log data across five distinct concerns:
infrastructure telemetry, routing decisions, export-control audit, agent
actions, and system operations records. These categories are currently
conflated or absent. This PRD establishes a clear taxonomy, defines the
minimum viable logging required to safely operate in export-controlled
environments today, and maps a phased path toward a complete audit and
observability stack.

**Axiom works fully without any EC or classified environment configuration.**
Export-control audit logging is an opt-in capability activated only when at
least one provider with `routing_tier = "export_controlled"` is configured.
Deployments with no EC provider — research labs, universities, non-domain-specific
facilities — run with standard structured file logging and have no dependency
on audit tables, HMAC keys, or tamper-evident infrastructure.

**Forensic trace logging is a baseline capability for all deployments.** Every
log record carries a `trace_id` and `session_id` for cross-component correlation.
An in-memory ring buffer captures recent DEBUG context and flushes automatically
to a timestamped snapshot file when any ERROR or CRITICAL event occurs. This
enables incident reconstruction, mitigation, and root cause analysis for any
unexpected failure — agent misbehavior, state corruption, routing anomalies —
without requiring EC infrastructure or special configuration.

For EC deployments, the most urgent need is a **routing audit log** that proves,
for any given request, what was classified, who classified it, which provider
was selected, and where the prompt was sent. Without this, the system cannot be
audited for EC compliance and cannot respond to a potential data exfiltration
incident.

---

## 1. Deployment Modes

Logging behavior scales with the facility's configuration. No mode requires
extra dependencies beyond what is already in use.

| Mode | Triggered By | Logging Behavior |
|---|---|---|
| **Standard** | No EC provider configured | Structured file logging (JSON lines). No audit tables. No HMAC keys. No PostgreSQL dependency for logging. |
| **EC-Audit** | Any provider with `routing_tier = "export_controlled"` present | Routing audit tables in PostgreSQL + HMAC chain. Activated automatically on `Gateway._load_config`. |
| **Full Compliance** | EC-Audit + Identity (Kratos) | Identity-enriched audit records, per-user EC session audit, SIEM export. |

The gateway detects mode at config load time:

```python
self._ec_audit_enabled = any(
    p.routing_tier == "export_controlled" for p in self.providers
)
```

All EC-specific audit code paths are gated on `_ec_audit_enabled`. A facility
with only public and `routing_tier="any"` providers never touches the audit
tables, the HMAC key, or the PostgreSQL audit schema.

---

## 2. Logging Taxonomy

Axiom produces five categories of log data. Each has a different audience,
retention policy, regulatory framework, and tamper-evidence requirement.

| Category | Produced By | Audience | Regulatory Framework | Tamper-Evident? |
|---|---|---|---|---|
| **System Log** | Platform infrastructure | DevOps, facility IT | Internal policy | No (v0.5.x) / Yes (v0.6.x) |
| **Routing Audit Log** | Gateway + Router | Compliance officer, security team | 10 CFR 810 (indirect) | Yes (Phase 1) |
| **EC Session Audit** | Gateway, Session manager | Compliance officer, legal | 10 CFR 810 | Yes (Phase 1) |
| **Agent Action Log** | Agent orchestrator | Facility operators, admins | Internal policy | No |
| **Ops Log** | System operations staff (human-authored) | Shift supervisors, regulatory body, safety reviewers | 10 CFR 50.9, ANSI/ANS-15.1 | Yes (existing PRD) |

### 1.1 System Log

Machine-generated infrastructure telemetry: startup/shutdown events, service
health, connection probe results, configuration changes, dependency errors.
Analogous to syslog or structured application logs. NOT a compliance record.

Uses five standard log levels: **DEBUG** (developer traces), **INFO** (normal
operations), **WARNING** (recoverable anomalies), **ERROR** (non-fatal failures),
**CRITICAL** (system-halting conditions). Active in every deployment mode —
including standard (non-EC) — with no EC or PostgreSQL dependency.

**Primary use:** Debugging, oncall incident response, health dashboards.
TIDY reads the System Log for growth monitoring and post-patch verification;
TRIAGE subscribes to ERROR/CRITICAL events to trigger intervention workflows.

### 1.2 Routing Audit Log

A structured record of every LLM routing decision: the classification result
(who classified it, what tier was assigned), which provider was selected, and
whether the request was sent or blocked. Hashed prompt content — never
plaintext — for correlation without exfiltration.

**Primary use:** EC compliance investigation, post-incident forensics,
demonstrating that EC queries never reached public cloud providers.

### 1.3 EC Session Audit

A session-level record when any export-controlled routing tier is invoked:
session ID, authenticated user (post-Phase 3), provider name, timestamp range,
request count, HMAC chain of hashes. No plaintext prompts or responses.

**Primary use:** Sustained-use pattern analysis, user-level EC access records.

### 1.4 Agent Action Log

Structured record of tool calls made by agents (axi_agent, signals,
hygiene): tool name, parameters (sanitized), result summary, duration,
session ID. Not tamper-evident in Phase 1 — agents are not yet EC-capable.

**Primary use:** Debugging agent behavior, operator trust/oversight.

### 1.5 Ops Log (out of scope — see separate PRD)

The system operations logbook is a human-authored tamper-evident record
governed by 10 CFR 50.9. It is maintained by `prd-system-ops-log.md` and its
technical implementation shares the HMAC-chain pattern with the EC Session
Audit but serves an entirely different regulatory and operational purpose.

**Relationship:** The Ops Log and EC Session Audit share infrastructure
patterns (append-only PostgreSQL tables, HMAC chains) but are completely
separate tables with separate retention policies, access controls, and
regulatory meaning. The System Log MUST NOT commingle entries with the Ops Log.

---

## 2. Export Control Threat Model & Logging Requirements

Each threat vector below identifies what logging evidence is required to detect
or investigate the attack.

### 2.1 Prompt Exfiltration via Misrouting

**Threat:** An EC-classified query is silently sent to a public cloud provider
(Anthropic, OpenAI) instead of the configured EC LLM.

**Detection requirement:** Every routing decision must be logged with provider
name, routing tier requested vs. tier of selected provider, and request hash.
A mismatch (EC requested, public provider selected) must generate an
`EC_ROUTING_VIOLATION` event.

**Current status:** The gateway raises `EC_PROVIDER_NOT_CONFIGURED` and blocks
the call, but this event is not persisted anywhere. An incident cannot be
reconstructed.

### 2.2 Classification Evasion (Adversarial Prompts)

**Threat:** A user crafts a prompt to evade both keyword detection and Ollama
classification, causing EC content to be routed to a public tier.

**Detection requirement:** Log the full classification result: keyword scanner
output, Ollama classifier output, final tier decision, and confidence score
(when available). Log hash of prompt at classification time. This enables
post-hoc review of misclassified prompts.

**Current status:** Classification result is ephemeral — computed in
`QueryRouter.classify()` and discarded after routing.

### 2.3 Session Pivoting (Public → EC Escalation)

**Threat:** A user begins a session with public-tier queries to establish
context, then escalates to EC queries within the same session window.

**Detection requirement:** EC session audit must record ALL requests within a
session, not just the first, and flag sessions where tier changes between PUBLIC
and EXPORT_CONTROLLED.

### 2.4 Unauthorized Provider Configuration

**Threat:** An attacker (or misconfigured deployment) adds a public provider
with higher priority than the EC provider, causing EC traffic to be silently
downgraded.

**Detection requirement:** Log configuration load events including provider
list, priority order, and routing tier assignments. Alert on any configuration
change that alters the EC provider chain.

### 2.5 Log Tampering

**Threat:** After an EC incident, an attacker or insider modifies log entries to
remove evidence of misrouting.

**Detection requirement:** HMAC chain over routing audit entries. Any gap in the
chain is detectable. Read-only access for compliance officers (no delete
permission on audit tables).

---

## 3. Relationship to Ops Log

The Ops Log (`prd-system-ops-log.md`) is a **human-authored operational
record** for facility operations. It records shift events, experiment
starts/stops, equipment status changes, and safety-relevant observations. It is
governed by 10 CFR 50.9 (accuracy requirements for documents submitted to the
regulatory body) and ANSI/ANS-15.1 (research system administration standard).

The System Log is a **machine-generated infrastructure telemetry record**. It
records what the software did — routing decisions, agent tool calls, service
health. It is governed by internal facility policy, not regulatory body regulations.

**Shared patterns:**
- Both use append-only PostgreSQL tables
- Both use HMAC chains for tamper detection (Phase 1 for EC Audit; existing for Ops Log)
- Both expose read-only views for compliance review

**Hard distinctions:**
- Ops Log entries are human-authored; System Log entries are machine-generated
- Ops Log has regulatory body retention requirements (operational lifetime + 5 years); System Log retention is policy-configurable
- Ops Log is NEVER written to by software agents; System Log is ONLY written to by software agents
- Ops Log tables are never mixed with System Log tables in schema or application code

The `axiom log` CLI noun serves System Log queries. The `axiom ops` CLI noun
serves Ops Log queries. These are separate commands backed by separate tables.

---

## 3.5 Forensic Trace Logging (All Deployments)

**Applies to standard, EC-audit, and full-compliance modes equally.**

When unexpected failures occur — agent misbehavior, corrupt state files, routing
loops, inter-process write collisions — operators need enough logged context to
reconstruct what happened, mitigate immediate effects, and prevent recurrence.
This is not an EC capability; it is a baseline operational requirement.

### FR-LOG-TRACE-001: Trace Context on Every Record

Every structured log record MUST carry a `trace_id` (operation-scoped) and
`session_id` (CLI/agent-session-scoped). Both are set via `contextvars` and
populated automatically by the `StructuredJsonFormatter` — individual call sites
do not need to pass these manually.

### FR-LOG-TRACE-002: Forensic Ring Buffer

All deployments MUST maintain an in-memory ring buffer of recent log records at
DEBUG level (default: 2000 records, configurable). The ring buffer costs only
RAM — no I/O until flushed. It enables capture of "what happened immediately
before the failure" without requiring DEBUG logging to be running continuously.

### FR-LOG-TRACE-003: Automatic Incident Snapshot

On any `ERROR` or `CRITICAL` log record, the ring buffer MUST be automatically
flushed to a timestamped JSONL file in `runtime/logs/forensic/`. The snapshot
includes the triggering record, reason string, and all buffered DEBUG records.
A 30-second cooldown prevents snapshot storms during cascading failures.

Forensic snapshots are never auto-deleted by log rotation. TIDY archives them
during monthly review (see §TIDY Stewardship).

### FR-LOG-TRACE-004: `axiom log trace` CLI

```
axiom log trace <trace_id>               # timeline of all records for this trace
axiom log snapshot [--trace <trace_id>]  # manual ring buffer flush
axiom log capture --duration <N>m        # write DEBUG to disk for N minutes
```

`axiom log trace` queries both the System Log and forensic snapshots, merging and
sorting by `ts`. Output is a human-readable timeline for incident reconstruction.

### FR-LOG-TRACE-005: Structured Record Schema

Every log record MUST include: `ts` (ISO 8601 UTC, ms precision), `level`,
`logger` (`__name__`), `trace_id`, `session_id`, `component`, `msg`. Optional
context fields (`provider`, `error`, `duration_ms`) are added at call sites.
See spec §2.4.1 for the canonical schema.

---

## 4. Phase 1: EC Security Blockers (v0.5.x)

Phase 1 addresses the minimum logging required to operate in an EC environment
with confidence. It is scoped to be implementable in a single sprint without
depending on Phase 3 (Identity) or Phase 6 (OpenFGA).

### FR-LOG-001: Routing Decision Record

Every call to `Gateway.complete_with_tools` MUST append a record to the
routing audit log before the HTTP call is made and again when the response
returns (or is blocked).

**Record fields:**

| Field | Type | Notes |
|---|---|---|
| `event_id` | UUID | Generated per decision |
| `session_id` | TEXT | CLI/agent session identifier |
| `timestamp` | TIMESTAMPTZ | UTC |
| `classifier` | TEXT | `"keyword"` / `"ollama"` / `"fallback"` |
| `tier_requested` | TEXT | `"public"` / `"export_controlled"` / `"any"` |
| `tier_assigned` | TEXT | Final tier after classification |
| `provider_name` | TEXT | Selected provider name or `"stub"` |
| `provider_tier` | TEXT | Provider's configured `routing_tier` |
| `blocked` | BOOL | True if EC_PROVIDER_NOT_CONFIGURED or VPN gate |
| `block_reason` | TEXT | Null if not blocked |
| `prompt_hash` | TEXT | SHA-256 of prompt content |
| `response_hash` | TEXT | SHA-256 of response text (null if blocked) |
| `hmac` | TEXT | HMAC-SHA256 over all fields + previous `hmac` |

**EC_ROUTING_VIOLATION:** If `tier_requested = "export_controlled"` AND
`provider_tier != "export_controlled"` AND `blocked = false`, the record MUST
set an `ec_violation = true` flag and the gateway MUST raise an exception. This
state should be unreachable by design but must be auditable if it occurs.

### FR-LOG-002: Classification Event Record

Every call to `QueryRouter.classify()` MUST append a classification record.

**Record fields:**

| Field | Type | Notes |
|---|---|---|
| `event_id` | UUID | |
| `routing_event_id` | UUID | FK to FR-LOG-001 record |
| `timestamp` | TIMESTAMPTZ | UTC |
| `keyword_matched` | BOOL | True if keyword scanner triggered |
| `keyword_term` | TEXT | Matched keyword (null if no match) |
| `ollama_result` | TEXT | Raw Ollama response (null if short-circuited) |
| `sensitivity` | TEXT | `"standard"` / `"strict"` |
| `final_tier` | TEXT | Tier returned to caller |
| `prompt_hash` | TEXT | SHA-256 of prompt |

### FR-LOG-003: Configuration Load Record

On startup (and on config reload), log the active provider configuration.

**Record fields:**

| Field | Type | Notes |
|---|---|---|
| `event_id` | UUID | |
| `timestamp` | TIMESTAMPTZ | UTC |
| `config_file` | TEXT | Path that was loaded |
| `providers_json` | JSONB | Provider list: name, priority, routing_tier, requires_vpn (NO api keys) |
| `ec_providers_count` | INT | Count of providers with `routing_tier = "export_controlled"` |

### FR-LOG-004: VPN Gate Record

When a VPN check is performed (either pass or fail), log the result.

**Record fields:**

| Field | Type | Notes |
|---|---|---|
| `event_id` | UUID | |
| `routing_event_id` | UUID | FK |
| `timestamp` | TIMESTAMPTZ | UTC |
| `provider_name` | TEXT | |
| `vpn_reachable` | BOOL | |
| `check_duration_ms` | INT | |

### FR-LOG-005: Log Integrity Verification Command

```bash
axiom log verify             # verify HMAC chain integrity for all audit tables
axiom log verify --since 7d  # last 7 days
axiom log verify --table routing_events
```

Returns exit code 0 if chain is intact, non-zero if any gap or HMAC mismatch
is detected.

### Out of Scope for Phase 1

The following are intentionally deferred:

- **Response content scanning log** — depends on FR-EC-003 (response scanner)
- **RAG access log** — depends on EC RAG dual-store architecture
- **Agent action log** — agents are not EC-capable yet; standard structured
  logging (Python `logging` module) is sufficient for Phase 1
- **Identity enrichment** — depends on Phase 3 (Ory Kratos); until then, logs
  use `session_id` only, not `user_id`
- **Streaming to external SIEM** — facility policy varies; Phase 2 adds
  configurable sinks (syslog, splunk, elastic)
- **EC session audit (sustained-use pattern)** — Phase 2; requires identity
  to be meaningful

---

## 5. Phase 2: Identity-Enriched Audit + SIEM Integration (v0.6.x)

After Phase 3 (Identity / Ory Kratos) ships:

- Add `user_id` and `username` fields to all audit records
- EC Session Audit table (sustained-use, per-user, HMAC-chained)
- Configurable log sinks: syslog, webhook, Elastic/OpenSearch, Splunk
- `axiom log export --since 30d --format jsonl` for SIEM ingestion
- Retention policy configuration in `llm-providers.toml` or `logging.toml`

---

## 6. Phase 3: Response Content Audit + RAG Access Log (v0.7.x)

After EC RAG dual-store and response scanner ship:

- Log RAG store access: which store (public vs. EC), query hash, document IDs
  returned (no content)
- Log response scanner results: classification of LLM output before delivery
  to user
- Integrate with FR-EC-003 (response scanning) and FR-EC-005 (store quarantine)

---

## 7. Non-Functional Requirements

**All deployments:**
- **System Log uses structured file logging** (JSON lines) with standard
  rotation. No PostgreSQL dependency, no HMAC keys, no special configuration.
  Works out of the box in any environment.
- **Audit tables are not the System Log.** EC audit infrastructure is additive
  and never replaces or depends on general-purpose logging.
- **System Log uses standard Python log levels** (DEBUG / INFO / WARNING / ERROR
  / CRITICAL). Levels are configurable per-logger in `runtime/config/logging.toml`.
  The runtime level is adjustable live without restart (TIDY and TRIAGE use this
  for post-patch verification and incident triage).
- **All components must use the project logger** (`logging.getLogger(__name__)`)
  — not bare `print()` statements — so that level filtering, sink fan-out, and
  structured formatting apply uniformly.

**EC-Audit mode only** (active when `routing_tier = "export_controlled"` provider is configured):
- **No plaintext EC content in any audit table.** All prompt/response content
  MUST be SHA-256 hashed before storage.
- **Audit log writes for EC requests are synchronous and blocking.** An
  export-controlled request is not dispatched until its audit record is
  committed. Audit write failures block the EC request (fail-secure).
  Non-EC requests are not affected — they log asynchronously best-effort.
- **Audit tables are append-only.** No UPDATE or DELETE on audit tables.
- **HMAC key must be set** via `NEUT_AUDIT_HMAC_KEY` or OS Keychain. The
  gateway refuses to process EC requests if no key is configured.

**Standard mode** (no EC provider configured):
- No audit tables, no HMAC keys, no PostgreSQL requirement for logging.
- Routing decisions are logged to the System Log at INFO level (not DEBUG —
  operators and TIDY need to see routing activity without debug noise).
- Failure to write system logs NEVER blocks requests.

---

## 8. Open Questions

1. Should `axiom log verify` run automatically as part of `axiom doctor`?
2. HMAC key rotation: how often, and how do we re-sign existing records?
3. What is the facility-specific retention requirement for routing audit logs?
   (10 CFR 810 does not specify; facility legal counsel should advise.)
4. If the audit DB is unavailable at startup when EC providers are configured,
   should the gateway refuse all requests (fail-closed) or refuse only EC
   requests (partial fail-closed)? Current answer: refuse only EC requests.
   Non-EC providers are unaffected.
5. For Phase 2 SIEM integration, should we support push (webhook/syslog) only,
   or also pull (query API)?

---

## 9. Storage Backend & PostgreSQL

Axiom prefers PostgreSQL for all persistent data. The logging system
uses a **flat-file fallback** so it works immediately without PostgreSQL,
then promotes to PostgreSQL automatically when it becomes available.

| Scenario | Audit backend | Behavior |
|---|---|---|
| PostgreSQL connected | PostgreSQL tables | Full capability |
| PostgreSQL down / not installed | JSONL flat-file (`runtime/logs/audit/`) | Automatic; multi-process safe (file locking); promotes to PostgreSQL on reconnect |
| Standard mode (no EC provider) | None (no-op) | Zero overhead, no dependency |

**PostgreSQL is preferred everywhere** and users are guided through installation
during `axiom setup` and reminded on each `axiom config` session if it is not
connected. Flat-file mode is a graceful fallback, not a supported steady state.

During `axiom config`, if PostgreSQL is unavailable:
```
⚠  PostgreSQL is not connected. Axiom is running with reduced capability:
   • Audit log → flat-file fallback (runtime/logs/audit/)
   • RAG store → unavailable
   Run 'axiom setup postgres' to install and configure PostgreSQL.
```

See [Logging Spec §3](../tech-specs/spec-logging.md) for backend selection
logic, multi-process file locking, and automatic promotion design.

## 10. Log Lifecycle: TIDY and TRIAGE

**TIDY** (the resource steward agent) owns log growth, rotation, and archival:
- Daily system log rotation (50MB limit, 7-day window)
- Weekly JSONL size checks and promoted-file cleanup
- Monthly audit table archival per retention policy
- Surfaces log health on `axiom status`

**TRIAGE** (the diagnostics agent) responds to log health events:
- `logs.hmac_chain_broken` → alert + disable EC writes until resolved
- `logs.audit_db_unavailable` → confirm flat-file fallback + setup guidance
- `security.ec_violation` → immediate alert + halt EC provider
- `logs.promotion_stalled` → diagnose PostgreSQL + re-run promotion

See [Agent Architecture Spec — TIDY Corpus Stewardship](../tech-specs/spec-agent-architecture.md)
for the TIDY task pattern.

## 11. Dependencies

| Dependency | Required For | When Available |
|---|---|---|
| PostgreSQL | Preferred audit backend; flat-file fallback used when unavailable | Now (existing); `axiom setup postgres` for guided install |
| `portalocker` | Multi-process-safe flat-file writes | Python package; add to `[project.dependencies]` |
| Ory Kratos | Identity enrichment in audit records (Phase 2) | v0.5.x |
| OS Keychain | HMAC key secure storage (Phase 2) | v0.5.x |
| EC RAG dual-store | RAG access log (Phase 3) | v0.7.x |
| Response scanner (FR-EC-003) | Response content audit (Phase 3) | v0.7.x |

---

## 12. Relation to Security PRD

`prd-security.md` FR-EC-006 ("Security Audit Log") is superseded by this PRD.
The Security PRD should be updated to reference this document for all logging
requirements. The phased schedule in the Security PRD (Phase 5, v0.6.x) is
replaced by the Phase 1 schedule here (v0.5.x) because the routing audit log
is a prerequisite for confident EC operations, not a future enhancement.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
