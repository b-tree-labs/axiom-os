# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``BoxIngestSource`` cursor integration — catalog/fetch dedup.

Closes the loop: catalog() filters out items whose stored etag matches
the cursor; fetch() consults the cursor for If-None-Match on the byte
download and updates the etag after a successful fetch. A re-run after
a token-cliff resumes instead of re-walking.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.sources.box.source import (
    BoxIngestSource,
)
from axiom.infra.connector_cursor import ConnectorCursor


class _FakeBoxApi:
    """API stub recording fetches + serving canned data."""

    def __init__(self, tree, file_meta=None, file_bytes=None):
        self.tree = tree
        self.file_meta = file_meta or {}
        self.file_bytes = file_bytes or {}
        self.calls_get_json: list[str] = []
        self.calls_get_bytes: list[str] = []
        self.json_etag_headers: list[str | None] = []
        self.bytes_etag_headers: list[str | None] = []

    def get_json(self, path, params=None, if_none_match=None):
        self.calls_get_json.append(path)
        self.json_etag_headers.append(if_none_match)
        if path.startswith("/folders/") and path.endswith("/items"):
            folder_id = path.split("/")[2]
            return {"entries": self.tree.get(folder_id, [])}
        if path.startswith("/files/"):
            file_id = path.split("/")[2]
            return self.file_meta.get(file_id, {})
        raise AssertionError(f"unexpected path: {path}")

    def get_bytes(self, path, if_none_match=None):
        self.calls_get_bytes.append(path)
        self.bytes_etag_headers.append(if_none_match)
        file_id = path.split("/")[2]
        return self.file_bytes.get(file_id, b"")


def _file(id, name, etag="0", modified_at="2026-06-01T00:00:00Z", size=10):
    return {
        "type": "file",
        "id": str(id),
        "name": name,
        "etag": etag,
        "modified_at": modified_at,
        "size": size,
        "path_collection": {"entries": [{"name": "All Files"}]},
    }


# -- catalog() filters via cursor --------------------------------------------


def test_catalog_skips_items_whose_etag_matches_cursor(tmp_path):
    tree = {
        "root": [
            _file(1, "a.md", etag="aa"),
            _file(2, "b.md", etag="bb"),
        ],
    }
    api = _FakeBoxApi(tree)
    cursor = ConnectorCursor(tmp_path / "c.json")
    cursor.set_etag("1", "aa")    # already seen with same etag

    src = BoxIngestSource(name="t", folder_id="root", api_client=api,
                          cursor=cursor)
    items = src.catalog()

    assert {i.item_id for i in items} == {"2"}


def test_catalog_yields_item_when_etag_changed(tmp_path):
    tree = {
        "root": [_file(1, "a.md", etag="new-etag")],
    }
    api = _FakeBoxApi(tree)
    cursor = ConnectorCursor(tmp_path / "c.json")
    cursor.set_etag("1", "old-etag")    # changed since last seen

    src = BoxIngestSource(name="t", folder_id="root", api_client=api,
                          cursor=cursor)
    items = src.catalog()
    assert {i.item_id for i in items} == {"1"}


def test_catalog_without_cursor_returns_everything(tmp_path):
    """Back-compat: no cursor = no skip; existing callers untouched."""
    tree = {"root": [_file(1, "a.md", etag="aa")]}
    api = _FakeBoxApi(tree)

    src = BoxIngestSource(name="t", folder_id="root", api_client=api)
    items = src.catalog()
    assert len(items) == 1


# -- fetch() consults + updates cursor ---------------------------------------


def test_fetch_passes_if_none_match_when_cursor_has_etag(tmp_path):
    tree = {"root": [_file(1, "a.md", etag="abc")]}
    api = _FakeBoxApi(
        tree,
        file_meta={"1": {"id": "1", "name": "a.md", "etag": "abc",
                         "size": 5, "modified_at": "2026-06-01T00:00:00Z",
                         "path_collection": {"entries": [{"name": "All Files"}]}}},
        file_bytes={"1": b"hello"},
    )
    cursor = ConnectorCursor(tmp_path / "c.json")
    cursor.set_etag("1", "abc")

    src = BoxIngestSource(name="t", folder_id="root", api_client=api,
                          cursor=cursor)
    src.fetch("1")

    # Both the metadata get_json AND the bytes get_bytes should have
    # the etag forwarded
    assert "abc" in api.json_etag_headers
    assert "abc" in api.bytes_etag_headers


def test_fetch_updates_cursor_etag_on_success(tmp_path):
    tree = {"root": [_file(1, "a.md", etag="xyz")]}
    api = _FakeBoxApi(
        tree,
        file_meta={"1": {"id": "1", "name": "a.md", "etag": "xyz",
                         "size": 5, "modified_at": "2026-06-01T00:00:00Z",
                         "path_collection": {"entries": [{"name": "All Files"}]}}},
        file_bytes={"1": b"hello"},
    )
    cursor = ConnectorCursor(tmp_path / "c.json")

    src = BoxIngestSource(name="t", folder_id="root", api_client=api,
                          cursor=cursor)
    src.fetch("1")

    assert cursor.get_etag("1") == "xyz"


def test_fetch_without_cursor_does_not_pass_if_none_match(tmp_path):
    tree = {"root": [_file(1, "a.md")]}
    api = _FakeBoxApi(
        tree,
        file_meta={"1": {"id": "1", "name": "a.md", "etag": "abc",
                         "size": 5, "modified_at": "2026-06-01T00:00:00Z",
                         "path_collection": {"entries": [{"name": "All Files"}]}}},
        file_bytes={"1": b"hello"},
    )
    src = BoxIngestSource(name="t", folder_id="root", api_client=api)
    src.fetch("1")

    assert api.json_etag_headers == [None]
    assert api.bytes_etag_headers == [None]
