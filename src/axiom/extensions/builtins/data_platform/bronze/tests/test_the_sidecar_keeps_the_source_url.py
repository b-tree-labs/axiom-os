# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A fetched item's shareable link is provenance: bronze keeps it and hands it back (#2007).

``FetchedItem.source_url`` is the document's link in its origin system. The bronze sidecar
dropped it, so it could never travel bronze -> conform -> silver -> gold to a citation. Read
back here through the record reader conform uses (``load_deposits``), never through a new
bronze -> RAG read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.bronze import BronzeWriter, FilesystemBronzeSink
from axiom.extensions.builtins.data_platform.conformance.deposits import load_deposits
from axiom.extensions.builtins.data_platform.contracts import FetchedItem
from axiom.rag.ingest_router import Disposition

LINK = "https://files.example.org/file/42"


def _land(root: Path, *, source_url: str | None) -> Path:
    item = FetchedItem(
        source_name="share",
        item_id="42",
        display_name="report.pdf",
        content=b"%PDF-1.7 body",
        content_type="application/pdf",
        size=13,
        modified_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        etag="v1",
        source_path="/Reports/report.pdf",
        source_url=source_url,
    )
    writer = BronzeWriter(
        rules=[],
        sink=FilesystemBronzeSink(root=root),
        default_disposition=Disposition.ALLOW,
        default_tier=None,
    )
    return writer.write(item).record_path


@pytest.mark.acceptance("2007-A1")
def test_a_source_url_is_written_into_the_sidecar_and_read_back_with_the_record(tmp_path):
    record_path = _land(tmp_path, source_url=LINK)

    assert json.loads(record_path.read_text())["source_url"] == LINK
    (deposit,), _, _ = load_deposits(tmp_path, "share")
    assert deposit.source_url == LINK


@pytest.mark.acceptance("2007-A2")
def test_an_item_without_a_source_url_is_unaffected(tmp_path):
    record_path = _land(tmp_path, source_url=None)

    assert "source_url" not in json.loads(record_path.read_text())
    (deposit,), _, _ = load_deposits(tmp_path, "share")
    assert deposit.source_url is None
