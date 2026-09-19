# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``IngestSink`` — the generic push-ingest core (shared by HTTP + MCP).

Per ADR-079 (§8.4.1 IngestSink endpoint) and the data-platform PRD's
push-first model (RDQ-001: a thin egress agent POSTs items to a central
``IngestSink``), this is the *one* place a pushed item lands. It is
**domain-agnostic** — it speaks ``source`` / ``item`` / ``disposition``,
never a specific facility or data domain.

It does NOT reinvent landing logic. Each pushed item is shaped into the
existing :class:`~..contracts.FetchedItem` and driven through the
existing :class:`~..bronze.BronzeWriter` (the provenance/disposition
gate) and, optionally, :func:`~..rag_embed.embed_bronze_record`. The
sink only adds two things the pull path doesn't have:

1. a **push entry point** — bytes arrive instead of being fetched;
2. an in-process **callback registry** so consumers hook ``item_landed``
   / ``item_quarantined`` / ``item_excluded`` events as they fire.

Two front doors share this core (the ADR-079 "shared core, two views"
pattern): the FastAPI router (:mod:`.api`) and the ``data.ingest_push``
skill (:mod:`..skills.ingest_push`). Both build an :class:`IngestSink`
and call :meth:`IngestSink.ingest`.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from axiom.rag.ingest_router import Disposition

from ..bronze import BronzeWriter, BronzeWriteResult
from ..contracts import FetchedItem
from ..ingest_run import IngestRunReport, RunStore

log = logging.getLogger(__name__)

# Callback event names. Mirror the three terminal dispositions of the gate
# plus a catch-all error event for items that never reached the gate.
EVENT_LANDED = "item_landed"
EVENT_QUARANTINED = "item_quarantined"
EVENT_EXCLUDED = "item_excluded"
EVENT_ERROR = "item_error"

_EVENTS = frozenset({EVENT_LANDED, EVENT_QUARANTINED, EVENT_EXCLUDED, EVENT_ERROR})

_DISPOSITION_EVENT = {
    Disposition.ALLOW: EVENT_LANDED,
    Disposition.QUARANTINE: EVENT_QUARANTINED,
    Disposition.EXCLUDE: EVENT_EXCLUDED,
}


class _StoreLike(Protocol):
    def upsert_chunks(
        self, chunks: list[Any], embeddings: list[list[float]] | None = ..., **kwargs: Any
    ) -> None: ...

    def connect(self) -> None: ...


@dataclass(frozen=True)
class PushItem:
    """One item on a push request, before it becomes a ``FetchedItem``.

    ``content`` is the decoded raw bytes. The HTTP/MCP front doors decode
    a text or base64 payload into bytes before constructing this — the
    core never sees the wire encoding.
    """

    item_id: str
    content: bytes
    content_type: str | None = None
    source_path: str | None = None
    display_name: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ItemDisposition:
    """Per-item outcome handed back to the pusher."""

    item_id: str
    disposition: str  # "landed" | "quarantined" | "excluded" | "error"
    reason: str
    content_hash: str | None = None
    matched_rule: str | None = None
    indexed: bool = False
    embed_skipped_reason: str | None = None


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one push request (one ``source``, N items)."""

    source: str
    accepted: int
    landed: int
    quarantined: int
    excluded: int
    errored: int
    items: list[ItemDisposition]
    # Generic per-stage funnel (IngestRunReport.to_dict()); same primitive the
    # pull/CDC jobs use. Optional so existing constructions stay valid.
    funnel: dict | None = None


# Map the gate's disposition enum onto the pusher-facing vocabulary.
_DISPOSITION_LABEL = {
    Disposition.ALLOW: "landed",
    Disposition.QUARANTINE: "quarantined",
    Disposition.EXCLUDE: "excluded",
}

CallbackFn = Callable[[str, "ItemDisposition", FetchedItem], None]


class CallbackRegistry:
    """In-process hooks fired during ingest.

    A consumer registers ``register_callback(event, fn)``; the sink fires
    matching callbacks as each item reaches its terminal disposition. A
    failing callback is logged and swallowed — a downstream hook must
    never break the landing path.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[CallbackFn]] = {ev: [] for ev in _EVENTS}

    def register_callback(self, event: str, fn: CallbackFn) -> None:
        if event not in _EVENTS:
            raise ValueError(
                f"unknown ingest event {event!r}; expected one of {sorted(_EVENTS)}"
            )
        self._hooks[event].append(fn)

    def unregister_all(self, event: str | None = None) -> None:
        if event is None:
            for ev in self._hooks:
                self._hooks[ev].clear()
        else:
            self._hooks.get(event, []).clear()

    def fire(self, event: str, item_disp: ItemDisposition, fetched: FetchedItem) -> None:
        for fn in self._hooks.get(event, []):
            try:
                fn(event, item_disp, fetched)
            except Exception as exc:  # noqa: BLE001 — a hook must not break ingest
                log.warning("ingest callback for %s failed: %s", event, exc)


class IngestSink:
    """Push-ingest core: items → BronzeWriter (gate) → optional embed.

    ``writer`` is the existing provenance-gated :class:`BronzeWriter`;
    ``store`` (optional) enables the RAG embed step via the existing
    :func:`embed_bronze_record`. ``callbacks`` lets a caller share a
    pre-populated registry; otherwise a fresh one is created and exposed
    on :attr:`callbacks`.
    """

    def __init__(
        self,
        *,
        writer: BronzeWriter,
        store: _StoreLike | None = None,
        callbacks: CallbackRegistry | None = None,
    ) -> None:
        self._writer = writer
        self._store = store
        self.callbacks = callbacks or CallbackRegistry()

    def register_callback(self, event: str, fn: CallbackFn) -> None:
        """Convenience pass-through to the callback registry."""
        self.callbacks.register_callback(event, fn)

    def ingest(
        self,
        source: str,
        items: Iterable[PushItem],
        *,
        run_store: RunStore | None = None,
    ) -> IngestResult:
        """Land each pushed item; fire callbacks; return per-item dispositions.

        Builds the generic :class:`IngestRunReport` funnel — the same primitive
        the pull/CDC jobs use — so push ingest reports the same in/out/dropped/
        failed stage telemetry. Persisted via ``run_store`` when supplied. Push
        has no discovery step (items arrive already chosen), so the funnel
        starts at ``to_process``.
        """
        dispositions: list[ItemDisposition] = []
        counts = {"landed": 0, "quarantined": 0, "excluded": 0, "error": 0}
        run = IngestRunReport.start(
            "push", source=source,
            stages=("to_process", "loaded", "indexed"),
        )

        for item in items:
            run.entered("to_process", 1)
            run.advanced("to_process", 1)
            fetched = self._to_fetched(source, item)
            try:
                result = self._writer.write(fetched)
            except Exception as exc:  # noqa: BLE001
                log.warning("ingest write failed for %s/%s: %s", source, item.item_id, exc)
                disp = ItemDisposition(
                    item_id=item.item_id, disposition="error", reason=f"write_failed: {exc}"
                )
                counts["error"] += 1
                run.entered("loaded", 1)
                run.failed("loaded", "write_failed", 1)
                dispositions.append(disp)
                self.callbacks.fire(EVENT_ERROR, disp, fetched)
                continue

            disp = self._finalize(result, fetched)
            counts[disp.disposition] += 1
            run.entered("loaded", 1)
            if disp.disposition == "landed":
                run.advanced("loaded", 1)
                if disp.indexed:
                    run.entered("indexed", 1)
                    run.advanced("indexed", 1)
                elif disp.embed_skipped_reason == "embed_failed":
                    run.entered("indexed", 1)
                    run.failed("indexed", "embed_failed", 1)
            else:
                run.dropped("loaded", disp.disposition, 1)  # quarantined / excluded
            dispositions.append(disp)
            self.callbacks.fire(_DISPOSITION_EVENT[result.disposition], disp, fetched)

        run.finish(failed=counts["error"] > 0 and counts["landed"] == 0)
        if run_store is not None:
            try:
                run_store.save(run)
            except Exception:  # noqa: BLE001 — telemetry must never sink a run
                log.warning("ingest run-store save failed for %s", run.run_id)

        return IngestResult(
            source=source,
            accepted=len(dispositions),
            landed=counts["landed"],
            quarantined=counts["quarantined"],
            excluded=counts["excluded"],
            errored=counts["error"],
            items=dispositions,
            funnel=run.to_dict(),
        )

    # -- internals ---------------------------------------------------------

    def _finalize(self, result: BronzeWriteResult, fetched: FetchedItem) -> ItemDisposition:
        indexed = False
        embed_skipped: str | None = None
        # Only ALLOW items reach embed; the embedder itself re-checks, but we
        # gate here so we don't construct work for quarantine/exclude.
        if result.disposition is Disposition.ALLOW and self._store is not None:
            from ..rag_embed import embed_bronze_record

            try:
                stats = embed_bronze_record(result, fetched, self._store)
                indexed = stats.indexed
                embed_skipped = stats.skipped_reason
            except Exception as exc:  # noqa: BLE001 — embed failure ≠ landing failure
                log.warning("ingest embed failed for %s: %s", fetched.item_id, exc)
                embed_skipped = "embed_failed"

        return ItemDisposition(
            item_id=result.item_id,
            disposition=_DISPOSITION_LABEL[result.disposition],
            reason=result.reason,
            content_hash=result.content_hash,
            matched_rule=result.matched_rule,
            indexed=indexed,
            embed_skipped_reason=embed_skipped,
        )

    @staticmethod
    def _to_fetched(source: str, item: PushItem) -> FetchedItem:
        display = item.display_name or (
            item.source_path.rsplit("/", 1)[-1] if item.source_path else item.item_id
        )
        return FetchedItem(
            source_name=source,
            item_id=item.item_id,
            display_name=display,
            content=item.content,
            content_type=item.content_type,
            size=len(item.content),
            modified_at=datetime.now(UTC),
            etag=None,
            source_path=item.source_path,
            extra=dict(item.metadata),
        )


def decode_content(raw: str, *, encoding: str = "text") -> bytes:
    """Decode a wire payload into raw bytes.

    ``encoding`` is ``"text"`` (UTF-8) or ``"base64"``. The front doors
    call this so the core only ever handles bytes.
    """
    if encoding == "base64":
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"invalid base64 content: {exc}") from exc
    if encoding == "text":
        return raw.encode("utf-8")
    raise ValueError(f"unknown content encoding {encoding!r}; expected 'text' or 'base64'")


__all__ = [
    "EVENT_ERROR",
    "EVENT_EXCLUDED",
    "EVENT_LANDED",
    "EVENT_QUARANTINED",
    "CallbackRegistry",
    "IngestResult",
    "IngestSink",
    "ItemDisposition",
    "PushItem",
    "decode_content",
]
