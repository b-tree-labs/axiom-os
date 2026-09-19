# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``embed_bronze_record`` — the bronze → RAG asset adapter.

The adapter never re-gates (bronze already ran the gate). It chunks,
embeds, and upserts via the existing ``axiom.rag`` primitives, citing
the human-readable ``source_path`` rather than the content-addressed
blob location.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from axiom.rag.ingest_router import Disposition, ProvenanceRule


def _build_bronze(tmp_path: Path, *, content: bytes, source_path: str, content_type: str = "text/markdown"):
    """Materialize a bronze write so we have a real BronzeWriteResult + content_path."""
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    item = FetchedItem(
        source_name="box-reports",
        item_id="42",
        display_name=Path(source_path).name,
        content=content,
        content_type=content_type,
        size=len(content),
        modified_at=datetime(2026, 5, 29, tzinfo=UTC),
        etag="e1",
        source_path=source_path,
        extra={"sha1": "deadbeef"},
    )
    writer = BronzeWriter(
        rules=[ProvenanceRule(pattern="/", disposition=Disposition.ALLOW, tier="rag-community")],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    return writer.write(item), item


class FakeStore:
    """Captures `upsert_chunks` calls so tests can assert against them."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upsert_chunks(self, chunks, embeddings=None, **kwargs) -> None:
        self.calls.append({"chunks": list(chunks), "embeddings": embeddings, **kwargs})


# ---------- happy path -----------------------------------------------------


def test_embed_allow_record_upserts_to_resolved_tier(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod

    record, item = _build_bronze(
        tmp_path,
        content=b"# Report\n\nbody paragraph one.\n\nbody paragraph two.\n",
        source_path="/Reports/q1.md",
    )

    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: [[0.0] * 4 for _ in texts])

    store = FakeStore()
    stats = re_mod.embed_bronze_record(record, item, store)

    assert stats.indexed is True
    assert stats.chunks_created >= 1
    assert len(store.calls) == 1
    call = store.calls[0]
    # chunks carry the human-readable source_path for citation
    assert all(c.source_path == "/Reports/q1.md" for c in call["chunks"])
    assert call["corpus"] == "rag-community"  # resolved tier from bronze
    assert call["checksum"] == record.content_hash
    assert call["data_source"] == "box-reports"


def test_embed_skips_quarantined_record(tmp_path: Path):
    """QUARANTINE items wait for human review — adapter does NOT embed."""
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    item = FetchedItem(
        source_name="box",
        item_id="1",
        display_name="x.md",
        content=b"# x",
        content_type="text/markdown",
        size=3,
        modified_at=None,
        etag=None,
        source_path="/Unknown/x.md",
        extra={},
    )
    writer = BronzeWriter(
        rules=[],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    record = writer.write(item)
    assert record.disposition is Disposition.QUARANTINE

    store = FakeStore()
    stats = re_mod.embed_bronze_record(record, item, store)
    assert stats.skipped_reason == "quarantine"
    assert store.calls == []


def test_embed_skips_excluded_record(tmp_path: Path):
    """EXCLUDE means there's no content to embed even if we wanted to."""
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    item = FetchedItem(
        source_name="box",
        item_id="2",
        display_name="p.zip",
        content=b"PK",
        content_type=None,
        size=2,
        modified_at=None,
        etag=None,
        source_path="/Licensed/p.zip",
        extra={},
    )
    writer = BronzeWriter(
        rules=[ProvenanceRule(pattern="/Licensed/", disposition=Disposition.EXCLUDE, reason="licensed")],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.ALLOW,
        default_tier="rag-community",
    )
    record = writer.write(item)
    assert record.content_path is None  # EXCLUDE never lands content

    store = FakeStore()
    stats = re_mod.embed_bronze_record(record, item, store)
    assert stats.skipped_reason == "exclude"
    assert store.calls == []


# ---------- embedding-provider behavior -----------------------------------


def test_embed_text_only_when_no_provider(tmp_path: Path, monkeypatch):
    """When no embedding provider is configured, embed_texts returns None.
    Adapter still upserts (text-only retrieval is supported)."""
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod

    record, item = _build_bronze(
        tmp_path,
        content=b"# X\n\nbody.\n",
        source_path="/x.md",
    )
    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)

    store = FakeStore()
    stats = re_mod.embed_bronze_record(record, item, store)
    assert stats.indexed is True
    assert store.calls[0]["embeddings"] is None


def test_embed_failure_does_not_upsert(tmp_path: Path, monkeypatch):
    """A configured embedder that RAISES must not land partial data —
    the document stays unindexed and is retried on re-run (#7 lesson)."""
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod

    record, item = _build_bronze(tmp_path, content=b"# X\n", source_path="/x.md")

    def boom(_):
        raise RuntimeError("embed provider unreachable")

    monkeypatch.setattr(re_mod.embedder, "embed_texts", boom)

    store = FakeStore()
    stats = re_mod.embed_bronze_record(record, item, store)
    assert stats.indexed is False
    assert stats.skipped_reason == "embed_failed"
    assert store.calls == []


# ---------- content-type routing ------------------------------------------


def test_embed_reads_content_from_bronze_blob(tmp_path: Path, monkeypatch):
    """The adapter sources bytes from `record.content_path`, not Box."""
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod

    payload = b"# heading\n\nparagraph.\n"
    record, item = _build_bronze(tmp_path, content=payload, source_path="/x.md")
    assert record.content_path is not None
    assert record.content_path.read_bytes() == payload

    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: [[0.0] for _ in texts])

    store = FakeStore()
    re_mod.embed_bronze_record(record, item, store)
    # Concatenated chunk text recovers the payload's words.
    all_text = "\n".join(c.text for c in store.calls[0]["chunks"])
    assert "heading" in all_text
    assert "paragraph" in all_text


def test_embed_uses_content_hash_as_checksum(tmp_path: Path, monkeypatch):
    """Checksum stamped on upserted chunks IS the bronze sha256 — the
    same hash will let the adapter short-circuit on a no-op re-fetch."""
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod

    payload = b"# Z\n\nsome content.\n"
    expected = hashlib.sha256(payload).hexdigest()
    record, item = _build_bronze(tmp_path, content=payload, source_path="/z.md")
    assert record.content_hash == expected

    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)
    store = FakeStore()
    re_mod.embed_bronze_record(record, item, store)
    assert store.calls[0]["checksum"] == expected
    assert store.calls[0]["content_hash"] == expected
