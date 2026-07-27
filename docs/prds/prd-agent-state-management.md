# Axiom Agent State Management PRD

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-02-24
**Last Updated:** 2026-03-14

---

## Executive Summary

Axiom agents accumulate state across 15+ filesystem locations—transcripts, corrections, session history, configuration, document registries, and learned preferences. As the platform evolves toward parallel agent execution and community-shared knowledge, three critical problems emerge:

1. **Concurrent access safety** — Multiple agents writing to the same JSON state files (`.publisher-registry.json`, `briefing_state.json`, `user_glossary.json`) will corrupt data without proper coordination.
2. **Verifiable data retention** — A operations facility platform must ship with auditable, enforceable retention policies. Ad-hoc `CLIP_RETENTION_DAYS` constants and `expires_at` fields are insufficient.
3. **Shared state for multi-user environments** — Community RAG corpora, facility configuration, and team glossaries require scope-aware state management that respects access tiers.

These are not new CLI features. They are **infrastructure concerns** that TIDY (the resource steward agent) should manage behind the scenes, with retention policies enforced automatically and concurrency handled transparently by a shared state access layer.

---

## Problem Statement

### The Concurrency Problem (Critical)

Today, Axiom agents operate sequentially. But the architecture is moving toward:
- Parallel extractors in the signal pipeline (voice + GitLab + Teams running concurrently)
- TIDY sweeping scratch files while other agents are writing
- Publisher syncing document state while sense is updating signal references
- Chat agent reading session state while sense is appending signals

Every shared JSON file is a race condition:

| File | Writers | Failure Mode |
|------|---------|-------------|
| `.publisher-registry.json` | Publisher, Signal | Lost document mappings |
| `.publisher-state.json` | Publisher, Signal | Corrupt lifecycle state |
| `runtime/inbox/state/briefing_state.json` | Signal, Chat | Lost briefing progress |
| `runtime/inbox/corrections/user_glossary.json` | Signal, Review | Lost corrections |
| `runtime/inbox/corrections/propagation_queue.json` | Signal, Review | Duplicate or lost propagations |
| `runtime/sessions/*.json` | Chat, Signal | Corrupt session history |

TIDY's manifest already solves this for scratch files with `fcntl.flock`. That pattern must be generalized.

### The Retention Problem (Must-Ship)

A platform for operations facilities cannot accumulate unbounded data with no lifecycle management. Current state:

| What Exists | Where | Behavior |
|-------------|-------|----------|
| Audio clip retention | `correction_review_guided.py` | `CLIP_RETENTION_DAYS = 7` (hardcoded) |
| Echo suppression expiry | `echo_suppression.py` | `expires_at` field, `cleanup_expired()` |
| Publisher sync cache | `docflow_providers/` | `expires_at` in cache records |

These are isolated, inconsistent, and unauditable. We need:
- A single retention policy configuration
- TIDY-enforced cleanup with audit logging
- Legal hold capability
- `axiom tidy retention` visibility (not a new noun—TIDY already owns this)

### The Shared State Problem (Phase 2)

The RAG architecture already defines three scopes: `community`, `facility`, `personal`. As Axiom moves toward multi-user deployment:
- Community RAG corpus updates need coordination across users
- Facility config (`people.md`, `initiatives.md`) must sync without conflicts
- Personal corrections may need to merge into facility glossaries

This is a data architecture concern, not a standalone state management system.

---

## Goals & Non-Goals

### Goals

1. **Safe concurrent state access** — No data corruption when multiple agents or processes access shared JSON files
2. **Verifiable data retention** — Auditable, configurable retention policies enforced by TIDY
3. **State inventory visibility** — TIDY can report what state exists, where, and how old it is
4. **Unified retention configuration** — Single `retention.yaml` replaces scattered constants
5. **Audit trail for deletions** — Every retention action logged for compliance

### Non-Goals

- New `axiom state` CLI noun (TIDY handles this)
- Backup/restore tooling (use standard tools: `tar`, Time Machine, `rsync`)
- Git-crypt integration (premature; revisit when multi-user is real)
- Enterprise RBAC, SAML, PostgreSQL state sync (way too early)
- Encryption at rest (macOS FileVault / Linux LUKS already handle this)
- Secrets management (defer to Vault/SOPS/1Password)

---

## User Stories

### Concurrent Access

**US-001**: As a developer running parallel sense extractors, I expect state files to remain consistent even when multiple processes write concurrently.

**US-002**: As an agent developer, I want a simple API to read/write shared state files safely without implementing my own locking.

### Retention

**US-010**: As a facility operator, I need verifiable data retention policies so I can demonstrate compliance during audits.

**US-011**: As a developer, I want old transcripts and voice memos cleaned up automatically so my disk doesn't fill up.

**US-012**: As a compliance officer, I need an audit log of what was deleted and when.

**US-013**: As legal counsel, I need a "legal hold" flag that suspends all automated deletion.

### Visibility

**US-020**: As a developer, I want to see what agent state exists on my machine and how much space it uses.

**US-021**: As TIDY, I want to report state health as part of my vitals monitoring.

---

## Architecture

### Principle: TIDY Is the State Steward

TIDY already manages scratch files, disk usage, and resource lifecycle. State management is a natural extension of TIDY's mandate—not a separate system.

```
┌─────────────────────────────────────────────────────┐
│                    TIDY Agent                         │
│                                                     │
│  Existing:              New:                        │
│  • Scratch management   • Retention enforcement     │
│  • Disk monitoring      • State inventory           │
│  • Orphan cleanup       • Retention audit logging   │
│  • Periodic sweep       • State health vitals       │
│                                                     │
└──────────────────────┬──────────────────────────────┘
                       │ uses
                       ▼
┌─────────────────────────────────────────────────────┐
│           Hybrid State Store                         │
│         (axiom.infra.state)              │
│                                                     │
│  StateBackend protocol → get_state_store()          │
│                                                     │
│  ┌──────────────────┐  ┌────────────────────────┐   │
│  │ FileStateBackend │  │ PgStateBackend         │   │
│  │ (offline/boot)   │  │ (online/shared)        │   │
│  │ • LockedJsonFile │  │ • ACID transactions    │   │
│  │ • atomic_write   │  │ • row-level locking    │   │
│  │ • zero deps      │  │ • audit trail          │   │
│  └──────────────────┘  └────────────────────────┘   │
│                                                     │
│  StateRegistry — catalog of all state locations     │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### Principle: No New CLI Nouns

State management surfaces through existing commands:

```bash
# Retention visibility (TIDY already has a CLI noun)
axiom tidy retention [--status] [--dry-run]
axiom tidy cleanup [--category <cat>] [--dry-run]

# State inventory is a TIDY vitals concern
axiom tidy vitals --include-state
```

---

## Functional Requirements

### FR-001: Safe State Access Layer

**Location:** `src/axiom/infra/state.py`

A shared module that any agent or extension imports for safe JSON file I/O:

```python
from axiom.infra.state import LockedJsonFile

# Read with shared lock
with LockedJsonFile("runtime/inbox/state/briefing_state.json") as state:
    data = state.read()

# Read-modify-write with exclusive lock
with LockedJsonFile(".publisher-registry.json", exclusive=True) as state:
    data = state.read()
    data["documents"].append(new_doc)
    state.write(data)
```

Requirements:
- **Advisory file locking** via `fcntl.flock` (Unix) with Windows no-op fallback
- **Atomic writes** via write-to-tempfile + `os.rename` (prevents partial writes on crash)
- **Shared locks for reads**, exclusive locks for writes
- **Timeout** parameter to prevent deadlocks (default 5 seconds)
- **Extracted from TIDY's existing `manifest.py`** `_LockedFile` pattern — generalized, not duplicated

### FR-002: State Location Registry

**Location:** `src/axiom/infra/state.py`

A declarative catalog of all known state locations with metadata:

```python
@dataclass
class StateLocation:
    path: str                    # Relative to project root
    category: str                # "runtime" | "config" | "documents" | "corrections" | "sessions"
    description: str
    sensitivity: str             # "low" | "medium" | "high" | "critical"
    retention_key: str | None    # Key in retention.yaml, or None for indefinite
    glob_pattern: str = "*"      # For directories
```

This is a **data declaration**, not a service. Used by:
- TIDY for inventory and retention enforcement
- Any future backup tooling
- Doctor agent for diagnostics

### FR-003: Unified Retention Configuration

**Location:** `runtime/config/retention.yaml`

```yaml
retention:
  raw_voice:
    days: 7
    after: processed       # Countdown starts when file is marked processed

  raw_signals:
    days: 30
    after: ingested

  transcripts:
    days: 90
    after: created

  sessions:
    days: 30
    after: last_accessed

  drafts:
    days: 14
    after: created

# Legal hold suspends ALL automated deletion
legal_hold:
  enabled: false

# Audit logging
audit:
  log_deletions: true
  log_path: runtime/logs/retention_audit.jsonl
```

Ships with sensible defaults. Facility operators can override.

### FR-004: Retention Enforcement (TIDY Integration)

TIDY's periodic sweep (already runs every 300s) gains retention awareness:

1. Load `retention.yaml` policies
2. Scan state locations matching retention keys
3. Identify files past retention cutoff
4. If `legal_hold.enabled`, skip all deletion
5. Delete expired files
6. Log each deletion to `retention_audit.jsonl`

### FR-005: Retention Audit Log

All retention actions logged in JSONL format:

```json
{"timestamp": "2026-03-14T14:30:00Z", "action": "delete", "path": "runtime/inbox/raw/voice/memo_123.m4a", "reason": "retention_policy", "policy": "raw_voice", "age_days": 8}
{"timestamp": "2026-03-14T14:30:00Z", "action": "skip", "path": "runtime/inbox/processed/meeting.json", "reason": "legal_hold"}
```

### FR-006: Retention Status Command

```bash
axiom tidy retention --status

Retention Policy Status
═══════════════════════
Category: raw_voice (7 days after processed)
  • 2 files past retention (45 MB recoverable)

Category: sessions (30 days after last_accessed)
  • 5 sessions past retention (12 MB recoverable)

Total: 57 MB recoverable
Legal hold: OFF

axiom tidy retention --dry-run

Would delete:
  • runtime/inbox/raw/voice/memo_2026-03-05.m4a (9 days old)
  • runtime/inbox/raw/voice/memo_2026-03-06.m4a (8 days old)
  ...
```

### FR-007: State Inventory (TIDY Vitals)

TIDY's vitals reporting includes state health:

```bash
axiom tidy vitals --include-state

State Inventory
═══════════════
Category: config (3 files, 2 KB)
  ✓ runtime/config/people.md         1.2 KB
  ✓ runtime/config/initiatives.md    0.5 KB
  ✓ runtime/config/models.toml      0.3 KB

Category: runtime (28 files, 1.2 MB)
  ✓ runtime/inbox/processed/        1.1 MB (23 files)
  ✓ runtime/inbox/state/            45 KB (3 files)

Total: 15 locations, 1.4 MB
Retention compliance: 3 categories overdue
```

---

## Shared State Considerations (Phase 2)

When Axiom moves to multi-user deployment, the concurrency problem extends beyond a single machine. These concerns are documented here for future reference but are **not in scope for Phase 0/1**.

### Community RAG Corpus

The RAG architecture defines `rag-community` as a shared corpus. Updates to community knowledge require:
- Write coordination (only admins publish to community corpus)
- Version tracking (what changed, when, by whom)
- This is a **data platform concern** handled by PostgreSQL + pgvector, not filesystem locking

### Facility Configuration Sync

`people.md` and `initiatives.md` are facility-wide. Multi-user sync options:
- Git-based (commit/push/pull) — simple, proven, familiar
- Database-backed — heavier but avoids merge conflicts
- Decision deferred until multi-user deployment is real

### Team Glossary Merging

Personal corrections (`user_glossary.json`) that prove universally useful could merge into a facility glossary. This is a **workflow decision**, not a state management feature.

---

## Existing Retention Mechanisms to Unify

These isolated implementations should be refactored to use the unified retention config:

| Component | Current Location | Current Behavior | Migration |
|-----------|-----------------|-----------------|-----------|
| Audio clips | `correction_review_guided.py` | `CLIP_RETENTION_DAYS = 7` | Read from `retention.yaml` |
| Echo suppression | `echo_suppression.py` | `expires_at`, `cleanup_expired()` | Keep (self-managing cache, already correct) |
| Publisher sync cache | `docflow_providers/` | `expires_at` in records | Keep (provider-managed, already correct) |

Note: Components that already self-manage expiration (echo suppression, provider caches) don't need to change. Only hardcoded retention constants should migrate to `retention.yaml`.

---

## Phased Implementation

### Phase 0: Concurrent Access Safety ✅ COMPLETE (2026-03-14)

**Deliverables:**
- [x] Extract `_LockedFile` from TIDY's `manifest.py` → `src/axiom/infra/state.py`
- [x] `LockedJsonFile` with read/write/read-modify-write patterns
- [x] `atomic_write` and `locked_read` convenience functions
- [x] Lock sidecar files (`.json.lock`) to avoid interference with atomic replace
- [x] `StateLocation` registry dataclass with 17 known locations
- [x] Migrate `.publisher-registry.json` and `.publisher-state.json` to `LockedJsonFile`
- [x] Migrate TIDY `manifest.py` to import from shared module

**Test Results (48 tests, 0 failures):**
- 11 basic read/write tests
- 4 atomic write safety tests (crash recovery, no temp file leaks)
- 3 concurrent access tests (4-process increment, shared reads, validity)
- 5 StateLocation registry validation tests
- 22 retention tests (see Phase 1)
- All 237 existing TIDY + publisher tests pass with refactored code

**Key test: `test_concurrent_increments_no_lost_updates`** — 4 processes each increment a shared counter 50 times. Final count must equal 200 exactly. With `LockedJsonFile`: passes. Without locking: would lose ~30-40% of updates.

### Phase 1: Retention Policies ✅ COMPLETE (2026-03-14)

**Deliverables:**
- [x] `runtime/config.example/retention.yaml` with sensible defaults
- [x] `hygiene/retention.py` — policy engine (load, scan, execute, status)
- [x] Retention enforcement integrated into TIDY's `sweep()` method
- [x] `axiom tidy retention` CLI subcommand (status, --dry-run, --cleanup)
- [x] JSONL audit logging for all retention actions
- [x] Legal hold flag support (suspends all automated deletion)
- [ ] Migrate `CLIP_RETENTION_DAYS` to read from config (deferred — low risk)

**Test Results (22 retention tests, 0 failures):**
- 5 config loading tests (file, fallback, missing, legal hold, field parsing)
- 7 scan tests (expired, recent, legal hold, multi-category, glob filtering)
- 6 execution tests (delete, dry-run, skip, audit log, append, error handling)
- 2 status report tests
- 2 TIDY sweep integration tests

### Phase 1.5: Hybrid State Backend ✅ COMPLETE (2026-03-14)

**Deliverables:**
- [x] `StateBackend` and `StateHandle` protocols (`axiom.infra.state`)
- [x] `FileStateBackend` — wraps `LockedJsonFile` through unified interface
- [x] `PgStateBackend` — wraps `PgStateStore` through unified interface
- [x] `HybridStateStore` — automatic backend selection with fallback
- [x] `get_state_store()` singleton for project-wide access
- [x] Backend selection via `NEUTRON_STATE_BACKEND` env var (`file` | `postgresql`)
- [x] Auto-detection: if `NEUTRON_STATE_DSN` or `DATABASE_URL` set, tries PostgreSQL, falls back to file

**Test Results (16 hybrid tests, 0 failures):**
- 7 FileStateBackend tests (protocol compliance, CRUD, concurrent safety)
- 5 backend selection tests (default, forced, env var, fallback)
- 4 operations + singleton tests

**Architectural decision:** `LockedJsonFile` with `fcntl.flock` is a transitional layer suitable for single-developer, single-machine use. It is strictly better than naked `path.write_text()` but has known limitations:

| Property | fcntl.flock (flat file) | PostgreSQL |
|----------|----------------------|------------|
| Advisory vs mandatory | Advisory only — non-participating code can corrupt | Mandatory — ACID enforced |
| Multi-machine | No | Yes |
| Transaction isolation | None (last-write-wins) | SERIALIZABLE available |
| Conflict detection | None | Row-level locking, deadlock detection |
| Audit trail | Manual JSONL append | Built into query layer |
| Crash recovery | Atomic write prevents partial, but no rollback | Full WAL recovery |
| Performance under contention | Degrades linearly | Connection pooling, concurrent readers |

**Decision:** Build a PostgreSQL-backed `StateStore` that implements the same read/write interface as `LockedJsonFile`. This enables transparent backend swapping. See [whitepaper](../research/whitepaper-state-backend-comparison.md) for measurable comparison.

**Axiom already requires PostgreSQL** in the tech stack — this is not a new dependency.

### Phase 2: Shared Session Store — IN PROGRESS

**Status:** Active development (triggered by external researcher deployment — first multi-client scenario).

**Deliverables:**
- PostgreSQL-backed session store (`sessions` + `session_messages` tables) — see [spec-session-store.md](../tech-specs/spec-session-store.md)
- Multi-client session access: start on laptop, resume on mobile or different machine
- Session delegation: background agents (SCAN, TIDY) can continue human-started sessions
- PG `LISTEN/NOTIFY` for live sync between clients watching the same session
- Graceful degradation: fall back to JSON files when PG is unreachable, sync on reconnect
- Interaction log as a view over session data (OKR O7)
- Per-session cost tracking and token budget enforcement
- `axi chat --resume`, `--session <id>`, `--list` CLI flags

**Trigger:** First deployment with >1 concurrent user (**met:** a self-hosted node + an external researcher's laptop, April 2026)

**Remaining Phase 2 work (not yet started):**
- Git-based config sync for `people.md`, `initiatives.md`
- Community RAG corpus write coordination
- Facility glossary merge workflow

---

## Data Retention Policy Defaults

| Data Category | Default Retention | Rationale |
|---------------|-------------------|-----------|
| Raw voice memos (`inbox/raw/voice/`) | 7 days after processed | Large files, transcript is the artifact |
| Raw signal sources (`inbox/raw/{gitlab,teams}/`) | 30 days | Source of truth for processed signals |
| Processed transcripts (`inbox/processed/`) | 90 days | Reference for corrections, briefings |
| Sessions (`sessions/`) | 30 days after last accessed | Chat history, can regenerate |
| Drafts (`drafts/`) | 14 days | Regeneratable, ephemeral |
| Corrections/glossary (`corrections/`) | Indefinite | Valuable learned preferences, small |
| Configuration (`config/`) | Indefinite | Critical operational data |

---

## Security Considerations

- **Encryption at rest**: Handled by OS-level full-disk encryption (FileVault, LUKS). Not Axiom's job.
- **Secrets**: Excluded from any state management. Always re-provisioned. `.env` stays in `.gitignore`.
- **Audit trail**: Retention log is append-only JSONL. Tampering detection is a Phase 2 concern.
- **Legal hold**: Binary flag in `retention.yaml`. When enabled, no automated deletion occurs.

---

## Success Metrics

| Metric | Phase 0 Target | Phase 1 Target |
|--------|----------------|----------------|
| Concurrent write safety | All shared JSON files | All shared JSON files |
| Data corruption incidents | Zero | Zero |
| Retention policy coverage | N/A | 100% of data categories |
| Retention compliance | N/A | All categories within policy |
| Audit log coverage | N/A | 100% of automated deletions logged |
| Disk usage growth | Unbounded | < 10% monthly |

---

## Open Questions

1. ~~Should TIDY's retention sweep run on its existing 300s timer, or should retention be a separate daily cron?~~ → Integrated into TIDY sweep; timer frequency TBD for production.
2. How do we handle retention for data that spans categories (e.g., a transcript with embedded corrections)?
3. What notification UX when cleanup frees significant space (e.g., "Freed 500MB")?
4. ~~Should `LockedJsonFile` support a callback-based API in addition to context manager?~~ → Context manager is sufficient; callback adds complexity without benefit.
5. When should PostgreSQL state backend become the default? At first multi-user deployment, or sooner for single-user reliability?

---

## Related Documents

- [Axiom Executive PRD](prd-executive.md)
- [Intelligence Amplification Research](../research/intelligence-amplification.md)
- [Agent State Management Tech Spec](../tech-specs/spec-agent-state-management.md)
- [RAG Architecture Spec](../tech-specs/spec-rag-architecture.md)
- [Data Architecture Spec](../tech-specs/spec-data-architecture.md)
- [TIDY Agent Extension](../../src/axiom/extensions/builtins/hygiene/)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
