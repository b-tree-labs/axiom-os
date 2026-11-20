# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The bronze gate records a placeholder, with why, instead of landing it as a
document (#1948).

The bytes are kept (bronze is the record of what arrived), but the manifest
says the item is a placeholder and the disposition is QUARANTINE, so nothing
downstream treats it as a document.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from axiom.extensions.builtins.data_platform.bronze import BronzeWriter, FilesystemBronzeSink
from axiom.extensions.builtins.data_platform.contracts import FetchedItem
from axiom.rag.ingest_router import Disposition, ProvenanceRule

LFS = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 9\n"


def _item(content: bytes, path: str) -> FetchedItem:
    return FetchedItem(
        source_name="example-source",
        item_id="item-1",
        display_name=path.rsplit("/", 1)[-1],
        content=content,
        content_type=None,
        size=len(content),
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        etag=None,
        source_path=path,
    )


def _writer(tmp_path, rules=None):
    return BronzeWriter(
        rules=rules or [],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.ALLOW,
        default_tier="rag-community",
    )


def test_an_lfs_pointer_is_recorded_as_a_placeholder(tmp_path):
    result = _writer(tmp_path).write(_item(LFS, "papers/report.pdf"))

    assert result.placeholder == "placeholder: git-lfs pointer"
    assert result.disposition is Disposition.QUARANTINE
    assert result.reason == "placeholder: git-lfs pointer"
    assert result.content_path is not None and result.content_path.read_bytes() == LFS
    sidecar = json.loads(result.record_path.read_text())
    assert sidecar["disposition"] == "quarantine"
    assert sidecar["reason"] == "placeholder: git-lfs pointer"
    assert sidecar["matched_rule"] == "placeholder"


def test_a_real_document_is_unchanged(tmp_path):
    result = _writer(tmp_path).write(_item(b"%PDF-1.7\n...", "papers/report.pdf"))

    assert result.placeholder is None
    assert result.disposition is Disposition.ALLOW


def test_an_excluded_path_stays_excluded(tmp_path):
    rules = [ProvenanceRule(pattern="papers/", disposition=Disposition.EXCLUDE)]
    result = _writer(tmp_path, rules).write(_item(LFS, "papers/report.pdf"))

    assert result.disposition is Disposition.EXCLUDE
    assert result.placeholder is None
