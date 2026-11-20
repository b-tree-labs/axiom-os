# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A connector's declared access labels reach every chunk it lands (#1971).

Ingest wrote no ``access_tier`` / ``classification``, so every chunk read as
public whatever its source. Now a connector declares them; the bronze writer
records them on each manifest (provenance); the embed step writes them on the
document and its chunks; a re-index reads them back off the manifest; and
``data.relabel`` brings what was indexed before into line, as a dry run first,
never loosening a label silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.rag.retriever import AccessContext, retrieve
from axiom.rag.sqlite_store import SQLiteRAGStore

from ..agents.plinth.connectors import (
    ConnectorConfig,
    declared_access_labels,
    load_connector,
    save_connector,
)
from ..ingest_sink import build_writer_for_config
from ..ingest_sink.core import IngestSink, PushItem
from ..orchestration.relabel import LOOSEN, TIGHTEN, UNCHANGED, apply_relabel, plan_relabel

TEXT = (
    b"The maintenance schedule lists every inspection of the pumping station, "
    b"the interval between them and who signs each one off."
)


@pytest.fixture(autouse=True)
def _text_only(monkeypatch):
    """No embedding provider: the store keeps text, retrieval runs on full text."""
    from ..rag_embed import embedder

    monkeypatch.setattr(embedder, "embed_texts", lambda texts: None)


@pytest.fixture
def store(tmp_path):
    s = SQLiteRAGStore(f"sqlite:///{tmp_path}/rag.db")
    s.connect()
    yield s
    s.close()


def _connector(tmp_path: Path, **labels) -> ConnectorConfig:
    return ConnectorConfig(
        name="example-source",
        kind="push",
        bronze_root=str(tmp_path / "bronze"),
        default_disposition="allow",
        default_tier="rag-org",
        **labels,
    )


def _ingest(config: ConnectorConfig, store, path: str = "docs/schedule.md") -> None:
    sink = IngestSink(writer=build_writer_for_config(config), store=store)
    sink.ingest(
        config.name,
        [
            PushItem(
                item_id=path,
                content=TEXT,
                content_type="text/markdown",
                source_path=path,
                display_name=Path(path).name,
            )
        ],
    )


def _paths(store, ctx: AccessContext) -> set[str]:
    return {
        c.source_path
        for c in retrieve(
            store, "maintenance schedule inspection", None, corpora=["rag-org"], access_context=ctx
        )
    }


# -- the declaration ----------------------------------------------------------


def test_a_connector_declares_its_labels_and_they_round_trip(tmp_path):
    cfg = _connector(tmp_path, access_tier="institutional", classification="internal-use")
    save_connector(cfg, state_dir=tmp_path)
    loaded = load_connector("example-source", state_dir=tmp_path)
    assert declared_access_labels(loaded) == {
        "access_tier": "institutional",
        "classification": "internal-use",
    }
    assert declared_access_labels(_connector(tmp_path)) is None


def test_a_tier_retrieval_cannot_read_is_refused_at_the_declaration(tmp_path):
    with pytest.raises(ValueError, match="example-source.*access_tier"):
        declared_access_labels(_connector(tmp_path, access_tier="restricted"))


# -- the issue's acceptance -----------------------------------------------------


def test_a_document_ingested_at_a_non_public_tier_is_withheld_from_a_public_reader(tmp_path, store):
    _ingest(_connector(tmp_path, access_tier="institutional"), store)

    assert _paths(store, AccessContext()) == set()
    assert _paths(store, AccessContext(max_access_tier="institutional")) == {"docs/schedule.md"}


def test_the_labels_are_recorded_on_the_bronze_manifest(tmp_path, store):
    _ingest(_connector(tmp_path, access_tier="course", classification="unclassified"), store)
    (manifest,) = (tmp_path / "bronze" / "example-source" / "_records").glob("**/*.json")
    assert json.loads(manifest.read_text())["access_labels"] == {
        "access_tier": "course",
        "classification": "unclassified",
    }


def test_an_undeclared_connector_lands_public_and_keeps_a_label_set_later(tmp_path, store):
    cfg = _connector(tmp_path)
    _ingest(cfg, store)
    assert store.document_labels("docs/schedule.md", "rag-org") == ("public", "unclassified")

    # A person (or the backfill) sets a label; a re-ingest that declares nothing keeps it.
    store.set_document_labels(
        "docs/schedule.md", "rag-org", access_tier="course", classification="unclassified"
    )
    _ingest(cfg, store)
    assert store.document_labels("docs/schedule.md", "rag-org") == ("course", "unclassified")
    assert _paths(store, AccessContext()) == set()


def test_a_reindex_applies_the_labels_from_the_manifest(tmp_path, store, monkeypatch):
    """data.reindex rebuilds a BronzeWriteResult from the manifest; the labels come with it."""
    from unittest.mock import MagicMock

    from ..skills import reindex

    _ingest(_connector(tmp_path, access_tier="institutional"), store)
    store.delete_document("docs/schedule.md", corpus="rag-org")

    class _Store:  # the skill builds a Postgres RAGStore; hand it the SQLite one
        def __new__(cls, dsn):
            return store

    monkeypatch.setattr("axiom.rag.store.RAGStore", _Store)
    monkeypatch.setattr(store, "_cur", lambda: _NoRows(), raising=False)
    reindex.run(
        {
            "bronze_root": str(tmp_path / "bronze"),
            "source": "example-source",
            "corpus": "rag-org",
            "dsn": "x",
        },
        MagicMock(),
    )
    assert store.document_labels("docs/schedule.md", "rag-org") == ("institutional", "unclassified")


class _NoRows:
    """``SELECT DISTINCT source_path FROM chunks``: nothing indexed yet."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return []


# -- the backfill ----------------------------------------------------------------


def _manifests(tmp_path: Path):
    for p in sorted((tmp_path / "bronze" / "example-source" / "_records").glob("**/*.json")):
        yield json.loads(p.read_text())


def test_the_backfill_dry_run_reports_each_change_and_writes_nothing(tmp_path, store):
    cfg = _connector(tmp_path)
    _ingest(cfg, store, "docs/a.md")
    _ingest(cfg, store, "docs/b.md")
    _ingest(cfg, store, "docs/c.md")
    store.set_document_labels(
        "docs/b.md", "rag-org", access_tier="classified", classification="unclassified"
    )
    store.set_document_labels(
        "docs/c.md", "rag-org", access_tier="institutional", classification="unclassified"
    )

    declared = {"access_tier": "institutional", "classification": "unclassified"}
    plan = plan_relabel(_manifests(tmp_path), store, source="example-source", declared=declared)
    kinds = {c.source_path: c.kind for c in plan.changes}
    assert kinds == {"docs/a.md": TIGHTEN, "docs/b.md": LOOSEN, "docs/c.md": UNCHANGED}
    report = plan.to_dict()
    assert report["counts"][TIGHTEN] == 1 and report["counts"][LOOSEN] == 1
    assert report["loosen_examples"][0] == {
        "source_path": "docs/b.md",
        "corpus": "rag-org",
        "from": ["classified", "unclassified"],
        "to": ["institutional", "unclassified"],
    }
    # Nothing written.
    assert store.document_labels("docs/a.md", "rag-org") == ("public", "unclassified")


def test_apply_writes_the_tightening_and_withholds_the_loosening(tmp_path, store):
    cfg = _connector(tmp_path)
    _ingest(cfg, store, "docs/a.md")
    _ingest(cfg, store, "docs/b.md")
    store.set_document_labels(
        "docs/b.md", "rag-org", access_tier="classified", classification="unclassified"
    )
    declared = {"access_tier": "institutional", "classification": "unclassified"}

    plan = plan_relabel(_manifests(tmp_path), store, source="example-source", declared=declared)
    written = apply_relabel(plan, store)
    assert written["documents"] == 1 and written["loosen_withheld"] == 1
    assert store.document_labels("docs/a.md", "rag-org") == ("institutional", "unclassified")
    assert store.document_labels("docs/b.md", "rag-org") == ("classified", "unclassified")
    assert _paths(store, AccessContext()) == set()

    apply_relabel(plan, store, allow_loosen=True)
    assert store.document_labels("docs/b.md", "rag-org") == ("institutional", "unclassified")


def test_relabel_is_a_data_verb_that_reports_unless_told_to_apply():
    from ..cli import _parser
    from ..skills import verbs

    assert "relabel" in verbs()
    args = _parser().parse_args(["relabel", "--source", "example-source"])
    assert args.apply is False and args.allow_loosen is False
    assert (
        _parser().parse_args(["relabel", "--source", "x", "--apply", "--allow-loosen"]).allow_loosen
        is True
    )
