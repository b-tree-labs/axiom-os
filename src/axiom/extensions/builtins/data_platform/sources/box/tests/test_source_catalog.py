# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``BoxIngestSource.catalog()`` — metadata-only folder walk.

The biggest single waste in the DP-1 stand-up: every file got a full
``GET /files/{id}`` followed by ``GET /files/{id}/content`` even when
we'd already seen the file. ``catalog()`` exposes the metadata Box's
``/folders/{id}/items`` already returns (id, etag, modified_at, size,
name, content_type, source_path) so the caller can decide:

- already in bronze with same etag → skip
- needs OCR routing → schedule offline
- size exceeds budget for tonight → defer

This is the contract change item from the rate-limit / dedup memo.
``list_changed()`` is preserved as a thin shim over ``catalog()`` so
nothing downstream breaks.
"""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.extensions.builtins.data_platform.sources.box.source import (
    BoxIngestSource,
    ItemMetadata,
)


class _FakeBoxApi:
    """Stub honoring the BoxApiClient protocol with a scripted folder tree."""

    def __init__(self, tree):
        # tree: {folder_id: [{entry...}]}  where entry has {type, id, name,
        # modified_at, etag, size, path_collection}
        self.tree = tree
        self.fetches: list[str] = []

    def get_json(self, path, params=None):
        # /folders/<id>/items
        if path.startswith("/folders/") and path.endswith("/items"):
            folder_id = path.split("/")[2]
            return {"entries": self.tree.get(folder_id, [])}
        # /files/<id>
        if path.startswith("/files/"):
            self.fetches.append(path)
            file_id = path.split("/")[2]
            return self._find(file_id) or {}
        raise AssertionError(f"unexpected path: {path}")

    def get_bytes(self, path):
        return b"x"

    def _find(self, file_id):
        for folder, entries in self.tree.items():
            for e in entries:
                if str(e.get("id")) == file_id and e.get("type") == "file":
                    return e
        return None


def _file(id, name, etag="0", modified_at="2026-06-01T00:00:00Z",
          size=1024, parents=None):
    return {
        "type": "file",
        "id": str(id),
        "name": name,
        "etag": etag,
        "modified_at": modified_at,
        "size": size,
        "path_collection": {
            "entries": [{"name": p} for p in (parents or ["All Files"])],
        },
    }


def _folder(id, name, parents=None):
    return {
        "type": "folder",
        "id": str(id),
        "name": name,
        "path_collection": {
            "entries": [{"name": p} for p in (parents or ["All Files"])],
        },
    }


# -- contract ----------------------------------------------------------------


def test_catalog_returns_item_metadata_for_every_file():
    tree = {
        "root": [
            _file(1, "a.pdf", etag="aa", size=100),
            _file(2, "b.md", etag="bb", size=200),
        ],
    }
    api = _FakeBoxApi(tree)
    src = BoxIngestSource(name="t", folder_id="root", api_client=api)

    items = src.catalog()

    assert len(items) == 2
    a = next(i for i in items if i.item_id == "1")
    assert a.etag == "aa"
    assert a.size == 100
    assert a.display_name == "a.pdf"
    assert isinstance(a.modified_at, datetime)
    # No bytes were fetched; catalog is metadata-only
    assert api.fetches == []


def test_catalog_recurses_into_subfolders():
    tree = {
        "root": [
            _folder(10, "sub"),
            _file(1, "top.md", etag="aa"),
        ],
        "10": [
            _file(11, "deep.pdf", etag="bb"),
        ],
    }
    api = _FakeBoxApi(tree)
    src = BoxIngestSource(name="t", folder_id="root", api_client=api)

    items = src.catalog()
    ids = {i.item_id for i in items}
    assert ids == {"1", "11"}


def test_catalog_filters_by_watermark():
    tree = {
        "root": [
            _file(1, "old.md", modified_at="2026-05-01T00:00:00Z"),
            _file(2, "new.md", modified_at="2026-06-01T12:00:00Z"),
        ],
    }
    api = _FakeBoxApi(tree)
    src = BoxIngestSource(name="t", folder_id="root", api_client=api)

    since = datetime(2026, 5, 15, tzinfo=UTC)
    items = src.catalog(since=since)
    assert {i.item_id for i in items} == {"2"}


def test_catalog_records_source_path_from_box_path_collection():
    tree = {
        "root": [_file(1, "x.pdf",
                       parents=["All Files", "Lit", "CRISP"])],
    }
    api = _FakeBoxApi(tree)
    src = BoxIngestSource(name="t", folder_id="root", api_client=api)

    items = src.catalog()
    assert items[0].source_path == "/Lit/CRISP/x.pdf"


def test_catalog_skips_non_file_entries():
    tree = {
        "root": [
            _file(1, "f.md"),
            {"type": "web_link", "id": "99", "name": "shortcut"},
        ],
    }
    api = _FakeBoxApi(tree)
    src = BoxIngestSource(name="t", folder_id="root", api_client=api)

    items = src.catalog()
    assert {i.item_id for i in items} == {"1"}


# -- list_changed preserved as a thin shim -----------------------------------


def test_list_changed_returns_same_ids_as_catalog():
    """Back-compat: list_changed delegates to catalog and returns ids only."""
    tree = {
        "root": [_file(1, "a.md"), _file(2, "b.md")],
    }
    api = _FakeBoxApi(tree)
    src = BoxIngestSource(name="t", folder_id="root", api_client=api)

    ids = src.list_changed()
    assert sorted(ids) == ["1", "2"]


# -- ItemMetadata dataclass shape --------------------------------------------


def test_item_metadata_carries_all_fields_needed_for_dedup():
    m = ItemMetadata(
        item_id="1",
        display_name="x.pdf",
        etag="ab",
        modified_at=datetime(2026, 6, 1, tzinfo=UTC),
        size=1024,
        content_type="application/pdf",
        source_path="/folder/x.pdf",
    )
    assert m.item_id == "1"
    assert m.etag == "ab"
    assert m.size == 1024
