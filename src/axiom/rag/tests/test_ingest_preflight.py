# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Preflight for advanced ingest (spec-rag-ingest-advanced §4).

Before any embed call: scan the input (counts, bytes, supported/unsupported,
already-present), confirm the destination is reachable, estimate destination
bytes vs free space (abort with numbers if it won't fit), and advise (not
block) on pathological chunk sizes. Reachability + free space are injected so
the logic is unit-testable without a real store.
"""

from __future__ import annotations

from axiom.rag.ingest_preflight import (
    chunk_size_advice,
    estimate_destination_bytes,
    run_preflight,
    scan_input,
)


def _write(p, content: str):
    p.write_text(content, encoding="utf-8")
    return p


def test_scan_counts_supported_unsupported_and_sizes(tmp_path):
    _write(tmp_path / "normal.md", "x" * 1000)
    _write(tmp_path / "tiny.txt", "x" * 50)          # < 200 bytes → small
    _write(tmp_path / "big.md", "x" * 60_000)        # > 50KB → large
    _write(tmp_path / "spectrum.cnf", "x" * 100)     # unsupported
    _write(tmp_path / "pic.jpg", "x" * 100)          # unsupported

    scan = scan_input([tmp_path])
    assert scan.supported_files == 3
    assert scan.unsupported_files == 2
    assert scan.by_ext == {".md": 2, ".txt": 1}
    assert scan.small_files == 1
    assert scan.large_files == 1
    assert scan.total_bytes == 1000 + 50 + 60_000


def test_scan_counts_already_present(tmp_path):
    _write(tmp_path / "a.md", "x" * 500)
    _write(tmp_path / "b.md", "x" * 500)
    present = {str(tmp_path / "a.md")}
    scan = scan_input([tmp_path], already_present_fn=lambda p: str(p) in present)
    assert scan.already_present == 1


def test_estimate_destination_bytes_uses_embedding_overhead():
    # chunks × avg × (1 + overhead); 4× overhead → 5× total
    assert estimate_destination_bytes(100, 512, embedding_overhead=4.0) == 100 * 512 * 5


def test_chunk_size_advice_flags_small_dominated(tmp_path):
    _write(tmp_path / "a.txt", "x" * 50)
    _write(tmp_path / "b.txt", "x" * 60)
    _write(tmp_path / "c.txt", "x" * 70)
    advice = chunk_size_advice(scan_input([tmp_path]))
    assert advice is not None and "small" in advice.lower()


def test_chunk_size_advice_none_for_normal(tmp_path):
    _write(tmp_path / "a.md", "x" * 4000)
    _write(tmp_path / "b.md", "x" * 5000)
    assert chunk_size_advice(scan_input([tmp_path])) is None


def test_run_preflight_aborts_when_unreachable(tmp_path):
    _write(tmp_path / "a.md", "x" * 1000)
    report = run_preflight(
        [tmp_path],
        reachable_fn=lambda: (False, "DATABASE_URL not set"),
        free_bytes_fn=lambda: 10**12,
    )
    assert report.abort_reason is not None
    assert "unreachable" in report.abort_reason.lower()


def test_run_preflight_aborts_when_capacity_exceeded(tmp_path):
    _write(tmp_path / "big.md", "x" * 100_000)
    report = run_preflight(
        [tmp_path],
        reachable_fn=lambda: (True, "ok"),
        free_bytes_fn=lambda: 1000,  # way too small
        avg_chunk_bytes=512,
    )
    assert report.abort_reason is not None
    assert "space" in report.abort_reason.lower()
    assert report.capacity_ok is False


def test_run_preflight_happy_path(tmp_path):
    _write(tmp_path / "a.md", "x" * 4000)
    report = run_preflight(
        [tmp_path],
        reachable_fn=lambda: (True, "ok"),
        free_bytes_fn=lambda: 10**12,
    )
    assert report.abort_reason is None
    assert report.reachable is True
    assert report.capacity_ok is True
    assert report.estimated_chunks >= 1
