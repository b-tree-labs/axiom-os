# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the gated run-ingest skill — the guarded_act wrap."""

from __future__ import annotations

from pathlib import Path

from axiom.rag.ingest_router import Disposition, ProvenanceRule


def _cfg(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    cfg = ConnectorConfig(
        name="box-reports",
        kind="box",
        bronze_root=str(tmp_path / "bronze"),
        rag_dsn_env="DP1_RAG_DSN_TEST",
        provenance_rules_file=None,
        default_disposition="allow",
        default_tier="rag-community",
        params={"folder_id": "100"},
    )
    save_connector(cfg, state_dir=tmp_path)
    return cfg


class FakeStore:
    def __init__(self):
        self.calls = []
        self.connected = False

    def connect(self):
        self.connected = True

    def upsert_chunks(self, chunks, embeddings=None, **kw):
        self.calls.append({"chunks": list(chunks), **kw})


class FakeBoxApi:
    def __init__(self, folders, files):
        self.folders, self.files = folders, files

    def get_json(self, path, params=None):
        if path.startswith("/folders/") and path.endswith("/items"):
            return {"entries": self.folders[path.split("/")[2]], "total_count": 0}
        if path.startswith("/files/"):
            return self.files[path.split("/")[2]][0]
        raise AssertionError(path)

    def get_bytes(self, path):
        return self.files[path.split("/")[2]][1]


def _build_pipeline(tmp_path, api):
    from axiom.extensions.builtins.data_platform.bronze import (
        BronzeWriter,
        FilesystemBronzeSink,
    )
    from axiom.extensions.builtins.data_platform.sources import BoxIngestSource

    source = BoxIngestSource(name="box-reports", folder_id="100", api_client=api)
    writer = BronzeWriter(
        rules=[ProvenanceRule(pattern="/Reports/", disposition=Disposition.ALLOW, tier="rag-community")],
        sink=FilesystemBronzeSink(root=tmp_path / "bronze"),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )
    return source, writer


def _file_entry(id_, name, modified, size):
    return {"type": "file", "id": id_, "name": name, "modified_at": modified, "size": size}


def _file_meta(id_, name, size, modified):
    return {
        "id": id_, "name": name, "size": size, "modified_at": modified, "etag": "e1",
        "path_collection": {
            "entries": [
                {"type": "folder", "name": "All Files"},
                {"type": "folder", "name": "Reports"},
            ]
        },
    }


# ---------- happy path -----------------------------------------------------


def test_run_ingest_proceeds_and_reports(tmp_path: Path, monkeypatch):
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.run_ingest import run_ingest

    _cfg(tmp_path)
    api = FakeBoxApi(
        folders={"100": [_file_entry("1", "a.md", "2026-05-29T12:00:00Z", 5)]},
        files={"1": (_file_meta("1", "a.md", 6, "2026-05-29T12:00:00Z"), b"# hi\n\n")},
    )
    source, writer = _build_pipeline(tmp_path, api)
    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)
    store = FakeStore()

    report = run_ingest(
        "box-reports",
        state_dir=tmp_path,
        store=store,
        source=source,
        writer=writer,
    )

    assert report.proceed is True
    assert report.items_seen == 1
    assert report.items_landed == 1
    assert store.connected is True
    assert len(store.calls) == 1


def test_run_ingest_empty_changeset_returns_proceed_zero(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.run_ingest import run_ingest

    _cfg(tmp_path)
    api = FakeBoxApi(folders={"100": []}, files={})
    source, writer = _build_pipeline(tmp_path, api)

    report = run_ingest(
        "box-reports",
        state_dir=tmp_path,
        store=FakeStore(),
        source=source,
        writer=writer,
    )
    assert report.proceed is True
    assert report.items_seen == 0
    assert report.items_landed == 0


# ---------- guarded_act gating --------------------------------------------


def test_run_ingest_refused_by_hard_disable_env(tmp_path: Path, monkeypatch):
    """`PLINTH_DATA_PLATFORM_INGEST_DISABLE=1` halts the run before any
    bronze write — the guarded_act env-prefix is derived from
    (agent, op_class)."""
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.run_ingest import run_ingest

    _cfg(tmp_path)
    api = FakeBoxApi(
        folders={"100": [_file_entry("1", "a.md", "2026-05-29T12:00:00Z", 5)]},
        files={"1": (_file_meta("1", "a.md", 6, "2026-05-29T12:00:00Z"), b"# hi\n\n")},
    )
    source, writer = _build_pipeline(tmp_path, api)
    store = FakeStore()

    monkeypatch.setenv("PLINTH_DATA_PLATFORM_INGEST_DISABLE", "1")

    report = run_ingest(
        "box-reports",
        state_dir=tmp_path,
        store=store,
        source=source,
        writer=writer,
    )
    assert report.proceed is False
    assert report.refused_reason == "hard_disable"
    assert store.calls == []


def test_run_ingest_volume_bound_refuses_or_confirms(tmp_path: Path, monkeypatch):
    """A huge changeset hits the volume bound — in `confirm` mode the
    guard refuses but emits `would_proceed` so the operator can re-run
    with explicit consent."""
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.run_ingest import run_ingest

    _cfg(tmp_path)
    entries = [_file_entry(str(i), f"f{i}.md", "2026-05-29T12:00:00Z", 5) for i in range(50)]
    files = {str(i): (_file_meta(str(i), f"f{i}.md", 4, "2026-05-29T12:00:00Z"), b"# x\n") for i in range(50)}
    api = FakeBoxApi(folders={"100": entries}, files=files)
    source, writer = _build_pipeline(tmp_path, api)

    # Default volume limit is 10 (AGENT_ACTION_DEFAULT_MAX_PER_TICK).
    monkeypatch.setenv("PLINTH_DATA_PLATFORM_INGEST_MAX_PER_TICK", "5")

    report = run_ingest(
        "box-reports",
        state_dir=tmp_path,
        store=FakeStore(),
        source=source,
        writer=writer,
        volume_mode="confirm",
    )
    assert report.proceed is False
    assert "needs_confirmation:volume" in report.refused_reason


def test_run_ingest_volume_off_allows_huge_batch(tmp_path: Path, monkeypatch):
    """`--volume-mode off` lets a backfill through without confirmation."""
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod
    from axiom.extensions.builtins.data_platform.agents.plinth.skills.run_ingest import run_ingest

    _cfg(tmp_path)
    entries = [_file_entry(str(i), f"f{i}.md", "2026-05-29T12:00:00Z", 5) for i in range(12)]
    files = {str(i): (_file_meta(str(i), f"f{i}.md", 4, "2026-05-29T12:00:00Z"), b"# x\n") for i in range(12)}
    api = FakeBoxApi(folders={"100": entries}, files=files)
    source, writer = _build_pipeline(tmp_path, api)
    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)

    report = run_ingest(
        "box-reports",
        state_dir=tmp_path,
        store=FakeStore(),
        source=source,
        writer=writer,
        volume_mode="off",
    )
    assert report.proceed is True
    assert report.items_landed == 12
