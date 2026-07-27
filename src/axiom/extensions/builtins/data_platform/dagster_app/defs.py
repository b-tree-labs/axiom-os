# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dagster :class:`Definitions` for the DP-1 source → RAG pipeline.

Two assets and a sensor per source — a minimal shim over the
pure-Python ``run_source_to_rag`` orchestrator. The platform is
*source-kind-agnostic*: every source is constructed through the
:class:`SourceKindRegistry`, so adding a kind (GDrive, SharePoint, S3,
…) needs no change here.

Assets (per source, ``<slug>`` derived from the source name):
- ``corpus__<slug>`` (group ``dp1``) — materialized when the sensor
  fires with the watermark from the previous run; calls
  :func:`run_source_to_rag` for one pass. Returns the landed count.
- ``rag_index_ready__<slug>`` — downstream marker asset declaring that
  the served RAG view reflects bronze.

Sensor:
- ``corpus__<slug>_sensor`` — polls ``IngestSource.list_changed(since)``
  every minute. ``since`` is the sensor cursor (persisted by Dagster).
  When new items appear, emits a ``RunRequest``.

The Postgres-backed run + event storage is configured in the Dagster
instance YAML (``dagster.yaml``) — not here.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Defer the dagster import to function bodies so module load doesn't
# require the [data-platform] extra. The package __init__ enforces the
# guard at the load_definitions() entry point.
from ..orchestration import run_source_to_rag

log = logging.getLogger(__name__)

# Box-specific env back-compat shim (see _box_specs_from_env). Connector
# TOMLs are the primary, kind-agnostic path; these envs only ever yield
# kind="box" specs.
_BOX_FOLDER_ID_ENV = "DP1_BOX_FOLDER_ID"
_BOX_SOURCE_NAME_ENV = "DP1_BOX_SOURCE_NAME"
_BOX_DEFAULT_TIER_ENV = "DP1_BOX_DEFAULT_TIER"
_BOX_SOURCES_ENV = "DP1_BOX_SOURCES"

_BRONZE_ROOT_ENV = "DP1_BRONZE_ROOT"
_RAG_DSN_ENV = "DP1_RAG_DSN"
_RULES_FILE_ENV = "DP1_PROVENANCE_RULES_FILE"


@dataclass(frozen=True)
class SourceSpec:
    """One configured ingest source, kind-agnostic.

    Kind-specific binding (Box's ``folder_id``, GDrive's ``drive_id``)
    lives under :attr:`params` — the platform never reads it; only the
    kind's :class:`SourceKindProvider` does.

    ``default_tier`` is the corpus the source lands in unless a
    provenance rule overrides it (``rag-community`` / ``rag-org`` /
    ``rag-internal``); ``None`` defers entirely to the rules file.
    """

    kind: str
    name: str
    default_tier: str | None = None
    params: dict[str, str] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        """Dagster-safe slug derived from the source name."""
        return re.sub(r"[^0-9a-z]+", "_", self.name.lower()).strip("_")

    @property
    def asset_name(self) -> str:
        """Dagster-safe asset/sensor key derived from the source name."""
        return f"corpus__{self.slug}"


def _registry():
    """The default source-kind registry (Box self-registers on import)."""
    # Importing the sources package triggers each kind's self-registration.
    from ..sources import default_source_kind_registry

    return default_source_kind_registry()


def _dedupe_names(specs: list[SourceSpec], *, origin: str) -> list[SourceSpec]:
    names = [s.name for s in specs]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise ValueError(f"{origin} has duplicate source names: {dups}")
    return specs


def _sources_from_connectors(*, registry=None) -> list[SourceSpec]:
    """Sources declared as connector TOMLs (the documented design).

    The Dagster pod mounts ``$AXI_STATE_DIR/plinth/connectors/`` so each
    connector becomes a live source. Every connector whose ``kind`` is
    registered flows through; unknown kinds are skipped with a warning.
    Kind-specific binding stays in ``params`` per ADR-056.
    """
    from ..agents.plinth.connectors import list_connectors

    reg = registry or _registry()
    specs: list[SourceSpec] = []
    for cfg in list_connectors():
        if not reg.has(cfg.kind):
            log.warning(
                "connector %r has unknown source kind %r (known: %s); skipping",
                cfg.name, cfg.kind, reg.kinds(),
            )
            continue
        specs.append(
            SourceSpec(
                kind=cfg.kind,
                name=cfg.name,
                default_tier=cfg.default_tier,
                params=dict(cfg.params),
            )
        )
    return _dedupe_names(specs, origin="connectors")


def _shared_box_env_params() -> dict[str, str]:
    """Env-level Box bindings shared by every env-shim spec.

    Mirrors what a connector TOML would carry per source:

    - ``NEUT_BOX_SESSION_DIR`` → ``params.session_dir`` (SSO session).
    - ``BOX_CCG_CONFIG`` / ``BOX_JWT_CONFIG`` → ``params.jwt_secret_ref``
      as ``env://<VAR>``, so the Box provider resolves the SAME
      server-auth JSON blob through the SecretStore and dispatches by
      shape (OAuth refresh-token / CCG / JWT keypair) — the Dagster env
      path and the connector/CLI path accept the exact same credential
      blob. Regression guard: an OAuth/CCG blob in ``BOX_CCG_CONFIG``
      must not be ignored in favor of the dead dev token (2026-06-30).
    """
    params: dict[str, str] = {}
    session_dir = os.environ.get("NEUT_BOX_SESSION_DIR")
    if session_dir:
        params["session_dir"] = session_dir
    for var in ("BOX_CCG_CONFIG", "BOX_JWT_CONFIG"):
        if os.environ.get(var):
            params["jwt_secret_ref"] = f"env://{var}"
            break
    return params


def _box_specs_from_env() -> list[SourceSpec]:
    """Box-only env back-compat shim.

    These envs predate connector TOMLs and are Box-shaped, so they only
    ever produce ``kind="box"`` specs (``folder_id`` moves into params).

    Precedence:
      1. ``DP1_BOX_SOURCES`` — JSON array of ``{name, folder_id,
         default_tier?}`` (dev / out-of-band).
      2. The single-folder ``DP1_BOX_FOLDER_ID`` env (legacy).
    """
    shared = _shared_box_env_params()

    raw = os.environ.get(_BOX_SOURCES_ENV)
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{_BOX_SOURCES_ENV} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError(f"{_BOX_SOURCES_ENV} must be a JSON array of objects")
        specs: list[SourceSpec] = []
        for i, item in enumerate(parsed):
            if not isinstance(item, dict) or not item.get("folder_id"):
                raise ValueError(
                    f"{_BOX_SOURCES_ENV}[{i}] requires a non-empty folder_id"
                )
            specs.append(
                SourceSpec(
                    kind="box",
                    name=str(item.get("name") or f"box-{i}"),
                    default_tier=item.get("default_tier"),
                    params={**shared, "folder_id": str(item["folder_id"])},
                )
            )
        return _dedupe_names(specs, origin=_BOX_SOURCES_ENV)

    folder_id = os.environ.get(_BOX_FOLDER_ID_ENV)
    if not folder_id:
        return []
    return [
        SourceSpec(
            kind="box",
            name=os.environ.get(_BOX_SOURCE_NAME_ENV, "box"),
            default_tier=os.environ.get(_BOX_DEFAULT_TIER_ENV),
            params={**shared, "folder_id": folder_id},
        )
    ]


def _iter_sources(*, registry=None) -> list[SourceSpec]:
    """Resolve the configured sources, kind-agnostic.

    Precedence:
      1. Connector TOMLs under ``$AXI_STATE_DIR/plinth/connectors/`` for
         any registered kind — the documented runtime-injection
         mechanism (ADR-056). One source per connector.
      2. The Box-only env back-compat shim (``DP1_BOX_SOURCES`` then
         ``DP1_BOX_FOLDER_ID``).

    Returns ``[]`` when nothing is configured; the caller decides whether
    that's an error.
    """
    connectors = _sources_from_connectors(registry=registry)
    if connectors:
        return connectors
    return _box_specs_from_env()


def _build_source(spec: SourceSpec, *, registry=None):
    """Construct an :class:`IngestSource` for ``spec`` via the registry.

    The platform never instantiates a kind's client directly — it
    reconstructs a :class:`ConnectorConfig` from the spec and hands it to
    the kind's :class:`SourceKindProvider.construct`, which owns the
    client lifecycle (including credential resolution via SecretRef).
    """
    from ..agents.plinth.connectors import ConnectorConfig

    reg = registry or _registry()
    bronze_root = os.environ.get(_BRONZE_ROOT_ENV, "/var/lib/axiom/bronze")
    cfg = ConnectorConfig(
        name=spec.name,
        kind=spec.kind,
        bronze_root=bronze_root,
        default_tier=spec.default_tier,
        params=dict(spec.params),
    )
    return reg.get(spec.kind).construct(cfg)


def _close_source(source) -> None:
    """Release a constructed source's client if it owns one."""
    close = getattr(source, "close", None)
    if callable(close):
        close()


def _build_writer(spec: SourceSpec):
    """Prefer the connector registry (single source of root + rules + tier) so
    Dagster lands in the same bronze tree under the same provenance rules as the
    CLI/HTTP/MCP paths — no split brain. Falls back to the legacy env-var writer
    only when the connector is not registered (back-compat / standalone runs)."""
    from pathlib import Path

    from axiom.rag.ingest_router import Disposition, load_rules_file

    from ..agents.plinth.connectors import load_connector
    from ..bronze import BronzeWriter, FilesystemBronzeSink
    from ..ingest_sink import build_writer_for_config

    try:
        return build_writer_for_config(load_connector(spec.name))
    except FileNotFoundError:
        log.warning(
            "connector %r not in registry; using legacy %s env writer "
            "(may diverge from CLI/HTTP bronze root — register the connector to converge)",
            spec.name, _BRONZE_ROOT_ENV,
        )

    bronze_root = Path(os.environ.get(_BRONZE_ROOT_ENV, "/var/lib/axiom/bronze"))
    rules_file = os.environ.get(_RULES_FILE_ENV)
    rules = load_rules_file(rules_file) if rules_file else []

    return BronzeWriter(
        rules=rules,
        sink=FilesystemBronzeSink(root=bronze_root),
        default_disposition=Disposition.QUARANTINE,
        default_tier=spec.default_tier,
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


def _build_definitions(*, registry=None):
    """Construct the Dagster ``Definitions`` (called inside an extra-installed env)."""
    # Imports stay function-scoped: the wider extension is imported in
    # environments without the [data-platform] extra. Bare `context` (no
    # annotation) is what the @asset decorator wants — Dagster's validator
    # identity-checks typed annotations against its internal class.
    from dagster import (
        DefaultSensorStatus,
        Definitions,
        RunRequest,
        SkipReason,
        asset,
        define_asset_job,
        sensor,
    )

    def _make_source_defs(spec: SourceSpec):
        """Build the (asset, job, sensor) trio for one source.

        A closure per source keeps each sensor's cursor (its
        modified_at watermark) independent, so sources poll and
        materialize on their own schedules.
        """

        @asset(
            name=spec.asset_name,
            group_name="dp1",
            description=f"Source '{spec.name}' ({spec.kind}) bronze + RAG embed pass (provenance-gated).",
        )
        def corpus(context):
            since_iso = context.partition_key if context.has_partition_key else None
            since = datetime.fromisoformat(since_iso) if since_iso else None

            source = _build_source(spec)
            writer = _build_writer(spec)
            store = _build_store()
            try:
                report = run_source_to_rag(source=source, writer=writer, store=store, since=since)
            finally:
                _close_source(source)

            context.add_output_metadata(
                {
                    "source": spec.name,
                    "kind": spec.kind,
                    "tier": spec.default_tier or "(rules)",
                    "items_seen": report.items_seen,
                    "items_landed": report.items_landed,
                    "items_failed": report.items_failed,
                }
            )
            return report.items_landed

        ready_name = f"rag_index_ready__{spec.slug}"

        @asset(
            name=ready_name,
            group_name="dp1",
            deps=[corpus],
            description=f"Marker: RAG served view reflects '{spec.name}' bronze.",
        )
        def rag_index_ready(context) -> bool:
            # The embed happens inside corpus (via run_source_to_rag), so
            # this asset is the downstream-subscribe freshness signal.
            return True

        job = define_asset_job(
            f"dp1_run_job__{spec.slug}",
            selection=[corpus, rag_index_ready],
        )

        @sensor(
            name=f"{spec.asset_name}_sensor",
            job=job,
            minimum_interval_seconds=60,
            default_status=DefaultSensorStatus.STOPPED,
        )
        def corpus_sensor(context):
            # Cursor is the last-seen modified_at watermark (ISO string).
            last = context.cursor
            since = datetime.fromisoformat(last) if last else None

            source = _build_source(spec)
            try:
                changed = source.list_changed(since=since)
            finally:
                _close_source(source)

            if not changed:
                yield SkipReason(f"no new items in '{spec.name}' since watermark")
                return

            now = datetime.now(UTC).isoformat()
            context.update_cursor(now)
            yield RunRequest(run_key=f"{spec.asset_name}-{now}")

        return [corpus, rag_index_ready], job, corpus_sensor

    assets: list = []
    jobs: list = []
    sensors: list = []
    for spec in _iter_sources(registry=registry):
        src_assets, job, src_sensor = _make_source_defs(spec)
        assets.extend(src_assets)
        jobs.append(job)
        sensors.append(src_sensor)

    return Definitions(assets=assets, jobs=jobs, sensors=sensors)


# Lazy-built so import-time doesn't require dagster.
definitions = None


def __getattr__(name):
    global definitions
    if name == "definitions":
        if definitions is None:
            definitions = _build_definitions()
        return definitions
    raise AttributeError(name)
