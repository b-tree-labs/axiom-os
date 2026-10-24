# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Performance envelope for the mirror engine.

These are not microbenchmarks; they are guardrails that fail if a change
turns an O(1) per-pass operation into an O(n) one. A document that lives
for a year accumulates hundreds of versions and the state file must not
grow without bound; a large document must reconcile in time proportional
to its size, not its history; a noop pass on an unchanged large document
must not re-hash the world twice.

Thresholds are deliberately loose (10x-plus headroom over observed) so the
suite is stable on a loaded CI box; they exist to catch a regression of
kind, not of milliseconds.
"""

from __future__ import annotations

import json
import time

import pytest

from axiom.extensions.builtins.publishing.mirror import MirrorEngine, RemoteDoc


class PerfEndpoint:
    def __init__(self, text: str):
        self.history = [RemoteDoc(text=text, version="1")]
        self.reads = 0

    @property
    def current(self):
        return self.history[-1]

    def read(self):
        self.reads += 1
        return self.current

    def write(self, text, expected_version):
        if expected_version != self.current.version:
            from axiom.extensions.builtins.publishing.mirror import VersionConflict
            raise VersionConflict(expected_version, self.current.version)
        doc = RemoteDoc(text=text, version=str(int(self.current.version) + 1))
        self.history.append(doc)
        return doc.version

    def human_save(self, text):
        self.history.append(RemoteDoc(
            text=text, version=str(int(self.current.version) + 1)))

    def versions(self, limit=10):
        return list(reversed(self.history[-limit:]))


def test_state_scales_with_size_not_version_count(tmp_path):
    """State is O(document size), never O(history): it holds one copy of the
    last-synced text (load-bearing for stale-flush repair) plus a capped
    hash window. So its size after 50 versions and after 500 versions of the
    same-sized document is essentially equal — history does not accumulate."""
    def state_size_after(versions: int) -> int:
        endpoint = PerfEndpoint("body\n")
        d = tmp_path / f"run{versions}"
        d.mkdir()
        engine = MirrorEngine(endpoint=endpoint, mirror_path=d / "doc.md",
                              state_path=d / "state.json", base_history=20)
        engine.reconcile()
        for i in range(versions):
            endpoint.human_save(f"revision {i}\n")
            engine.reconcile()
        blob = json.loads((d / "state.json").read_text())
        assert len(blob["old_base_hashes"]) <= 20, "history cap breached"
        return (d / "state.json").stat().st_size

    at_50 = state_size_after(50)
    at_500 = state_size_after(500)
    # ten times the history, the same-sized state (within the capped window's
    # worth of hashes) — the invariant that history does not accumulate
    assert abs(at_500 - at_50) < 4 * 1024, (at_50, at_500)


def test_state_holds_one_document_copy_not_many(tmp_path):
    """With a 1 MB document, the state file is about one document — one copy
    of the last-synced text — not a copy per version."""
    big = ("x" * 1024 + "\n") * 1024  # ~1 MB
    endpoint = PerfEndpoint(big)
    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    engine.reconcile()
    for i in range(200):
        endpoint.human_save(big + f"r{i}\n")
        engine.reconcile()
    # one document plus JSON overhead, regardless of the 200 versions behind it
    assert state.stat().st_size < len(big) + 64 * 1024


def test_reconcile_scales_with_size_not_history(tmp_path):
    """A 1 MB document with 300 versions behind it must reconcile in time
    driven by the document size, not by how long it has lived."""
    big = ("x" * 1024 + "\n") * 1024  # ~1 MB
    endpoint = PerfEndpoint(big)
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()
    for i in range(300):
        endpoint.human_save(big + f"rev {i}\n")
        engine.reconcile()

    endpoint.human_save(big + "final\n")
    start = time.perf_counter()
    report = engine.reconcile()
    elapsed = time.perf_counter() - start
    assert report.action == "pull"
    # a ~1 MB hash + write is single-digit milliseconds; 1s is a vast ceiling
    # that only a genuinely superlinear regression could breach
    assert elapsed < 1.0, f"reconcile took {elapsed:.3f}s"


def test_noop_pass_is_cheap_on_a_large_document(tmp_path):
    big = ("y" * 1024 + "\n") * 1024  # ~1 MB
    endpoint = PerfEndpoint(big)
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()

    start = time.perf_counter()
    for _ in range(50):
        assert engine.reconcile().action == "noop"
    elapsed = time.perf_counter() - start
    # 50 noop passes over 1 MB: two hashes each; comfortably sub-second
    assert elapsed < 2.0, f"50 noops took {elapsed:.3f}s"


@pytest.mark.parametrize("size_mb", [1, 4])
def test_push_of_large_local_edit_is_linear(tmp_path, size_mb):
    body = ("z" * 1024 + "\n") * (1024 * size_mb)
    endpoint = PerfEndpoint("small start\n")
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()
    mirror.write_text(body)
    start = time.perf_counter()
    report = engine.reconcile()
    elapsed = time.perf_counter() - start
    assert report.action == "push"
    assert endpoint.current.text == body
    # generous linear ceiling: ~0.5s per MB is far above real hash+write cost
    assert elapsed < 0.5 * size_mb + 1.0, f"{size_mb}MB push took {elapsed:.3f}s"


def test_one_reconcile_reads_remote_a_bounded_number_of_times(tmp_path):
    """A quiet reconcile must not read the remote more than a small constant
    number of times — a regression that re-reads in a loop shows up here."""
    endpoint = PerfEndpoint("stable\n")
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()
    endpoint.reads = 0
    engine.reconcile()  # noop pass
    assert endpoint.reads <= 2
    endpoint.reads = 0
    endpoint.human_save("moved\n")
    engine.reconcile()  # pull pass
    assert endpoint.reads <= 2
