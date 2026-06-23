# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Document state persistence — tracks lifecycle status for all documents.

Persists to .neut/publisher/publisher-state.json. Each document's state includes
its lifecycle position, publication records, and pending feedback.

Uses LockedJsonFile for safe concurrent access across agents.
"""

from __future__ import annotations

# Import directly from the models.py file (avoid models/ package conflict)
from dataclasses import dataclass, field
from pathlib import Path

from axiom.infra.state import LockedJsonFile


# Copy the necessary classes from models.py to avoid circular imports
@dataclass
class PublicationRecord:
    """Record of a document's publication to storage."""

    storage_id: str  # e.g., "medical-isotope-prd.docx" or SharePoint asset ID
    url: str  # URL or file path where the document is stored
    version: str = "v1.0.0"  # Semantic version: v{major}.{minor}.{patch}
    published_at: str = ""  # ISO 8601 timestamp
    commit_sha: str = ""  # Git commit SHA when published
    generation_provider: str = ""  # e.g., "pandoc-docx"
    storage_provider: str = ""  # e.g., "local", "sharepoint", "onedrive"
    artifact_hash: str = ""  # SHA256 hash of the generated artifact (for no-op detection)

    def to_dict(self) -> dict:
        return {
            "artifact_hash": self.artifact_hash,
            "commit_sha": self.commit_sha,
            "generation_provider": self.generation_provider,
            "published_at": self.published_at,
            "storage_id": self.storage_id,
            "storage_provider": self.storage_provider,
            "url": self.url,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PublicationRecord:
        return cls(
            storage_id=data.get("storage_id", ""),
            url=data.get("url", ""),
            version=data.get("version", "v1.0.0"),
            published_at=data.get("published_at", ""),
            commit_sha=data.get("commit_sha", ""),
            generation_provider=data.get("generation_provider", ""),
            storage_provider=data.get("storage_provider", ""),
            artifact_hash=data.get("artifact_hash", ""),
        )


@dataclass
class DocumentState:
    """Complete lifecycle state for a single document."""

    doc_id: str
    source_path: str  # Relative path from repo root
    status: str = "draft"  # "draft", "published", "orphan"
    published: PublicationRecord | None = None
    active_draft: PublicationRecord | None = None  # Current draft being edited
    draft_history: list = field(default_factory=list)
    pending_comments: list[dict] = field(default_factory=list)
    stakeholders: list[str] = field(default_factory=list)
    last_branch: str = ""  # Git branch where last published
    last_commit: str = ""  # Git commit SHA of last publication

    def to_dict(self) -> dict:
        def _rec_to_dict(r):
            if r is None:
                return None
            return r.to_dict() if isinstance(r, PublicationRecord) else r

        return {
            "active_draft": _rec_to_dict(self.active_draft),
            "doc_id": self.doc_id,
            "draft_history": [_rec_to_dict(h) for h in self.draft_history],
            "last_branch": self.last_branch,
            "last_commit": self.last_commit,
            "pending_comments": self.pending_comments,
            "published": self.published.to_dict() if self.published else None,
            "source_path": self.source_path,
            "stakeholders": self.stakeholders,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DocumentState:
        published_data = data.get("published")
        published = PublicationRecord.from_dict(published_data) if published_data else None

        draft_data = data.get("active_draft")
        active_draft = PublicationRecord.from_dict(draft_data) if isinstance(draft_data, dict) and draft_data else None

        raw_history = data.get("draft_history", [])
        draft_history = [
            PublicationRecord.from_dict(h) if isinstance(h, dict) else h
            for h in raw_history
        ]

        return cls(
            doc_id=data.get("doc_id", ""),
            source_path=data.get("source_path", ""),
            status=data.get("status", "draft"),
            published=published,
            active_draft=active_draft,
            draft_history=draft_history,
            pending_comments=data.get("pending_comments", []),
            stakeholders=data.get("stakeholders", []),
            last_branch=data.get("last_branch", ""),
            last_commit=data.get("last_commit", ""),
        )


@dataclass
class Comment:
    """Reviewer comment extracted from a published artifact."""

    comment_id: str
    author: str
    timestamp: str  # ISO 8601
    text: str
    context: str | None = None  # Text range the comment is anchored to
    resolved: bool = False
    replies: list[Comment] = field(default_factory=list)
    source: str = ""  # Provider name that produced this comment

    def to_dict(self) -> dict:
        return {
            "comment_id": self.comment_id,
            "author": self.author,
            "timestamp": self.timestamp,
            "text": self.text,
            "context": self.context,
            "resolved": self.resolved,
            "replies": [r.to_dict() for r in self.replies],
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Comment:
        replies = [cls.from_dict(r) for r in data.get("replies", [])]
        return cls(
            comment_id=data.get("comment_id", ""),
            author=data.get("author", ""),
            timestamp=data.get("timestamp", ""),
            text=data.get("text", ""),
            context=data.get("context"),
            resolved=data.get("resolved", False),
            replies=replies,
            source=data.get("source", ""),
        )


@dataclass
class LinkEntry:
    """Registry entry mapping a document to its published URL."""

    doc_id: str  # e.g., "experiment-manager-prd"
    source_path: str  # e.g., "docs/prds/prd-experiment-manager.md"
    published_url: str  # From StorageProvider.get_canonical_url()
    draft_url: str | None = None
    storage_id: str = ""  # Provider-specific reference
    last_published: str = ""  # ISO 8601
    version: str = "v1"
    commit_sha: str = ""

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "source_path": self.source_path,
            "published_url": self.published_url,
            "draft_url": self.draft_url,
            "storage_id": self.storage_id,
            "last_published": self.last_published,
            "version": self.version,
            "commit_sha": self.commit_sha,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LinkEntry:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class StateStore:
    """Manages document lifecycle state persistence."""

    def __init__(self, state_path: Path):
        self.path = state_path
        self.documents: dict[str, DocumentState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data: dict = {}
        try:
            with LockedJsonFile(self.path) as f:
                data = f.read()
            for item in data.get("documents", []):
                doc = DocumentState.from_dict(item)
                self.documents[doc.doc_id] = doc
        except (KeyError, OSError):
            pass

    def save(self) -> None:
        """Persist state to disk with exclusive lock and atomic write."""
        data = {
            "documents": [d.to_dict() for d in self.documents.values()],
        }
        with LockedJsonFile(self.path, exclusive=True) as f:
            f.write(data)

    def get(self, doc_id: str) -> DocumentState | None:
        """Get document state by ID."""
        return self.documents.get(doc_id)

    def get_by_path(self, source_path: str) -> DocumentState | None:
        """Get document state by source path."""
        for doc in self.documents.values():
            if doc.source_path == source_path:
                return doc
        return None

    def update(self, doc_state: DocumentState) -> None:
        """Add or update a document state."""
        self.documents[doc_state.doc_id] = doc_state
        self.save()

    def remove(self, doc_id: str) -> bool:
        """Remove a document from state tracking."""
        if doc_id in self.documents:
            del self.documents[doc_id]
            self.save()
            return True
        return False

    def list_by_status(self, status: str | None = None) -> list[DocumentState]:
        """List documents, optionally filtered by status."""
        if status:
            return [d for d in self.documents.values() if d.status == status]
        return list(self.documents.values())

    @property
    def count(self) -> int:
        return len(self.documents)
