# ADR-011: Concurrent File Write Safety — `locked_append_jsonl` as Canonical Pattern

**Status:** Accepted
**Date:** 2026-03-19
**Owner:** Ben Booth

---

## Context

Axiom runs multiple concurrent writers against shared files:

- A CLI invocation while a background daemon (heartbeat, RAG watch) is running
- Two simultaneous CLI invocations (`axiom signal ingest` + `axiom signal review`)
- A web API server handling concurrent requests alongside CLI usage
- Agent daemons (SCAN, TIDY) processing queues while the user runs commands

Prior to this ADR, 33 of 35 file write locations in the codebase used plain
`open(..., "a")` or `open(..., "w")` with no locking. The only protected code
was `LockedJsonFile` in `infra/state.py`, which handles full-file JSON
read-modify-write but did not cover the JSONL append pattern used by all logs,
queues, and audit files.

**Risk observed:**
- Interleaved writes from two processes produce malformed JSONL (partial lines
  written by one process overwritten mid-write by another)
- Read-modify-write on JSON state files loses one writer's changes when both
  read the same version before either writes back
- Audit trails (correction_applied.jsonl, propagation_log.jsonl, routing_audit.jsonl)
  can lose entries silently with no error raised

The underlying OS guarantee (`write()` syscalls under 4096 bytes to a local
filesystem are usually atomic on Linux/macOS) is not reliable across operating
systems, network filesystems, or larger records, and should not be relied upon.

---

## Decision

**`locked_append_jsonl(path, record)` in `infra/state.py` is the canonical
pattern for all JSONL append writes in this codebase.** Plain `open(..., "a")`
is not permitted for shared files.

For full-file read-modify-write, `LockedJsonFile` (already established) is the
canonical pattern.

### Locking Implementation

**Unix (macOS, Linux):** `fcntl.flock(LOCK_EX)` on a companion `.lock` file.
Advisory lock, compatible with multiple Python processes on the same machine.

**Windows:** `portalocker` (when installed) for cross-platform compatibility.
Falls back to best-effort on Windows without it.

The lock is held only for the duration of the append (not during the business
logic that produces the record), so contention is minimal.

### Atomic Write Guarantee

For full-file writes (`LockedJsonFile.write`), the implementation uses
`tempfile.mkstemp` + `os.replace` — the file is never in a partially-written
state, even if the process crashes mid-write.

For JSONL appends, `os.fsync` is called after each write to flush to disk
before releasing the lock.

---

## Pattern Reference

```python
# JSONL append (logs, queues, audit files) — USE THIS
from axiom.infra.state import locked_append_jsonl

locked_append_jsonl("runtime/logs/my_agent/events.jsonl", {
    "ts": datetime.now(timezone.utc).isoformat(),
    "event": "ingest_complete",
    "count": 47,
})

# Full-file JSON read-modify-write (state files) — USE THIS
from axiom.infra.state import LockedJsonFile

with LockedJsonFile("runtime/state/my_state.json", exclusive=True) as f:
    data = f.read()
    data["counter"] += 1
    f.write(data)

# One-shot atomic write — USE THIS
from axiom.infra.state import atomic_write

atomic_write("runtime/state/my_config.json", {"version": 2, "items": [...]})

# NEVER DO THIS for shared files:
with open("runtime/logs/events.jsonl", "a") as f:    # ❌ no locking
    f.write(json.dumps(record) + "\n")

with open("runtime/state/config.json", "w") as f:    # ❌ no locking, not atomic
    json.dump(data, f)
```

---

## Files Fixed

| File | Previous pattern | Fixed pattern |
|---|---|---|
| `infra/routing_audit.py` | `open(_AUDIT_PATH, "a")` | `locked_append_jsonl` |
| `infra/orchestrator/bus.py` | `open(self._log_path, "a")` | `locked_append_jsonl` |
| `eve_agent/correction_review.py` (3 locations) | `open(..., "a")` | `locked_append_jsonl` |
| `eve_agent/correction_propagation.py` (2 locations) | `open(..., "a")` | `locked_append_jsonl` |
| `web_api/server.py` | `open(_chat_log_path, "a")` | `locked_append_jsonl` |
| `mo_agent/retention.py` | `open(audit_path, "a")` | `locked_append_jsonl` |
| `eve_agent/echo_suppression.py` | `open(index_file, "w")` multi-line | atomic tempfile + `os.replace` |
| `eve_agent/media_library.py` (2 locations) | `write_text(json.dumps(...))` | `atomic_write` |
| `eve_agent/signal_rag.py` (2 locations) | `write_text(json.dumps(...))` | `atomic_write` |
| `eve_agent/router.py` (transit log) | `write_text(json.dumps(...))` | `atomic_write` |
| `prt_agent/scripts/publish.py` | bare `open` read-modify-write | `LockedJsonFile` |
| `eve_agent/correction_review_guided.py` | already used `LockedJsonFile` | no change needed |
| `eve_agent/correction_propagation.py` (glossary) | already used `LockedJsonFile` | no change needed |

## Remaining Locations (low priority — not yet fixed)

| File | Pattern | Notes |
|---|---|---|
| `note/cli.py` | `open("a")` | Single-user CLI, not concurrent |
| `eve_agent/voice_id.py` | `write_text(json.dumps(...))` | Low write frequency |
| `eve_agent/blocker_tracker.py` | `write_text(json.dumps(...))` | Low write frequency |
| `eve_agent/smart_router.py` | `write_text(json.dumps(...))` | Low write frequency |
| `repo/orchestrator.py` | `json.dump(export_data)` | One-shot export, not shared |

---

## Consequences

**Positive:**
- JSONL audit trails are correct under concurrent access
- Extension developers have a clear, enforced pattern to follow
- No platform-specific code required in extension code (all locking is in `infra/state.py`)

**Negative:**
- `os.fsync` per append adds ~1–3ms latency. For non-EC, non-audit writes this
  is acceptable. For high-frequency logging (>100 writes/sec), consider batching.
- `portalocker` dependency on Windows (optional; falls back to best-effort)

---

## Extension Developer Guidance

Every extension that writes to a shared file **must** use one of:
- `locked_append_jsonl` — for append-only JSONL (logs, queues, events)
- `LockedJsonFile` — for read-modify-write JSON state files
- `atomic_write` — for one-shot JSON state writes

This is enforced by code review. A linter rule (`no-bare-file-write`) should
be added to `pyproject.toml` to flag `open(..., "a")` and `open(..., "w")`
outside of `infra/state.py` and test files.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
