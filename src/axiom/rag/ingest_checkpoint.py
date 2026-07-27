# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Checkpoint & resume core for advanced ingest (spec-rag-ingest-advanced §7).

A long ingest that can't survive a SIGINT or a network drop is brittle, not
long-running. The contract: at any moment, killing the process and re-running
with ``--resume`` continues from the last completed chunk *batch* with no
double work. Batch granularity (not per-chunk) keeps write amplification down
while still surviving a mid-file kill.

This module is pure logic + append-only file I/O — no store, no network — so it
is fully unit-testable. The orchestrator (U5) wires it to the real ingest loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.state import locked_append_jsonl


@dataclass(frozen=True)
class BatchRef:
    """A unit of completed work: a chunk range of one file at a known checksum."""

    file: str
    checksum: str
    chunk_range: tuple[int, int]  # [start, end)


@dataclass(frozen=True)
class PlannedFile:
    """A file slated for ingest and how many chunks it will produce."""

    file: str
    checksum: str
    n_chunks: int


@dataclass
class LoadedCheckpoint:
    destination: str | None
    corpus: str | None
    completed: set[BatchRef]


class CheckpointMismatchError(RuntimeError):
    """Resume requested into a different target/corpus than the checkpoint records."""


def plan_batches(files: list[PlannedFile], batch_size: int) -> list[BatchRef]:
    """Expand planned files into batch-granularity work units."""
    batches: list[BatchRef] = []
    for f in files:
        for start in range(0, f.n_chunks, batch_size):
            end = min(start + batch_size, f.n_chunks)
            batches.append(BatchRef(f.file, f.checksum, (start, end)))
    return batches


def record_batch(
    checkpoint_path: Path,
    batch: BatchRef,
    *,
    destination: str,
    corpus: str,
    batch_id: str = "",
    ts: str | None = None,
) -> None:
    """Append a completed batch to the checkpoint (crash-safe, multi-process-safe)."""
    locked_append_jsonl(
        checkpoint_path,
        {
            "ts": ts or datetime.now(UTC).isoformat(),
            "file": batch.file,
            "checksum": batch.checksum,
            "chunk_range": [batch.chunk_range[0], batch.chunk_range[1]],
            "batch_id": batch_id,
            "destination": destination,
            "corpus": corpus,
        },
    )


def load_checkpoint(checkpoint_path: Path) -> LoadedCheckpoint:
    """Load completed batches + recorded target/corpus. Empty if the file is absent."""
    path = Path(checkpoint_path)
    completed: set[BatchRef] = set()
    destination: str | None = None
    corpus: str | None = None
    if not path.exists():
        return LoadedCheckpoint(None, None, completed)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a torn final line from a hard kill
        cr = rec.get("chunk_range") or [0, 0]
        completed.add(BatchRef(rec["file"], rec["checksum"], (cr[0], cr[1])))
        destination = rec.get("destination", destination)
        corpus = rec.get("corpus", corpus)
    return LoadedCheckpoint(destination, corpus, completed)


def remaining_batches(
    files: list[PlannedFile],
    batch_size: int,
    loaded: LoadedCheckpoint | None,
) -> list[BatchRef]:
    """Planned batches minus already-completed ones.

    A file whose checksum changed since the checkpoint produces batches with the
    new checksum, which never match the old completed refs — so it is fully
    re-ingested, as required.
    """
    planned = plan_batches(files, batch_size)
    if loaded is None or not loaded.completed:
        return planned
    return [b for b in planned if b not in loaded.completed]


def verify_resume_target(
    loaded: LoadedCheckpoint | None,
    *,
    destination: str,
    corpus: str,
    force: bool = False,
) -> None:
    """Refuse to resume into a different target/corpus than the checkpoint records."""
    if loaded is None or (loaded.destination is None and loaded.corpus is None):
        return
    if force:
        return
    if loaded.destination != destination or loaded.corpus != corpus:
        raise CheckpointMismatchError(
            f"checkpoint records target={loaded.destination!r} corpus={loaded.corpus!r}, "
            f"but resume requested target={destination!r} corpus={corpus!r}. "
            f"Pass --force-target-change to override."
        )
