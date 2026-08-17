# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Domain-consumer agent state management — safe concurrent access with hybrid backend.

Provides:

Low-level:
- LockedJsonFile: Advisory-locked JSON file I/O (read, write, read-modify-write)
- atomic_write / locked_read: Convenience one-shot helpers
- locked_append_jsonl: Multi-process-safe append to .jsonl files (the canonical
  pattern for all append-only logs, queues, and audit files in this codebase)

Tamper-evident:
- TamperEvidentChain: HMAC-SHA256 chain over sequential records.  Shared by
  the System Audit Log (routing decisions, EC events) and the Operations Log
  (tamper-evident logbook for auditable operations).  Both use the same chain
  algorithm; only the HMAC key source and PostgreSQL table differ.

Hybrid backend (the primary API for consumers):
- StateHandle / StateBackend: Protocols for backend-agnostic state access
- FileStateBackend: Flat-file backend (LockedJsonFile)
- PgStateBackend: PostgreSQL backend (ACID transactions)
- HybridStateStore: Automatic backend selection with fallback
- get_state_store(): Project-wide singleton

Registry:
- StateLocation / STATE_LOCATIONS: Declarative catalog of all known state locations

Usage::

    from axiom.infra.state import get_state_store

    store = get_state_store()

    # Read (backend-agnostic)
    data = store.read("runtime/inbox/state/briefing_state.json")

    # Read-modify-write (backend-agnostic)
    with store.open("runtime/inbox/state/briefing_state.json", exclusive=True) as h:
        data = h.read()
        data["counter"] += 1
        h.write(data)
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ===========================================================================
# Low-level: LockedJsonFile
# ===========================================================================

class LockedJsonFile:
    """Safe concurrent JSON file access with advisory locking.

    Uses fcntl.flock on Unix for process-level coordination.
    Writes are atomic (tempfile + os.replace) to prevent corruption on crash.

    Usage::

        # Read-only (shared lock)
        with LockedJsonFile(path) as f:
            data = f.read()

        # Read-modify-write (exclusive lock)
        with LockedJsonFile(path, exclusive=True) as f:
            data = f.read()
            data["key"] = "value"
            f.write(data)
    """

    def __init__(
        self,
        path: str | Path,
        *,
        exclusive: bool = False,
        timeout: float = 5.0,
    ):
        self._path = Path(path)
        self._exclusive = exclusive
        self._timeout = timeout
        self._lock_fd: int | None = None

    def __enter__(self) -> LockedJsonFile:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        self._lock_fd = os.open(
            str(lock_path), os.O_RDWR | os.O_CREAT, 0o644,
        )
        self._acquire_lock()
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._lock_fd is not None:
            self._release_lock()
            os.close(self._lock_fd)
            self._lock_fd = None
        return False

    def read(self) -> Any:
        """Read and parse JSON content. Returns empty dict/list on missing or empty file."""
        if self._lock_fd is None:
            raise RuntimeError("LockedJsonFile must be used as a context manager")
        try:
            content = self._path.read_text(encoding="utf-8")
            if not content.strip():
                return {}
            return json.loads(content)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def write(self, data: Any) -> None:
        """Atomically write JSON data (tempfile in same dir + os.replace)."""
        if self._lock_fd is None:
            raise RuntimeError("LockedJsonFile must be used as a context manager")
        if not self._exclusive:
            raise RuntimeError("write() requires exclusive=True")

        dir_path = str(self._path.parent)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(self._path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _acquire_lock(self) -> None:
        if sys.platform == "win32" or self._lock_fd is None:
            return
        try:
            import fcntl
            lock_type = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
            fcntl.flock(self._lock_fd, lock_type)
        except (ImportError, OSError):
            pass

    def _release_lock(self) -> None:
        if sys.platform == "win32" or self._lock_fd is None:
            return
        try:
            import fcntl
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def atomic_write(path: str | Path, data: Any) -> None:
    """Atomically write JSON data to a file with exclusive locking."""
    with LockedJsonFile(path, exclusive=True) as f:
        f.write(data)


def locked_read(path: str | Path) -> Any:
    """Read JSON data from a file with shared locking."""
    with LockedJsonFile(path) as f:
        return f.read()


def locked_append_jsonl(path: str | Path, record: Any) -> None:
    """Append one JSON record as a line to a .jsonl file with exclusive locking.

    Multi-process safe on Unix (fcntl.flock) and Windows (portalocker when
    available, otherwise best-effort).  The lock is held only for the duration
    of the append — no read-modify-write, so contention is minimal even under
    heavy concurrent load.

    Usage::

        locked_append_jsonl("runtime/logs/audit/routing.jsonl", {"ts": ..., "tier": ...})

    This is the canonical way to write to any append-only JSONL file in the
    codebase.  Plain ``open(..., "a")`` without locking is NOT safe when
    multiple processes (CLI + daemon + web API) can write concurrently.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, separators=(",", ":"), sort_keys=True) + "\n"

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # Acquire exclusive lock
        if sys.platform != "win32":
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
        else:
            try:
                import portalocker  # type: ignore[import-untyped]
                portalocker.lock(lock_fd, portalocker.LOCK_EX)
            except ImportError:
                pass  # best-effort on Windows without portalocker

        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    finally:
        # Release lock and close
        if sys.platform != "win32":
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        os.close(lock_fd)


# ===========================================================================
# Tamper-evident chain (shared by Audit Log and Operations Log)
# ===========================================================================

_CHAIN_GENESIS = "GENESIS"


def _canonical_json(record: dict) -> str:
    """Deterministic JSON serialisation (sorted keys, no whitespace)."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


class TamperEvidentChain:
    """HMAC-SHA256 chain over sequential records.

    Each record is stamped with an ``hmac`` field that covers all other fields
    in the record plus the HMAC of the preceding record.  Any gap, deletion,
    or field modification breaks the chain and is detectable by
    :meth:`verify`.

    This class is **shared infrastructure** for:

    * **System Audit Log** — routing decisions, sensitivity events, VPN checks.
      Key source: ``AXIOM_AUDIT_HMAC_KEY`` env var (required in controlled mode).
    * **Operations Log** — tamper-evident logbook for auditable operations.
      Key source: ``NEUT_OPS_LOG_HMAC_KEY`` env var (required for regulated facilities).

    Both use the identical chain algorithm.  The key source and PostgreSQL
    table are the only things that differ between the two consumers.

    Usage::

        chain = TamperEvidentChain(key=os.environ["AXIOM_AUDIT_HMAC_KEY"])

        # Stamp a record before writing it
        record = {"ts": "...", "tier": "export_controlled", "provider": "ec-llm"}
        stamped = chain.stamp(record, prev_hmac=last_written_hmac)
        # stamped now has an "hmac" field — persist this to the DB/file

        # Verify a sequence of records retrieved from storage
        ok, broken_at = chain.verify(records)  # records is list[dict] ordered by ts
    """

    def __init__(self, key: str | bytes):
        if isinstance(key, str):
            key = key.encode("utf-8")
        self._key = key

    def stamp(self, record: dict, *, prev_hmac: str = _CHAIN_GENESIS) -> dict:
        """Return a copy of *record* with an ``hmac`` field added.

        The original record must NOT already have an ``hmac`` key.
        """
        if "hmac" in record:
            raise ValueError("record already has an 'hmac' field; remove it before stamping")
        message = _canonical_json(record) + prev_hmac
        digest = _hmac.new(self._key, message.encode("utf-8"), hashlib.sha256).hexdigest()
        return {**record, "hmac": digest}

    def verify(self, records: list[dict]) -> tuple[bool, str | None]:
        """Verify the HMAC chain over an ordered sequence of records.

        Returns ``(True, None)`` if the chain is intact.
        Returns ``(False, broken_at_hmac)`` where *broken_at_hmac* is the
        ``hmac`` value of the first broken link.
        """
        prev_hmac = _CHAIN_GENESIS
        for record in records:
            stored_hmac = record.get("hmac", "")
            body = {k: v for k, v in record.items() if k != "hmac"}
            expected = self.stamp(body, prev_hmac=prev_hmac)["hmac"]
            if not _hmac.compare_digest(stored_hmac, expected):
                return False, stored_hmac
            prev_hmac = stored_hmac
        return True, None

    @staticmethod
    def genesis() -> str:
        """The sentinel value used as prev_hmac for the first record."""
        return _CHAIN_GENESIS


# ===========================================================================
# Hybrid backend: Protocols
# ===========================================================================

@runtime_checkable
class StateHandle(Protocol):
    """Handle to a single state document within a transaction/lock."""

    def read(self) -> Any: ...
    def write(self, data: Any) -> None: ...


@runtime_checkable
class StateBackend(Protocol):
    """Backend that can open state documents for read/write."""

    @contextmanager
    def open(self, path: str, *, exclusive: bool = False) -> Generator[StateHandle, None, None]: ...

    def read(self, path: str) -> Any: ...

    def write(self, path: str, data: Any) -> None: ...

    @property
    def name(self) -> str: ...


# ===========================================================================
# FileStateBackend — wraps LockedJsonFile
# ===========================================================================

class _FileHandle:
    """Adapter: LockedJsonFile → StateHandle protocol."""

    def __init__(self, locked_file):
        self._f = locked_file

    def read(self) -> Any:
        return self._f.read()

    def write(self, data: Any) -> None:
        self._f.write(data)


class FileStateBackend:
    """Flat-file state backend using LockedJsonFile.

    Resolves paths relative to a project root directory.
    """

    def __init__(self, root: Path):
        self._root = root

    @property
    def name(self) -> str:
        return "file"

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute():
            return p
        return self._root / path

    @contextmanager
    def open(
        self, path: str, *, exclusive: bool = False,
    ) -> Generator[_FileHandle, None, None]:
        abs_path = self._resolve(path)
        with LockedJsonFile(abs_path, exclusive=exclusive) as f:
            yield _FileHandle(f)

    def read(self, path: str) -> Any:
        with self.open(path) as h:
            return h.read()

    def write(self, path: str, data: Any) -> None:
        with self.open(path, exclusive=True) as h:
            h.write(data)


# ===========================================================================
# PgStateBackend — wraps PgStateStore
# ===========================================================================

class PgStateBackend:
    """PostgreSQL state backend using PgStateStore."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._store = None

    @property
    def name(self) -> str:
        return "postgresql"

    def _get_store(self):
        if self._store is None:
            from axiom.infra.state_pg import PgStateStore
            self._store = PgStateStore(self._dsn)
        return self._store

    @contextmanager
    def open(
        self, path: str, *, exclusive: bool = False,
    ) -> Generator[StateHandle, None, None]:
        store = self._get_store()
        with store.open(path, exclusive=exclusive) as handle:
            yield handle

    def read(self, path: str) -> Any:
        return self._get_store().read(path)

    def write(self, path: str, data: Any) -> None:
        self._get_store().write(path, data)


# ===========================================================================
# HybridStateStore — tries PostgreSQL, falls back to flat files
# ===========================================================================

# Module-level singleton
_store: HybridStateStore | None = None


class HybridStateStore:
    """Hybrid state store with automatic backend selection.

    Resolution order:
    1. If NEUTRON_STATE_BACKEND=file → always use flat files
    2. If NEUTRON_STATE_BACKEND=postgresql → always use PostgreSQL (fail if unavailable)
    3. If NEUTRON_STATE_DSN or DATABASE_URL is set → try PostgreSQL, fall back to file
    4. Otherwise → flat files
    """

    def __init__(
        self,
        root: Path | None = None,
        dsn: str | None = None,
        backend: str | None = None,
    ):
        self._root = root
        self._dsn = dsn
        self._forced_backend = backend
        self._backend: StateBackend | None = None
        self._fallback: FileStateBackend | None = None

    def _resolve_root(self) -> Path:
        if self._root is not None:
            return self._root
        from axiom import REPO_ROOT
        return REPO_ROOT

    def get_backend(self) -> StateBackend:
        """Resolve and cache the active backend."""
        if self._backend is not None:
            return self._backend

        root = self._resolve_root()
        forced = self._forced_backend or os.environ.get("NEUTRON_STATE_BACKEND", "")
        dsn = self._dsn or os.environ.get(
            "NEUTRON_STATE_DSN", os.environ.get("DATABASE_URL", ""),
        )

        self._fallback = FileStateBackend(root)

        if forced == "file":
            logger.debug("State backend: file (forced)")
            self._backend = self._fallback
        elif forced == "postgresql":
            pg = self._try_pg(dsn)
            if pg is None:
                raise RuntimeError(
                    "NEUTRON_STATE_BACKEND=postgresql but PostgreSQL is unavailable. "
                    "Set NEUTRON_STATE_DSN or install psycopg."
                )
            self._backend = pg
        elif dsn:
            pg = self._try_pg(dsn)
            if pg is not None:
                self._backend = pg
                logger.info("State backend: postgresql (auto-detected)")
            else:
                logger.info("State backend: file (postgresql unavailable, falling back)")
                self._backend = self._fallback
        else:
            logger.debug("State backend: file (default)")
            self._backend = self._fallback

        return self._backend

    def _try_pg(self, dsn: str) -> PgStateBackend | None:
        if not dsn:
            return None
        try:
            pg = PgStateBackend(dsn)
            pg._get_store().ensure_schema()
            return pg
        except Exception as exc:
            logger.debug("PostgreSQL unavailable: %s", exc)
            return None

    @contextmanager
    def open(
        self, path: str, *, exclusive: bool = False,
    ) -> Generator[StateHandle, None, None]:
        backend = self.get_backend()
        with backend.open(path, exclusive=exclusive) as handle:
            yield handle

    def read(self, path: str) -> Any:
        return self.get_backend().read(path)

    def write(self, path: str, data: Any) -> None:
        self.get_backend().write(path, data)

    @property
    def backend_name(self) -> str:
        return self.get_backend().name


def get_state_store(
    root: Path | None = None,
    dsn: str | None = None,
    backend: str | None = None,
) -> HybridStateStore:
    """Get or create the module-level HybridStateStore singleton."""
    global _store
    if _store is None:
        _store = HybridStateStore(root=root, dsn=dsn, backend=backend)
    return _store


def reset_state_store() -> None:
    """Reset the singleton (for testing)."""
    global _store
    _store = None


# ===========================================================================
# State Location Registry
# ===========================================================================

@dataclass(frozen=True)
class StateLocation:
    """Metadata about a known state storage location."""

    path: str  # Relative to project root
    category: str  # runtime | config | documents | corrections | sessions
    description: str
    sensitivity: str  # low | medium | high | critical
    retention_key: str | None = None  # Key in retention.yaml, or None = indefinite
    glob_pattern: str = "*"  # For scanning directories


STATE_LOCATIONS: list[StateLocation] = [
    # Runtime — ephemeral, has retention
    StateLocation("runtime/inbox/raw/voice", "runtime", "Voice memo audio files", "medium", "raw_voice", "*.m4a"),
    StateLocation("runtime/inbox/raw/gitlab", "runtime", "GitLab export JSON files", "low", "raw_signals", "*.json"),
    StateLocation("runtime/inbox/raw/teams", "runtime", "Teams transcript files", "high", "raw_signals", "*.json"),
    StateLocation("runtime/inbox/raw/teams_chat", "runtime", "Teams chat export files", "high", "raw_signals", "*.json"),
    StateLocation("runtime/inbox/processed", "runtime", "Processed transcripts and signals", "high", "transcripts"),
    StateLocation("runtime/inbox/state", "runtime", "Briefing and sync state", "medium"),

    # Configuration — indefinite, critical
    StateLocation("runtime/config/people.md", "config", "Team roster with aliases", "medium"),
    StateLocation("runtime/config/initiatives.md", "config", "Active initiatives list", "low"),
    StateLocation("runtime/config/llm-providers.toml", "config", "LLM provider configuration", "low"),
    StateLocation("runtime/config/facility.toml", "config", "Facility metadata", "low"),

    # Documents — publisher lifecycle
    StateLocation(".publisher-registry.json", "documents", "Published doc URL mappings", "medium"),
    StateLocation(".publisher-state.json", "documents", "Document lifecycle state", "medium"),
    StateLocation("runtime/drafts", "documents", "Generated drafts", "low", "drafts", "*.md"),

    # Corrections — learned preferences (indefinite)
    StateLocation("runtime/inbox/corrections/review_state.json", "corrections", "Correction review progress", "low"),
    StateLocation("runtime/inbox/corrections/user_glossary.json", "corrections", "Learned transcription corrections", "low"),
    StateLocation("runtime/inbox/corrections/propagation_queue.json", "corrections", "Pending correction propagations", "low"),

    # Sessions
    StateLocation("runtime/sessions", "sessions", "Chat session history", "high", "sessions", "*.json"),

    # Credentials (user-scoped, ~/.neut/credentials/)
    StateLocation("~/.neut/credentials/teams", "credentials", "Teams browser session cookies", "critical"),
]
