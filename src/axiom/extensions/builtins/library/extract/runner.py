# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Run one extractor on one file, isolated, bounded, and never fatal (ADR-213 D6).

``thread`` isolation runs the extractor in a worker thread and stops waiting at
its timeout (the thread is abandoned, its result discarded). ``process``
isolation runs it in a spawned child with an address-space limit and kills the
child at its timeout; contributed kit code always runs this way.

Whatever happens, the caller gets an :class:`Outcome`: a result, or a status and
a reason a person can act on. One extractor failing never stops another.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.library.extract.contract import (
    ExtractionContext,
    ExtractionResult,
    ExtractorUnavailable,
    NotYet,
)
from axiom.extensions.builtins.library.extract.registry import Registered

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    #: ``done`` | ``failed`` | ``timeout`` | ``not-yet`` | ``unavailable`` | ``skipped``.
    status: str
    result: ExtractionResult | None = None
    error: str = ""
    remedy: str = ""
    duration_ms: int = 0
    tracking: str = ""


def _call(impl: Any, ctx: ExtractionContext) -> ExtractionResult:
    fn = getattr(impl, "extract", impl)
    result = fn(ctx)
    if result is None:
        return ExtractionResult(summary="nothing found")
    if not isinstance(result, ExtractionResult):
        raise TypeError(f"returned {type(result).__name__}, not an ExtractionResult")
    return result


def _classify(exc: BaseException, name: str) -> Outcome:
    if isinstance(exc, NotYet):
        return Outcome(
            "not-yet", error=str(exc), tracking=exc.tracking,
            remedy="This format is recognised and not yet extracted; the file is stored and listed. "
            + (f"Tracked at {exc.tracking}." if exc.tracking else ""),
        )
    if isinstance(exc, ExtractorUnavailable):
        return Outcome("unavailable", error=str(exc), remedy=f"Install what {name} needs on the node, then re-extract.")
    if isinstance(exc, MemoryError):
        return Outcome("failed", error="ran out of memory", remedy="Raise the extractor's memory limit or split the file, then re-extract.")
    return Outcome(
        "failed",
        error=f"{type(exc).__name__}: {exc}"[:2000],
        remedy="The file is safe in bronze. If it is valid, this is the extractor's bug: re-extract after a fix, "
        "or report it with this message.",
    )


def _run_thread(entry: Registered, ctx: ExtractionContext) -> Outcome:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = _call(entry.resolve(), ctx)
        except BaseException as exc:  # noqa: BLE001 - every failure is an outcome
            box["error"] = exc
            box["trace"] = traceback.format_exc(limit=4)

    t = threading.Thread(target=target, name=f"extract:{entry.manifest.name}", daemon=True)
    t.start()
    t.join(entry.manifest.limits.timeout_s)
    if t.is_alive():
        return Outcome(
            "timeout",
            error=f"did not finish within {entry.manifest.limits.timeout_s:g}s",
            remedy="The file is stored. Re-extract when the node is less busy, or raise the extractor's timeout.",
        )
    if "error" in box:
        log.info("library: %s failed on %s: %s", entry.manifest.name, ctx.file_name, box.get("trace", ""))
        return _classify(box["error"], entry.manifest.name)
    return Outcome("done", result=box["result"])


def _child(entry_path: str, ctx: ExtractionContext, memory_mb: int, conn: Any) -> None:  # pragma: no cover - runs in a child
    try:
        import resource

        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:  # noqa: BLE001 - not every platform enforces it
        pass
    try:
        from axiom.extensions.builtins.library.contrib import resolve_entry

        conn.send(("ok", _call(resolve_entry(entry_path), ctx)))
    except BaseException as exc:  # noqa: BLE001
        try:
            conn.send(("error", exc))
        except Exception:  # noqa: BLE001 - an unpicklable exception
            conn.send(("error", RuntimeError(f"{type(exc).__name__}: {exc}")))
    finally:
        conn.close()


def _run_process(entry: Registered, ctx: ExtractionContext) -> Outcome:
    m = entry.manifest
    if not m.entry:
        return Outcome("failed", error="process isolation needs an importable entry", remedy="Declare entry = 'module:function'.")
    spawn = mp.get_context("spawn")
    parent, child = spawn.Pipe(duplex=False)
    clean = ExtractionContext(
        path=ctx.path, file_name=ctx.file_name, size=ctx.size, sha256=ctx.sha256, mime=ctx.mime,
        site=ctx.site, item_id=ctx.item_id, member=ctx.member, depth=ctx.depth,
        siblings=dict(ctx.siblings), params=dict(ctx.params),
    )
    proc = spawn.Process(target=_child, args=(m.entry, clean, m.limits.max_memory_mb, child), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(m.limits.timeout_s):
            return Outcome(
                "timeout", error=f"did not finish within {m.limits.timeout_s:g}s (stopped)",
                remedy="The file is stored. Raise the extractor's timeout or split the file, then re-extract.",
            )
        kind, value = parent.recv()
    except EOFError:
        return Outcome("failed", error="the extractor's process ended without a result (likely its memory limit)",
                       remedy="Raise max_memory_mb for this extractor, then re-extract.")
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        parent.close()
    if kind == "ok":
        return Outcome("done", result=value)
    return _classify(value, m.name)


def run(entry: Registered, ctx: ExtractionContext) -> Outcome:
    """Run one planned extractor; time it; turn every way it can end into an :class:`Outcome`."""
    m = entry.manifest
    t0 = time.monotonic()
    if m.status == "stub":
        try:
            outcome = _run_thread(entry, ctx)
        except Exception as exc:  # noqa: BLE001
            outcome = _classify(exc, m.name)
        if outcome.status == "done":
            outcome = Outcome("not-yet", error=f"{m.summary}: not yet extracted", remedy="The file is stored and listed.")
    else:
        outcome = _run_process(entry, ctx) if m.isolation == "process" else _run_thread(entry, ctx)
    outcome.duration_ms = int((time.monotonic() - t0) * 1000)
    return outcome


__all__ = ["Outcome", "run"]
