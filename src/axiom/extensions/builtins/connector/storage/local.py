# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``LocalStorageConnector`` — a directory-backed ``StorageConnectorProvider``
(ADR-062). The first implementation of the file-store seam: list / read / write
against a local folder, no cloud. The storage analogue of the LocalFileEditor —
a portability proof, and a real offline/shared-drive backend."""

from __future__ import annotations

import mimetypes
from collections.abc import Iterator
from pathlib import Path

from .protocol import (
    FileContent,
    FileRef,
    ListParams,
    PutReceipt,
    ReplyParams,
    StorageCapability,
    WatchParams,
)


class LocalStorageConnector:
    """A file-store over ``root``. Paths in ``FileRef`` are POSIX-relative to it."""

    vendor = "local"
    capabilities = frozenset(
        {StorageCapability.LIST, StorageCapability.READ, StorageCapability.WRITE}
    )

    def __init__(self, *, root: str) -> None:
        self._root = Path(root)

    def _abs(self, rel: str) -> Path:
        return self._root / rel

    def list_files(self, params: ListParams) -> Iterator[FileRef]:
        base = self._root / params.folder
        if not base.exists():
            return
        walker = base.rglob("*") if params.recursive else base.glob("*")
        count = 0
        for p in walker:
            if not p.is_file():
                continue
            yield FileRef(vendor=self.vendor, path=p.relative_to(self._root).as_posix())
            count += 1
            if params.max_items and count >= params.max_items:
                return

    def get_file(self, ref: FileRef) -> FileContent:
        p = self._abs(ref.path)
        media_type, _ = mimetypes.guess_type(p.name)
        return FileContent(data=p.read_bytes(), media_type=media_type)

    def put_file(self, ref: FileRef, content: FileContent) -> PutReceipt:
        p = self._abs(ref.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.data if content.data is not None else b"".join(content.stream())
        p.write_bytes(data)
        return PutReceipt(ref=ref, version=str(p.stat().st_mtime_ns),
                          bytes_written=len(data))

    def start_watch(self, params: WatchParams):
        raise NotImplementedError("local storage connector does not support watch")

    def ingest_replies(self, params: ReplyParams):
        raise NotImplementedError("local storage connector has no reply feed")


__all__ = ["LocalStorageConnector"]
