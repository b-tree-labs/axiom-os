# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session manager for memory provenance — spec-memory §3.7.

A session is the unit of "things you did together" — the CLI / chat /
process invocation that wrote a fragment. Sessions exist so the read
side can apply MIRIX-type-aware default scope rules: episodic stays
session-bound; core / procedural / resource cross sessions; semantic
crosses by relevance.

This module owns:

- The session id + name shape (immutable id + renameable display name)
- The on-disk registry under ``~/.axi/sessions/`` (one JSON file per
  session — greppable, append-only)
- The process-local "current session" resolution used by
  :class:`axiom.memory.composition.CompositionService.write` when no
  explicit ``session_id`` is supplied

The registry is **operational metadata**, not a memorable fragment. It
does not flow through CompositionService and is never federated.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.paths import get_user_state_dir

SESSION_URI_PREFIX = "session://"
DEFAULT_AUTO_RESUME_WINDOW_HOURS = 4


@dataclass(frozen=True)
class SessionMetadata:
    """One row in the session registry. Spec-memory §3.7.4."""

    session_id: str
    name: str
    principal_id: str
    created_at: str
    last_active_at: str
    cwd_hint: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "principal_id": self.principal_id,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "cwd_hint": self.cwd_hint,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionMetadata":
        return cls(
            session_id=data["session_id"],
            name=data["name"],
            principal_id=data["principal_id"],
            created_at=data["created_at"],
            last_active_at=data.get("last_active_at", data["created_at"]),
            cwd_hint=data.get("cwd_hint", ""),
            note=data.get("note", ""),
        )


def _sessions_dir() -> Path:
    d = get_user_state_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _name_for_cwd(cwd: Path) -> str:
    """Auto-name: ``<cwd-basename>-<YYYY-MM-DD-HHMM>``. Spec-memory §3.7.1."""
    base = cwd.name or "axi"
    # Filesystem-safe: only [a-z0-9-_].
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-").lower() or "axi"
    ts = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    return f"{base}-{ts}"


def _new_session_id() -> str:
    """Stable URI — UUIDv4 (we don't need ordering)."""
    return f"{SESSION_URI_PREFIX}{uuid.uuid4()}"


def _registry_path(session_id: str) -> Path:
    # Use the uuid portion as the filename so paths are short + greppable.
    short = session_id.removeprefix(SESSION_URI_PREFIX)
    return _sessions_dir() / f"{short}.json"


def _name_index_path() -> Path:
    """Lookup index name → session_id (lazy; rebuilt from disk on miss)."""
    return _sessions_dir() / ".by-name.json"


def _load_metadata(session_id: str) -> SessionMetadata | None:
    p = _registry_path(session_id)
    if not p.exists():
        return None
    try:
        return SessionMetadata.from_dict(json.loads(p.read_text()))
    except (json.JSONDecodeError, KeyError):
        return None


def _save_metadata(meta: SessionMetadata) -> None:
    _registry_path(meta.session_id).write_text(json.dumps(meta.to_dict(), indent=2))
    # Best-effort name index refresh — never blocks a write.
    try:
        _refresh_name_index()
    except OSError:
        pass


def _refresh_name_index() -> None:
    index: dict[str, str] = {}
    for f in _sessions_dir().glob("*.json"):
        if f.name.startswith("."):
            continue
        try:
            m = SessionMetadata.from_dict(json.loads(f.read_text()))
            index[m.name] = m.session_id
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    _name_index_path().write_text(json.dumps(index, indent=2))


def list_sessions(principal_id: str | None = None) -> list[SessionMetadata]:
    """Return all sessions, optionally filtered by principal."""
    out: list[SessionMetadata] = []
    for f in sorted(_sessions_dir().glob("*.json")):
        if f.name.startswith("."):
            continue
        try:
            m = SessionMetadata.from_dict(json.loads(f.read_text()))
        except (json.JSONDecodeError, KeyError):
            continue
        if principal_id is None or m.principal_id == principal_id:
            out.append(m)
    out.sort(key=lambda m: m.last_active_at, reverse=True)
    return out


def find_by_name(name: str) -> SessionMetadata | None:
    """Look up a session by its display name."""
    idx_path = _name_index_path()
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text())
            sid = idx.get(name)
            if sid:
                meta = _load_metadata(sid)
                if meta is not None:
                    return meta
        except (json.JSONDecodeError, OSError):
            pass
    # Fall back to a scan (index may be stale).
    for m in list_sessions():
        if m.name == name:
            return m
    return None


def resolve(name_or_id: str) -> SessionMetadata | None:
    """Look up by either display name or session_id / bare uuid."""
    if name_or_id.startswith(SESSION_URI_PREFIX):
        return _load_metadata(name_or_id)
    # Try as session_id first (URI- and bare-uuid forms both OK).
    candidate = name_or_id if name_or_id.startswith(SESSION_URI_PREFIX) else (
        f"{SESSION_URI_PREFIX}{name_or_id}"
    )
    meta = _load_metadata(candidate)
    if meta is not None:
        return meta
    return find_by_name(name_or_id)


def create_session(
    principal_id: str,
    *,
    name: str | None = None,
    cwd: Path | None = None,
    note: str = "",
) -> SessionMetadata:
    """Create a new session and persist it. Spec-memory §3.7.2."""
    cwd = cwd or Path.cwd()
    auto_name = name or _name_for_cwd(cwd)
    # Disambiguate name collisions deterministically.
    if find_by_name(auto_name) is not None:
        suffix = uuid.uuid4().hex[:4]
        auto_name = f"{auto_name}-{suffix}"
    now = datetime.now(UTC).isoformat()
    meta = SessionMetadata(
        session_id=_new_session_id(),
        name=auto_name,
        principal_id=principal_id,
        created_at=now,
        last_active_at=now,
        cwd_hint=cwd.name,
        note=note,
    )
    _save_metadata(meta)
    return meta


def rename(session_id: str, new_name: str) -> SessionMetadata:
    """Rename a session. session_id is immutable; the display name changes."""
    meta = _load_metadata(session_id)
    if meta is None:
        raise KeyError(f"unknown session: {session_id}")
    if find_by_name(new_name) is not None:
        raise ValueError(f"name already in use: {new_name}")
    updated = replace(meta, name=new_name)
    _save_metadata(updated)
    return updated


def touch(session_id: str) -> None:
    """Update last_active_at for a session. Called by the write path."""
    meta = _load_metadata(session_id)
    if meta is None:
        return
    updated = replace(meta, last_active_at=datetime.now(UTC).isoformat())
    _save_metadata(updated)


# ---------------------------------------------------------------------------
# Process-local "current session" resolution
# ---------------------------------------------------------------------------
#
# The active session for the current process is held in a thread-local.
# Resolution order, on first call:
#
#   1. ``AXI_SESSION_ID`` env var (explicit override; chat / agent
#      surfaces inject this when they spawn subprocesses).
#   2. Auto-resume: most recent session for ``(principal_id, cwd basename)``
#      whose ``last_active_at`` is within the auto-resume window.
#   3. Create a fresh session with the auto-generated name.
#
# The chosen session sticks for the lifetime of the process unless
# :func:`use_session` rebinds it.


_local = threading.local()


def current_principal_id() -> str:
    """Best-effort principal resolution. The full identity stack lives
    in ``axiom.identity``; here we just need a stable per-principal
    bucket for the session registry."""
    pid = os.environ.get("AXI_PRINCIPAL_ID")
    if pid:
        return pid
    return f"@{os.environ.get('USER', 'local')}:local"


def _auto_resume_window_seconds() -> float:
    raw = os.environ.get("AXI_SESSION_AUTO_RESUME_HOURS")
    try:
        hours = float(raw) if raw else DEFAULT_AUTO_RESUME_WINDOW_HOURS
    except ValueError:
        hours = DEFAULT_AUTO_RESUME_WINDOW_HOURS
    return hours * 3600.0


def _autobind_candidate(principal_id: str, cwd: Path) -> SessionMetadata | None:
    """Pick the most recent session in the auto-resume window for this
    (principal, cwd) pair."""
    cutoff = datetime.now(UTC).timestamp() - _auto_resume_window_seconds()
    target_cwd = cwd.name
    candidates = [
        m for m in list_sessions(principal_id=principal_id)
        if m.cwd_hint == target_cwd
    ]
    for m in candidates:
        try:
            ts = datetime.fromisoformat(m.last_active_at).timestamp()
        except ValueError:
            continue
        if ts >= cutoff:
            return m
    return None


def _resolve_initial_session() -> SessionMetadata:
    # 1. Explicit override.
    override = os.environ.get("AXI_SESSION_ID")
    if override:
        # Accept either the URI form or bare uuid.
        if not override.startswith(SESSION_URI_PREFIX):
            override = f"{SESSION_URI_PREFIX}{override}"
        meta = _load_metadata(override)
        if meta is not None:
            return meta

    principal_id = current_principal_id()
    cwd = Path.cwd()

    # 2. Auto-resume.
    candidate = _autobind_candidate(principal_id, cwd)
    if candidate is not None:
        return candidate

    # 3. Fresh session.
    return create_session(principal_id, cwd=cwd)


def _session_disabled() -> bool:
    """Skip auto-session resolution in test contexts and when explicitly
    disabled. Tests that want to exercise sessions must set
    ``AXI_DISABLE_SESSION=0`` *and* point ``AXI_STATE_DIR`` at a
    tmp_path-scoped directory."""
    if os.environ.get("AXI_DISABLE_SESSION") == "1":
        return True
    if os.environ.get("AXI_DISABLE_SESSION") == "0":
        return False
    return "PYTEST_CURRENT_TEST" in os.environ


def current_session() -> SessionMetadata | None:
    """Return the active session for this process. Resolves on first call.

    Returns None when session resolution is disabled (test contexts and
    ``AXI_DISABLE_SESSION=1``). Callers should treat ``None`` the same
    as "no session active" and write the fragment with
    ``session_id=""``.
    """
    cached = getattr(_local, "session", None)
    if cached is not None:
        return cached
    if _session_disabled():
        return None
    meta = _resolve_initial_session()
    _local.session = meta
    return meta


def current_session_id() -> str:
    """Return only the session_id of the active session, or ``""`` when
    no session is active (test contexts; explicit disable)."""
    meta = current_session()
    return meta.session_id if meta is not None else ""


def use_session(session_id_or_name: str) -> SessionMetadata:
    """Rebind the current process to an existing session."""
    meta = resolve(session_id_or_name)
    if meta is None:
        raise KeyError(f"unknown session: {session_id_or_name}")
    _local.session = meta
    return meta


def new_session(name: str | None = None) -> SessionMetadata:
    """Create a fresh session and rebind the current process to it."""
    meta = create_session(current_principal_id(), name=name)
    _local.session = meta
    return meta


def reset_for_tests() -> None:
    """Test helper — clear the process-local cache. Production code
    should not call this."""
    if hasattr(_local, "session"):
        del _local.session
