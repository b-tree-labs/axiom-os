# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Advanced ingest orchestrator (spec-rag-ingest-advanced §7).

The durable run loop that ties the building blocks together: for each remaining
chunk batch, do the work with exponential-backoff retry; checkpoint every
completed batch so a kill/resume never double-works; and if a batch exhausts
its retries, record the failure and keep going rather than killing the whole
run. The per-batch work (chunk → embed → store) and ``sleep`` are injected, so
the loop is deterministic and needs no real store, network, or wall-clock wait.

The argparse verb + SIGINT handlers + CLI subprocess smokes land in U5b on top
of this core.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .ingest_checkpoint import (
    LoadedCheckpoint,
    PlannedFile,
    plan_batches,
    record_batch,
    remaining_batches,
)
from .ingest_preflight import PreflightReport, run_preflight

_DEFAULT_MAX_RETRIES = 5
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 60.0


class BatchFailed(Exception):
    """A batch exhausted its retries. Carries the number of retries attempted."""

    def __init__(self, attempts: int):
        self.attempts = attempts
        super().__init__(f"batch failed after {attempts} retries")


def retry_with_backoff(
    fn,
    *,
    max_retries: int,
    sleep_fn=time.sleep,
    base: float = _BACKOFF_BASE_S,
    cap: float = _BACKOFF_CAP_S,
):
    """Call ``fn()``; retry on any exception with capped exponential backoff.

    Returns ``(result, n_retries)`` on success. Raises ``BatchFailed`` after
    ``max_retries`` failed retries. Sleeps before each retry, never after the
    final failure.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):  # attempt 0 is the first (non-retry) try
        try:
            return fn(), attempt
        except Exception as e:  # noqa: BLE001 — batch work is opaque; any failure retries
            last_exc = e
            if attempt < max_retries:
                sleep_fn(min(base * (2**attempt), cap))
    raise BatchFailed(max_retries) from last_exc


@dataclass
class IngestRunResult:
    files_indexed: int  # files with ≥1 successful batch and no failed batch
    chunks_indexed: int
    batches_resumed: int  # batches skipped because the checkpoint already had them
    files_failed: int
    retries: int
    failed_files: list[str] = field(default_factory=list)


def run_ingest(
    planned_files: list[PlannedFile],
    *,
    batch_size: int,
    ingest_batch_fn,
    checkpoint_path,
    destination: str,
    corpus: str,
    progress,
    loaded_checkpoint: LoadedCheckpoint | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    sleep_fn=time.sleep,
    backoff_base: float = _BACKOFF_BASE_S,
    backoff_cap: float = _BACKOFF_CAP_S,
) -> IngestRunResult:
    """Run remaining batches with retry + checkpoint, surviving per-batch failure.

    ``ingest_batch_fn(batch: BatchRef) -> int`` does one batch's work and returns
    the chunk count written; it raises on failure. ``progress`` is a
    ProgressState-like object (``batch_done``/``file_done``/``retry``).
    """
    all_batches = plan_batches(planned_files, batch_size)
    remaining = remaining_batches(planned_files, batch_size, loaded_checkpoint)
    batches_resumed = len(all_batches) - len(remaining)

    chunks = 0
    retries = 0
    failed_files: list[str] = []
    succeeded_files: set[str] = set()

    for batch in remaining:
        try:
            n, n_retries = retry_with_backoff(
                lambda b=batch: ingest_batch_fn(b),
                max_retries=max_retries,
                sleep_fn=sleep_fn,
                base=backoff_base,
                cap=backoff_cap,
            )
            retries += n_retries
            chunks += n
            record_batch(checkpoint_path, batch, destination=destination, corpus=corpus)
            progress.batch_done(n)
            succeeded_files.add(batch.file)
        except BatchFailed as bf:
            retries += bf.attempts
            if batch.file not in failed_files:
                failed_files.append(batch.file)
            progress.retry()

    indexed = sorted(succeeded_files - set(failed_files))
    for _ in indexed:
        progress.file_done()

    return IngestRunResult(
        files_indexed=len(indexed),
        chunks_indexed=chunks,
        batches_resumed=batches_resumed,
        files_failed=len(failed_files),
        retries=retries,
        failed_files=failed_files,
    )


class SigintCoordinator:
    """First Ctrl-C → checkpoint and exit; a second within the window → hard abort.

    Pure state machine (injected clock); the actual signal.signal wiring lives in
    the CLI verb (U5b-cli) and is exercised by a subprocess signal test.
    """

    def __init__(self, *, window_s: float = 2.0, clock=time.monotonic):
        self._window = window_s
        self._clock = clock
        self._first_at: float | None = None

    def signal(self) -> str:
        now = self._clock()
        if self._first_at is not None and (now - self._first_at) <= self._window:
            return "abort"
        self._first_at = now
        return "checkpoint"


def format_preflight(report: PreflightReport, *, corpus: str, target: str) -> str:
    """Render the preflight panel as plain text (CLI wraps it in a rich panel)."""
    s = report.scan
    lines = [
        f"Preflight — ingest → {target}/{corpus}",
        f"  files:      {s.supported_files:,} supported "
        f"({s.unsupported_files:,} unsupported, {s.already_present:,} already indexed)",
        f"  size:       {s.total_bytes / 1e6:,.1f} MB raw",
        f"  predicted:  ~{report.estimated_chunks:,} chunks · "
        f"~{report.estimated_dest_bytes / 1e6:,.1f} MB on destination",
        f"  space:      {report.free_bytes / 1e6:,.1f} MB free",
        f"  reachable:  {report.reachable} ({report.reachable_detail})",
    ]
    if report.advice:
        lines.append(f"  note:       {report.advice}")
    lines.append(f"  ABORT:      {report.abort_reason}" if report.abort_reason else "  ready.")
    return "\n".join(lines)


def cmd_dry_run(
    paths,
    *,
    corpus: str,
    target: str,
    reachable_fn,
    free_bytes_fn,
    out=print,
) -> int:
    """Preflight only, no writes. Returns 0 if the run could proceed, else non-zero."""
    report = run_preflight(paths, reachable_fn=reachable_fn, free_bytes_fn=free_bytes_fn)
    out(format_preflight(report, corpus=corpus, target=target))
    return 1 if report.abort_reason else 0
