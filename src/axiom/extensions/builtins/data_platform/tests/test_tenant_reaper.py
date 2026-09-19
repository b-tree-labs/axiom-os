# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""The connector reaper — the data platform's half of a tenant offboard."""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
    list_connectors,
    save_connector,
)
from axiom.extensions.builtins.data_platform.tenant_reaper import ConnectorReaper
from axiom.infra.tenancy import ReaperRegistry, offboard


def _connector(tmp_path, name, site):
    save_connector(
        ConnectorConfig(name=name, kind="push", bronze_root=str(tmp_path / "b"), site=site),
        state_dir=tmp_path,
    )


def test_finds_only_this_tenants_connectors(tmp_path) -> None:
    _connector(tmp_path, "acu-archive", "acu-flowloop")
    _connector(tmp_path, "vcu-archive", "vcu-flowloop")
    found = list(ConnectorReaper(state_dir=tmp_path).find("acu-flowloop"))
    assert [r.identifier for r in found] == ["acu-archive"]


def test_a_connector_with_no_site_is_never_reaped(tmp_path) -> None:
    """Unattributed is not the same as belonging to whoever is being removed."""
    save_connector(
        ConnectorConfig(name="legacy", kind="push", bronze_root=str(tmp_path / "b")),
        state_dir=tmp_path,
    )
    assert list(ConnectorReaper(state_dir=tmp_path).find("acu-flowloop")) == []


def test_reap_removes_this_tenant_and_leaves_the_others(tmp_path) -> None:
    _connector(tmp_path, "acu-archive", "acu-flowloop")
    _connector(tmp_path, "vcu-archive", "vcu-flowloop")
    removed = list(ConnectorReaper(state_dir=tmp_path).reap("acu-flowloop"))
    assert len(removed) == 1
    remaining = {c.name for c in list_connectors(state_dir=tmp_path)}
    assert remaining == {"vcu-archive"}


def test_offboard_reports_the_connector_but_stays_incomplete(tmp_path) -> None:
    """Even a clean connector delete is not an offboard: retrieval, caches and
    the rest still hold the tenant's data, and the report says so."""
    _connector(tmp_path, "acu-archive", "acu-flowloop")
    registry = ReaperRegistry()
    registry.register(ConnectorReaper(state_dir=tmp_path))
    report = offboard("acu-flowloop", dry_run=False, registry=registry)
    assert [r.identifier for r in report.found] == ["acu-archive"]
    assert report.removed
    assert report.complete is False
    assert report.unreaped  # caches, sessions, memory and the rest remain
