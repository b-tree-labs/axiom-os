# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.reindex`` reprocesses a record when its extractor changed (#1947).

The skip set was every ``source_path`` in ``chunks``, in any corpus, so a new
extractor version changed nothing unless the corpus was deleted first. The skip
key now includes the extractor record: a record is skipped only when it is
indexed in the target corpus AND the extractors the registry would run now
match the ones recorded on the document (``documents.parser``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.rag.ingest_router import Disposition, ProvenanceRule

EXTRACTOR = "site.extract.widget_sheet"


def _register(version: str) -> None:
    from axiom.extensions.builtins.library.extract import registry
    from axiom.extensions.builtins.library.extract.contract import ExtractionResult, TextSection

    def extract(ctx):
        body = (
            f"Widget sheet read by version {version}. The widget measures forty units across.\n\n"
        )
        return ExtractionResult(text=[TextSection(text=body * 3)])

    registry.register(
        {
            "name": EXTRACTOR,
            "version": version,
            "summary": "Widget sheets",
            "outputs": ["text"],
            "tags": ["rag"],
            "match": {"extensions": [".widget"]},
        },
        extract,
        scope="site",
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    from axiom.extensions.builtins.library.extract import registry

    registry.clear_local()
    yield
    registry.clear_local()


def _land(root: Path, name: str, content: bytes, content_type: str | None = None) -> None:
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
    BronzeWriter(
        rules=[ProvenanceRule(pattern="/", disposition=Disposition.ALLOW, tier="rag-community")],
        sink=FilesystemBronzeSink(root=root),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    ).write(item)


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A bronze tree with one widget sheet and one Markdown note, and a SQLite RAG store."""
    import axiom.rag.store as rag_store
    from axiom.extensions.builtins.data_platform import rag_embed
    from axiom.rag.sqlite_store import SQLiteRAGStore

    bronze = tmp_path / "bronze"
    _land(bronze, "pump.widget", b"\x00\x01binary widget sheet")
    _land(
        bronze,
        "notes.md",
        b"# Notes\n\nPlain notes about the pump, read directly.\n" * 3,
        "text/markdown",
    )

    db = f"sqlite:///{tmp_path}/rag.db"
    monkeypatch.setattr(rag_store, "RAGStore", lambda dsn: SQLiteRAGStore(db))
    monkeypatch.setattr(rag_embed.embedder, "embed_texts", lambda texts: None)

    def store() -> SQLiteRAGStore:
        s = SQLiteRAGStore(db)
        s.connect()
        return s

    return bronze, store


def _reindex(bronze: Path, corpus: str = "rag-community"):
    from axiom.extensions.builtins.data_platform.skills import reindex

    result = reindex.run(
        {"bronze_root": str(bronze), "source": "share", "corpus": corpus, "dsn": "sqlite://unused"},
        ctx=None,
    )
    assert result.ok, result.errors
    return result


def _summary(result) -> str:
    return next(a for a in result.actions_taken if a.startswith("reindexed"))


def test_a_new_extractor_version_reprocesses_only_its_records(node):
    bronze, store = node
    _register("1.0.0")
    assert "reindexed 2 docs" in _summary(_reindex(bronze))
    assert (
        store().get_document("/Specs/pump.widget", "rag-community")["parser"]["version"] == "1.0.0"
    )

    # Same version: nothing to do.
    assert "reindexed 0 docs" in _summary(_reindex(bronze))

    # v2 registered: the widget sheet is reprocessed, the Markdown note is not.
    _register("2.0.0")
    first_v2 = _summary(_reindex(bronze))
    assert "reindexed 1 docs" in first_v2
    assert "1 with a changed extractor" in first_v2
    doc = store().get_document("/Specs/pump.widget", "rag-community")
    assert doc["parser"]["version"] == "2.0.0"

    # Re-run with v2: skipped.
    assert "reindexed 0 docs" in _summary(_reindex(bronze))


def test_the_skip_set_is_the_corpus_the_record_goes_to(node):
    """A document of the same path in another corpus is not "already indexed" here."""
    from axiom.rag.chunker import Chunk

    bronze, store = node
    _register("1.0.0")
    other = store()
    chunk = Chunk(
        text="b", source_path="/Specs/pump.widget", source_title="P", chunk_index=0, start_line=1
    )
    other.upsert_chunks(
        [chunk],
        corpus="rag-other",
        parser={
            "engine": EXTRACTOR,
            "version": "1.0.0",
            "planned": [{"name": EXTRACTOR, "version": "1.0.0"}],
        },
    )
    assert "reindexed 2 docs" in _summary(_reindex(bronze))
    assert store().get_document("/Specs/pump.widget", "rag-community") is not None


def test_the_store_reads_one_corpus_s_parser_records(tmp_path):
    from axiom.rag.chunker import Chunk
    from axiom.rag.sqlite_store import SQLiteRAGStore

    s = SQLiteRAGStore(f"sqlite:///{tmp_path}/rag.db")
    s.connect()
    chunk = Chunk(
        text="b",
        source_path="a.widget",
        source_title="A",
        chunk_index=0,
        start_line=1,
        source_type="w",
    )
    s.upsert_chunks([chunk], corpus="one", parser={"engine": EXTRACTOR, "version": "1.0.0"})
    s.upsert_chunks([chunk], corpus="two")
    assert s.document_parsers("one") == {"a.widget": {"engine": EXTRACTOR, "version": "1.0.0"}}
    assert s.document_parsers("two") == {"a.widget": None}


# ------------------------------------------------------------------ the verb


def test_axi_data_reindex_is_a_verb():
    from axiom.extensions.builtins.data_platform import skills as data_skills
    from axiom.extensions.builtins.data_platform.cli import _parser

    assert data_skills.bind_default().has("data.reindex")
    args = _parser().parse_args(
        ["reindex", "--source", "share", "--corpus", "rag-community", "--bronze-root", "/b"]
    )
    assert (args.verb, args.source, args.corpus, args.bronze_root) == (
        "reindex",
        "share",
        "rag-community",
        "/b",
    )


def test_axi_data_reindex_runs(node, capsys):
    from axiom.extensions.builtins.data_platform.cli import main

    bronze, store = node
    _register("1.0.0")
    code = main(
        ["reindex", "--source", "share", "--corpus", "rag-community", "--bronze-root", str(bronze)]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "reindexed 2 docs" in out
    assert store().get_document("/Specs/pump.widget", "rag-community") is not None


def test_one_bronze_root_default_for_the_data_verbs(monkeypatch):
    from axiom.extensions.builtins.data_platform.bronze import default_bronze_root
    from axiom.extensions.builtins.data_platform.skills import reindex

    monkeypatch.delenv("DP1_BRONZE_ROOT", raising=False)
    from axiom.infra.paths import get_user_state_dir

    # Under the node's state directory, as every data verb reads it (#2075).
    assert default_bronze_root() == get_user_state_dir() / "bronze"
    assert reindex.bronze_root_for({}) == default_bronze_root()
    assert reindex.bronze_root_for({"bronze_root": "/b"}) == Path("/b")
