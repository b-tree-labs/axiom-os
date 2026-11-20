# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The connector pull refuses placeholders and says so once per run (#1948).

This is the path the reported incident took: a connector pulled ``.pdf`` files
that were Git LFS pointers. Each one must be recorded, kept out of the RAG, and
counted, and the run must raise one alert for the source, not one per file.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.data_platform import _herald
from axiom.extensions.builtins.data_platform import rag_embed as re_mod
from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
    save_connector,
)
from axiom.extensions.builtins.data_platform.agents.plinth.skills.run_ingest import run_ingest
from axiom.extensions.builtins.data_platform.bronze import BronzeWriter, FilesystemBronzeSink
from axiom.extensions.builtins.data_platform.ingest_run.placeholders import PlaceholderLedger
from axiom.extensions.builtins.data_platform.sources import BoxIngestSource
from axiom.rag.ingest_router import Disposition

NOTE = b"# Note\n\nA short operating note with enough prose to retrieve on.\n"


def _lfs(n: int) -> bytes:
    return (
        b"version https://git-lfs.github.com/spec/v1\n"
        + f"oid sha256:{n:064x}\nsize 1048576\n".encode()
    )


class FakeStore:
    def __init__(self):
        self.calls = []

    def connect(self):
        pass

    def upsert_chunks(self, chunks, embeddings=None, **kw):
        self.calls.append(kw)


class FakeApi:
    def __init__(self, files):
        self.files = files

    def get_json(self, path, params=None):
        if path.startswith("/folders/"):
            entries = [
                {
                    "type": "file",
                    "id": i,
                    "name": n,
                    "modified_at": "2026-01-01T00:00:00Z",
                    "size": len(b),
                }
                for i, (n, b) in self.files.items()
            ]
            return {"entries": entries, "total_count": len(entries)}
        fid = path.split("/")[2]
        name, body = self.files[fid]
        return {
            "id": fid,
            "name": name,
            "size": len(body),
            "modified_at": "2026-01-01T00:00:00Z",
            "etag": "e1",
            "path_collection": {
                "entries": [
                    {"type": "folder", "name": "All Files"},
                    {"type": "folder", "name": "papers"},
                ]
            },
        }

    def get_bytes(self, path):
        return self.files[path.split("/")[2]][1]


def test_a_pull_of_lfs_pointers_records_skips_and_alerts_once(tmp_path: Path, monkeypatch):
    save_connector(
        ConnectorConfig(
            name="example-source",
            kind="box",
            bronze_root=str(tmp_path / "bronze"),
            rag_dsn_env="DP1_RAG_DSN_TEST",
            provenance_rules_file=None,
            default_disposition="allow",
            default_tier="rag-community",
            params={"folder_id": "100"},
        ),
        state_dir=tmp_path,
    )
    api = FakeApi(
        {
            "1": ("report.pdf", _lfs(1)),
            "2": ("appendix.pdf", _lfs(2)),
            "3": ("readme.md", NOTE),
        }
    )
    source = BoxIngestSource(name="example-source", folder_id="100", api_client=api)
    writer = BronzeWriter(
        rules=[],
        sink=FilesystemBronzeSink(root=tmp_path / "bronze"),
        default_disposition=Disposition.ALLOW,
        default_tier="rag-community",
    )
    sent: list[dict] = []
    monkeypatch.setattr(
        _herald,
        "publish_event",
        lambda intent, summary, **kw: sent.append({"summary": summary, **kw}),
    )
    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)
    store = FakeStore()

    report = run_ingest(
        "example-source",
        state_dir=tmp_path,
        store=store,
        source=source,
        writer=writer,
        volume_mode="off",
        max_workers=1,
    )

    assert report.items_landed == 1  # the real note only
    assert len(store.calls) == 1
    assert len(sent) == 1
    assert "2 placeholder" in sent[0]["summary"]
    assert report.funnel["metrics"]["placeholders"] == 2
    dropped = next(s for s in report.funnel["funnel"] if s["stage"] == "loaded")["dropped"]
    assert dropped == {"placeholder: git-lfs pointer": 2}

    [row] = PlaceholderLedger.for_state_dir(tmp_path).summary()
    assert row["source"] == "example-source" and row["count"] == 2
