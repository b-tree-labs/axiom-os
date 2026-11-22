# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data diagnose`` reports placeholder counts per source (#1948).

The count is produced by ingest and kept in its per-source record; the check
reads only that record. It never scans bronze manifests or silver rows for it
(ADR-128: a health check reads a projection, never computes over a working
tier). The proof: the check still answers with the bronze tree gone.
"""

from __future__ import annotations

import shutil
from unittest.mock import patch

from axiom.extensions.builtins.data_platform import _herald
from axiom.extensions.builtins.data_platform import rag_embed as re_mod
from axiom.extensions.builtins.data_platform.bronze import BronzeWriter, FilesystemBronzeSink
from axiom.extensions.builtins.data_platform.ingest_run.placeholders import PlaceholderLedger
from axiom.extensions.builtins.data_platform.ingest_sink import IngestSink, PushItem
from axiom.extensions.builtins.data_platform.skills import diagnose, troubleshoot, verify
from axiom.rag.ingest_router import Disposition

LFS = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"c" * 64 + b"\nsize 9\n"


class _Ctx:
    def __init__(self, state_dir):
        self.state_dir = state_dir


def _ingest(tmp_path, monkeypatch):
    monkeypatch.setattr(_herald, "publish_event", lambda *a, **k: None)
    monkeypatch.setattr(re_mod.embedder, "embed_texts", lambda texts: None)
    writer = BronzeWriter(
        rules=[],
        sink=FilesystemBronzeSink(root=tmp_path / "bronze"),
        default_disposition=Disposition.ALLOW,
        default_tier="rag-community",
    )
    sink = IngestSink(writer=writer, placeholder_ledger=PlaceholderLedger.for_state_dir(tmp_path))
    sink.ingest(
        "example-source",
        [
            PushItem(item_id="p1", content=LFS, source_path="papers/report.pdf"),
            PushItem(item_id="p2", content=LFS + b"\n", source_path="papers/appendix.pdf"),
        ],
    )
    sink.ingest(
        "other-source",
        [
            PushItem(item_id="q1", content=b"%PDF-1.7\n", source_path="docs/ok.pdf"),
        ],
    )


def _diagnose(tmp_path):
    with (
        patch.object(verify, "_try_import", return_value=True),
        patch.object(diagnose.shutil, "which", return_value=None),
    ):
        return diagnose.run({}, ctx=_Ctx(tmp_path))


def test_diagnose_reports_placeholder_counts_per_source(tmp_path, monkeypatch):
    _ingest(tmp_path, monkeypatch)
    # Remove bronze: the check must not need it.
    shutil.rmtree(tmp_path / "bronze")

    result = _diagnose(tmp_path)

    finding = next(f for f in result.value["findings"] if f["check"] == "placeholders")
    assert finding["ok"] is False
    assert finding["per_source"] == {"example-source": 2}
    assert "example-source: 2 placeholder file(s)" in finding["detail"]
    assert "papers/report.pdf" in finding["detail"]
    assert "git lfs pull" in finding["remediation"]
    assert any("placeholder" in e for e in result.errors)


def test_no_placeholders_is_a_passing_check(tmp_path):
    result = _diagnose(tmp_path)

    finding = next(f for f in result.value["findings"] if f["check"] == "placeholders")
    assert finding["ok"] is True
    assert finding["per_source"] == {}
    assert result.errors == ["kubectl not on PATH"]


def test_troubleshoot_names_the_recovery():
    steps = troubleshoot._next_steps_for(
        [{"check": "placeholders", "ok": False, "per_source": {"example-source": 2}}]
    )
    assert any("re-upload" in s and "next pull" in s for s in steps)
