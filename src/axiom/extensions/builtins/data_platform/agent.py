# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PLINTH — the data-platform orchestrator agent (skeleton).

PLINTH is the home that future data-platform initiatives plug into. Its
role is to orchestrate ingestion, source-monitoring, and medallion
pack-flow over the contributions registered in a
:class:`DataPlatformRegistry`.

This is a SKELETON. ``run_scheduled_ingest`` is the job-dispatch
interface; at this stage it drives the :class:`IngestSource` protocol
(``list_changed`` then ``fetch``) directly and returns a report. There is
no real scheduler and no real ingest engine — the heavy lakehouse layer
(Iceberg / Dagster / dbt, declared as the ``data-platform`` optional
extra) wires those in later behind this same dispatch surface.

A real downstream-mutating op MUST route through
``axiom.policy.agent_action_guard.guarded_act`` per AEOS §4.1. The
skeleton performs no external mutation, so it does not yet take that
path; the seam is called out where the engine call will live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .contracts import IngestSource
from .registry import DataPlatformRegistry


@dataclass(frozen=True)
class IngestReport:
    """Outcome of one scheduled-ingest dispatch."""

    source: str
    items_seen: int
    items_fetched: int
    dry_run: bool = False


class PlinthAgent:
    """Data-platform orchestrator.

    Holds a :class:`DataPlatformRegistry` of consumer-contributed sources
    and packs, and exposes the ``run_scheduled_ingest`` dispatch the
    scheduler (future) calls per source.
    """

    name = "PLINTH"
    description = "PLINTH — data-platform orchestrator: ingestion, source-monitoring, pack-flow"

    def __init__(self, registry: DataPlatformRegistry | None = None) -> None:
        self.registry = registry if registry is not None else DataPlatformRegistry()

    def run_scheduled_ingest(
        self,
        source: IngestSource | str,
        *,
        since: datetime | None = None,
        dry_run: bool = False,
    ) -> IngestReport:
        """Dispatch one ingest pass over ``source``.

        ``source`` may be a live :class:`IngestSource` or the registered
        name of one. The skeleton polls :meth:`IngestSource.list_changed`
        for the changed set, then (unless ``dry_run``) fetches each item.

        The fetched bytes are intentionally discarded here — handing them
        to a real medallion-bronze write is the heavy layer's job and the
        point where ``guarded_act`` will wrap the external mutation.
        """
        resolved = self.registry.get_source(source) if isinstance(source, str) else source

        changed = list(resolved.list_changed(since))
        fetched = 0
        if not dry_run:
            for item in changed:
                # Seam: the heavy layer routes resolved.fetch(item) into a
                # medallion-bronze write via guarded_act. The skeleton only
                # exercises the protocol and counts.
                resolved.fetch(item)
                fetched += 1

        return IngestReport(
            source=resolved.name,
            items_seen=len(changed),
            items_fetched=fetched,
            dry_run=dry_run,
        )

    def execute(self, source: IngestSource | str) -> IngestReport:
        """AEOS §4.1 ``execute`` entry point — alias of a default ingest pass."""
        return self.run_scheduled_ingest(source)


__all__ = ["PlinthAgent", "IngestReport"]
