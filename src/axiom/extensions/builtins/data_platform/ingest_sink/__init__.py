# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""IngestSink — generic push-ingest endpoint for the data platform.

Shared core (:mod:`.core`) + two front doors: the FastAPI router
(:mod:`.api`) and the ``data.ingest_push`` skill. See ADR-079 §8.4.1 and
the data-platform PRD push-first model (RDQ-001).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from axiom.rag.ingest_router import Disposition, load_rules_file

from ..agents.plinth.connectors import ConnectorConfig, load_connector
from ..bronze import BronzeWriter, FilesystemBronzeSink
from .core import (
    CallbackRegistry,
    IngestResult,
    IngestSink,
    ItemDisposition,
    PushItem,
    decode_content,
)

# Sentinel: build the RAG store from the connector's configured DSN env.
AUTO_STORE = "auto"


def _store_from_config(config: ConnectorConfig) -> object | None:
    """Build the RAG store the same way the pull path does (run_ingest._build_store).

    Returns None (embed disabled) if the connector's DSN env is unset, rather
    than raising — a push that lands in bronze but isn't embedded is recoverable
    via reindex; a hard failure here would drop the whole push.
    """
    dsn = os.environ.get(config.rag_dsn_env)
    if not dsn:
        return None
    from axiom.rag.store import RAGStore

    return RAGStore(dsn)


def build_writer_for_config(config: ConnectorConfig) -> BronzeWriter:
    """The ONE BronzeWriter construction site — the connector registry is the
    single source of bronze root + rules + disposition + tier.

    Every ingest surface (CLI pull, CDC refresh, Dagster, HTTP ``/ingest``, MCP
    push) must resolve its writer through here so a pushed item and a pulled item
    land in the same bronze tree under the same provenance rules (no split brain).
    """
    rules = load_rules_file(config.provenance_rules_file) if config.provenance_rules_file else []
    return BronzeWriter(
        rules=rules,
        sink=FilesystemBronzeSink(root=Path(config.bronze_root)),
        default_disposition=Disposition(config.default_disposition),
        default_tier=config.default_tier,
    )


def sink_for_connector(
    connector_name: str,
    *,
    state_dir: Path | None = None,
    store: object | None = None,
    callbacks: CallbackRegistry | None = None,
) -> IngestSink:
    """Build an :class:`IngestSink` from a connector's config.

    Reuses the same provenance rules + bronze root the pull path uses, so a
    pushed item lands identically to a fetched one. ``store`` is optional — pass
    a RAG store to enable the embed step, or :data:`AUTO_STORE` to build it from
    the connector's configured DSN env (matching the pull path).
    """
    config = load_connector(connector_name, state_dir=state_dir)
    resolved_store = _store_from_config(config) if store is AUTO_STORE else store
    writer = build_writer_for_config(config)
    return IngestSink(writer=writer, store=resolved_store, callbacks=callbacks)  # type: ignore[arg-type]


def make_connector_sink_resolver(
    *,
    state_dir: Path | None = None,
    store: object | None = AUTO_STORE,
    callbacks: CallbackRegistry | None = None,
) -> Callable[[str], IngestSink]:
    """A cached per-connector resolver for surfaces that learn the connector
    per-request (HTTP ``/ingest``, MCP push).

    Caches one :class:`IngestSink` per connector name so each request reuses the
    same writer/store. Raises ``KeyError`` for an unknown connector so the
    surface can fail loudly (422) instead of silently quarantining into a
    rule-less tree — the bug this change removes.
    """
    cache: dict[str, IngestSink] = {}

    def resolve(connector_name: str) -> IngestSink:
        sink = cache.get(connector_name)
        if sink is None:
            try:
                sink = sink_for_connector(
                    connector_name, state_dir=state_dir, store=store, callbacks=callbacks
                )
            except FileNotFoundError as exc:
                raise KeyError(connector_name) from exc
            cache[connector_name] = sink
        return sink

    return resolve


__all__ = [
    "AUTO_STORE",
    "CallbackRegistry",
    "IngestResult",
    "IngestSink",
    "ItemDisposition",
    "PushItem",
    "build_writer_for_config",
    "decode_content",
    "make_connector_sink_resolver",
    "sink_for_connector",
]
