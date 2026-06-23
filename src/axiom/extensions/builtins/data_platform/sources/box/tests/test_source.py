# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``BoxIngestSource`` — the Box-folder ``IngestSource`` that
PLINTH/Dagster drive in DP-1.

The Box class talks to Box via a pluggable ``api_client`` so unit tests
do not need Playwright or network. The real client (``BoxBrowserApiClient``)
wraps the existing ``publishing/box_browser`` Playwright session; it is
not exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class FakeBoxApi:
    """Minimal Box API stub.

    ``folders[folder_id]`` is a list of item dicts shaped like Box's
    ``GET /2.0/folders/{id}/items``. ``files[file_id]`` is a tuple of
    (metadata-dict, content-bytes).
    """

    def __init__(
        self,
        *,
        folders: dict[str, list[dict[str, Any]]] | None = None,
        files: dict[str, tuple[dict[str, Any], bytes]] | None = None,
    ) -> None:
        self.folders = folders or {}
        self.files = files or {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET_JSON", path, params))
        if path.startswith("/folders/") and path.endswith("/items"):
            fid = path.split("/")[2]
            return {"entries": list(self.folders.get(fid, [])), "total_count": len(self.folders.get(fid, []))}
        if path.startswith("/files/"):
            fid = path.split("/")[2]
            meta, _ = self.files[fid]
            return meta
        raise AssertionError(f"unexpected GET_JSON {path}")

    def get_bytes(self, path: str) -> bytes:
        self.calls.append(("GET_BYTES", path, None))
        # /files/{id}/content
        fid = path.split("/")[2]
        _, content = self.files[fid]
        return content


def _file_entry(id_: str, name: str, modified_at: str, size: int) -> dict[str, Any]:
    return {"type": "file", "id": id_, "name": name, "modified_at": modified_at, "size": size}


def _folder_entry(id_: str, name: str) -> dict[str, Any]:
    return {"type": "folder", "id": id_, "name": name}


# ---------- construction + identity ----------------------------------------


def test_box_ingest_source_is_an_ingest_source():
    from axiom.extensions.builtins.data_platform.contracts import IngestSource
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    src = BoxIngestSource(name="box-reports", folder_id="100", api_client=FakeBoxApi())
    assert isinstance(src, IngestSource)
    assert src.name == "box-reports"


def test_name_is_required():
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    try:
        BoxIngestSource(name="", folder_id="100", api_client=FakeBoxApi())
    except ValueError:
        return
    raise AssertionError("empty name must raise ValueError")


# ---------- list_changed ----------------------------------------------------


def test_list_changed_no_since_returns_all_file_ids():
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    api = FakeBoxApi(
        folders={
            "100": [
                _file_entry("1", "a.pdf", "2026-05-28T12:00:00Z", 100),
                _file_entry("2", "b.pdf", "2026-05-29T12:00:00Z", 200),
            ]
        }
    )
    src = BoxIngestSource(name="box", folder_id="100", api_client=api)
    assert sorted(src.list_changed(since=None)) == ["1", "2"]


def test_list_changed_filters_by_since():
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    api = FakeBoxApi(
        folders={
            "100": [
                _file_entry("1", "old.pdf", "2026-05-20T12:00:00Z", 100),
                _file_entry("2", "new.pdf", "2026-05-29T12:00:00Z", 200),
            ]
        }
    )
    src = BoxIngestSource(name="box", folder_id="100", api_client=api)
    since = datetime(2026, 5, 25, tzinfo=UTC)
    assert src.list_changed(since=since) == ["2"]


def test_list_changed_recurses_into_subfolders():
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    api = FakeBoxApi(
        folders={
            "100": [
                _file_entry("1", "top.pdf", "2026-05-29T12:00:00Z", 100),
                _folder_entry("200", "sub"),
            ],
            "200": [
                _file_entry("3", "deep.pdf", "2026-05-29T13:00:00Z", 300),
            ],
        }
    )
    src = BoxIngestSource(name="box", folder_id="100", api_client=api)
    assert sorted(src.list_changed(since=None)) == ["1", "3"]


def test_list_changed_ignores_unknown_entry_types():
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    api = FakeBoxApi(
        folders={
            "100": [
                _file_entry("1", "a.pdf", "2026-05-29T12:00:00Z", 100),
                {"type": "web_link", "id": "99"},
            ]
        }
    )
    src = BoxIngestSource(name="box", folder_id="100", api_client=api)
    assert src.list_changed(since=None) == ["1"]


# ---------- fetch -----------------------------------------------------------


def test_fetch_returns_fetched_item_with_metadata_and_bytes():
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    api = FakeBoxApi(
        files={
            "1": (
                {
                    "id": "1",
                    "name": "doc.pdf",
                    "size": 8,
                    "modified_at": "2026-05-29T12:00:00Z",
                    "etag": "7",
                    "sha1": "deadbeef",
                    "path_collection": {
                        "entries": [
                            {"type": "folder", "name": "All Files"},
                            {"type": "folder", "name": "Reports"},
                        ]
                    },
                },
                b"%PDF-1.7",
            )
        }
    )
    src = BoxIngestSource(name="box-reports", folder_id="100", api_client=api)
    item = src.fetch("1")

    assert item.source_name == "box-reports"
    assert item.item_id == "1"
    assert item.display_name == "doc.pdf"
    assert item.content == b"%PDF-1.7"
    assert item.size == 8
    assert item.content_type == "application/pdf"  # derived from extension
    assert item.etag == "7"
    assert item.extra["sha1"] == "deadbeef"
    assert item.modified_at == datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    # path is human-readable, excludes the synthetic Box "All Files" root
    assert item.source_path == "/Reports/doc.pdf"


def test_fetch_handles_files_without_optional_metadata():
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    api = FakeBoxApi(
        files={
            "1": (
                {"id": "1", "name": "data.bin", "size": 4, "modified_at": "2026-05-29T12:00:00Z"},
                b"\x00\x01\x02\x03",
            )
        }
    )
    src = BoxIngestSource(name="box", folder_id="100", api_client=api)
    item = src.fetch("1")
    assert item.content == b"\x00\x01\x02\x03"
    assert item.size == 4
    assert item.etag is None
    assert item.extra == {}


def test_fetch_content_size_must_match_metadata_size():
    """A short-read mid-download is a data-integrity bug — fetch raises."""
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    api = FakeBoxApi(
        files={
            "1": (
                {"id": "1", "name": "doc.pdf", "size": 100, "modified_at": "2026-05-29T12:00:00Z"},
                b"short",  # only 5 bytes — Box said 100
            )
        }
    )
    src = BoxIngestSource(name="box", folder_id="100", api_client=api)
    try:
        src.fetch("1")
    except ValueError as exc:
        assert "size" in str(exc).lower()
        return
    raise AssertionError("size mismatch must raise ValueError")
