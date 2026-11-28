# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``embed_bronze_record`` never chunks or embeds a placeholder (#1948), and its
``skipped_reason`` says why.

Two routes in: the gate marked the record a placeholder at landing (the normal
case), or a record arrives ALLOW without that mark (a record landed before the
gate knew, replayed by a reindex). The second is caught by sniffing the blob.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from axiom.extensions.builtins.data_platform import rag_embed as re_mod
from axiom.extensions.builtins.data_platform.bronze import BronzeWriter, FilesystemBronzeSink
from axiom.extensions.builtins.data_platform.contracts import FetchedItem
from axiom.extensions.builtins.data_platform.rag_embed import embed_bronze_record
from axiom.rag.ingest_router import Disposition

LFS = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"b" * 64 + b"\nsize 9\n"


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upsert_chunks(self, chunks, embeddings=None, **kwargs) -> None:
        self.calls.append({"chunks": list(chunks)})


def _land(tmp_path, content: bytes, path: str):
    item = FetchedItem(
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
    writer = BronzeWriter(
        rules=[],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.ALLOW,
        default_tier="rag-community",
    )
    return writer.write(item), item


def _no_embedding(monkeypatch):
    calls: list = []

    def fake(texts):
        calls.append(texts)
        return None

    monkeypatch.setattr(re_mod.embedder, "embed_texts", fake)
    return calls


def test_a_placeholder_marked_at_landing_is_skipped_with_its_reason(tmp_path, monkeypatch):
    embedded = _no_embedding(monkeypatch)
    record, item = _land(tmp_path, LFS, "papers/report.pdf")
    store = FakeStore()

    stats = embed_bronze_record(record, item, store)

    assert stats.indexed is False
    assert stats.skipped_reason == "placeholder: git-lfs pointer"
    assert store.calls == [] and embedded == []


def test_an_unmarked_allow_record_is_still_caught(tmp_path, monkeypatch):
    embedded = _no_embedding(monkeypatch)
    record, item = _land(tmp_path, LFS, "notes/readme.md")
    # A record landed before the gate recognised placeholders: ALLOW, unmarked.
    record = dataclasses.replace(record, disposition=Disposition.ALLOW, placeholder=None)
    store = FakeStore()

    stats = embed_bronze_record(record, item, store)

    assert stats.indexed is False
    assert stats.skipped_reason == "placeholder: git-lfs pointer"
    assert store.calls == [] and embedded == []


def test_a_real_document_still_embeds(tmp_path, monkeypatch):
    _no_embedding(monkeypatch)
    text = b"# Note\n\nA short operating note with enough prose to retrieve on.\n"
    record, item = _land(tmp_path, text, "notes/readme.md")
    store = FakeStore()

    stats = embed_bronze_record(record, item, store)

    assert stats.indexed is True
    assert len(store.calls) == 1
