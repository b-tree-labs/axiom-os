# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Progress event stream for advanced ingest (spec-rag-ingest-advanced §6).

Both the TTY panel and the headless JSON stream are renderers over one internal
progress state, so the state + JSON renderer are fully unit-testable without a
terminal. The clock is injected for deterministic throughput.
"""

from __future__ import annotations

import json

from axiom.rag.ingest_progress import (
    JsonEventSink,
    ProgressState,
    RunInfo,
    format_progress,
)


class _FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_progress_state_counts():
    ps = ProgressState(files_total=3, clock=_FakeClock(0.0))
    ps.batch_done(10)
    ps.batch_done(5)
    ps.file_done()
    ps.file_skipped()
    ps.retry()
    snap = ps.snapshot()
    assert snap.chunks_done == 15
    assert snap.files_done == 1
    assert snap.files_skipped == 1
    assert snap.retries == 1
    assert snap.files_total == 3


def test_progress_state_throughput_uses_injected_clock():
    clock = _FakeClock(0.0)
    ps = ProgressState(files_total=1, clock=clock)
    ps.batch_done(10)
    ps.batch_done(10)
    clock.t = 2.0
    snap = ps.snapshot()
    assert snap.chunks_done == 20
    assert snap.elapsed_s == 2.0
    assert snap.throughput_cps == 10.0


def test_snapshot_to_dict_shape():
    ps = ProgressState(files_total=2, clock=_FakeClock(0.0))
    ps.batch_done(4)
    d = ps.snapshot().to_dict()
    for key in (
        "files_done",
        "files_total",
        "files_skipped",
        "chunks_done",
        "retries",
        "elapsed_s",
        "throughput_cps",
    ):
        assert key in d


def test_json_sink_emits_file_heartbeat_done():
    lines: list[str] = []
    sink = JsonEventSink(write=lines.append)
    ps = ProgressState(files_total=3, clock=_FakeClock(0.0))
    ps.batch_done(5)
    ps.file_done()

    sink.file_completed("docs/a.md", 5, ps.snapshot())
    sink.heartbeat(ps.snapshot())
    sink.run_completed(ps.snapshot())

    objs = [json.loads(x) for x in lines]
    assert objs[0]["event"] == "file"
    assert objs[0]["file"] == "docs/a.md"
    assert objs[0]["chunks"] == 5
    assert "throughput_cps" in objs[0]
    assert objs[1]["event"] == "heartbeat"
    assert objs[2]["event"] == "done"


def test_format_progress_contains_key_fields():
    info = RunInfo(
        path="docs/",
        target="local",
        corpus="rag-org",
        embedding_backend="nomic-embed-text (local)",
        checkpoint_path=".axi/rag-ingest/run.checkpoint",
    )
    ps = ProgressState(files_total=100, clock=_FakeClock(0.0))
    for _ in range(38):
        ps.file_done()
    ps.batch_done(20)
    block = format_progress(info, ps.snapshot())
    assert "38" in block and "100" in block  # files progress
    assert "rag-org" in block
    assert "local" in block
    assert "ETA" in block  # honest placeholder until calibration (U4) sets it
