# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""``BoxIngestSource`` must page through ``/folders/{id}/items`` and ask for the
fields it reads.

Found on the ut-triga node 2026-09-25: 50 walked Box folders held EXACTLY 100
distinct files and none held more — Box's default page size. The console-log
folder alone has 11,000+ files; bronze had 100 of them. The walk issued ONE
``GET /folders/{id}/items`` with no ``limit``/``marker`` and never looped.

Second defect in the same call: nothing requested ``fields``, and Box's default
item fields are ``type,id,sequence_id,etag,name`` — so every ``modified_at`` was
None and the ``since`` watermark filter skipped every file on an incremental
run.
"""
from __future__ import annotations

from datetime import UTC, datetime

from axiom.extensions.builtins.data_platform.sources.box.source import BoxIngestSource

PAGE = 3  # small page so the test walks several pages


class _PagingBoxApi:
    """Honors Box marker pagination: ``limit`` + ``usemarker`` + ``marker``.

    Serves at most ``limit`` entries per call and returns ``next_marker``
    until the folder is exhausted, exactly like the real endpoint.
    """

    def __init__(self, tree: dict[str, list[dict]]):
        self.tree = tree
        self.calls: list[tuple[str, dict | None]] = []

    def get_json(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if not (path.startswith("/folders/") and path.endswith("/items")):
            raise AssertionError(f"unexpected path: {path}")
        folder_id = path.split("/")[2]
        entries = self.tree.get(folder_id, [])
        params = params or {}
        limit = int(params.get("limit", 100))
        start = int(params.get("marker", 0))
        page = entries[start : start + limit]
        nxt = start + limit
        out = {"entries": page, "total_count": len(entries), "limit": limit}
        out["next_marker"] = str(nxt) if nxt < len(entries) else None
        return out

    def get_bytes(self, path):
        return b"x"


def _file(id, name):
    return {
        "type": "file",
        "id": str(id),
        "name": name,
        "etag": "0",
        "modified_at": "2026-06-01T00:00:00Z",
        "size": 1,
        "path_collection": {"entries": [{"name": "All Files"}, {"name": "F"}]},
    }


def test_catalog_pages_through_a_folder_larger_than_one_page():
    n = PAGE * 4 + 1  # 13 files -> 5 pages at PAGE per page
    api = _PagingBoxApi({"root": [_file(i, f"{i}.txt") for i in range(n)]})
    src = BoxIngestSource(name="t", folder_id="root", api_client=api, page_size=PAGE)

    items = src.catalog()

    assert sorted(int(i.item_id) for i in items) == list(range(n))
    listing_calls = [c for c in api.calls if c[0] == "/folders/root/items"]
    assert len(listing_calls) == 5, "one call per page, until next_marker is null"
    assert all(c[1].get("usemarker") for c in listing_calls)
    assert listing_calls[0][1].get("marker") is None
    assert [c[1].get("marker") for c in listing_calls[1:]] == ["3", "6", "9", "12"]


def test_catalog_pages_inside_subfolders_too():
    api = _PagingBoxApi(
        {
            "root": [{"type": "folder", "id": "sub", "name": "sub"}] + [_file(i, f"{i}.txt") for i in range(PAGE + 1)],
            "sub": [_file(100 + i, f"s{i}.txt") for i in range(PAGE * 2 + 1)],
        }
    )
    src = BoxIngestSource(name="t", folder_id="root", api_client=api, page_size=PAGE)

    ids = sorted(int(i.item_id) for i in src.catalog())

    assert ids == list(range(PAGE + 1)) + [100 + i for i in range(PAGE * 2 + 1)]


def test_catalog_requests_the_fields_it_reads():
    api = _PagingBoxApi({"root": [_file(1, "a.txt")]})
    src = BoxIngestSource(name="t", folder_id="root", api_client=api)

    items = src.catalog(since=datetime(2026, 1, 1, tzinfo=UTC))

    fields = set(api.calls[0][1].get("fields", "").split(","))
    for needed in ("type", "id", "name", "etag", "modified_at", "size", "path_collection"):
        assert needed in fields, f"catalog reads {needed!r} but never asked Box for it"
    # With modified_at present the watermark filter keeps the newer file.
    assert [i.item_id for i in items] == ["1"]


def test_catalog_stops_if_box_repeats_a_marker():
    class _Stuck(_PagingBoxApi):
        def get_json(self, path, params=None):
            out = super().get_json(path, params)
            out["next_marker"] = (params or {}).get("marker") or "0"  # never advances
            return out

    api = _Stuck({"root": [_file(i, f"{i}.txt") for i in range(PAGE)]})
    src = BoxIngestSource(name="t", folder_id="root", api_client=api, page_size=PAGE)

    items = src.catalog()  # must terminate

    assert len(items) == PAGE
