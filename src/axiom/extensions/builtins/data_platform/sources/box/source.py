# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``BoxIngestSource`` — pull-oriented Box-folder source.

The Box authenticated session lives in the publishing extension
(``providers/storage/box_browser.py``); that module owns Playwright + SSO
and was built for *uploads*. This source adds the *list + download*
direction the upload-oriented browser provider lacks, against the same
authenticated session, exposed as the platform's
:class:`~axiom.extensions.builtins.data_platform.contracts.IngestSource`
protocol.

Construction takes an ``api_client``. In production this is
:class:`BoxBrowserApiClient`, which wraps the Playwright session and
issues Box REST calls in the browser context. In tests it's a stub —
unit tests for this module pass a ``FakeBoxApi``; no Playwright in the
unit-test path. See ADR-049 (portable connector contract): the same
source is driven by a Dagster sensor in heavy and a minimal runner in
lean.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class ItemMetadata:
    """Metadata-only descriptor for one file in a source.

    Produced by :meth:`BoxIngestSource.catalog` (no bytes fetched).
    Lets the caller decide before paying the byte-budget per item:

    - skip when ``etag`` matches the bronze record
    - defer when ``size`` exceeds the current budget
    - route by ``content_type`` (image PDF → OCR lane)

    See ``feedback_rag_dedup_three_tiers`` memory + lakehouse epic
    #386 Day-1 for the place this slots into the connector contract.
    """

    item_id: str
    display_name: str
    etag: str | None
    modified_at: datetime | None
    size: int | None
    content_type: str | None
    source_path: str | None


class BoxApiClient(Protocol):
    """Minimal Box REST surface the source needs.

    Methods take Box paths *without* the ``https://api.box.com/2.0``
    prefix (the client owns the host). ``get_json`` hits a JSON
    endpoint; ``get_bytes`` downloads a binary blob.
    """

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def get_bytes(self, path: str) -> bytes: ...


def _parse_iso(ts: str) -> datetime:
    """Parse a Box ISO-8601 timestamp.

    Box returns ``2026-05-29T12:00:00Z`` — Python's ``fromisoformat``
    accepts the trailing ``Z`` on 3.11+.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _content_type_for(name: str) -> str | None:
    """Guess a media type from the filename, since Box doesn't return one."""
    ct, _ = mimetypes.guess_type(name)
    return ct


def _source_path_from_box(meta: dict[str, Any], filename: str) -> str | None:
    """Render Box's ``path_collection`` as a human-readable POSIX path.

    Box prefixes every path with a synthetic ``All Files`` root; strip it
    so the recorded path matches what a user would see in the Box UI.
    """
    pc = meta.get("path_collection") or {}
    entries = pc.get("entries") or []
    parts: list[str] = []
    for entry in entries:
        n = entry.get("name")
        if not n or n == "All Files":
            continue
        parts.append(n)
    parts.append(filename)
    return "/" + "/".join(parts) if parts else None


class BoxIngestSource:
    """An :class:`IngestSource` that yields files under one Box folder.

    Recurses into subfolders. Watermark filtering is applied client-side
    against the items' ``modified_at`` — Box's ``GET /folders/{id}/items``
    does not accept a since-filter, so we paginate and filter here. For
    very large folders the heavy-tier driver (Dagster sensor) caches the
    last seen ``etag`` per item to avoid re-fetching unchanged files; the
    skeleton interface just returns the ids and lets the driver decide.
    """

    def __init__(self, *, name: str, folder_id: str, api_client: BoxApiClient,
                 cursor: Any = None) -> None:
        if not name:
            raise ValueError("BoxIngestSource requires a non-empty name")
        if not folder_id:
            raise ValueError("BoxIngestSource requires a folder_id")
        self.name = name
        self.folder_id = folder_id
        self._api = api_client
        # Optional cursor (typed as :class:`axiom.infra.connector_cursor.ConnectorCursor`).
        # When provided, ``catalog()`` filters out items whose stored etag
        # matches the cursor (already seen unchanged), and ``fetch()``
        # forwards ``If-None-Match`` + updates the cursor on success.
        # When absent, behavior is unchanged for back-compat callers.
        self._cursor = cursor

    # ---- IngestSource ---------------------------------------------------

    def catalog(self, since: datetime | None = None) -> list[ItemMetadata]:
        """Walk the folder and return metadata for every file.

        No bytes fetched. Box's ``/folders/{id}/items`` already returns
        ``id, name, etag, modified_at, size, path_collection`` per
        entry — :meth:`catalog` exposes those as :class:`ItemMetadata`
        records so the caller can dedup/route/budget before paying the
        byte-fetch token per item.

        When a cursor is attached to this source, items whose stored
        etag matches the cursor are filtered out (unchanged since last
        run). Etag-based dedup is the connector-tier of the four-tier
        scheme; see ``feedback_rag_dedup_three_tiers``.
        """
        out: list[ItemMetadata] = []
        self._walk_catalog(self.folder_id, since, out)
        if self._cursor is not None:
            out = [
                m for m in out
                if m.etag is None or self._cursor.get_etag(m.item_id) != m.etag
            ]
        return out

    def list_changed(self, since: datetime | None = None) -> list[str]:
        """Back-compat shim: ids only. Internally a projection of
        :meth:`catalog`."""
        return [m.item_id for m in self.catalog(since)]

    def fetch(self, item: str) -> FetchedItem:  # noqa: F821 (forward import)
        from ...contracts import FetchedItem

        cached_etag = (
            self._cursor.get_etag(item) if self._cursor is not None else None
        )
        # Box's get_json/get_bytes accept if_none_match when the client is
        # the rate-limit-aware variant (BoxSessionApiClient). Older clients
        # may not — guard with **kwargs so we don't break the protocol.
        meta = self._maybe_if_none_match_get_json(f"/files/{item}", cached_etag)
        content = self._maybe_if_none_match_get_bytes(
            f"/files/{item}/content", cached_etag,
        )

        declared_size = int(meta.get("size", len(content)))
        if declared_size != len(content):
            raise ValueError(
                f"Box file {item!r}: declared size {declared_size} != "
                f"downloaded size {len(content)} — short read"
            )

        filename = meta.get("name") or item
        modified_at_raw = meta.get("modified_at")

        extra: dict[str, str] = {}
        if "sha1" in meta:
            extra["sha1"] = str(meta["sha1"])

        fetched_etag = str(meta["etag"]) if meta.get("etag") is not None else None
        if self._cursor is not None and fetched_etag:
            self._cursor.set_etag(str(meta.get("id", item)), fetched_etag)

        return FetchedItem(
            source_name=self.name,
            item_id=str(meta.get("id", item)),
            display_name=filename,
            content=content,
            content_type=_content_type_for(filename),
            size=declared_size,
            modified_at=_parse_iso(modified_at_raw) if modified_at_raw else None,
            etag=fetched_etag,
            source_path=_source_path_from_box(meta, filename),
            extra=extra,
        )

    def _maybe_if_none_match_get_json(self, path: str, etag: str | None):
        """Pass ``if_none_match`` only if the API client accepts it."""
        if etag is None:
            return self._api.get_json(path)
        try:
            return self._api.get_json(path, if_none_match=etag)
        except TypeError:
            return self._api.get_json(path)

    def _maybe_if_none_match_get_bytes(self, path: str, etag: str | None):
        if etag is None:
            return self._api.get_bytes(path)
        try:
            return self._api.get_bytes(path, if_none_match=etag)
        except TypeError:
            return self._api.get_bytes(path)

    # ---- internals ------------------------------------------------------

    def _walk_catalog(self, folder_id: str, since: datetime | None,
                      out: list[ItemMetadata]) -> None:
        """Depth-first walk producing :class:`ItemMetadata` per file."""
        page = self._api.get_json(f"/folders/{folder_id}/items")
        for entry in page.get("entries", []):
            kind = entry.get("type")
            if kind == "file":
                m_raw = entry.get("modified_at")
                modified = _parse_iso(m_raw) if m_raw else None
                if since is not None and (modified is None or modified <= since):
                    continue
                name = entry.get("name") or str(entry.get("id"))
                out.append(ItemMetadata(
                    item_id=str(entry["id"]),
                    display_name=name,
                    etag=str(entry["etag"]) if entry.get("etag") is not None else None,
                    modified_at=modified,
                    size=int(entry["size"]) if entry.get("size") is not None else None,
                    content_type=_content_type_for(name),
                    source_path=_source_path_from_box(entry, name),
                ))
            elif kind == "folder":
                self._walk_catalog(str(entry["id"]), since, out)
            # other entry types (web_link, etc.) are skipped


__all__ = ["BoxApiClient", "BoxIngestSource", "ItemMetadata"]
