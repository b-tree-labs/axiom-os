# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LocalStorageProvider — filesystem-based storage for testing and air-gapped use."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.storage.base import StorageEntry, StorageProvider, UploadResult


class LocalStorageProvider(StorageProvider):
    """Filesystem-based storage provider."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.base_dir = Path(config.get("base_dir", "storage"))
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def upload(
        self,
        local_path: Path,
        destination: str | None = None,
        metadata: dict | None = None,
    ) -> UploadResult:
        dest_name = destination or local_path.name
        metadata = metadata or {}
        dest_path = self.base_dir / dest_name
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_path), str(dest_path))

        storage_id = str(dest_path.relative_to(self.base_dir))
        return UploadResult(
            storage_id=storage_id,
            canonical_url=dest_path.resolve().as_uri(),
            version=metadata.get("version", "v1"),
            metadata={"local_path": str(dest_path)},
        )

    def download(self, storage_id: str, local_path: Path) -> Path:
        source = self.base_dir / storage_id
        if not source.exists():
            raise FileNotFoundError(f"Artifact not found: {storage_id}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(local_path))
        return local_path

    def move(self, source: str, destination: str) -> UploadResult:
        source_path = self.base_dir / source
        if not source_path.exists():
            raise FileNotFoundError(f"Artifact not found: {source}")
        dest = self.base_dir / destination
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(dest))
        return UploadResult(
            storage_id=str(dest.relative_to(self.base_dir)),
            canonical_url=dest.resolve().as_uri(),
        )

    def get_canonical_url(self, storage_id: str) -> str:
        return (self.base_dir / storage_id).resolve().as_uri()

    def list_artifacts(self, folder: str = "") -> list[StorageEntry]:
        prefix_path = self.base_dir / folder if folder else self.base_dir
        if not prefix_path.exists():
            return []

        search_path = prefix_path if prefix_path.is_dir() else prefix_path.parent
        entries = []
        for path in sorted(search_path.rglob("*")):
            if path.is_file():
                stat = path.stat()
                entries.append(
                    StorageEntry(
                        storage_id=str(path.relative_to(self.base_dir)),
                        name=path.name,
                        size_bytes=stat.st_size,
                        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                        url=path.resolve().as_uri(),
                    )
                )
        return entries

    def delete(self, storage_id: str) -> bool:
        path = self.base_dir / storage_id
        if path.exists():
            path.unlink()
            return True
        return False
