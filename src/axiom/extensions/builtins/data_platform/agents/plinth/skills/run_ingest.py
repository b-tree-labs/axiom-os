# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``run-ingest`` skill — gated Box → bronze → RAG pass driven by PLINTH.

Routes every external mutation (bronze write + RAG upsert) through
``guarded_act`` per ADR-045 D6 (reversibility + volume bound). The
per-candidate work is one Box file → bronze → embed; the run is the
batch.

PLINTH calls this for ad-hoc runs (operator-initiated via
``axi plinth run-ingest``) and for Dagster-triggered runs (the Dagster
sensor materializes ``box_corpus``, which calls into the same path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from axiom.infra.paths import get_user_state_dir
from axiom.policy.agent_action_guard import AgentAction, GuardDecision, guarded_act
from axiom.rag.ingest_router import Disposition, load_rules_file

from ....bronze import BronzeWriter, FilesystemBronzeSink
from ....rag_embed import embed_bronze_record
from ....sources import IngestSource, default_source_kind_registry
from ..connectors import ConnectorConfig, load_connector

log = logging.getLogger(__name__)


class _StoreLike(Protocol):
    def upsert_chunks(self, chunks: list[Any], embeddings: list[list[float]] | None = ..., **kwargs: Any) -> None: ...
    def connect(self) -> None: ...


@dataclass(frozen=True)
class RunIngestReport:
    """Outcome of one PLINTH-driven ingest run."""

    connector: str
    proceed: bool
    items_seen: int
    items_landed: int
    items_failed: int
    refused_reason: str = ""
    refused: list[str] = field(default_factory=list)


def run_ingest(
    connector_name: str,
    *,
    since: datetime | None = None,
    state_dir: Path | None = None,
    store: _StoreLike | None = None,
    source: IngestSource | None = None,
    writer: BronzeWriter | None = None,
    volume_mode: str = "confirm",
) -> RunIngestReport:
    """Run one PLINTH-gated ingest pass.

    All construction args (``source`` / ``writer`` / ``store``) are
    overridable so unit tests can drive the gate without hitting Box or
    Postgres. In production, the helpers below construct them from the
    connector config.
    """
    config = load_connector(connector_name, state_dir=state_dir)

    source, source_owned = _resolve_source(source, config)
    writer = writer or _build_writer(config)
    store = store or _build_store(config)

    state_root = state_dir or get_user_state_dir()

    try:
        store.connect()

        item_ids = source.list_changed(since=since)
        if not item_ids:
            return RunIngestReport(
                connector=connector_name,
                proceed=True,
                items_seen=0,
                items_landed=0,
                items_failed=0,
            )

        landed: list[str] = []
        failed: list[str] = []

        def do_one(item_id: str) -> bool:
            try:
                fetched = source.fetch(item_id)
                result = writer.write(fetched)
                # ALLOW only → embed; QUARANTINE / EXCLUDE return cleanly.
                stats = embed_bronze_record(result, fetched, store)
                if stats.indexed:
                    landed.append(item_id)
                elif stats.skipped_reason == "embed_failed":
                    failed.append(item_id)
                # quarantine / exclude are not failures (expected disposition outcomes)
                return True
            except Exception as exc:
                log.warning("PLINTH run-ingest item %s failed: %s", item_id, exc)
                failed.append(item_id)
                return False

        action = AgentAction(
            agent="plinth",
            op_class="data_platform.ingest",
            name="run_ingest",
            candidates=list(item_ids),
            reversible=True,
        )
        decision: GuardDecision = guarded_act(
            action,
            do_one=do_one,
            state_dir=state_root,
            volume_mode=volume_mode,
        )

        return RunIngestReport(
            connector=connector_name,
            proceed=decision.proceed,
            items_seen=len(item_ids),
            items_landed=len(landed),
            items_failed=len(failed),
            refused_reason=decision.reason if not decision.proceed else "",
            refused=[str(c) for c in decision.refused] + [str(c) for c in decision.would_proceed],
        )
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
