# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance for #1948: placeholders are recorded, flagged once, never embedded.

A source pulled several hundred ``.pdf`` files that were Git LFS pointers. They
landed, extraction failed quietly, and they were missing from retrieval for
months with nothing said. Here an LFS pointer named ``.pdf`` goes through
ingest: it is recorded in the bronze manifest as a placeholder, it is not
embedded, exactly one alert is raised for the source (not one per file), and
the per-source count is written by ingest where a health check can read it.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.data_platform import _herald
from axiom.extensions.builtins.data_platform import rag_embed as re_mod
from axiom.extensions.builtins.data_platform.bronze import BronzeWriter, FilesystemBronzeSink
from axiom.extensions.builtins.data_platform.ingest_run.placeholders import PlaceholderLedger
from axiom.extensions.builtins.data_platform.ingest_sink import IngestSink, PushItem
from axiom.rag.ingest_router import Disposition


def _lfs(n: int) -> bytes:
    return (
        b"version https://git-lfs.github.com/spec/v1\n"
        + f"oid sha256:{n:064x}\nsize 1048576\n".encode()
    )


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def connect(self) -> None:
        pass

    def upsert_chunks(self, chunks, embeddings=None, **kwargs) -> None:
        self.calls.append({"chunks": list(chunks), **kwargs})


@pytest.fixture
def alerts(monkeypatch):
    sent: list[dict] = []

    def capture(intent, summary, **kw):
        sent.append({"intent": intent, "summary": summary, **kw})
        return "receipt-1"

    monkeypatch.setattr(_herald, "publish_event", capture)
    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)
    return sent


def _sink(tmp_path, store):
    writer = BronzeWriter(
        rules=[],
        sink=FilesystemBronzeSink(root=tmp_path / "bronze"),
        default_disposition=Disposition.ALLOW,
        default_tier="rag-community",
    )
    return IngestSink(
        writer=writer, store=store, placeholder_ledger=PlaceholderLedger(tmp_path / "health")
    )


def _items():
    real = b"# Note\n\nA short operating note with enough prose to retrieve on.\n"
    return [
        PushItem(item_id="p1", content=_lfs(1), source_path="papers/report.pdf"),
        PushItem(item_id="p2", content=_lfs(2), source_path="papers/appendix.pdf"),
        PushItem(item_id="p3", content=_lfs(3), source_path="papers/figures.pdf"),
        PushItem(item_id="n1", content=real, source_path="notes/readme.md"),
    ]


def test_an_lfs_pointer_named_pdf_is_recorded_flagged_and_not_embedded(tmp_path, alerts):
    store = FakeStore()
    result = _sink(tmp_path, store).ingest("example-source", _items())

    # Recorded in the bronze manifest with its disposition and reason.
    by_id = {d.item_id: d for d in result.items}
    assert by_id["p1"].disposition == "quarantined"
    assert by_id["p1"].reason == "placeholder: git-lfs pointer"
    assert by_id["p1"].indexed is False
    manifests = list((tmp_path / "bronze" / "example-source").rglob("*.json"))
    sidecars = {json.loads(p.read_text())["item_id"]: json.loads(p.read_text()) for p in manifests}
    assert sidecars["p1"]["reason"] == "placeholder: git-lfs pointer"
    assert sidecars["p1"]["disposition"] == "quarantine"

    # Not embedded: only the real note reached the store.
    assert [c["data_source"] for c in store.calls] == ["example-source"]
    assert by_id["n1"].indexed is True

    # Exactly one alert for the source, naming the count and one example path,
    # with the recovery hint.
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["intent"] == "data.ingest.placeholders"
    assert "example-source" in alert["summary"]
    assert "3 placeholder" in alert["summary"]
    assert "papers/report.pdf" in alert["summary"]
    assert "git lfs pull" in alert["body"]
    assert "next pull" in alert["body"]
    assert alert["cause"].subject == "example-source"

    # The run's own funnel says so.
    assert result.funnel["metrics"]["placeholders"] == 3


def test_ingest_writes_the_per_source_count_a_health_check_reads(tmp_path, alerts):
    _sink(tmp_path, FakeStore()).ingest("example-source", _items())

    rows = PlaceholderLedger(tmp_path / "health").summary()
    assert rows == [
        {
            "source": "example-source",
            "count": 3,
            "example": "papers/report.pdf",
            "kinds": {"placeholder: git-lfs pointer": 3},
            "updated_at": rows[0]["updated_at"],
        }
    ]


def test_a_clean_run_raises_nothing(tmp_path, alerts):
    real = b"# Note\n\nA short operating note with enough prose to retrieve on.\n"
    _sink(tmp_path, FakeStore()).ingest(
        "example-source", [PushItem(item_id="n1", content=real, source_path="notes/readme.md")]
    )

    assert alerts == []
    assert PlaceholderLedger(tmp_path / "health").summary() == []


def test_the_real_file_arriving_clears_the_count(tmp_path, alerts):
    sink = _sink(tmp_path, FakeStore())
    sink.ingest("example-source", _items())

    # The source re-uploaded one real file; the next pull lands it.
    sink.ingest(
        "example-source",
        [PushItem(item_id="p1", content=b"%PDF-1.7\n", source_path="papers/report.pdf")],
    )

    [row] = PlaceholderLedger(tmp_path / "health").summary()
    assert row["count"] == 2
    assert row["example"] != "papers/report.pdf"
    # The second run found no new placeholder, so it raised nothing.
    assert len(alerts) == 1
