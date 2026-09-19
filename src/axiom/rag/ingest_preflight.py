# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Preflight for advanced ingest (spec-rag-ingest-advanced §4).

Runs unconditionally before any embed call, cheapest checks first, so we fail
fast: input scan → destination reachability → capacity estimate → chunk-size
advice. Reachability and free space are injected callables so the logic is
unit-testable without a live store or embedder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from pathlib import Path

from .extract import SUPPORTED_EXTENSIONS
from .ingest import walk_candidate_files

_SMALL_FILE_BYTES = 200
_LARGE_FILE_BYTES = 50_000
_DEFAULT_AVG_CHUNK_BYTES = 512
# A 768-dim float32 vector + index overhead is ~4× the chunk text size on disk.
_DEFAULT_EMBEDDING_OVERHEAD = 4.0


@dataclass
class InputScan:
    supported_files: int = 0
    unsupported_files: int = 0
    total_bytes: int = 0
    by_ext: dict[str, int] = field(default_factory=dict)
    small_files: int = 0  # supported files < 200 bytes
    large_files: int = 0  # supported files > 50 KB
    already_present: int = 0  # supported files already indexed (per injected predicate)


@dataclass
class PreflightReport:
    scan: InputScan
    reachable: bool
    reachable_detail: str
    estimated_chunks: int
    estimated_dest_bytes: int
    free_bytes: int
    capacity_ok: bool
    advice: str | None
    abort_reason: str | None


def scan_input(paths, *, already_present_fn=None) -> InputScan:
    """Walk the input, classify by supported extension, sum bytes and edge cases."""
    scan = InputScan()
    for raw in paths:
        root = Path(raw)
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = walk_candidate_files(root)
        else:
            continue
        for f in files:
            ext = f.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                scan.unsupported_files += 1
                continue
            size = f.stat().st_size
            scan.supported_files += 1
            scan.total_bytes += size
            scan.by_ext[ext] = scan.by_ext.get(ext, 0) + 1
            if size < _SMALL_FILE_BYTES:
                scan.small_files += 1
            elif size > _LARGE_FILE_BYTES:
                scan.large_files += 1
            if already_present_fn is not None and already_present_fn(f):
                scan.already_present += 1
    return scan


def estimate_chunks(total_bytes: int, avg_chunk_bytes: int = _DEFAULT_AVG_CHUNK_BYTES) -> int:
    if total_bytes <= 0:
        return 0
    return max(1, ceil(total_bytes / avg_chunk_bytes))


def estimate_destination_bytes(
    n_chunks: int,
    avg_chunk_bytes: int = _DEFAULT_AVG_CHUNK_BYTES,
    embedding_overhead: float = _DEFAULT_EMBEDDING_OVERHEAD,
) -> int:
    """Bytes-on-disk estimate: chunk text + embedding vector + index overhead."""
    return int(n_chunks * avg_chunk_bytes * (1 + embedding_overhead))


def chunk_size_advice(scan: InputScan) -> str | None:
    """Non-blocking note when the corpus is dominated by tiny or huge files."""
    if scan.supported_files == 0:
        return None
    if scan.small_files / scan.supported_files > 0.5:
        return (
            "Corpus is dominated by very small files (<200 bytes); a coarser "
            "chunker tier may retrieve better."
        )
    if scan.large_files / scan.supported_files > 0.5:
        return (
            "Corpus is dominated by very large files (>50 KB); a finer chunker "
            "tier may retrieve better."
        )
    return None


def run_preflight(
    paths,
    *,
    reachable_fn,
    free_bytes_fn,
    avg_chunk_bytes: int = _DEFAULT_AVG_CHUNK_BYTES,
    embedding_overhead: float = _DEFAULT_EMBEDDING_OVERHEAD,
    already_present_fn=None,
) -> PreflightReport:
    """Compose the preflight. Sets abort_reason (with numbers) if it shouldn't run."""
    scan = scan_input(paths, already_present_fn=already_present_fn)
    reachable, detail = reachable_fn()

    est_chunks = estimate_chunks(scan.total_bytes, avg_chunk_bytes)
    est_bytes = estimate_destination_bytes(est_chunks, avg_chunk_bytes, embedding_overhead)
    free = free_bytes_fn() if reachable else 0
    capacity_ok = reachable and est_bytes <= free
    advice = chunk_size_advice(scan)

    abort_reason: str | None = None
    if not reachable:
        abort_reason = f"destination unreachable: {detail}"
    elif not capacity_ok:
        abort_reason = (
            f"insufficient space: need ~{est_bytes:,} bytes "
            f"({est_chunks:,} chunks), have {free:,} free"
        )

    return PreflightReport(
        scan=scan,
        reachable=reachable,
        reachable_detail=detail,
        estimated_chunks=est_chunks,
        estimated_dest_bytes=est_bytes,
        free_bytes=free,
        capacity_ok=capacity_ok,
        advice=advice,
        abort_reason=abort_reason,
    )
