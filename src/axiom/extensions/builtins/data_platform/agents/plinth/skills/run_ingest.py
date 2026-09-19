# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``run-ingest`` skill — gated source → bronze → RAG pass driven by PLINTH.

Routes every external mutation (bronze write + RAG upsert) through
``guarded_act`` per ADR-045 D6 (reversibility + volume bound). The
per-candidate work is one source item → bronze → embed; the run is the
batch.

PLINTH calls this for ad-hoc runs (operator-initiated via
``axi plinth run-ingest``) and for Dagster-triggered runs (a source's
sensor materializes its ``corpus__<slug>`` asset, which calls into the
same path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from axiom.infra.paths import get_user_state_dir
from axiom.policy.agent_action_guard import AgentAction, GuardDecision, guarded_act
from axiom.rag.ingest_router import Disposition, load_rules_file

from ....bronze import BronzeWriter, FilesystemBronzeSink
from ....ingest_run import IngestRunReport, RunStore
from ....rag_embed import embed_bronze_record
from ....sources import IngestSource, default_source_kind_registry
from ..connectors import ConnectorConfig, load_connector

log = logging.getLogger(__name__)


class _StoreLike(Protocol):
    def upsert_chunks(self, chunks: list[Any], embeddings: list[list[float]] | None = ..., **kwargs: Any) -> None: ...
    def connect(self) -> None: ...


@dataclass(frozen=True)
class RunIngestReport:
    """Outcome of one PLINTH-driven ingest run.

    ``funnel`` is the generic per-stage telemetry (job-agnostic
    :class:`IngestRunReport`): how many items entered/advanced each stage and
    how many were dropped/failed and why. The flat ``items_*`` counts are kept
    for back-compat; ``funnel`` is the richer single-source-of-truth.
    """

    connector: str
    proceed: bool
    items_seen: int
    items_landed: int
    items_failed: int
    refused_reason: str = ""
    refused: list[str] = field(default_factory=list)
    funnel: dict | None = None


def run_ingest(
    connector_name: str,
    *,
    since: datetime | None = None,
    state_dir: Path | None = None,
    store: _StoreLike | None = None,
    source: IngestSource | None = None,
    writer: BronzeWriter | None = None,
    volume_mode: str = "confirm",
    max_workers: int | None = None,
    run_store: RunStore | None = None,
) -> RunIngestReport:
    """Run one PLINTH-gated ingest pass.

    All construction args (``source`` / ``writer`` / ``store``) are
    overridable so unit tests can drive the gate without hitting the source or
    Postgres. In production, the helpers below construct them from the
    connector config.

    ``run_store`` (optional) persists the generic :class:`IngestRunReport`
    funnel — the same primitive any ingest job uses. When omitted the funnel is
    still built and returned on the report; it just isn't persisted.
    """
    config = load_connector(connector_name, state_dir=state_dir)

    source, source_owned = _resolve_source(source, config)
    writer = writer or _build_writer(config)
    store = store or _build_store(config)

    state_root = state_dir or get_user_state_dir()

    run = IngestRunReport.start("pull", source=connector_name)

    def _finalize(report: RunIngestReport, *, failed: bool = False) -> RunIngestReport:
        run.finish(failed=failed)
        if run_store is not None:
            try:
                run_store.save(run)
            except Exception:  # noqa: BLE001 — telemetry must never sink a run
                log.warning("ingest run-store save failed for %s", run.run_id)
        return replace(report, funnel=run.to_dict())

    try:
        store.connect()

        item_ids = source.list_changed(since=since)
        run.entered("discovered", len(item_ids))
        run.advanced("discovered", len(item_ids))
        if not item_ids:
            return _finalize(RunIngestReport(
                connector=connector_name,
                proceed=True,
                items_seen=0,
                items_landed=0,
                items_failed=0,
            ))
        run.entered("to_process", len(item_ids))
        run.advanced("to_process", len(item_ids))

        landed: list[str] = []
        failed: list[str] = []

        # Concurrency: the bottleneck is per-item source fetch + extract/OCR
        # (I/O + CPU bound), not the embedder. Run items in a bounded pool.
        # psycopg2 connections aren't thread-safe, so each worker thread gets
        # its OWN RAGStore with ensure_schema=False (the primary `store`
        # already ran the DDL once above — workers racing CREATE INDEX would
        # deadlock on AccessExclusiveLock).
        import os
        import threading
        if max_workers is None:
            try:
                workers = int(os.environ.get("DP1_INGEST_WORKERS", "1"))
            except ValueError:
                workers = 1
        else:
            workers = max_workers
        workers = max(1, workers)

        _tls = threading.local()
        _lock = threading.Lock()
        _worker_stores: list = []
        _dsn = os.environ.get(config.rag_dsn_env) if workers > 1 else None

        def _store_for_thread():
            if workers <= 1:
                return store
            s = getattr(_tls, "store", None)
            if s is None:
                from axiom.rag.store import RAGStore
                s = RAGStore(_dsn, ensure_schema=False)
                s.connect()
                _tls.store = s
                with _lock:
                    _worker_stores.append(s)
            return s

        def do_one(item_id: str) -> bool:
            try:
                fetched = source.fetch(item_id)
                with _lock:
                    run.entered("fetched", 1)
                    run.advanced("fetched", 1)
                result = writer.write(fetched)
                # ALLOW only → embed; QUARANTINE / EXCLUDE return cleanly.
                stats = embed_bronze_record(result, fetched, _store_for_thread())
                with _lock:
                    run.entered("loaded", 1)
                    if stats.indexed:
                        run.advanced("loaded", 1)
                        run.entered("indexed", 1)
                        run.advanced("indexed", 1)
                        landed.append(item_id)
                    elif stats.skipped_reason == "embed_failed":
                        run.failed("indexed", "embed_failed", 1)
                        failed.append(item_id)
                    else:
                        # quarantine / exclude — expected disposition, not a failure
                        run.dropped("loaded", stats.skipped_reason or "disposition", 1)
                return True
            except Exception as exc:
                log.warning("PLINTH run-ingest item %s failed: %s", item_id, exc)
                with _lock:
                    run.failed("fetched", type(exc).__name__, 1)
                    failed.append(item_id)
                return False

        action = AgentAction(
            agent="plinth",
            op_class="data_platform.ingest",
            name="run_ingest",
            candidates=list(item_ids),
            reversible=True,
        )
        try:
            decision: GuardDecision = guarded_act(
                action,
                do_one=do_one,
                state_dir=state_root,
                volume_mode=volume_mode,
                max_workers=workers,
            )
        finally:
            for s in _worker_stores:
                try:
                    s.close()
                except Exception:
                    pass

        if not decision.proceed:
            run.refuse(decision.reason)
        return _finalize(RunIngestReport(
            connector=connector_name,
            proceed=decision.proceed,
            items_seen=len(item_ids),
            items_landed=len(landed),
            items_failed=len(failed),
            refused_reason=decision.reason if not decision.proceed else "",
            refused=[str(c) for c in decision.refused] + [str(c) for c in decision.would_proceed],
        ))
    finally:
        if source_owned:
            _close_source(source)


# ---- construction helpers (overridable for tests) ------------------------


def _resolve_source(source, config):
    """Source-agnostic: look up the kind's provider and ask it to construct."""
    if source is not None:
        return source, False
    provider = default_source_kind_registry().get(config.kind)
    return provider.construct(config), True


def _build_writer(config: ConnectorConfig) -> BronzeWriter:
    rules = load_rules_file(config.provenance_rules_file) if config.provenance_rules_file else []
    return BronzeWriter(
        rules=rules,
        sink=FilesystemBronzeSink(root=Path(config.bronze_root)),
        default_disposition=Disposition(config.default_disposition),
        default_tier=config.default_tier,
    )


def _build_store(config: ConnectorConfig) -> _StoreLike:
    import os

    from axiom.rag.store import RAGStore

    dsn = os.environ.get(config.rag_dsn_env)
    if not dsn:
        raise RuntimeError(
            f"connector {config.name!r}: env {config.rag_dsn_env!r} is unset; "
            "set the RAG DSN before running ingest"
        )
    return RAGStore(dsn)


def _close_source(source) -> None:
    api = getattr(source, "_api", None)
    close = getattr(api, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


__all__ = ["RunIngestReport", "run_ingest"]
