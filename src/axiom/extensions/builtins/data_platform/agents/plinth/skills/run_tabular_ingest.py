# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``run-tabular-ingest`` — the row lane's peer to ``run_ingest`` (ADR-001).

Same gated shape as the document drive path (``list_changed`` → per-item work,
wrapped in ``guarded_act`` for ADR-045 reversibility + volume bound), but the
per-batch work is ``fetch_rows`` → :class:`TabularBronzeWriter` → typed bronze
rows, with row-level ``content_hash`` dedup. There is no RAG embed step — the
terminal artifact is rows in a table, not chunks.

:func:`run_connector_ingest` is the shape switch: it reads the connector's
provider ``shape`` (``source_shape``) and dispatches to this loop for tabular
sources or to :func:`run_ingest` for document sources, so one CLI verb
(``data ingest``) serves both lanes.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.policy.agent_action_guard import AgentAction, GuardDecision, guarded_act
from axiom.rag.ingest_router import Disposition, load_rules_file

from ....bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ....ingest_run import IngestRunReport, RunStore
from ....sources import default_source_kind_registry
from ....sources.contracts import source_shape
from ..connectors import ConnectorConfig, load_connector
from .run_ingest import RunIngestReport, _close_source, run_ingest

log = logging.getLogger(__name__)

# The row lane's funnel stops at `loaded` — no extract/index (that's document).
_TABULAR_STAGES = ("discovered", "to_process", "fetched", "loaded")


def run_connector_ingest(
    connector_name: str,
    *,
    state_dir: Path | None = None,
    **kwargs: Any,
) -> RunIngestReport:
    """Dispatch a connector's ingest to the lane matching its provider ``shape``.

    Tabular → :func:`run_tabular_ingest`; document (the default) →
    :func:`run_ingest`. Both return a :class:`RunIngestReport`, so the CLI verb
    reads one shape regardless of lane.
    """
    config = load_connector(connector_name, state_dir=state_dir)
    provider = default_source_kind_registry().get(config.kind)
    if source_shape(provider) == "tabular":
        return run_tabular_ingest(connector_name, state_dir=state_dir, **kwargs)
    return run_ingest(connector_name, state_dir=state_dir, **kwargs)


def run_tabular_ingest(
    connector_name: str,
    *,
    since: datetime | None = None,
    state_dir: Path | None = None,
    source: Any | None = None,
    writer: TabularBronzeWriter | None = None,
    volume_mode: str = "confirm",
    max_workers: int | None = None,  # accepted for CLI parity; the row lane is serial
    run_store: RunStore | None = None,
) -> RunIngestReport:
    """Run one PLINTH-gated tabular ingest pass.

    ``source`` / ``writer`` are overridable so unit tests drive the gate with no
    network and no DB. Row-level counts (in / landed / duplicate) are recorded
    as run metrics; ``items_*`` count *batches*.
    """
    config = load_connector(connector_name, state_dir=state_dir)
    source, source_owned = _resolve_tabular_source(source, config)
    writer = writer or _build_tabular_writer(config)
    state_root = state_dir or get_user_state_dir()

    run = IngestRunReport.start("cdc", source=connector_name, stages=_TABULAR_STAGES)

    def _finalize(report: RunIngestReport, *, failed: bool = False) -> RunIngestReport:
        run.finish(failed=failed)
        if run_store is not None:
            try:
                run_store.save(run)
            except Exception:  # noqa: BLE001 — telemetry must never sink a run
                log.warning("tabular ingest run-store save failed for %s", run.run_id)
        return replace(report, funnel=run.to_dict())

    try:
        item_ids = source.list_changed(since=since)
        run.entered("discovered", len(item_ids))
        run.advanced("discovered", len(item_ids))
        if not item_ids:
            return _finalize(RunIngestReport(
                connector=connector_name, proceed=True,
                items_seen=0, items_landed=0, items_failed=0,
            ))
        run.entered("to_process", len(item_ids))
        run.advanced("to_process", len(item_ids))

        landed_batches: list[str] = []
        failed_batches: list[str] = []
        totals = {"rows_in": 0, "rows_landed": 0, "rows_duplicate": 0}

        def do_one(item_id: str) -> bool:
            try:
                batch = source.fetch_rows(item_id)
                run.entered("fetched", 1)
                run.advanced("fetched", 1)
                res = writer.write(batch)
                run.entered("loaded", 1)
                totals["rows_in"] += res.rows_in
                totals["rows_landed"] += res.rows_landed
                totals["rows_duplicate"] += res.rows_duplicate
                if res.disposition is Disposition.EXCLUDE:
                    run.dropped("loaded", "excluded", 1)
                elif res.rows_landed > 0:
                    run.advanced("loaded", 1)
                    landed_batches.append(item_id)
                else:
                    # every row was a known duplicate — nothing new to land
                    run.dropped("loaded", "no_new_rows", 1)
                return True
            except Exception as exc:  # noqa: BLE001 — one bad batch must not sink the run
                log.warning("tabular ingest batch %s failed: %s", item_id, exc)
                run.failed("fetched", type(exc).__name__, 1)
                failed_batches.append(item_id)
                return False

        action = AgentAction(
            agent="plinth",
            op_class="data_platform.ingest_tabular",
            name="run_tabular_ingest",
            candidates=list(item_ids),
            reversible=True,
        )
        decision: GuardDecision = guarded_act(
            action,
            do_one=do_one,
            state_dir=state_root,
            volume_mode=volume_mode,
        )

        for k, v in totals.items():
            run.set_metric(k, v)
        if not decision.proceed:
            run.refuse(decision.reason)
        return _finalize(RunIngestReport(
            connector=connector_name,
            proceed=decision.proceed,
            items_seen=len(item_ids),
            items_landed=len(landed_batches),
            items_failed=len(failed_batches),
            refused_reason=decision.reason if not decision.proceed else "",
            refused=[str(c) for c in decision.refused] + [str(c) for c in decision.would_proceed],
        ))
    finally:
        if source_owned:
            _close_source(source)


# ---- construction helpers (overridable for tests) ------------------------


def _resolve_tabular_source(source: Any | None, config: ConnectorConfig):
    if source is not None:
        return source, False
    provider = default_source_kind_registry().get(config.kind)
    return provider.construct(config), True


def _build_tabular_writer(config: ConnectorConfig) -> TabularBronzeWriter:
    rules = load_rules_file(config.provenance_rules_file) if config.provenance_rules_file else []
    return TabularBronzeWriter(
        rules=rules,
        sink=FilesystemTabularBronzeSink(root=Path(config.bronze_root)),
        default_disposition=Disposition(config.default_disposition),
        default_tier=config.default_tier,
    )


__all__ = ["run_connector_ingest", "run_tabular_ingest"]
