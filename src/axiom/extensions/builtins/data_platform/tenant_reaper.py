# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Connector reaper — the data platform's half of a tenant offboard.

A connector carries its site (ADR-023-A1 §A1.5(2)), which is what makes this
reaper possible at all: the subsystem can answer "what does this tenant own"
without a second registry to consult and disagree with.

It removes the connector *record*, not the rows it landed. Bronze and silver
deletion is a retention decision with its own policy and its own timing, and
it is listed in ``UNREAPED`` rather than done quietly here — an offboard that
silently dropped a facility's measurement history would be a worse failure
than one that admits it has not.
"""

from __future__ import annotations

from collections.abc import Iterable

from axiom.infra.tenancy import TenantResource

from .agents.plinth.connectors import delete_connector, list_connectors


class ConnectorReaper:
    subsystem = "connectors"

    def __init__(self, state_dir=None) -> None:  # noqa: ANN001 — Path | None
        self._state_dir = state_dir

    def _for_site(self, site: str):
        for config in list_connectors(state_dir=self._state_dir):
            if (getattr(config, "site", None) or "") == site:
                yield config

    def find(self, site: str) -> Iterable[TenantResource]:
        return [
            TenantResource(
                subsystem=self.subsystem,
                kind="connector",
                identifier=config.name,
                detail=f"{config.kind} → {config.bronze_root}",
            )
            for config in self._for_site(site)
        ]

    def reap(self, site: str) -> Iterable[str]:
        removed = []
        for config in list(self._for_site(site)):
            delete_connector(config.name, state_dir=self._state_dir)
            removed.append(f"connectors: removed connector {config.name!r}")
        return removed


__all__ = ["ConnectorReaper"]
