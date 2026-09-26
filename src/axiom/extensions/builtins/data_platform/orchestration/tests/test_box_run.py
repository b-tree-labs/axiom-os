# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end test for ``run_box_to_rag`` — the pure-Python pipeline
that the Dagster orchestrator and the PLINTH ``run-ingest`` skill both
call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from axiom.rag.ingest_router import Disposition, ProvenanceRule


class FakeBoxApi:
    def __init__(self, *, folders, files):
        self.folders = folders
        self.files = files

    def get_json(self, path, params=None):
        if path.startswith("/folders/") and path.endswith("/items"):
            fid = path.split("/")[2]
            entries = self.folders.get(fid, [])
            return {"entries": entries, "total_count": len(entries)}
        if path.startswith("/files/"):
            fid = path.split("/")[2]
            return self.files[fid][0]
        raise AssertionError(path)

    def get_bytes(self, path):
        fid = path.split("/")[2]
        return self.files[fid][1]


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upsert_chunks(self, chunks, embeddings=None, **kwargs) -> None:
        self.calls.append({"chunks": list(chunks), "embeddings": embeddings, **kwargs})


def _file_entry(id_, name, modified, size):
    return {"type": "file", "id": id_, "name": name, "modified_at": modified, "size": size}


def _file_meta(id_, name, size, modified, etag="e1"):
    return {
        "id": id_,
        "name": name,
        "size": size,
        "modified_at": modified,
        "etag": etag,
        "path_collection": {
            "entries": [
                {"type": "folder", "name": "All Files"},
                {"type": "folder", "name": "Reports"},
            ]
        },
    }


def _build_pipeline(tmp_path: Path, *, api: FakeBoxApi):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    source = BoxIngestSource(name="box-reports", folder_id="100", api_client=api)
    writer = BronzeWriter(
        rules=[
            ProvenanceRule(
                pattern="/Reports/",
                disposition=Disposition.ALLOW,
                tier="rag-community",
            )
        ],
        sink=FilesystemBronzeSink(root=tmp_path),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    return source, writer


# ---------- end-to-end -----------------------------------------------------


def test_run_box_to_rag_full_path_landed(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.data_platform import orchestration
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod

    api = FakeBoxApi(
        folders={
            "100": [
                _file_entry("1", "a.md", "2026-05-29T12:00:00Z", 100),
                _file_entry("2", "b.md", "2026-05-29T12:00:00Z", 200),
            ]
        },
        files={
            "1": (
                _file_meta("1", "a.md", 21, "2026-05-29T12:00:00Z"),
                b"# A\n\nbody of file a.\n",
            ),
            "2": (
                _file_meta("2", "b.md", 21, "2026-05-29T12:00:00Z"),
                b"# B\n\nbody of file b.\n",
            ),
        },
    )
    source, writer = _build_pipeline(tmp_path, api=api)
    store = FakeStore()
    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)

    report = orchestration.run_box_to_rag(source=source, writer=writer, store=store)

    assert report.items_seen == 2
    assert report.items_landed == 2
    assert report.items_failed == 0
    assert len(report.bronze_results) == 2
    assert all(r.disposition is Disposition.ALLOW for r in report.bronze_results)
    # Both files reached the store (two upsert calls).
    assert len(store.calls) == 2
    assert all(call["corpus"] == "rag-community" for call in store.calls)


def test_run_box_to_rag_passes_since_watermark(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.data_platform import orchestration
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod

    api = FakeBoxApi(
        folders={
            "100": [
                _file_entry("1", "old.md", "2026-05-20T12:00:00Z", 100),
                _file_entry("2", "new.md", "2026-05-29T12:00:00Z", 200),
            ]
        },
        files={
            "1": (_file_meta("1", "old.md", 5, "2026-05-20T12:00:00Z"), b"# old"),
            "2": (_file_meta("2", "new.md", 5, "2026-05-29T12:00:00Z"), b"# new"),  # 5 bytes each ✓
        },
    )
    source, writer = _build_pipeline(tmp_path, api=api)
    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)

    since = datetime(2026, 5, 25, tzinfo=UTC)
    report = orchestration.run_box_to_rag(
        source=source, writer=writer, store=FakeStore(), since=since
    )
    assert report.items_seen == 1
    assert report.items_landed == 1


def test_run_box_to_rag_continues_when_one_item_fails_to_embed(tmp_path: Path, monkeypatch):
    """A single embed failure must NOT abort the run — the rest still land."""
    from axiom.extensions.builtins.data_platform import orchestration
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod

    api = FakeBoxApi(
        folders={
            "100": [
                _file_entry("1", "a.md", "2026-05-29T12:00:00Z", 100),
                _file_entry("2", "b.md", "2026-05-29T12:00:00Z", 200),
            ]
        },
        files={
            "1": (_file_meta("1", "a.md", 4, "2026-05-29T12:00:00Z"), b"# a\n"),
            "2": (_file_meta("2", "b.md", 4, "2026-05-29T12:00:00Z"), b"# b\n"),
        },
    )
    source, writer = _build_pipeline(tmp_path, api=api)

    calls = {"n": 0}

    def flaky_embed(texts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first one fails")
        return None

    monkeypatch.setattr(re_mod.embedder, "embed_texts", flaky_embed)

    report = orchestration.run_box_to_rag(source=source, writer=writer, store=FakeStore())
    assert report.items_seen == 2
    assert report.items_landed == 1  # one embed failed
    assert report.items_failed == 1


def test_run_box_to_rag_with_embed_false_lands_bronze_only(tmp_path: Path, monkeypatch):
    """`embed=False` runs Box → bronze and STOPS. Useful for backfills
    where the embedding pass is a later asset."""
    from axiom.extensions.builtins.data_platform import orchestration

    api = FakeBoxApi(
        folders={"100": [_file_entry("1", "a.md", "2026-05-29T12:00:00Z", 100)]},
        files={"1": (_file_meta("1", "a.md", 4, "2026-05-29T12:00:00Z"), b"# a\n")},
    )
    source, writer = _build_pipeline(tmp_path, api=api)
    store = FakeStore()

    report = orchestration.run_box_to_rag(
        source=source, writer=writer, store=store, embed=False
    )
    assert report.items_seen == 1
    assert len(report.bronze_results) == 1
    assert store.calls == []  # no embed
    assert report.embed_stats == []
