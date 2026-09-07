# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""StorageProvider ABC and shared data models.

Extracted from PRESS agent to be reusable across all extensions that need
object storage (Model Corral, datasets, artifacts, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class UploadResult:
    """Result from StorageProvider.upload()."""

    storage_id: str = ""
    canonical_url: str = ""
    version: str = "v1"
    success: bool = True
    error: str = ""
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.url and not self.canonical_url:
            self.canonical_url = self.url
        elif self.canonical_url and not self.url:
            self.url = self.canonical_url


@dataclass
class StorageEntry:
    """Entry returned by StorageProvider.list_artifacts()."""

    storage_id: str
    name: str
    size_bytes: int = 0
    last_modified: str = ""
    url: str = ""


class StorageProvider(ABC):
    """Abstract base for object storage backends."""

    @abstractmethod
    def upload(
        self,
        local_path: Path,
        destination: str | None = None,
        metadata: dict | None = None,
    ) -> UploadResult:
        """Upload a file to storage."""
        ...

    @abstractmethod
    def download(self, storage_id: str, local_path: Path) -> Path:
        """Download a file from storage."""
        ...

    @abstractmethod
    def move(self, source: str, destination: str) -> UploadResult:
        """Move/rename an artifact within storage."""
        ...

    @abstractmethod
    def get_canonical_url(self, storage_id: str) -> str:
        """Return the canonical URL for an artifact."""
        ...

    @abstractmethod
    def list_artifacts(self, folder: str = "") -> list[StorageEntry]:
        """List artifacts under a prefix/folder."""
        ...

    @abstractmethod
    def delete(self, storage_id: str) -> bool:
        """Delete an artifact. Returns True if deleted, False if not found."""
        ...
