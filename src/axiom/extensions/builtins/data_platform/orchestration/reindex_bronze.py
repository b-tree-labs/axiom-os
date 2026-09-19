# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``reindex_bronze`` — offline re-index of already-landed bronze records.

Bronze is the substrate of record: once a source's content has landed (Box
pull, etc.), turning it into RAG chunks needs no re-fetch. This drives the
embed/chunk step over *every* bronze record, independent of the source
connector — the recovery path when capture succeeded but indexing didn't
finish (observed 2026-06-08: 5,363 files captured, only one subtree indexed).

Design (lessons from that incident):

- **Idempotent** — skip records whose ``source_path`` is already indexed, so a
  re-run resumes rather than duplicates.
- **Per-record isolation** — one bad/slow record (a malformed PDF, a wedged
  extract) is bounded by ``per_record_timeout`` and skipped with a reason; it
  never stalls the whole pass.
- **Lock-safe writes** — relies on the store's bounded ``lock_timeout`` (see
  ``RAGStore.connect``) so a contended write fails fast instead of wedging.
- **Observable** — returns a structured report and calls ``on_record`` per item
  so callers (skill/CLI) can stream progress.

Pure orchestration: the embed unit, content resolver, and record source are
injected, so this is unit-testable without OCR, a real bronze tree, or a DB.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReindexReport:
    """Outcome of one ``reindex_bronze`` pass."""

    seen: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (source_path, error)

    def _skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


class _Timeout(Exception):
    pass


def _install_alarm(seconds: int) -> bool:
    """Arm a SIGALRM (main-thread only). Returns True if armed."""
    try:
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(_Timeout()))
        signal.alarm(seconds)
        return True
    except (ValueError, AttributeError):
        return False  # not main thread / platform without SIGALRM — run unbounded


def reindex_bronze(
    records: Iterable[dict[str, Any]],
    *,
    already_indexed: set[str],
    embed_one: Callable[[dict[str, Any]], int],
    per_record_timeout: int = 180,
    on_record: Callable[[str, str, int], None] | None = None,
) -> ReindexReport:
    """Re-index every bronze record not already indexed.

    ``records`` yields bronze manifest dicts (``source_path``, ``disposition``,
    ``content_sha256`` …). ``embed_one(record)`` performs OCR/chunk/write for
    one record and returns the number of chunks written; it raises to signal a
    per-record failure. ``already_indexed`` is the set of source_paths to skip.
    ``on_record(status, source_path, chunks)`` streams progress
    (status ∈ {"ok","skip","fail"}).
    """
    report = ReindexReport()
    for rec in records:
        report.seen += 1
        sp = rec.get("source_path") or ""
        if rec.get("disposition") != "allow":
            report._skip("not-allow")
            continue
        if not sp or not rec.get("content_sha256"):
            report._skip("no-id")
            continue
        if sp in already_indexed:
            report._skip("already-indexed")
            continue

        armed = _install_alarm(per_record_timeout)
        try:
            n = embed_one(rec)
        except _Timeout:
            report.failed += 1
            report.failures.append((sp, "timeout"))
            if on_record:
                on_record("fail", sp, 0)
            continue
        except Exception as exc:  # noqa: BLE001 — one record must not abort the pass
            if armed:
                signal.alarm(0)
            report.failed += 1
            report.failures.append((sp, f"{type(exc).__name__}: {exc}"))
            if on_record:
                on_record("fail", sp, 0)
            continue
        finally:
            if armed:
                signal.alarm(0)

        if n <= 0:
            report._skip("no-text")
            if on_record:
                on_record("skip", sp, 0)
            continue
        already_indexed.add(sp)
        report.indexed += 1
        report.chunks += n
        if on_record:
            on_record("ok", sp, n)
    return report


__all__ = ["ReindexReport", "reindex_bronze"]
