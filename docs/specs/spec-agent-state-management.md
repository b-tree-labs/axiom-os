# Axiom Agent State Management — Technical Specification

**Status:** Draft (scope clarified to operational state as of 2026-04-26; cognitive state is `spec-memory.md`'s domain)
**Owner:** Ben Booth
**Created:** 2026-02-24
**Last Updated:** 2026-04-26
**PRD Reference:** [agent-state-management PRD](../requirements/prd-agent-state-management.md)
**Authority:** This spec governs **operational state only** — cursor positions, modal state, autosave drafts, session presence, lock files, in-flight rate-limit counters. **Cognitive state** (anything memorable per spec-memory.md §1) routes through `MemoryStore` per `spec-memory.md`. On any conflict over an ambiguous write, spec-memory wins.
**Related:** `spec-memory.md` (authoritative for cognitive state — events, projections, federation, retraction), `prd-memory.md`

---

## 0. Reconciliation with spec-memory.md (added 2026-04-26)

`spec-memory.md` introduces a `MemoryStore` / `EphemeralStore` distinction (spec-memory §9) to draw a hard line between cognitive memory (memorable, projectable, federable, retractable, classified) and operational state (UI cursors, transient caches, presence flags). This spec governs the latter.

Concretely:

- **Cognitive writes route through `CompositionService`** — anything an extension wants projected into a brief, federated to a peer, or audited as an agent decision.
- **Operational writes stay here** — anything that's truly transient, that can always be rebuilt from L1 events, that has no provenance obligation. Examples: which tab the user has open, the autosave-every-3-seconds buffer, the last-active timestamp, file locks for concurrent agent processes.
- **The test for which is which:** "Can I delete this stored data and rebuild it from L1 events?" If yes, it's operational state and stays here. If no, it's memory and must go through spec-memory.
- The new `EphemeralStore` protocol (spec-memory §9) is the recommended new home for genuinely-transient state going forward; existing flat-file users in `axiom.infra.state` are grandfathered until ADR-033 Stage 4 lands the migration helper.
- The retention policy machinery here continues to govern operational scope; `MemoryStore` has its own retention via MIRIX `RetentionTier` (ACTIVE / COMPRESSED / ARCHIVED).

When extension authors face the question "should this go in `axiom.infra.state` or `axiom.memory`?" — the spec-memory test above is the answer; spec-memory.md §9 is the long-form guide; `axi ext lint` catches uncertain cases.

---

## Overview

This specification defines three infrastructure capabilities for Axiom agent state:

1. **Safe concurrent access** — A shared module (`axiom.infra.state`) that provides locked, atomic JSON file I/O for any agent or extension.
2. **Hybrid state backend** — A unified `StateBackend` protocol (`axiom.infra.state`) that routes to flat files or PostgreSQL with automatic fallback.
3. **Retention enforcement** — TIDY integration for configurable, auditable data lifecycle management.

---

## Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        TIDY Agent                                 │
│  ┌───────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ Scratch Mgmt  │  │ Retention Sweep  │  │ State Vitals     │  │
│  │ (existing)    │  │ (new)            │  │ (new)            │  │
│  └───────┬───────┘  └────────┬─────────┘  └────────┬─────────┘  │
└──────────┼───────────────────┼──────────────────────┼────────────┘
           │                   │                      │
           ▼                   ▼                      ▼
┌─────────────────────────────────────────────────────────────────┐
│              axiom.infra.state                        │
│                                                                  │
│  StateBackend protocol    HybridStateStore                       │
│  get_state_store()        auto-detect backend                    │
│                                                                  │
│  ┌────────────────────┐   ┌────────────────────────────────┐     │
│  │ FileStateBackend   │   │ PgStateBackend                 │     │
│  │ (LockedJsonFile)   │   │ (PgStateStore / ACID)          │     │
│  └────────────────────┘   └────────────────────────────────┘     │
│                                                                  │
│  StateRegistry: STATE_LOCATIONS, RetentionPolicy                 │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Module Structure

```
src/axiom/infra/
├── state.py                # LockedJsonFile, hybrid store, protocols, StateLocation registry
├── state_pg.py             # PostgreSQL backend (PgStateStore)
└── ...

src/axiom/extensions/builtins/hygiene/
├── manager.py              # Add retention sweep to existing sweep cycle
├── manifest.py             # Refactor: import LockedFile from infra.state
├── retention.py            # Retention policy engine (NEW)
├── cli.py                  # Add `axiom tidy retention` subcommand (EXTEND)
└── ...

runtime/config.example/
├── retention.yaml          # Default retention policies (NEW)
└── ...
```

---

## Safe Concurrent Access Layer

### Design

The core insight: TIDY's `manifest.py` already has a working `_LockedFile` implementation. We extract and generalize it into `axiom.infra.state` so all agents can use it.

### `LockedJsonFile`

```python
# src/axiom/infra/state.py

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LockedJsonFile:
    """Safe concurrent JSON file access with advisory locking.

    Uses fcntl.flock on Unix for process-level coordination.
    Writes are atomic (tempfile + rename) to prevent corruption on crash.

    Usage:
        # Read-only (shared lock)
        with LockedJsonFile(path) as f:
            data = f.read()

        # Read-modify-write (exclusive lock)
        with LockedJsonFile(path, exclusive=True) as f:
            data = f.read()
            data["key"] = "value"
            f.write(data)
    """

    def __init__(self, path: str | Path, *, exclusive: bool = False, timeout: float = 5.0):
        self._path = Path(path)
        self._exclusive = exclusive
        self._timeout = timeout
        self._fd: int | None = None
        self._data: Any = None

    def __enter__(self) -> LockedJsonFile:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Open or create the file
        flags = os.O_RDWR | os.O_CREAT if self._exclusive else os.O_RDONLY | os.O_CREAT
        self._fd = os.open(str(self._path), flags, 0o644)
        self._acquire_lock()
        return self

    def __exit__(self, *exc) -> bool:
        if self._fd is not None:
            self._release_lock()
            os.close(self._fd)
            self._fd = None
        return False

    def read(self) -> Any:
        """Read and parse JSON content. Returns empty dict if file is empty."""
        if self._fd is None:
            raise RuntimeError("Must be used as context manager")
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip():
                return {}
            return json.loads(content)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def write(self, data: Any) -> None:
        """Atomically write JSON data (tempfile + rename)."""
        if self._fd is None or not self._exclusive:
            raise RuntimeError("write() requires exclusive=True in context manager")
        # Write to temp file in same directory (ensures same filesystem for rename)
        dir_path = self._path.parent
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(self._path))
        except BaseException:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _acquire_lock(self) -> None:
        if sys.platform == "win32" or self._fd is None:
            return
        try:
            import fcntl
            lock_type = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
            fcntl.flock(self._fd, lock_type)
        except (ImportError, OSError):
            pass

    def _release_lock(self) -> None:
        if sys.platform == "win32" or self._fd is None:
            return
        try:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
```

### `atomic_write` Convenience Function

```python
def atomic_write(path: str | Path, data: Any) -> None:
    """Atomically write JSON data to a file with exclusive locking."""
    with LockedJsonFile(path, exclusive=True) as f:
        f.write(data)
```

### Refactoring TIDY's Manifest

TIDY's `manifest.py` should be refactored to import from `axiom.infra.state`:

```python
# Before (manifest.py)
class _LockedFile: ...  # 35 lines of locking logic

# After (manifest.py)
from axiom.infra.state import LockedJsonFile

class Manifest:
    def _load(self) -> None:
        with LockedJsonFile(self._path) as f:
            data = f.read()
        # ... parse entries

    def _save(self) -> None:
        with LockedJsonFile(self._path, exclusive=True) as f:
            f.write([e.to_dict() for e in self._entries.values()])
```

### Migration Path for Existing State Files

Files that currently use raw `json.load`/`json.dump`:

| File | Current Access | Migration |
|------|---------------|-----------|
| `.publisher-registry.json` | `json.load(open(...))` | `LockedJsonFile` |
| `.publisher-state.json` | `json.load(open(...))` | `LockedJsonFile` |
| `briefing_state.json` | `json.load(open(...))` | `LockedJsonFile` |
| `review_state.json` | `json.load(open(...))` | `LockedJsonFile` |
| `user_glossary.json` | `json.load(open(...))` | `LockedJsonFile` |
| `propagation_queue.json` | `json.load(open(...))` | `LockedJsonFile` |
| `.tidy-manifest.json` | Custom `_LockedFile` | `LockedJsonFile` (already locked) |

Priority: Publisher state files first (most likely to see concurrent access), then signal pipeline state.

---

## Hybrid State Backend

### Protocols

```python
# src/axiom/infra/state_hybrid.py

@runtime_checkable
class StateHandle(Protocol):
    """Handle to a single state document within a transaction/lock."""
    def read(self) -> Any: ...
    def write(self, data: Any) -> None: ...

@runtime_checkable
class StateBackend(Protocol):
    """Backend that can open state documents for read/write."""
    @contextmanager
    def open(self, path: str, *, exclusive: bool = False) -> Generator[StateHandle]: ...
    def read(self, path: str) -> Any: ...
    def write(self, path: str, data: Any) -> None: ...
    @property
    def name(self) -> str: ...
```

### Backend Selection

`HybridStateStore` resolves the backend once at initialization:

1. `NEUTRON_STATE_BACKEND=file` → always flat files
2. `NEUTRON_STATE_BACKEND=postgresql` → always PostgreSQL (fails if unavailable)
3. `NEUTRON_STATE_DSN` or `DATABASE_URL` set → try PostgreSQL, fall back to file
4. Nothing set → flat files (default)

### Usage

```python
from axiom.infra.state import get_state_store

store = get_state_store()

# Read (backend-agnostic)
data = store.read("runtime/inbox/state/briefing_state.json")

# Read-modify-write (backend-agnostic)
with store.open("runtime/inbox/state/briefing_state.json", exclusive=True) as h:
    data = h.read()
    data["counter"] += 1
    h.write(data)

# Check which backend is active
print(store.backend_name)  # "file" or "postgresql"
```

### PostgreSQL Schema

```sql
CREATE TABLE IF NOT EXISTS agent_state (
    path         TEXT PRIMARY KEY,
    data         JSONB NOT NULL DEFAULT '{}',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by   TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS state_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    path         TEXT NOT NULL,
    action       TEXT NOT NULL,
    old_version  INTEGER,
    new_version  INTEGER,
    actor        TEXT NOT NULL DEFAULT '',
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details      JSONB
);
```

Key features:
- **Optimistic concurrency** — `version` column incremented on write; raises `ConcurrentModificationError` on conflict
- **Row-level locking** — `SELECT ... FOR UPDATE` (exclusive) / `FOR SHARE` (shared)
- **Transactional audit** — audit log entry written in same transaction as state change

---

## State Location Registry

### Declaration

```python
# src/axiom/infra/state.py

@dataclass(frozen=True)
class StateLocation:
    """Metadata about a known state storage location."""
    path: str                          # Relative to project root
    category: str                      # runtime | config | documents | corrections | sessions
    description: str
    sensitivity: str                   # low | medium | high | critical
    retention_key: str | None = None   # Key in retention.yaml, or None = indefinite
    glob_pattern: str = "*"            # For scanning directories


STATE_LOCATIONS: list[StateLocation] = [
    # Runtime — ephemeral, has retention
    StateLocation("runtime/inbox/raw/voice",     "runtime",     "Voice memo audio files",         "medium",   "raw_voice",     "*.m4a"),
    StateLocation("runtime/inbox/raw/gitlab",    "runtime",     "GitLab export JSON files",       "low",      "raw_signals",   "*.json"),
    StateLocation("runtime/inbox/raw/teams",     "runtime",     "Teams transcript files",         "high",     "raw_signals",   "*.json"),
    StateLocation("runtime/inbox/processed",     "runtime",     "Processed transcripts/signals",  "high",     "transcripts",   "*"),
    StateLocation("runtime/inbox/state",         "runtime",     "Briefing and sync state",        "medium",   None),

    # Configuration — indefinite, critical
    StateLocation("runtime/config/people.md",       "config", "Team roster with aliases",    "medium"),
    StateLocation("runtime/config/initiatives.md",  "config", "Active initiatives list",     "low"),
    StateLocation("runtime/config/models.toml",     "config", "LLM endpoint configuration", "low"),

    # Documents — publisher lifecycle
    StateLocation(".publisher-registry.json", "documents", "Published doc URL mappings",   "medium"),
    StateLocation(".publisher-state.json",    "documents", "Document lifecycle state",     "medium"),
    StateLocation("runtime/drafts",           "documents", "Generated drafts",            "low",    "drafts",  "*.md"),

    # Corrections — learned preferences
    StateLocation("runtime/inbox/corrections/review_state.json",      "corrections", "Review progress",       "low"),
    StateLocation("runtime/inbox/corrections/user_glossary.json",     "corrections", "Learned corrections",   "low"),
    StateLocation("runtime/inbox/corrections/propagation_queue.json", "corrections", "Pending propagations",  "low"),

    # Sessions
    StateLocation("runtime/sessions", "sessions", "Chat session history", "high", "sessions", "*.json"),
]
```

---

## Retention Policy Engine

### Configuration Schema

```yaml
# runtime/config/retention.yaml
retention:
  raw_voice:
    days: 7
    after: processed     # "processed" | "ingested" | "created" | "last_accessed"

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

legal_hold:
  enabled: false

audit:
  log_deletions: true
  log_path: runtime/logs/retention_audit.jsonl
```

### Retention Engine

```python
# src/axiom/extensions/builtins/hygiene/retention.py

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from axiom.infra.state import STATE_LOCATIONS, StateLocation


@dataclass
class RetentionPolicy:
    key: str
    days: int
    after: str  # "processed" | "ingested" | "created" | "last_accessed"


@dataclass
class RetentionAction:
    path: Path
    policy_key: str
    age_days: int
    action: str  # "delete" | "skip"
    reason: str  # "retention_policy" | "legal_hold"


def load_retention_config(config_dir: Path) -> tuple[list[RetentionPolicy], bool, Path]:
    """Load retention config. Returns (policies, legal_hold, audit_path)."""
    config_path = config_dir / "retention.yaml"
    if not config_path.exists():
        # Fall back to example defaults
        config_path = config_dir.parent / "config.example" / "retention.yaml"
    if not config_path.exists():
        return [], False, config_dir.parent / "logs" / "retention_audit.jsonl"

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    policies = []
    for key, val in cfg.get("retention", {}).items():
        policies.append(RetentionPolicy(key=key, days=val["days"], after=val.get("after", "created")))

    legal_hold = cfg.get("legal_hold", {}).get("enabled", False)
    audit_path = Path(cfg.get("audit", {}).get("log_path", "runtime/logs/retention_audit.jsonl"))
    return policies, legal_hold, audit_path


def get_file_age_reference(path: Path, after: str) -> datetime:
    """Determine the reference timestamp for retention calculation."""
    stat = path.stat()
    if after == "last_accessed":
        return datetime.fromtimestamp(stat.st_atime, tz=timezone.utc)
    elif after == "created":
        return datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
    else:
        # "processed", "ingested" — use mtime as proxy
        return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)


def scan_retention(
    project_root: Path,
    policies: list[RetentionPolicy],
    legal_hold: bool,
) -> list[RetentionAction]:
    """Scan state locations and identify files past retention."""
    now = datetime.now(timezone.utc)
    actions: list[RetentionAction] = []

    policy_map = {p.key: p for p in policies}

    for loc in STATE_LOCATIONS:
        if loc.retention_key is None or loc.retention_key not in policy_map:
            continue

        policy = policy_map[loc.retention_key]
        cutoff = now - timedelta(days=policy.days)
        loc_path = project_root / loc.path

        if not loc_path.exists():
            continue

        # Collect files to check
        files: list[Path] = []
        if loc_path.is_dir():
            files = list(loc_path.glob(loc.glob_pattern))
        elif loc_path.is_file():
            files = [loc_path]

        for file_path in files:
            if not file_path.is_file():
                continue
            ref_time = get_file_age_reference(file_path, policy.after)
            age_days = (now - ref_time).days

            if ref_time < cutoff:
                action = "skip" if legal_hold else "delete"
                reason = "legal_hold" if legal_hold else "retention_policy"
                actions.append(RetentionAction(
                    path=file_path,
                    policy_key=policy.key,
                    age_days=age_days,
                    action=action,
                    reason=reason,
                ))

    return actions


def execute_retention(
    actions: list[RetentionAction],
    audit_path: Path,
    dry_run: bool = False,
) -> dict[str, int]:
    """Execute retention actions and log to audit trail.

    Returns summary: {"deleted": N, "skipped": N, "bytes_freed": N}
    """
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {"deleted": 0, "skipped": 0, "bytes_freed": 0}

    for action in actions:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action.action if not dry_run else "dry_run",
            "path": str(action.path),
            "reason": action.reason,
            "policy": action.policy_key,
            "age_days": action.age_days,
        }

        if action.action == "delete" and not dry_run:
            try:
                size = action.path.stat().st_size
                action.path.unlink()
                summary["deleted"] += 1
                summary["bytes_freed"] += size
            except OSError:
                entry["action"] = "error"
        elif action.action == "skip":
            summary["skipped"] += 1

        with open(audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    return summary
```

### TIDY Sweep Integration

TIDY's existing periodic sweep in `manager.py` gains retention awareness:

```python
# In MoManager.sweep() — extend existing method

def sweep(self) -> dict:
    """Periodic resource sweep — existing scratch cleanup + retention."""
    results = self._sweep_scratch()  # existing

    # Retention sweep (new)
    config_dir = self._project_root / "runtime" / "config"
    policies, legal_hold, audit_path = load_retention_config(config_dir)
    if policies:
        actions = scan_retention(self._project_root, policies, legal_hold)
        retention_results = execute_retention(actions, self._project_root / audit_path)
        results["retention"] = retention_results

    return results
```

### CLI Extension

```python
# Extend hygiene/cli.py

def register_retention_commands(subparsers):
    ret_parser = subparsers.add_parser("retention", help="Data retention status and cleanup")
    ret_parser.add_argument("--status", action="store_true", help="Show retention status")
    ret_parser.add_argument("--dry-run", action="store_true", help="Preview cleanup without deleting")
    ret_parser.add_argument("--cleanup", action="store_true", help="Execute retention cleanup")
    ret_parser.add_argument("--category", help="Filter by retention category")
```

---

## Testing Strategy

### Unit Tests

```python
# src/axiom/extensions/builtins/hygiene/tests/test_retention.py

def test_scan_finds_expired_files(tmp_path):
    """Files past retention cutoff are identified."""
    ...

def test_legal_hold_prevents_deletion(tmp_path):
    """Legal hold flag changes action from delete to skip."""
    ...

def test_audit_log_written(tmp_path):
    """Every retention action produces an audit log entry."""
    ...

def test_dry_run_deletes_nothing(tmp_path):
    """Dry run logs but doesn't delete."""
    ...
```

```python
# tests/infra/test_state.py

def test_locked_json_read_write(tmp_path):
    """Basic read-modify-write cycle works."""
    ...

def test_atomic_write_survives_crash(tmp_path):
    """Partial writes don't corrupt the file."""
    ...

def test_concurrent_writes_no_corruption(tmp_path):
    """Two processes writing simultaneously produce valid JSON."""
    # Fork or use multiprocessing to verify locking
    ...

def test_exclusive_lock_blocks_concurrent_write(tmp_path):
    """Second exclusive lock waits for first to release."""
    ...
```

### Integration Tests

```python
def test_mo_sweep_includes_retention(tmp_path):
    """TIDY's sweep cycle runs retention when config exists."""
    ...

def test_retention_config_missing_is_noop(tmp_path):
    """No retention.yaml = no retention actions."""
    ...
```

---

## Migration Plan

### Phase 0: Safe State Access (3 days)

1. Create `src/axiom/infra/state.py` with `LockedJsonFile`, `atomic_write`, `StateLocation`, `STATE_LOCATIONS`
2. Refactor TIDY's `manifest.py` to import `LockedJsonFile` from `infra.state`
3. Migrate publisher state files to use `LockedJsonFile`
4. Tests for concurrent access safety

### Phase 1: Retention (1 week)

1. Create `runtime/config.example/retention.yaml`
2. Create `hygiene/retention.py`
3. Integrate retention sweep into TIDY's `manager.py`
4. Add `axiom tidy retention` CLI subcommand
5. Migrate `CLIP_RETENTION_DAYS` to read from `retention.yaml`
6. Tests for retention engine

---

## §4 PostgreSQL Session Store (Phase 2)

Phase 2 of state management moves chat sessions from local JSON files to PostgreSQL. This enables multi-client session access, agent delegation, and interaction logging.

**Full specification:** [spec-session-store.md](spec-session-store.md)

### Summary

| Component | Description |
|-----------|-------------|
| **`sessions` table** | Session metadata: owner, node, status, context snapshot, delegates, tool mode, budget |
| **`session_messages` table** | Append-only message log: role, content, tool calls, provider, model, tokens, cost |
| **`interaction_log` view** | SQL view over `session_messages` for OKR O7 analytics |
| **`PGSessionStore` class** | `src/axiom/infra/session_store.py` — create, load, save_message, list, delegate, listen |
| **Live sync** | PG `LISTEN/NOTIFY` trigger on `session_messages` inserts |
| **Graceful degradation** | Falls back to existing `SessionStore` (JSON) when PG is unreachable; syncs on reconnect |

### Relationship to Phase 1

Phase 1 (`LockedJsonFile`, retention policies) remains operational and serves as the fallback backend. The `Session` and `Message` dataclasses from Phase 1 are reused as in-memory representations. `PGSessionStore` populates them from PG instead of JSON files.

### Retention Extension

TIDY retention sweeps (Phase 1) are extended to PostgreSQL sessions:
- `active` → indefinite
- `paused`/`completed` → `archived` after 30 days
- `archived` → deleted after 90 days (CASCADE removes messages)

Same retention engine, new target.

---

## Related Documents

- [Agent State Management PRD](../requirements/prd-agent-state-management.md)
- [Session Store Spec](spec-session-store.md)
- [TIDY Agent Extension](../../src/axiom/extensions/builtins/hygiene/)
- [RAG Architecture Spec](spec-rag-architecture.md)
- [Data Architecture Spec](spec-data-architecture.md)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
