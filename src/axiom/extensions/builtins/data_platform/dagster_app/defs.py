# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dagster :class:`Definitions` for the DP-1 Box → RAG pipeline.

Two assets and a sensor — minimal shim over the pure-Python
``run_box_to_rag`` orchestrator.

Assets:
- ``box_corpus`` (group ``dp1``) — materialized when the sensor fires
  with the watermark from the previous run; calls
  :func:`run_box_to_rag` for one pass. Returns the
  :class:`BoxRunReport` for observability.
- ``rag_index_ready`` — downstream marker asset declaring that the
  served RAG view reflects bronze. The work is already done inside
  ``box_corpus`` (which calls ``embed_bronze_record``); this asset
  exists so downstream consumers can subscribe to "RAG is fresh".

Sensor:
- ``box_corpus_sensor`` — polls ``BoxIngestSource.list_changed(since)``
  every minute. ``since`` is the sensor cursor (persisted by Dagster).
  When new items appear, emits a ``RunRequest`` to materialize
  ``box_corpus``.

The Postgres-backed run + event storage is configured in the Dagster
instance YAML (``dagster.yaml``) — not here. That config ships with
Slice 5's Terraform module.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

# Defer the dagster import to function bodies so module load doesn't
# require the [data-platform] extra. The package __init__ enforces the
# guard at the load_definitions() entry point.
from ..orchestration import run_box_to_rag

_BOX_FOLDER_ID_ENV = "DP1_BOX_FOLDER_ID"
_BOX_SOURCE_NAME_ENV = "DP1_BOX_SOURCE_NAME"
_BRONZE_ROOT_ENV = "DP1_BRONZE_ROOT"
_RAG_DSN_ENV = "DP1_RAG_DSN"
_RULES_FILE_ENV = "DP1_PROVENANCE_RULES_FILE"


def _build_source(context):
    """Construct a :class:`BoxIngestSource` from environment + session."""
    from pathlib import Path

    from ..sources import BoxIngestSource
    from ..sources.box.session_api import BoxSessionApiClient

    folder_id = os.environ.get(_BOX_FOLDER_ID_ENV)
    name = os.environ.get(_BOX_SOURCE_NAME_ENV, "box")
    if not folder_id:
        raise RuntimeError(f"{_BOX_FOLDER_ID_ENV} not set")

    session_dir = Path(os.environ.get("NEUT_BOX_SESSION_DIR") or
                       (Path.home() / ".axi" / "credentials" / "box"))

    # Auth precedence (production path; ends the 60-min token cliff):
    #   1. BOX_JWT_CONFIG env (Server JWT app) — auto-refreshes forever
    #   2. AXI_BOX_USE_BROWSER_API=1 — Playwright fallback (legacy)
    #   3. BOX_DEVELOPER_TOKEN env — 60-min dev token (last resort)
    jwt_auth = None
    if os.environ.get("BOX_JWT_CONFIG"):
        from ..sources.box.jwt_auth import BoxJwtAuth, BoxJwtConfig
        try:
            jwt_cfg = BoxJwtConfig.from_env("BOX_JWT_CONFIG")
            jwt_auth = BoxJwtAuth(jwt_cfg)
        except Exception as exc:  # noqa: BLE001
            # Don't crash the daemon if JWT config is malformed — fall
            # back to dev-token path so an operator can still recover.
            import logging
            logging.getLogger(__name__).warning(
                "BoxJwtAuth init failed (%s); falling back to dev token", exc,
            )

    # Pure-Python cookie replay; no Chromium needed in the daemon pod.
    # Opt back into the Playwright client with AXI_BOX_USE_BROWSER_API=1.
    if os.environ.get("AXI_BOX_USE_BROWSER_API", "").lower() in {"1", "true", "yes"}:
        from ..sources import BoxBrowserApiClient
        api = BoxBrowserApiClient(session_dir=session_dir, headless=True)
    else:
        api = BoxSessionApiClient(session_dir=session_dir, jwt_auth=jwt_auth)
    return BoxIngestSource(name=name, folder_id=folder_id, api_client=api), api


def _build_writer():
    from pathlib import Path

    from axiom.rag.ingest_router import Disposition, load_rules_file

    from ..bronze import BronzeWriter, FilesystemBronzeSink

    bronze_root = Path(os.environ.get(_BRONZE_ROOT_ENV, "/var/lib/axiom/bronze"))
    rules_file = os.environ.get(_RULES_FILE_ENV)
    rules = load_rules_file(rules_file) if rules_file else []

    return BronzeWriter(
        rules=rules,
        sink=FilesystemBronzeSink(root=bronze_root),
        default_disposition=Disposition.QUARANTINE,
        default_tier=None,
    )


def _build_store():
    """Connect to the served pgvector store the embed asset upserts into."""
    from axiom.rag.store import RAGStore

    dsn = os.environ.get(_RAG_DSN_ENV)
    if not dsn:
        raise RuntimeError(f"{_RAG_DSN_ENV} not set")
    store = RAGStore(dsn)
    store.connect()
    return store


def _build_definitions():
    """Construct the Dagster ``Definitions`` (called inside an extra-installed env)."""
    # Imports stay function-scoped: the wider extension is imported in
    # environments without the [data-platform] extra. The annotation-
    # lookup trick from earlier versions is obsolete — we dropped typed
    # `context: AssetExecutionContext` annotations in v0.29.6 because
    # Dagster's validator identity-checks against its internal class
    # and rejects the public re-export; bare `context` (no annotation)
    # is the cleanest path and what the @asset decorator wants.
    from dagster import (
        DefaultSensorStatus,
        Definitions,
        RunRequest,
        SkipReason,
        asset,
        define_asset_job,
        sensor,
    )

    @asset(group_name="dp1", description="Box folder bronze + RAG embed pass (provenance-gated).")
    def box_corpus(context):
        since_iso = context.partition_key if context.has_partition_key else None
        since = datetime.fromisoformat(since_iso) if since_iso else None

        source, api = _build_source(context)
        writer = _build_writer()
        store = _build_store()
        try:
            report = run_box_to_rag(source=source, writer=writer, store=store, since=since)
        finally:
            api.close()

        context.add_output_metadata(
            {
                "items_seen": report.items_seen,
                "items_landed": report.items_landed,
                "items_failed": report.items_failed,
            }
        )
        return report.items_landed

    @asset(group_name="dp1", deps=[box_corpus], description="Marker: RAG served view reflects bronze.")
    def rag_index_ready(context) -> bool:
        # The embed happens inside `box_corpus` (via `run_box_to_rag`),
        # so this asset is the downstream-subscribe signal that the
        # served view is fresh. No work to do here.
        return True

    box_run_job = define_asset_job("dp1_box_run_job", selection=[box_corpus, rag_index_ready])

    @sensor(
        job=box_run_job,
        minimum_interval_seconds=60,
        default_status=DefaultSensorStatus.STOPPED,
    )
    def box_corpus_sensor(context):
        # Cursor is the last-seen modified_at watermark (ISO string).
        last = context.cursor
        since = datetime.fromisoformat(last) if last else None

        source, api = _build_source(context)
        try:
            changed = source.list_changed(since=since)
        finally:
            api.close()

        if not changed:
            yield SkipReason("no new Box items since watermark")
            return

        now = datetime.now(UTC).isoformat()
        context.update_cursor(now)
        yield RunRequest(run_key=f"box-{now}")

    return Definitions(
        assets=[box_corpus, rag_index_ready],
        jobs=[box_run_job],
        sensors=[box_corpus_sensor],
    )


# Lazy-built so import-time doesn't require dagster.
definitions = None


def __getattr__(name):
    global definitions
    if name == "definitions":
        if definitions is None:
            definitions = _build_definitions()
        return definitions
    raise AttributeError(name)
