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
import os
from collections.abc import Iterable, Sequence
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


# Box caps ``limit`` at 1000 per page; fewer round-trips on 11k-file folders.
_PAGE_SIZE = 1000
# What ``_walk_catalog`` reads off each entry. Box's default item fields are
# only ``type,id,sequence_id,etag,name`` -- everything else must be requested.
_ITEM_FIELDS = "type,id,name,etag,modified_at,size,path_collection"


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


def box_web_url(item_id: str | None) -> str | None:
    """Canonical Box web URL for a file id — the shareable link a human opens.

    Box keys its web links on the numeric file id, not the path;
    ``app.box.com`` resolves to the authenticated user's enterprise instance.
    This is the *only* place the Box URL shape is defined — the platform core
    never hardcodes a source's link format (ADR-091). Returns ``None`` for a
    falsy id so callers can pass it straight through.
    """
    return f"https://app.box.com/file/{item_id}" if item_id else None


def normalize_exclude_prefixes(prefixes: Iterable[str]) -> list[str]:
    """Clean a list of subtree prefixes: strip, require a leading ``/`` (the
    form ``source_path`` uses), drop trailing slashes and empties, dedupe."""
    out: list[str] = []
    for raw in prefixes or ():
        p = (raw or "").strip()
        if not p:
            continue
        p = "/" + p.strip("/")
        if p != "/" and p not in out:
            out.append(p)
    return out


def normalize_extensions(exts: Iterable[str]) -> list[str]:
    """Clean an extension list: strip, drop a leading dot, lowercase, dedupe."""
    out: list[str] = []
    for raw in exts or ():
        e = (raw or "").strip().lstrip(".").lower()
        if e and e not in out:
            out.append(e)
    return out


def exclude_extensions_from_params(params: dict[str, Any] | None) -> list[str]:
    """``params.exclude_extensions`` (comma-separated, as connector TOML stores it)."""
    raw = (params or {}).get("exclude_extensions") or ""
    return normalize_extensions(str(raw).split(","))


def max_item_bytes_from_params(params: dict[str, Any] | None) -> int | None:
    """``params.max_item_bytes`` as an int, or None when unset/empty."""
    raw = (params or {}).get("max_item_bytes")
    if raw in (None, ""):
        return None
    return int(raw)


def exclude_prefixes_from_params(params: dict[str, Any] | None) -> list[str]:
    """Read ``params.exclude_prefixes`` (a comma-separated string, as connector
    TOML stores it) into a clean list."""
    raw = (params or {}).get("exclude_prefixes") or ""
    return normalize_exclude_prefixes(str(raw).split(","))


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
                 cursor: Any = None, page_size: int = _PAGE_SIZE,
                 exclude_prefixes: Sequence[str] = (),
                 exclude_extensions: Sequence[str] = (),
                 max_item_bytes: int | None = None) -> None:
        if not name:
            raise ValueError("BoxIngestSource requires a non-empty name")
        if not folder_id:
            raise ValueError("BoxIngestSource requires a folder_id")
        if page_size < 1:
            raise ValueError("BoxIngestSource page_size must be >= 1")
        self.name = name
        self.folder_id = folder_id
        self._api = api_client
        self._page_size = page_size
        # Subtrees (source_path prefixes, as the Box UI shows them without the
        # "All Files" root) this source never walks. A telemetry drop of CSVs
        # and plot HTML is not a document corpus: on the ut-triga node one such
        # subtree held 413 GB the RAG corpus should never fetch. Excluded
        # folders are not even listed, so they cost no API calls.
        self._exclude_prefixes = tuple(normalize_exclude_prefixes(exclude_prefixes))
        # A document corpus root on a shared drive also holds photos, videos and
        # multi-gigabyte outputs. On the ut-triga node a pass with the telemetry
        # subtree already excluded still set out to fetch 268 GB into 141 GB of
        # free disk (12 GB of pictures landed before it was killed). Extensions
        # and a per-file byte cap are skipped at catalog time, before any fetch.
        self._exclude_extensions = frozenset(normalize_extensions(exclude_extensions))
        if max_item_bytes is not None and max_item_bytes < 1:
            raise ValueError("BoxIngestSource max_item_bytes must be >= 1 when set")
        self._max_item_bytes = max_item_bytes
        # Optional cursor (typed as :class:`axiom.infra.connector_cursor.ConnectorCursor`).
        # When provided, ``catalog()`` filters out items whose stored etag
        # matches the cursor (already seen unchanged), and ``fetch()``
        # forwards ``If-None-Match`` + updates the cursor on success.
        # When absent, behavior is unchanged for back-compat callers.
        self._cursor = cursor

    # ---- lifecycle ------------------------------------------------------

    def close(self) -> None:
        """Release the underlying API client (session/browser/JWT).

        The provider owns client lifecycle now; a generic driver calls
        ``source.close()`` without knowing the client kind.
        """
        close = getattr(self._api, "close", None)
        if callable(close):
            close()

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
        resolved_id = str(meta.get("id", item))
        if self._cursor is not None and fetched_etag:
            self._cursor.set_etag(resolved_id, fetched_etag)

        return FetchedItem(
            source_name=self.name,
            item_id=resolved_id,
            display_name=filename,
            content=content,
            content_type=_content_type_for(filename),
            size=declared_size,
            modified_at=_parse_iso(modified_at_raw) if modified_at_raw else None,
            etag=fetched_etag,
            source_path=_source_path_from_box(meta, filename),
            source_url=box_web_url(resolved_id),
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
        for entry in self._iter_folder_items(folder_id):
            kind = entry.get("type")
            name = entry.get("name") or str(entry.get("id"))
            source_path = _source_path_from_box(entry, name)
            if self._excluded(source_path):
                continue  # a file under an excluded subtree, or the subtree itself
            if kind == "file":
                if self._skipped_file(name, entry.get("size")):
                    continue
                m_raw = entry.get("modified_at")
                modified = _parse_iso(m_raw) if m_raw else None
                if since is not None and (modified is None or modified <= since):
                    continue
                out.append(ItemMetadata(
                    item_id=str(entry["id"]),
                    display_name=name,
                    etag=str(entry["etag"]) if entry.get("etag") is not None else None,
                    modified_at=modified,
                    size=int(entry["size"]) if entry.get("size") is not None else None,
                    content_type=_content_type_for(name),
                    source_path=source_path,
                ))
            elif kind == "folder":
                self._walk_catalog(str(entry["id"]), since, out)
            # other entry types (web_link, etc.) are skipped

    def _skipped_file(self, name: str, size: Any) -> bool:
        """True for a file this source never catalogs: an excluded extension, or
        a size over the per-file cap. Unknown size is never skipped by size."""
        ext = os.path.splitext(name or "")[1].lstrip(".").lower()
        if ext and ext in self._exclude_extensions:
            return True
        if self._max_item_bytes is not None and size is not None:
            try:
                return int(size) > self._max_item_bytes
            except (TypeError, ValueError):
                return False
        return False

    def _excluded(self, source_path: str | None) -> bool:
        """True when ``source_path`` is an excluded subtree or lies inside one.

        Matches whole path segments: excluding ``/A/B`` skips ``/A/B`` and
        ``/A/B/x.csv`` but not ``/A/Bx``.
        """
        if not source_path or not self._exclude_prefixes:
            return False
        return any(source_path == p or source_path.startswith(p + "/") for p in self._exclude_prefixes)

    def _iter_folder_items(self, folder_id: str):
        """Every entry of one folder, across ALL pages.

        Box serves ``/folders/{id}/items`` in pages (100 by default, 1000 max)
        and only the fields ``type,id,sequence_id,etag,name`` unless asked.
        Both bit the ut-triga node on 2026-09-25: every folder with more than
        100 files was silently truncated to its first page (50 folders sat at
        exactly 100), and ``modified_at`` was never returned, so an incremental
        ``since`` run filtered out every file. Marker pagination is used because
        offset pagination stops at 10,000 and the console-log folder is larger.
        """
        params: dict[str, Any] = {
            "limit": self._page_size,
            "usemarker": "true",
            "fields": _ITEM_FIELDS,
        }
        seen_markers: set[str] = set()
        seen_ids: set[str] = set()
        while True:
            page = self._api.get_json(f"/folders/{folder_id}/items", params) or {}
            for entry in page.get("entries", []):
                # An id can repeat across pages (Box paging drift, or a marker
                # that failed to advance); the walk must yield each item once.
                key = str(entry.get("id"))
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                yield entry
            marker = page.get("next_marker")
            if not marker or marker in seen_markers:
                return  # exhausted, or Box failed to advance -- never spin
            seen_markers.add(marker)
            params = {**params, "marker": marker}


__all__ = ["BoxApiClient", "BoxIngestSource", "ItemMetadata", "box_web_url",
           "exclude_extensions_from_params", "exclude_prefixes_from_params",
           "max_item_bytes_from_params", "normalize_exclude_prefixes", "normalize_extensions"]
