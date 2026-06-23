# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LocalStorageProvider — filesystem-based storage for testing and air-gapped use.

Upload = copy to output directory. URLs = file:// paths.
No authentication required.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...factory import PublisherFactory
from ..base import (
    StorageEntry,
    StorageProvider,
    UploadResult,
)


class LocalStorageProvider(StorageProvider):
    """Filesystem-based storage provider."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        # Default output directory
        self.base_dir = Path(config.get("base_dir", "docs/_tools/generated"))
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def upload(
        self, local_path: Path, destination: str | None = None, metadata: dict | None = None,
    ) -> UploadResult:
        """Copy file to the output directory."""
        dest_name = destination or local_path.name
        metadata = metadata or {}
        dest_path = self.base_dir / dest_name
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(str(local_path), str(dest_path))

        storage_id = str(dest_path.relative_to(self.base_dir))
        url = dest_path.resolve().as_uri()

        return UploadResult(
            storage_id=storage_id,
            canonical_url=url,
            version=metadata.get("version", "v1"),
            metadata={"local_path": str(dest_path)},
        )

    def download(self, storage_id: str, local_path: Path) -> Path:
        """Copy file from the output directory."""
        source = self.base_dir / storage_id
        if not source.exists():
            raise FileNotFoundError(f"Artifact not found: {storage_id}")

        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(local_path))
        return local_path

    def move(self, source: str, destination: str) -> UploadResult:
        """Move file to a new location in the output directory."""
        source_path = self.base_dir / source
        if not source_path.exists():
            raise FileNotFoundError(f"Artifact not found: {source}")

        dest = self.base_dir / destination
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(dest))

        new_id = str(dest.relative_to(self.base_dir))
        return UploadResult(
            storage_id=new_id,
            canonical_url=dest.resolve().as_uri(),
        )

    def get_canonical_url(self, storage_id: str) -> str:
        """Return file:// URI for the artifact."""
        path = self.base_dir / storage_id
        return path.resolve().as_uri()

    def list_artifacts(self, folder: str = "") -> list[StorageEntry]:
        """List files under a folder in the output directory."""
        prefix_path = self.base_dir / folder if folder else self.base_dir
        entries = []

        if not prefix_path.exists():
            return entries

        search_path = prefix_path if prefix_path.is_dir() else prefix_path.parent
        _pattern = prefix_path.name + "*" if not prefix_path.is_dir() else "*"

        for path in sorted(search_path.rglob("*")):
            if path.is_file():
                rel = str(path.relative_to(self.base_dir))
                stat = path.stat()
                entries.append(StorageEntry(
                    storage_id=rel,
                    name=path.name,
                    size_bytes=stat.st_size,
                    last_modified=datetime.fromtimestamp(
                        stat.st_mtime, tz=UTC
                    ).isoformat(),
                    url=path.resolve().as_uri(),
                ))

        return entries

    def delete(self, storage_id: str) -> bool:
        """Delete a file from the output directory."""
        path = self.base_dir / storage_id
        if path.exists():
            path.unlink()
            return True
        return False


# Self-register with factory
PublisherFactory.register("storage", "local", LocalStorageProvider)
