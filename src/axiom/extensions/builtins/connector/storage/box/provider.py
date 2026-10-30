# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``BoxStorageProvider``: the Box REST client behind ``StorageConnectorProvider``.

A ``FileRef.path`` is a Box id (a folder id for :meth:`list_files`, a file id
for :meth:`get_file`), and ``hints`` carry what Box states about the file: its
name, the folder path it sits in, ``sha1``, ``etag`` and ``modified_at``.

Phase 1 of ADR-220 is read-side: LIST, READ, CHECKSUM, CHANGE_CURSOR (every
listed file carries the etag a later pass compares against) and LOGIN (an
interactive OAuth flow exists for this vendor). WRITE joins when the off-site
backup and drop-target callers switch to this provider (phase 2); until then
:meth:`put_file` says so rather than half-working.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from axiom.infra.paths import get_user_state_dir

from ..protocol import (
    FileContent,
    FileRef,
    ListParams,
    PutReceipt,
    ReplyParams,
    StorageCapability,
    WatchParams,
)
from . import KIND
from .auth import AUTH_MODES, auth_from_ref

#: Fields read off each listed entry (Box returns only a few unless asked).
_FIELDS = "type,id,name,size,sha1,etag,modified_at,path_collection"
_PAGE = 1000


class BoxStorageProvider:
    """One Box connector: a root folder, a credential, and the REST client."""

    vendor = KIND
    capabilities = frozenset(
        {
            StorageCapability.LIST,
            StorageCapability.READ,
            StorageCapability.CHECKSUM,
            StorageCapability.CHANGE_CURSOR,
            StorageCapability.LOGIN,
        }
    )
    auth_modes = AUTH_MODES

    def __init__(self, *, client: Any, folder_id: str = "0", name: str = "") -> None:
        self._client = client
        self.folder_id = str(folder_id or "0")
        self.name = name

    # ---- protocol ---------------------------------------------------------

    def list_files(self, params: ListParams) -> Iterator[FileRef]:
        """Files under ``params.folder`` (a folder id; empty means the connector's root)."""
        count = 0
        stack = [str(params.folder or self.folder_id)]
        while stack:
            folder = stack.pop(0)
            for entry in self._items(folder):
                kind = entry.get("type")
                if kind == "folder":
                    if params.recursive:
                        stack.append(str(entry["id"]))
                    continue
                if kind != "file":
                    continue
                yield self._ref(entry)
                count += 1
                if params.max_items and count >= params.max_items:
                    return

    def get_file(self, ref: FileRef) -> FileContent:
        data = self._client.get_bytes(f"/files/{ref.path}/content")
        media = (ref.hints or {}).get("media_type")
        return FileContent(data=data or b"", media_type=media)

    def put_file(self, ref: FileRef, content: FileContent) -> PutReceipt:
        raise NotImplementedError(
            f"connector {self.name or self.vendor!r}: write is not in this provider's "
            f"capabilities yet (ADR-220 phase 2)"
        )

    def start_watch(self, params: WatchParams):
        raise NotImplementedError(f"connector {self.name or self.vendor!r} does not support watch")

    def ingest_replies(self, params: ReplyParams):
        raise NotImplementedError(f"connector {self.name or self.vendor!r} has no reply feed")

    # ---- manager hooks ----------------------------------------------------

    def probe(self) -> tuple[bool, str]:
        """Can this connector read its root folder with its credential?"""
        try:
            meta = self._client.get_json(f"/folders/{self.folder_id}", {"fields": "id,name"}) or {}
        except Exception as exc:  # noqa: BLE001 - the reason is the probe result
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"folder {meta.get('name') or self.folder_id} is readable"

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    # ---- internals --------------------------------------------------------

    def _items(self, folder: str) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            page = (
                self._client.get_json(
                    f"/folders/{folder}/items",
                    {"fields": _FIELDS, "limit": _PAGE, "offset": offset},
                )
                or {}
            )
            entries = page.get("entries") or []
            yield from entries
            offset += len(entries)
            if not entries or offset >= int(page.get("total_count") or 0):
                return

    def _ref(self, entry: dict[str, Any]) -> FileRef:
        parents = [
            p.get("name", "") for p in (entry.get("path_collection") or {}).get("entries", [])
        ]
        hints = {
            "name": entry.get("name"),
            "folder_path": "/" + "/".join(p for p in parents if p),
            "size": entry.get("size"),
            "sha1": entry.get("sha1"),
            "etag": entry.get("etag"),
            "modified_at": entry.get("modified_at"),
        }
        return FileRef(
            vendor=self.vendor,
            path=str(entry["id"]),
            hints={k: v for k, v in hints.items() if v is not None},
        )


def build(
    *,
    name: str = "",
    folder_id: str = "0",
    credential_ref: str | None = None,
    jwt_secret_ref: str | None = None,
    session_dir: str | None = None,
    client: Any = None,
    **_: Any,
) -> BoxStorageProvider:
    """The registered factory: a connector's settings -> a provider.

    ``credential_ref`` is the platform-generic field; ``jwt_secret_ref`` is the
    key existing Box connector files carry, read as the same thing. ``client`` is
    for tests and for a caller that already holds one.
    """
    if client is None:
        from .session_api import BoxSessionApiClient

        auth = auth_from_ref(credential_ref or jwt_secret_ref)
        where = (
            Path(session_dir).expanduser()
            if session_dir
            else (get_user_state_dir() / "credentials" / KIND)
        )
        client = BoxSessionApiClient(session_dir=where, jwt_auth=auth)
    return BoxStorageProvider(client=client, folder_id=folder_id, name=name)


__all__ = ["BoxStorageProvider", "build"]
