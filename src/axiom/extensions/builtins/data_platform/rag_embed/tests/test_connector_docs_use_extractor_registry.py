# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Connector bronze records extract through the extractor registry (#1946).

Uploads ran the ADR-213 extractor registry; connector pulls called
``axiom.rag.extract.extract_text`` directly, so a contributed extractor never
saw a document that arrived through ``axi data ingest`` or ``data.reindex``.
These tests register a contributed extractor and show it runs on a connector
bronze record on both paths, and that its name and version are recorded on
the document.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.rag.ingest_router import Disposition, ProvenanceRule

EXTRACTOR = "site.extract.widget_sheet"
WIDGET_TEXT = "Widget sheet section one. The widget measures forty units across.\n\n" * 3


def _widget_extractor(calls: list[str]):
    from axiom.extensions.builtins.library.extract.contract import ExtractionResult, TextSection

    def extract(ctx):
        calls.append(ctx.file_name)
        return ExtractionResult(text=[TextSection(text=WIDGET_TEXT, title="")])

    return extract


@pytest.fixture
def widget_v1():
    """A contributed extractor for ``.widget`` files, version 1.0.0; cleared after."""
    from axiom.extensions.builtins.library.extract import registry

    calls: list[str] = []
    registry.register(
        {
            "name": EXTRACTOR,
            "version": "1.0.0",
            "summary": "Widget sheets",
            "outputs": ["text"],
            "tags": ["rag"],
            "match": {"extensions": [".widget", ".png"]},
        },
        _widget_extractor(calls),
        scope="site",
    )
    yield calls
    registry.clear_local()


def _bronze(tmp_path: Path, *, name: str, content: bytes, content_type: str | None):
    from axiom.extensions.builtins.data_platform.bronze import BronzeWriter, FilesystemBronzeSink
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    item = FetchedItem(
        source_name="share",
        item_id=f"id-{name}",
        display_name=name,
        content=content,
        content_type=content_type,
        size=len(content),
        modified_at=datetime(2026, 11, 1, tzinfo=UTC),
        etag="e1",
        source_path=f"/Specs/{name}",
        extra={},
    )
    writer = BronzeWriter(
        rules=[ProvenanceRule(pattern="/", disposition=Disposition.ALLOW, tier="rag-community")],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    return writer.write(item), item


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upsert_chunks(self, chunks, embeddings=None, **kwargs) -> None:
        self.calls.append({"chunks": list(chunks), **kwargs})


@pytest.fixture
def no_embed(monkeypatch):
    from axiom.extensions.builtins.data_platform import rag_embed

    monkeypatch.setattr(rag_embed.embedder, "embed_texts", lambda texts: None)


def test_a_contributed_extractor_runs_on_a_connector_record(tmp_path, widget_v1, no_embed):
    from axiom.extensions.builtins.data_platform.rag_embed import embed_bronze_record

    record, item = _bronze(
        tmp_path, name="pump.widget", content=b"\x00\x01binary", content_type=None
    )
    store = _Recorder()

    stats = embed_bronze_record(record, item, store)

    assert stats.indexed, stats.skipped_reason
    # The extractor saw the original name, not the content-addressed blob name.
    assert widget_v1 == ["pump.widget"]
    assert "forty units" in " ".join(c.text for c in store.calls[0]["chunks"])
    parser = store.calls[0]["parser"]
    assert parser["engine"] == EXTRACTOR
    assert parser["version"] == "1.0.0"
    assert parser["extractors"] == [{"name": EXTRACTOR, "version": "1.0.0"}]


def test_an_image_is_extractable_when_an_extractor_takes_it(tmp_path, widget_v1, no_embed):
    from axiom.extensions.builtins.data_platform.rag_embed import embed_bronze_record

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    record, item = _bronze(tmp_path, name="board.png", content=png, content_type="image/png")
    store = _Recorder()

    stats = embed_bronze_record(record, item, store)

    assert stats.indexed, stats.skipped_reason
    assert store.calls[0]["parser"]["engine"] == EXTRACTOR


def test_no_extractor_text_falls_back_to_extract_text(tmp_path, no_embed, monkeypatch):
    """A format no extractor yields text for keeps today's extraction."""
    from axiom.extensions.builtins.data_platform import rag_embed

    monkeypatch.setattr(
        rag_embed.embedder,
        "extract_text",
        lambda path: "Fallback body text for a legacy document.\n" * 3,
    )
    record, item = _bronze(tmp_path, name="old.doc", content=b"not really a doc", content_type=None)
    store = _Recorder()

    stats = rag_embed.embed_bronze_record(record, item, store)

    assert stats.indexed, stats.skipped_reason
    assert "Fallback body" in store.calls[0]["chunks"][0].text
    assert store.calls[0]["parser"]["engine"] == "axiom.rag.extract"


def test_a_markdown_record_keeps_its_direct_read(tmp_path, no_embed):
    """The Library's derived rag.md arrives as text/markdown: it is already extracted text,
    so the registry must not run on it a second time."""
    from axiom.extensions.builtins.data_platform.rag_embed import embed_bronze_record
    from axiom.extensions.builtins.library.extract import registry

    calls: list[str] = []
    registry.register(
        {
            "name": "site.extract.markdown_spy",
            "version": "1.0.0",
            "summary": "spy",
            "outputs": ["text"],
            "tags": ["rag"],
            "priority": 100,
            "match": {"extensions": [".md"], "mime": ["text/markdown"]},
        },
        _widget_extractor(calls),
        scope="site",
    )
    try:
        record, item = _bronze(
            tmp_path,
            name="item.pdf.md",
            content=b"# Title\n\nAlready extracted text.\n",
            content_type="text/markdown",
        )
        store = _Recorder()
        stats = embed_bronze_record(record, item, store)
    finally:
        registry.clear_local()

    assert stats.indexed
    assert calls == []
    assert store.calls[0].get("parser") is None


# ---------------------------------------------------------------- data.reindex


class _ReindexStore:
    """A RAGStore stand-in: what the data.reindex skill touches, nothing more."""

    instances: list[_ReindexStore] = []

    def __init__(self, dsn: str) -> None:
        self.calls: list[dict] = []
        _ReindexStore.instances.append(self)

    def connect(self) -> None:
        return None

    @contextlib.contextmanager
    def _cur(self):
        class _C:
            def execute(self, *a, **k):
                return None

            def fetchall(self):
                return []

        yield _C()

    def document_parsers(self, corpus: str) -> dict:
        return {}

    def upsert_chunks(self, chunks, embeddings=None, **kwargs) -> None:
        self.calls.append({"chunks": list(chunks), **kwargs})


def test_a_contributed_extractor_runs_during_data_reindex(
    tmp_path, widget_v1, no_embed, monkeypatch
):
    import axiom.rag.store as rag_store
    from axiom.extensions.builtins.data_platform.skills import reindex

    _bronze(tmp_path, name="valve.widget", content=b"\x00\x02binary", content_type=None)
    _ReindexStore.instances.clear()
    monkeypatch.setattr(rag_store, "RAGStore", _ReindexStore)

    result = reindex.run(
        {
            "bronze_root": str(tmp_path),
            "source": "share",
            "corpus": "rag-community",
            "dsn": "postgresql://unused",
        },
        ctx=None,
    )

    assert result.ok, result.errors
    assert widget_v1 == ["valve.widget"]
    (store,) = _ReindexStore.instances
    assert store.calls[0]["parser"]["engine"] == EXTRACTOR
    assert store.calls[0]["parser"]["version"] == "1.0.0"
