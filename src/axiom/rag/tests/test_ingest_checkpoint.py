# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Checkpoint & resume core for advanced ingest (spec-rag-ingest-advanced §7).

The contract: killing the process and re-running with --resume continues from
the last completed chunk batch with no double work. Batch-granularity so a
mid-file kill loses at most one in-flight batch. A file whose checksum changed
since the checkpoint is fully re-ingested. Resuming into a different target or
corpus than the checkpoint records is refused unless forced.
"""

from __future__ import annotations

from axiom.rag.ingest_checkpoint import (
    BatchRef,
    CheckpointMismatchError,
    PlannedFile,
    load_checkpoint,
    plan_batches,
    record_batch,
    remaining_batches,
    verify_resume_target,
)


def test_plan_batches_splits_by_chunk_count():
    files = [PlannedFile(file="a.md", checksum="cs1", n_chunks=25)]
    batches = plan_batches(files, batch_size=10)
    assert batches == [
        BatchRef("a.md", "cs1", (0, 10)),
        BatchRef("a.md", "cs1", (10, 20)),
        BatchRef("a.md", "cs1", (20, 25)),
    ]


def test_plan_batches_spans_multiple_files():
    files = [
        PlannedFile("a.md", "cs1", 5),
        PlannedFile("b.md", "cs2", 12),
    ]
    batches = plan_batches(files, batch_size=10)
    assert batches == [
        BatchRef("a.md", "cs1", (0, 5)),
        BatchRef("b.md", "cs2", (0, 10)),
        BatchRef("b.md", "cs2", (10, 12)),
    ]


def test_record_and_load_roundtrip(tmp_path):
    cp = tmp_path / "run.checkpoint"
    record_batch(cp, BatchRef("a.md", "cs1", (0, 10)), destination="local", corpus="rag-org")
    record_batch(cp, BatchRef("a.md", "cs1", (10, 20)), destination="local", corpus="rag-org")

    loaded = load_checkpoint(cp)
    assert loaded.destination == "local"
    assert loaded.corpus == "rag-org"
    assert loaded.completed == {
        BatchRef("a.md", "cs1", (0, 10)),
        BatchRef("a.md", "cs1", (10, 20)),
    }


def test_load_missing_checkpoint_is_empty(tmp_path):
    loaded = load_checkpoint(tmp_path / "nope.checkpoint")
    assert loaded.completed == set()
    assert loaded.destination is None


def test_remaining_excludes_completed_batches(tmp_path):
    cp = tmp_path / "run.checkpoint"
    files = [PlannedFile("a.md", "cs1", 25)]  # → 3 batches at bs=10
    # First two batches completed before the kill.
    record_batch(cp, BatchRef("a.md", "cs1", (0, 10)), destination="local", corpus="rag-org")
    record_batch(cp, BatchRef("a.md", "cs1", (10, 20)), destination="local", corpus="rag-org")

    remaining = remaining_batches(files, batch_size=10, loaded=load_checkpoint(cp))
    assert remaining == [BatchRef("a.md", "cs1", (20, 25))]


def test_changed_checksum_forces_full_reingest(tmp_path):
    cp = tmp_path / "run.checkpoint"
    record_batch(cp, BatchRef("a.md", "OLD", (0, 10)), destination="local", corpus="rag-org")
    record_batch(cp, BatchRef("a.md", "OLD", (10, 20)), destination="local", corpus="rag-org")

    # File now hashes differently → old batch refs don't apply; all new batches run.
    files = [PlannedFile("a.md", "NEW", 20)]
    remaining = remaining_batches(files, batch_size=10, loaded=load_checkpoint(cp))
    assert remaining == [
        BatchRef("a.md", "NEW", (0, 10)),
        BatchRef("a.md", "NEW", (10, 20)),
    ]


def test_no_checkpoint_returns_all_planned():
    files = [PlannedFile("a.md", "cs1", 15)]
    assert remaining_batches(files, batch_size=10, loaded=None) == plan_batches(files, 10)


def test_resume_target_mismatch_refused(tmp_path):
    cp = tmp_path / "run.checkpoint"
    record_batch(cp, BatchRef("a.md", "cs1", (0, 10)), destination="local", corpus="rag-org")
    loaded = load_checkpoint(cp)

    # Same target/corpus → ok.
    verify_resume_target(loaded, destination="local", corpus="rag-org")

    # Different corpus → refused.
    try:
        verify_resume_target(loaded, destination="local", corpus="rag-community")
        raise AssertionError("expected CheckpointMismatchError")
    except CheckpointMismatchError:
        pass

    # Forced → allowed.
    verify_resume_target(loaded, destination="peer:east", corpus="rag-community", force=True)
