# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat session persistence.

Stores conversation messages, context, and active actions as JSON files.
Each session gets its own file under the sessions directory.

Usage:
    store = SessionStore()
    session = store.create()
    session.add_message("user", "Publish the executive PRD")
    session.add_message("assistant", "I'll publish docs/prds/prd-executive.md")
    store.save(session)

    # Resume later
    session = store.load(session.session_id)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class Message:
    """A single message in a chat session."""

    role: str  # "user", "assistant", "system"
    content: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            role=d["role"],
            content=d["content"],
            timestamp=d.get("timestamp", ""),
            tool_calls=d.get("tool_calls", []),
        )


@dataclass
class Session:
    """A chat session with message history and metadata."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    messages: list[Message] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    updated_at: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    # Whose session this is. `principal_id` is the acting agent/user principal
    # (`@axi:bens`); `accountable_human_id` is the human ultimately accountable
    # (ADR-035). ChatAgent already reads `principal_id` for memory provenance —
    # these fields populate that previously-dormant seam. Empty = unbound.
    principal_id: str = ""
    accountable_human_id: str = ""

    def add_message(
        self,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> Message:
        """Add a message to the session."""
        msg = Message(role=role, content=content, tool_calls=tool_calls or [])
        self.messages.append(msg)
        self.updated_at = datetime.now(UTC).isoformat()
        # Auto-title from first user message if untitled
        if not self.title and role == "user" and content.strip():
            self.title = content.strip()[:60]
        return msg

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "title": self.title,
            "messages": [m.to_dict() for m in self.messages],
            "context": self.context,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.usage:
            d["usage"] = self.usage
        if self.principal_id:
            d["principal_id"] = self.principal_id
        if self.accountable_human_id:
            d["accountable_human_id"] = self.accountable_human_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        return cls(
            session_id=d["session_id"],
            title=d.get("title", ""),
            messages=[Message.from_dict(m) for m in d.get("messages", [])],
            context=d.get("context", {}),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            usage=d.get("usage", {}),
            principal_id=d.get("principal_id", ""),
            accountable_human_id=d.get("accountable_human_id", ""),
        )


class SessionStore:
    """Manages chat session persistence as JSON files."""

    def __init__(self, sessions_dir: Path | None = None):
        if sessions_dir is None:
            from axiom import REPO_ROOT
            sessions_dir = REPO_ROOT / "runtime" / "sessions"
        self._dir = sessions_dir

    def create(self, context: dict[str, Any] | None = None) -> Session:
        """Create and persist a new session."""
        session = Session(context=context or {})
        self.save(session)
        return session

    def save(self, session: Session) -> Path | None:
        """Save a session to disk. Skips empty sessions (no messages)."""
        if not session.messages:
            return None
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{session.session_id}.json"
        path.write_text(
            json.dumps(session.to_dict(), indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, session_id: str) -> Session | None:
        """Load a session from disk (checks archive if not in main dir)."""
        for search_dir in [self._dir, self._dir / "archive"]:
            path = search_dir / f"{session_id}.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    # If loading from archive, move it back to active
                    if search_dir.name == "archive":
                        path.rename(self._dir / f"{session_id}.json")
                    return Session.from_dict(data)
                except (json.JSONDecodeError, KeyError):
                    return None
        return None

    def rename(self, session_id: str, title: str) -> bool:
        """Rename a session. Returns True on success."""
        session = self.load(session_id)
        if session is None:
            return False
        session.title = title
        self.save(session)
        return True

    def archive(self, session_id: str) -> bool:
        """Move a session to the archive directory."""
        path = self._dir / f"{session_id}.json"
        if not path.exists():
            return False
        archive_dir = self._dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        path.rename(archive_dir / path.name)
        return True

    def list_sessions(self, include_archived: bool = False) -> list[str]:
        """List all session IDs (most recent first)."""
        if not self._dir.exists():
            return []
        files = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        result = [f.stem for f in files]
        if include_archived:
            archive_dir = self._dir / "archive"
            if archive_dir.exists():
                archived = sorted(
                    archive_dir.glob("*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                result.extend(f.stem for f in archived)
        return result

    def load_meta(self, session_id: str) -> dict[str, Any] | None:
        """Load only session metadata (no full message list). Fast for listing."""
        for search_dir in [self._dir, self._dir / "archive"]:
            path = search_dir / f"{session_id}.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    return {
                        "id": data["session_id"],
                        "title": data.get("title", ""),
                        "message_count": len(data.get("messages", [])),
                        "created_at": data.get("created_at", ""),
                        "updated_at": data.get("updated_at", ""),
                        "archived": search_dir.name == "archive",
                    }
                except (json.JSONDecodeError, KeyError):
                    return None
        return None

    def cleanup_archive(self, max_age_days: int = 90) -> int:
        """Delete archived sessions older than max_age_days. Returns count deleted."""
        archive_dir = self._dir / "archive"
        if not archive_dir.exists():
            return 0
        cutoff = datetime.now(UTC).timestamp() - (max_age_days * 86400)
        deleted = 0
        for path in archive_dir.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        return deleted
