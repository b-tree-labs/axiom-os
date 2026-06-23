# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""connector.* skills over the Registry Fabric (ADR-074 Phase 2)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.connect.connectors import (
    slack_connector_descriptor,
    teams_connector_descriptor,
)
from axiom.infra.connector_fabric import (
    ConnectionStatus,
    ConnectionStore,
    ConnectorState,
    InProcessConnectorFabric,
)

reg = pytest.importorskip(
    "axiom.extensions.builtins.connect.skills.registry_ops",
    reason="connect registry skills",
)


@pytest.fixture
def env():
    fab = InProcessConnectorFabric()
    fab.register(slack_connector_descriptor())
    fab.register(teams_connector_descriptor())
    return {"fabric": fab, "connections": ConnectionStore(), "state": ConnectorState()}


def test_catalog_surfaces_enabled_availability_and_setup_links(env):
    res = reg.list_connectors({**env}, ctx=None)
    slack = next(e for e in res.value["entries"] if e["name"] == "ai.axiom.connector.slack")
    assert slack["enabled"] is False          # off by default — user switches on
    assert slack["availability"] == "available"
    assert slack["setup"]["install_kind"] == "app_manifest"
    assert any("api.slack.com" in u for u in slack["setup"]["urls"].values())
    teams = next(e for e in res.value["entries"] if e["name"] == "ai.axiom.connector.teams")
    assert teams["availability"] == "planned"  # trickle-out, still listed


def test_enable_and_disable_toggle_state(env):
    assert reg.enable({**env, "name": "ai.axiom.connector.slack"}, ctx=None).ok
    assert env["state"].is_enabled("ai.axiom.connector.slack")
    cat = reg.list_connectors({**env}, ctx=None).value["entries"]
    assert next(e for e in cat if e["name"] == "ai.axiom.connector.slack")["enabled"] is True
    reg.disable({**env, "name": "ai.axiom.connector.slack"}, ctx=None)
    assert not env["state"].is_enabled("ai.axiom.connector.slack")


def test_enable_unknown_connector_errors(env):
    assert not reg.enable({**env, "name": "ai.axiom.connector.ghost"}, ctx=None).ok


def test_show_returns_setup_deeplinks(env):
    res = reg.show({**env, "name": "ai.axiom.connector.slack"}, ctx=None)
    assert res.ok
    assert res.value["setup"]["install_kind"] == "app_manifest"
    assert res.value["setup"]["urls"]
    assert res.value["setup"]["needs"]


def test_catalog_lists_connectors(env):
    res = reg.list_connectors({**env, "artifact_class": "connector"}, ctx=None)
    assert res.ok
    names = [e["name"] for e in res.value["entries"]]
    assert "ai.axiom.connector.slack" in names
    # catalog summary surfaces trust tier + kind for a UI / AXI
    slack = next(e for e in res.value["entries"] if e["name"] == "ai.axiom.connector.slack")
    assert slack["trust_tier"] == "first_party"
    assert slack["kind"] == "channel_adapter"


def test_resolve_returns_serverjson_and_required_secrets(env):
    res = reg.show({**env, "name": "ai.axiom.connector.slack"}, ctx=None)
    assert res.ok
    sj = res.value["descriptor"]
    assert sj["name"] == "ai.axiom.connector.slack"
    # required secret inputs surfaced so install knows what to collect
    assert set(res.value["required_secrets"]) == {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}


def test_resolve_unknown_connector_errors(env):
    res = reg.show({**env, "name": "ai.axiom.connector.nope"}, ctx=None)
    assert not res.ok
    assert any("nope" in e for e in res.errors)


def test_install_creates_pending_connection(env):
    res = reg.install(
        {
            **env,
            "connector": "ai.axiom.connector.slack",
            "name": "slack-ops",
            "owner": "@ben",
            "secret_ref": "kubernetes://axiom-data/dp1-slack",
        },
        ctx=None,
    )
    assert res.ok
    conn = env["connections"].get("slack-ops")
    assert conn is not None
    assert conn.status is ConnectionStatus.PENDING
    assert conn.connector == "ai.axiom.connector.slack"
    # secrets never inline
    assert conn.secret_ref.startswith("kubernetes://")


def test_install_unknown_connector_errors(env):
    res = reg.install(
        {**env, "connector": "ai.axiom.connector.ghost", "name": "x", "owner": "@ben", "secret_ref": "env://X"},
        ctx=None,
    )
    assert not res.ok


def test_health_reports_connection_statuses(env):
    reg.install(
        {**env, "connector": "ai.axiom.connector.slack", "name": "slack-ops", "owner": "@ben", "secret_ref": "env://X"},
        ctx=None,
    )
    env["connections"].set_status("slack-ops", ConnectionStatus.ACTIVE)
    res = reg.status({**env}, ctx=None)
    assert res.ok
    statuses = {c["name"]: c["status"] for c in res.value["connections"]}
    assert statuses["slack-ops"] == "active"
