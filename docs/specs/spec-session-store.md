# Session Store — Technical Specification

**Status:** Draft (subordinate to `spec-memory.md` as of 2026-04-26)
**Date:** 2026-04-07
**Last Updated:** 2026-04-26
**Owner:** Ben Booth
**Authority:** This spec is **subordinate to `spec-memory.md`**. On any conflict, spec-memory wins. Sessions are projections of L1 episodic fragments per spec-memory §3 + §6; the PG table specified here is a **rebuild-from-L1 read cache**, not the authoritative store.
**Related:** `spec-memory.md` (authoritative for episodic fragments + projections), `spec-agent-state-management.md`, `spec-axi-cli.md`, `spec-agent-architecture.md`, `spec-observability.md`, `prd-agent-state-management.md`, `prd-agents.md`, `prd-memory.md`

---

## 0. Reconciliation with spec-memory.md (added 2026-04-26)

This spec was written assuming session messages were the primary table and the interaction log was a *view* over session data. `spec-memory.md` inverts that model: **the interaction log is L1**; sessions are **projections** of L1 conversation_turn fragments.

Concretely:

- A chat turn (human message, assistant response, tool call) is an `episodic` `MemoryFragment` written via `CompositionService.write` per spec-memory §3 + §8.
- `content` carries `{role, text, model, tool_calls, ...}`; `scope = conversation_id`; `provenance = (T, U, A, R)`.
- The PG `session_messages` table specified here is **a rebuild-from-L1 cache** — fast queries, conventional indexes — but it is not the source of truth. `EventStore.list(scope=conversation_id)` is.
- The cache is replayable: a new node joining a conversation rebuilds its `session_messages` rows by replaying L1.
- Live-sync (LISTEN/NOTIFY) stays as-is at the cache layer; cache invalidations are driven by L1 writes through `CompositionService`.

When the cache implementation aligns to be rebuild-from-L1, this spec's §2 (Schema) and §3 (Read/Write Paths) get re-anchored as cache-shape notes. Until then, treat the cache as authoritative for read efficiency, the L1 log as authoritative for replay/audit/federation.

---

## 1. Overview

### 1.1 Problem

Chat sessions today live as JSON files on the local filesystem (`runtime/sessions/*.json`). This means:

- **No resume across clients.** Close the terminal, lose the context. Start on laptop, can't pick up on mobile.
- **No agent continuation.** A background agent (SCAN, TIDY) can't continue a session a human started.
- **No interaction log.** OKR O7 requires every completion tuple to be captured. With JSON files scattered across machines, this data is siloed and unqueryable.
- **No cost visibility.** Token usage is tracked per-turn in memory but never persisted or aggregated.
- **No live sync.** If an agent is running a long task, a human watching from another client sees nothing until the task completes.

### 1.2 Solution

Move session storage to the shared PostgreSQL instance that already runs on every Axiom node (alongside pgvector for RAG). Sessions become first-class database objects: any client or agent connected to the same PG can read, write, and subscribe to live updates.

### 1.3 Design Principles

1. **Same PG, no new database.** Sessions use the PostgreSQL instance that RAG already depends on. One connection string, one backup, one ops surface.
2. **Append-only messages.** `session_messages` is an INSERT-only table. No UPDATE, no DELETE in normal operation. This eliminates write conflicts between concurrent clients.
3. **Local decision, global state.** Each client loads session state from PG, makes decisions locally, and writes results back. No distributed locking.
4. **Graceful degradation.** If PG is unreachable, fall back to local JSON files. Sync to PG when reconnected. Chat never breaks because the database is down.
5. **Sessions are the interaction log.** Every message row in `session_messages` is an interaction log entry. The interaction log (OKR O7) is not a separate table — it's a view over session data.

---

## 2. Schema

### 2.1 Tables

```sql
-- Sessions: metadata about a conversation
CREATE TABLE sessions (
    session_id      TEXT PRIMARY KEY,             -- 12-char hex (matches existing format)
    title           TEXT NOT NULL DEFAULT '',      -- auto-titled from first user message
    owner           TEXT NOT NULL DEFAULT '',      -- node_id or user identifier
    node_id         TEXT NOT NULL DEFAULT '',      -- node that created the session
    status          TEXT NOT NULL DEFAULT 'active',-- active, paused, completed, archived
    context         JSONB NOT NULL DEFAULT '{}',  -- workspace snapshot (CLAUDE.md, model.yaml, etc.)
    delegates       JSONB NOT NULL DEFAULT '[]',  -- agent/node IDs authorized to write
    tool_mode       TEXT NOT NULL DEFAULT 'full', -- full, simple, or "only:name1,name2"
    max_budget_tokens INTEGER DEFAULT NULL,        -- per-session token budget (NULL = unlimited)
    version         INTEGER NOT NULL DEFAULT 1,   -- optimistic concurrency for metadata updates
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Messages: append-only conversation log
CREATE TABLE session_messages (
    message_id      SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role            TEXT NOT NULL,                 -- user, assistant, tool, system
    content         TEXT NOT NULL DEFAULT '',
    tool_calls      JSONB DEFAULT NULL,            -- tool call metadata (OpenAI format)
    provider        TEXT NOT NULL DEFAULT '',       -- LLM provider name (e.g., "qwen-private-llm", "claude-sonnet")
    model           TEXT NOT NULL DEFAULT '',       -- model identifier
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,    -- prompt cache tokens read
    cost            REAL NOT NULL DEFAULT 0.0,      -- estimated cost in USD
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2.2 Indexes

```sql
-- Fast message retrieval by session (ordered by time)
CREATE INDEX idx_session_messages_session ON session_messages(session_id, created_at);

-- Fast session listing by owner (most recent first)
CREATE INDEX idx_sessions_owner ON sessions(owner, updated_at DESC);

-- Fast session listing by status
CREATE INDEX idx_sessions_status ON sessions(status, updated_at DESC);
```

### 2.3 Live Sync Trigger

PG `LISTEN/NOTIFY` allows clients to receive real-time notifications when new messages are added to a session they're watching.

```sql
CREATE OR REPLACE FUNCTION notify_session_message() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('session_' || NEW.session_id, NEW.message_id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER session_message_notify
    AFTER INSERT ON session_messages
    FOR EACH ROW EXECUTE FUNCTION notify_session_message();
```

Clients subscribe with: `LISTEN session_<session_id>;`

### 2.4 Interaction Log View

The interaction log (OKR O7) is a view over session data, not a separate table:

```sql
CREATE VIEW interaction_log AS
SELECT
    m.message_id,
    m.session_id,
    s.owner,
    s.node_id,
    m.role,
    m.provider,
    m.model,
    m.tokens_in,
    m.tokens_out,
    m.cache_read,
    m.cost,
    m.created_at
FROM session_messages m
JOIN sessions s ON s.session_id = m.session_id
WHERE m.role IN ('user', 'assistant')  -- exclude tool results for summary view
ORDER BY m.created_at;
```

---

## 3. PGSessionStore Class

### 3.1 Interface

```python
class PGSessionStore:
    """PostgreSQL-backed session store with multi-client support.

    Connects to the same PG instance as RAGStore. Falls back to
    local JSON SessionStore if PG is unreachable.
    """

    def __init__(self, database_url: str | None = None):
        """Connect to PG. If database_url is None, read from settings."""
        ...

    # --- Session lifecycle ---

    def create(
        self,
        owner: str = "",
        context: dict | None = None,
        tool_mode: str = "full",
        max_budget_tokens: int | None = None,
    ) -> Session:
        """Create a new session. Returns a Session dataclass."""
        ...

    def load(self, session_id: str) -> Session:
        """Load a session and its messages from PG."""
        ...

    def update_meta(self, session_id: str, **kwargs) -> None:
        """Update session metadata (title, status, delegates, etc.).
        Uses optimistic locking via version column."""
        ...

    def archive(self, session_id: str) -> None:
        """Mark session as archived."""
        ...

    # --- Messages ---

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        provider: str = "",
        model: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cache_read: int = 0,
        cost: float = 0.0,
    ) -> int:
        """Append a message to the session. Returns message_id.
        INSERT only — never updates existing messages."""
        ...

    def get_messages(
        self,
        session_id: str,
        after: int = 0,
        limit: int | None = None,
    ) -> list[Message]:
        """Get messages for a session, optionally after a given message_id.
        Used for incremental loading and live sync."""
        ...

    # --- Listing ---

    def list_sessions(
        self,
        owner: str | None = None,
        status: str = "active",
        limit: int = 20,
    ) -> list[SessionMeta]:
        """List sessions, most recent first. SessionMeta is a lightweight
        summary (no message content)."""
        ...

    # --- Delegation ---

    def delegate(self, session_id: str, delegate_id: str) -> None:
        """Authorize an agent or node to write to this session."""
        ...

    def revoke_delegate(self, session_id: str, delegate_id: str) -> None:
        """Remove write authorization."""
        ...

    def can_write(self, session_id: str, writer_id: str) -> bool:
        """Check if writer_id is the owner or a delegate."""
        ...

    # --- Live sync ---

    def listen(self, session_id: str) -> Iterator[int]:
        """Yield message_ids as they are inserted via PG LISTEN/NOTIFY.
        Blocks until a notification arrives or timeout."""
        ...

    # --- Aggregation ---

    def session_usage(self, session_id: str) -> SessionUsage:
        """Aggregate token counts and cost for the session."""
        ...

    def session_budget_remaining(self, session_id: str) -> int | None:
        """Returns remaining token budget, or None if unlimited."""
        ...

    # --- Graceful degradation ---

    @property
    def available(self) -> bool:
        """True if PG connection is alive."""
        ...

    def close(self) -> None:
        """Close PG connection."""
        ...
```

### 3.2 SessionMeta

Lightweight summary for listing — doesn't load message content:

```python
@dataclass
class SessionMeta:
    session_id: str
    title: str
    owner: str
    status: str
    message_count: int
    total_tokens: int
    total_cost: float
    last_active: str       # ISO timestamp of most recent message
    created_at: str
```

### 3.3 SessionUsage

Aggregate usage for a session:

```python
@dataclass
class SessionUsage:
    total_tokens_in: int
    total_tokens_out: int
    total_cache_read: int
    total_cost: float
    turn_count: int          # number of assistant messages
    providers_used: list[str]
    models_used: list[str]
```

---

## 4. Concurrency

### 4.1 Messages: Append-Only

`session_messages` is INSERT-only. Two clients writing to the same session simultaneously just produce interleaved messages — both are preserved, ordered by `created_at`. This is safe because:

- Messages are immutable after insertion
- The `message_id` SERIAL provides total ordering
- No UPDATE or DELETE in normal operation

### 4.2 Session Metadata: Optimistic Locking

`sessions` metadata (title, status, delegates, tool_mode) can be updated by any authorized writer. Concurrent updates use optimistic locking:

```python
def update_meta(self, session_id: str, **kwargs) -> None:
    current = self._get_session_row(session_id)
    result = self._execute(
        """UPDATE sessions
           SET {fields}, version = version + 1, updated_at = now()
           WHERE session_id = %s AND version = %s""",
        [..., session_id, current.version],
    )
    if result.rowcount == 0:
        raise ConcurrentModificationError(session_id)
```

If two clients try to update metadata simultaneously, one succeeds and the other gets a `ConcurrentModificationError`. The losing client reloads and retries. This is rare — metadata updates are infrequent (title change, status change, delegate addition).

---

## 5. Delegation

Sessions have an owner (the human or agent that created them) and optional delegates (agents or nodes authorized to continue the conversation).

### 5.1 RACI Model

| Role | Who | Permissions |
|------|-----|------------|
| **Responsible** | Owner | Full control: read, write, delegate, archive |
| **Accountable** | Owner | Final approval on session outcomes |
| **Consulted** | Delegates | Read and write messages to the session |
| **Informed** | Any node in federation with PG access | Read-only via `GET /api/v1/sessions/<id>/messages` |

### 5.2 Delegation Flow

```
1. Human starts chat session → owner = human's node_id
2. Human says "SCAN, continue analyzing these signals"
3. Chat agent calls: store.delegate(session_id, "scan-agent")
4. SCAN loads session, appends messages with role="assistant"
5. Human can watch via LISTEN/NOTIFY or poll GET /messages
6. Human can revoke: store.revoke_delegate(session_id, "scan-agent")
```

### 5.3 Authorization Check

Every `save_message()` call checks `can_write()`:

```python
def can_write(self, session_id: str, writer_id: str) -> bool:
    session = self._get_session_row(session_id)
    return writer_id == session.owner or writer_id in session.delegates
```

Unauthorized writes are rejected with `SessionAccessDenied`.

---

## 6. Live Sync

### 6.1 PG LISTEN/NOTIFY

When a message is inserted, the trigger fires `pg_notify('session_<id>', '<message_id>')`. Any client LISTENing on that channel receives the notification immediately.

```python
def listen(self, session_id: str) -> Iterator[int]:
    """Yield message_ids as they arrive."""
    self._execute(f"LISTEN session_{session_id}")
    while True:
        if select.select([self._conn], [], [], timeout=30):
            self._conn.poll()
            while self._conn.notifies:
                notify = self._conn.notifies.pop(0)
                yield int(notify.payload)
```

### 6.2 WebSocket Relay (via neut-serve)

For web/mobile clients that can't hold a PG connection, `neut-serve` relays LISTEN/NOTIFY over WebSocket:

```
WS /api/v1/sessions/<id>/stream

Client connects → server LISTENs on PG → on notify →
server sends: {"message_id": 42, "session_id": "abc123"}
Client fetches: GET /api/v1/sessions/abc123/messages?after=41
```

This is a thin relay — no message buffering, no state. The WebSocket connection is a proxy for PG LISTEN.

---

## 7. Context Reconstruction

### 7.1 The Problem

`_build_system_prompt()` reads local files:
- `CLAUDE.md` from repo root
- `.claude/context.md` for personal context
- `model.yaml` detection for workspace context
- `--context` file content

A remote client resuming a session doesn't have these files.

### 7.2 Solution: Snapshot + Overlay

On session creation, snapshot the local context into `sessions.context` JSONB:

```python
def _snapshot_context(self) -> dict:
    ctx = {}
    claude_md = _REPO_ROOT / "CLAUDE.md"
    if claude_md.exists():
        ctx["claude_md"] = claude_md.read_text()[:8000]
    personal = _REPO_ROOT / ".claude" / "context.md"
    if personal.exists():
        ctx["personal_context"] = personal.read_text()[:2000]
    if self._workspace_context:
        ctx["workspace_context"] = self._workspace_context
    return ctx
```

On resume:
1. Load `sessions.context` from PG
2. If local files exist (same machine), use fresh local files (they may have changed)
3. If local files don't exist (different machine), use the snapshot
4. Session-specific context (`--context` file) always comes from the snapshot

---

## 8. Graceful Degradation

### 8.1 Tier 0: No PG Available

If PG is unreachable at session start:

```python
def _get_store(self) -> PGSessionStore | SessionStore:
    """Try PG first, fall back to JSON files."""
    if self._pg_store and self._pg_store.available:
        return self._pg_store
    return self._json_store  # existing SessionStore
```

Chat works normally with local JSON sessions. No features lost except multi-client access and live sync.

### 8.2 Sync on Reconnect

When PG becomes available after a local-only session:

```python
def sync_local_to_pg(json_store: SessionStore, pg_store: PGSessionStore) -> int:
    """Import local JSON sessions into PG. Returns count imported."""
    local_sessions = json_store.list_sessions()
    imported = 0
    for sid in local_sessions:
        if pg_store.load(sid) is None:  # not already in PG
            session = json_store.load(sid)
            pg_store.create_from_session(session)
            imported += 1
    return imported
```

This runs automatically when PG reconnects, or manually via `axi chat --sync`.

---

## 9. Web API

### 9.1 Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/sessions` | List sessions (filterable by owner, status) |
| `GET` | `/api/v1/sessions/<id>` | Get session metadata |
| `GET` | `/api/v1/sessions/<id>/messages` | Get messages (supports `?after=<message_id>` for incremental) |
| `POST` | `/api/v1/sessions/<id>/messages` | Append a message (for agent/API access) |
| `PATCH` | `/api/v1/sessions/<id>` | Update session metadata (title, status, delegates) |
| `WS` | `/api/v1/sessions/<id>/stream` | WebSocket for live message notifications |

### 9.2 Existing `/chat` Endpoint Update

```
POST /chat
{
    "message": "what's the startup procedure?",
    "session_id": "abc123"        // optional: continue existing session
}

Response:
{
    "response": "The startup procedure involves...",
    "session_id": "abc123",       // always returned
    "message_id": 42,
    "usage": {
        "tokens_in": 1234,
        "tokens_out": 567,
        "cost": 0.02,
        "session_total_cost": 0.15
    }
}
```

---

## 10. Cost Tracking

### 10.1 Per-Message

Every `session_messages` row stores `tokens_in`, `tokens_out`, `cache_read`, and `cost`. Cost is computed at write time using the pricing table in `usage.py` (already implemented).

### 10.2 Per-Session Aggregation

```sql
SELECT
    SUM(tokens_in) as total_in,
    SUM(tokens_out) as total_out,
    SUM(cost) as total_cost,
    COUNT(*) FILTER (WHERE role = 'assistant') as turn_count
FROM session_messages
WHERE session_id = %s;
```

Exposed via `PGSessionStore.session_usage()` and the `/api/v1/sessions/<id>` endpoint.

### 10.3 Budget Enforcement

If `sessions.max_budget_tokens` is set, `ChatAgent.turn()` checks remaining budget before each API call:

```python
remaining = self._store.session_budget_remaining(self.session_id)
if remaining is not None and remaining <= 0:
    return "Session token budget exhausted. Use `axi chat --resume` to continue with a fresh budget."
```

Budget is checked *before* the API call, not after, so the last turn is always within budget.

---

## 11. Retention

Extends the existing TIDY retention framework (defined in `spec-agent-state-management.md`):

| Status | Retention | Action |
|--------|-----------|--------|
| `active` | Indefinite | No cleanup |
| `paused` | 30 days after `updated_at` | TIDY transitions to `archived` |
| `completed` | 30 days after `updated_at` | TIDY transitions to `archived` |
| `archived` | 90 days after archival | TIDY deletes (CASCADE removes messages) |

TIDY sweep query:

```sql
-- Archive stale sessions
UPDATE sessions SET status = 'archived'
WHERE status IN ('paused', 'completed')
AND updated_at < now() - INTERVAL '30 days';

-- Delete old archives
DELETE FROM sessions
WHERE status = 'archived'
AND updated_at < now() - INTERVAL '90 days';
```

---

## 12. Migration Path

### 12.1 Existing JSON Sessions

The existing `SessionStore` writes to `runtime/sessions/*.json`. On first PG connection, these are imported:

```python
def migrate_json_sessions(json_dir: Path, pg_store: PGSessionStore) -> int:
    """One-time import of JSON sessions into PG."""
    count = 0
    for path in json_dir.glob("*.json"):
        session = Session.from_dict(json.loads(path.read_text()))
        if not pg_store.session_exists(session.session_id):
            pg_store.create_from_session(session)
            count += 1
    return count
```

After migration, JSON files are kept as backup but no longer written to (PG is authoritative).

### 12.2 Alembic Migration

```python
def upgrade():
    op.execute("""
        CREATE TABLE sessions (...);
        CREATE TABLE session_messages (...);
        CREATE INDEX ...;
        CREATE FUNCTION notify_session_message() ...;
        CREATE TRIGGER session_message_notify ...;
        CREATE VIEW interaction_log AS ...;
    """)

def downgrade():
    op.execute("DROP VIEW IF EXISTS interaction_log")
    op.execute("DROP TABLE IF EXISTS session_messages CASCADE")
    op.execute("DROP TABLE IF EXISTS sessions CASCADE")
    op.execute("DROP FUNCTION IF EXISTS notify_session_message()")
```

---

## 13. Test Plan

| Test | What it validates |
|------|------------------|
| `test_create_session` | PGSessionStore.create() inserts row, returns Session |
| `test_save_and_load_messages` | Append 5 messages, load, verify order and content |
| `test_load_nonexistent` | Load unknown session_id raises SessionNotFound |
| `test_optimistic_locking` | Two concurrent update_meta on same version: one succeeds, one raises |
| `test_append_only` | Messages cannot be updated or deleted via store API |
| `test_delegation` | delegate() adds to JSONB array; can_write() returns True for delegate |
| `test_revoke_delegate` | revoke_delegate() removes; can_write() returns False |
| `test_unauthorized_write` | save_message() from non-owner non-delegate raises SessionAccessDenied |
| `test_listen_notify` | Insert message in one connection, verify LISTEN receives notification |
| `test_session_usage` | Insert messages with cost, verify session_usage() aggregation |
| `test_budget_remaining` | Set budget, add messages, verify remaining decreases |
| `test_budget_unlimited` | No budget set, verify budget_remaining returns None |
| `test_list_sessions` | Create 3 sessions, list with owner filter, verify order |
| `test_list_by_status` | Create active + archived, list active only |
| `test_context_snapshot` | Create with context, load from different "client", verify snapshot |
| `test_graceful_degradation` | Close PG connection, verify available returns False |
| `test_json_migration` | Write JSON session, run migrate, verify PG has it |
| `test_interaction_log_view` | Insert messages, query interaction_log view, verify columns |
| `test_session_meta` | list_sessions returns SessionMeta with correct counts |
| `test_tool_mode_persisted` | Create with tool_mode="simple", load, verify tool_mode |

---

## 14. File Layout

```
src/axiom/
├── infra/
│   ├── session_store.py          # PGSessionStore, SessionMeta, SessionUsage
│   └── orchestrator/
│       └── session.py            # Session, Message (unchanged — in-memory representation)
├── extensions/builtins/
│   ├── chat/
│   │   ├── agent.py              # ChatAgent — wired to PGSessionStore
│   │   ├── tools.py              # get_tool_definitions(mode) — tool filtering
│   │   └── usage.py              # TurnUsage, UsageTracker (unchanged)
│   └── http/
│       └── routes.py             # Session REST + WebSocket endpoints
├── migrations/
│   └── versions/
│       └── xxx_add_session_tables.py  # Alembic migration
```

---

## 15. Dependencies

| Dependency | Purpose | Already in project? |
|------------|---------|-------------------|
| `psycopg2` | PG connection, LISTEN/NOTIFY | Yes (RAG store) |
| `select` | LISTEN polling | stdlib |

No new dependencies required.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
