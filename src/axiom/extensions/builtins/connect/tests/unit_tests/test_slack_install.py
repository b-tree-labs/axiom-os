# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Automated Slack app-manifest install (ADR-074 Phase 2)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.connect.connectors import (
    slack_connector_descriptor,
)
from axiom.infra.connector_fabric import (
    ConnectionStatus,
    ConnectionStore,
    InProcessConnectorFabric,
)

si = pytest.importorskip(
    "axiom.extensions.builtins.connect.skills.slack_install",
    reason="slack install skill",
)


def test_manifest_declares_socket_mode_scopes_events_interactivity():
    m = si.build_app_manifest(site="ExampleSite", channel="ops-channel")
    # App presence is the install identity, not channel- or agent-derived.
    assert m["display_information"]["name"] == "Axiom · ExampleSite"
    s = m["settings"]
    assert s["socket_mode_enabled"] is True
    assert s["interactivity"]["is_enabled"] is True
    # Must hear both public and private channel messages (incidents are private).
    assert set(s["event_subscriptions"]["bot_events"]) >= {
        "app_mention", "message.channels", "message.groups"}
    bot_scopes = set(m["oauth_config"]["scopes"]["bot"])
    assert {"chat:write", "chat:write.customize", "app_mentions:read",
            "channels:history", "channels:join", "reactions:write"} <= bot_scopes
    # Neutral fallback name; agents attribute per-message via username override.
    assert m["features"]["bot_user"]["display_name"] == "Axiom"


@pytest.fixture
def env():
    fab = InProcessConnectorFabric()
    fab.register(slack_connector_descriptor())
    return {"fabric": fab, "connections": ConnectionStore()}


def test_install_without_config_token_returns_create_from_manifest_link(env):
    res = si.slack_install(
        {**env, "channel": "ops-channel", "owner": "@ben", "secret_ref": "kubernetes://axiom-data/dp1-slack"},
        ctx=None,
    )
    assert res.ok
    # one-click "create from manifest" deep link prefilled with the manifest
    assert "manifest" in res.value["create_url"].lower()
    # a pending connection was registered through the fabric
    conn = env["connections"].get("slack-ops-channel")
    assert conn is not None and conn.status is ConnectionStatus.PENDING
    assert conn.secret_ref.startswith("kubernetes://")


def test_install_with_config_token_creates_app_via_manifest_api(env):
    calls = {}

    def fake_manifest_api(token, manifest):
        calls["token"] = token
        calls["manifest"] = manifest
        return {"app_id": "A12345", "ok": True}

    res = si.slack_install(
        {
            **env,
            "channel": "ops-channel",
            "owner": "@ben",
            "secret_ref": "kubernetes://axiom-data/dp1-slack",
            "config_token": "xoxe-abc",
            "manifest_api": fake_manifest_api,
        },
        ctx=None,
    )
    assert res.ok
    assert res.value["app_id"] == "A12345"
    assert calls["token"] == "xoxe-abc"
    assert calls["manifest"]["settings"]["socket_mode_enabled"] is True
    # next step is the (human) workspace-install consent — deep-linked
    assert "install_url" in res.value


def test_install_requires_channel_and_secret_ref(env):
    res = si.slack_install({**env, "owner": "@ben"}, ctx=None)
    assert not res.ok
