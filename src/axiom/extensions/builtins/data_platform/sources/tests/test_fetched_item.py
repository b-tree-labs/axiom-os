# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``FetchedItem`` — the metadata-carrying payload a source
returns from ``fetch``. Bronze needs more than bytes to write a sidecar
manifest (id, modified_at, etag for incremental sync, content_type for
extraction routing), so :class:`IngestSource.fetch` returns this record
rather than ``bytes``."""

from __future__ import annotations

from datetime import UTC, datetime


def test_fetched_item_carries_bytes_and_required_metadata():
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    item = FetchedItem(
        source_name="box-folder-x",
        item_id="123",
        display_name="doc.pdf",
        content=b"%PDF-1.7",
        content_type="application/pdf",
        size=8,
        modified_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        etag="abc",
        source_path="/Shared/Reports/doc.pdf",
        extra={"sha1": "deadbeef"},
    )

    assert item.source_name == "box-folder-x"
    assert item.item_id == "123"
    assert item.content == b"%PDF-1.7"
    assert item.size == 8
    assert item.etag == "abc"
    assert item.extra["sha1"] == "deadbeef"


def test_fetched_item_is_immutable():
    from dataclasses import FrozenInstanceError

    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    item = FetchedItem(
        source_name="s",
        item_id="i",
        display_name="n",
        content=b"",
        content_type=None,
        size=0,
        modified_at=None,
        etag=None,
        source_path=None,
        extra={},
    )

    # Bronze sidecars derive from the original FetchedItem — mutating it
    # mid-flight would corrupt the provenance chain.
    try:
        item.etag = "x"  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("FetchedItem must be frozen")


def test_fetched_item_size_consistency_with_content():
    """Size MUST equal len(content). The bronze sidecar relies on this."""
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    item = FetchedItem(
        source_name="s",
        item_id="i",
        display_name="n",
        content=b"hello",
        content_type=None,
        size=5,
        modified_at=None,
        etag=None,
        source_path=None,
        extra={},
    )
    assert item.size == len(item.content)
