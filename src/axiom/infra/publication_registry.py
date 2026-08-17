# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Publication Registry — shared state between PRESS and SCAN.

Tracks the relationship between source .md files and their published
.docx counterparts on OneDrive/Box/etc. PRESS writes entries on publish;
SCAN reads them to detect divergence.

Single source of truth at .neut/publisher/publications.json.

Usage:
    from axiom.infra.publication_registry import PublicationRegistry

    registry = PublicationRegistry()

    # PRESS records a publication
    registry.record_publication(
        doc_id="prd-executive",
        source_path="docs/prds/prd-executive.md",
        source_hash="sha256:abc123...",
        published_name="prd-executive.docx",
        published_hash="sha256:def456...",
        endpoint="onedrive",
        endpoint_folder="consumer/requirements",
        endpoint_modified="2026-03-16T18:35:00Z",
    )

    # SCAN checks for divergence
    for pub in registry.all():
        if pub.endpoint_modified != current_onedrive_modified:
            # Someone edited the doc on OneDrive!
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.hash_utils import fingerprint_file
from axiom.infra.state import LockedJsonFile

_REGISTRY_PATH = _REPO_ROOT / ".neut" / "publisher" / "publications.json"


@dataclass
class PublicationRecord:
    """A single published document's state."""

    doc_id: str                      # Unique identifier (e.g., "prd-executive")
    source_path: str                 # Relative to repo root (e.g., "docs/prds/prd-executive.md")
    source_hash: str = ""            # SHA-256 of source .md content at publish time
    published_name: str = ""         # Filename on endpoint (e.g., "prd-executive.docx")
    published_hash: str = ""         # SHA-256 of generated .docx at publish time
    published_at: str = ""           # ISO timestamp of publication
    endpoint: str = ""               # "onedrive" | "box" | "local"
    endpoint_folder: str = ""        # Folder path on endpoint
    endpoint_modified: str = ""      # Endpoint's modified timestamp at publish time
    endpoint_item_id: str = ""       # Endpoint-specific item ID (stable across renames)
    endpoint_url: str = ""           # Web URL to the published doc
    published_by: str = ""           # Who published (user name)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublicationRecord:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class PublicationRegistry:
    """Manages the registry of published documents.

    Thread-safe via LockedJsonFile. Shared between PRESS (writes) and SCAN (reads).
    """

    def __init__(self, path: Path | None = None):
        self._path = path or _REGISTRY_PATH
        self._records: dict[str, PublicationRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        data: dict = {}
        try:
            with LockedJsonFile(self._path) as f:
                data = f.read()
            for item in data.get("publications", []):
                rec = PublicationRecord.from_dict(item)
                self._records[rec.doc_id] = rec
        except Exception:
            self._records = {}

    def _save(self) -> None:
        data = {
            "version": "1.0",
            "updated_at": datetime.now(UTC).isoformat(),
            "publications": [r.to_dict() for r in self._records.values()],
        }
        with LockedJsonFile(self._path, exclusive=True) as f:
            f.write(data)

    def record_publication(
        self,
        doc_id: str,
        source_path: str,
        published_name: str,
        endpoint: str = "onedrive",
        endpoint_folder: str = "",
        endpoint_modified: str = "",
        endpoint_item_id: str = "",
        endpoint_url: str = "",
        source_hash: str = "",
        published_hash: str = "",
    ) -> PublicationRecord:
        """Record a publication event. Called by PRESS after successful upload."""
        # Compute source hash if not provided
        if not source_hash:
            source_file = _REPO_ROOT / source_path
            if source_file.exists():
                source_hash = _hash_file(source_file)

        # Compute published hash if not provided
        if not published_hash:
            # Look for generated .docx
            try:
                rel = Path(source_path).parent.relative_to("docs")
                gen_path = _REPO_ROOT / ".neut" / "generated" / "docs" / rel / published_name
                if gen_path.exists():
                    published_hash = _hash_file(gen_path)
            except Exception:
                pass

        # Get publisher identity
        published_by = ""
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore
            published_by = SettingsStore().get("user.name", "")
        except Exception:
            pass

        record = PublicationRecord(
            doc_id=doc_id,
            source_path=source_path,
            source_hash=source_hash,
            published_name=published_name,
            published_hash=published_hash,
            published_at=datetime.now(UTC).isoformat(),
            endpoint=endpoint,
            endpoint_folder=endpoint_folder,
            endpoint_modified=endpoint_modified,
            endpoint_item_id=endpoint_item_id,
            endpoint_url=endpoint_url,
            published_by=published_by,
        )

        self._records[doc_id] = record
        self._save()
        return record

    def get(self, doc_id: str) -> PublicationRecord | None:
        return self._records.get(doc_id)

    def get_by_name(self, published_name: str) -> PublicationRecord | None:
        for rec in self._records.values():
            if rec.published_name == published_name:
                return rec
        return None

    def all(self) -> list[PublicationRecord]:
        return list(self._records.values())

    def published_names(self) -> set[str]:
        return {r.published_name for r in self._records.values()}

    def check_source_divergence(self, doc_id: str) -> bool:
        """Check if the source .md has changed since last publication."""
        rec = self._records.get(doc_id)
        if not rec or not rec.source_hash:
            return False
        source_file = _REPO_ROOT / rec.source_path
        if not source_file.exists():
            return True
        return _hash_file(source_file) != rec.source_hash

    def remove(self, doc_id: str) -> bool:
        if doc_id in self._records:
            del self._records[doc_id]
            self._save()
            return True
        return False


def _hash_file(path: Path) -> str:
    """SHA-256 hash of file content."""
    return f"sha256:{fingerprint_file(path)}"
