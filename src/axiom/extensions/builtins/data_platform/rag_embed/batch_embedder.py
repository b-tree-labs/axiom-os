# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Device/cgroup-aware batched embedding — the accelerated bulk embed path.

Why this exists (see docs/working/batch-rag-ingest-acceleration.md): serving a
sentence-embedding model one request at a time (e.g. via an inference server on
CPU) tops out ~4 chunks/s and doesn't scale with request concurrency. The ~9×
speedup on the *same* hardware came from two things this module owns:

1. **Honor the cgroup CPU quota, and pin the math-library thread env.** Torch's
   `set_num_threads` alone does NOT engage intra-op parallelism for embedding;
   `OMP_NUM_THREADS`/`MKL_NUM_THREADS` must be set (before torch imports). And
   the ceiling is the *container* quota, not the host core count.
2. **Batch encode + batch the DB writes**, streaming only un-embedded rows
   (idempotent / resumable).

The orchestration core (:func:`batch_embed_missing`) is injectable —
encoder, batch source, and writer are parameters — so it is unit-testable
without torch, a model, or a database.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterator
from pathlib import Path


def effective_cpu_quota(default: int = 4) -> int:
    """Best-effort CPU core budget for *this container*, cgroup-aware.

    Reads the cgroup v2 (`cpu.max`) then v1 (`cpu.cfs_quota_us`/`period`) quota
    so a 64-core host with a 32-core pod cap returns 32, not 64. Falls back to
    ``os.cpu_count()`` then ``default``.
    """
    # cgroup v2
    v2 = Path("/sys/fs/cgroup/cpu.max")
    try:
        quota, period = v2.read_text().split()
        if quota != "max":
            return max(1, math.floor(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    # cgroup v1
    try:
        q = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        p = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if q > 0 and p > 0:
            return max(1, math.floor(q / p))
    except (OSError, ValueError):
        pass
    return os.cpu_count() or default


def pin_cpu_threads(n: int | None = None, *, reserve: int = 1) -> int:
    """Pin OpenMP/MKL/torch thread counts to the CPU budget, leaving headroom.

    MUST be called before importing torch/sentence-transformers for the OMP/MKL
    env to take effect. ``reserve`` leaves cores for system/control-plane work
    (the headroom principle — a hot job must not starve its host). Returns the
    thread count applied.
    """
    budget = n if n is not None else effective_cpu_quota()
    threads = max(1, budget - max(0, reserve))
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(var, str(threads))
    try:
        import torch

        torch.set_num_threads(threads)
    except Exception:
        pass  # torch optional; the env vars still take effect for the runtime
    return threads


def batch_embed_missing(
    fetch_batches: Callable[[], Iterator[list[tuple[int, str]]]],
    encode: Callable[[list[str]], list[list[float]]],
    write_batch: Callable[[list[tuple[int, list[float]]]], None],
    *,
    doc_prefix: str = "search_document: ",
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Embed every (id, text) batch and write it back; return total embedded.

    Pure orchestration: ``fetch_batches`` yields lists of un-embedded
    ``(id, text)`` (the resumable/idempotent source), ``encode`` maps texts →
    vectors, ``write_batch`` persists ``(id, vector)`` pairs. ``doc_prefix`` is
    the asymmetric-retrieval document prefix (nomic-style); pair it with a
    matching query prefix at search time.
    """
    total = 0
    for batch in fetch_batches():
        if not batch:
            continue
        vecs = encode([doc_prefix + (t or "") for _, t in batch])
        write_batch([(cid, v) for (cid, _), v in zip(batch, vecs, strict=True)])
        total += len(batch)
        if on_progress:
            on_progress(total)
    return total


__all__ = ["effective_cpu_quota", "pin_cpu_threads", "batch_embed_missing"]
