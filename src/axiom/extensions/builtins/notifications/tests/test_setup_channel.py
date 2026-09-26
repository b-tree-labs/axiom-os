# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.setup`` — the runbook collapsed into a verb.

Links mode prints the clickable vendor steps; apply mode persists to the
installed herald.toml (0600), auto-declares the channel in
expected_channels, and confirms a fresh SendContext actually registered it.
"""

from __future__ import annotations

import os
import stat

import pytest

from axiom.extensions.builtins.notifications import channel_config
from axiom.extensions.builtins.notifications.skills import setup_channel


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "herald.toml"
    monkeypatch.setattr(channel_config, "_config_path", lambda: path)
    for var in (
        "AXIOM_HERALD_EXPECTED_CHANNELS",
        "AXIOM_HERALD_TEAMS_WEBHOOK_URL",
        "AXIOM_HERALD_SLACK_WEBHOOK_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


def test_links_mode_emits_clickable_urls(config_path):
    out = setup_channel.run({"channel": "teams"}, ctx=None)
    assert out.ok
    assert out.value["mode"] == "links"
    urls = [link["url"] for link in out.value["links"]]
    assert any("make.powerautomate.com" in u for u in urls)
    assert "--webhook-url" in out.value["next"]


def test_links_deep_link_uses_stored_identity(config_path):
    out = setup_channel.run(
        {
            "channel": "teams",
            "tenant_id": "tid-1",
            "group_id": "gid-1",
            "channel_id": "19:abc@thread.tacv2",
            "channel_name": "Alerts",
        },
        ctx=None,
    )
    urls = [link["url"] for link in out.value["links"]]
    assert any("teams.microsoft.com/l/channel/" in u and "gid-1" in u for u in urls)


def test_apply_persists_registers_and_declares(config_path):
    pytest.importorskip("httpx")
    out = setup_channel.run(
        {
            "channel": "teams",
            "webhook_url": "https://x.logic.azure.com/workflows/1/triggers/manual/paths/invoke?sig=s",
        },
        ctx=None,
    )
    assert out.ok, out.errors
    assert out.value["mode"] == "applied"
    assert "teams" in out.value["registered"]
    assert "teams" in out.value["expected_channels"]
    # durable + private
    assert config_path.exists()
    assert stat.S_IMODE(os.stat(config_path).st_mode) == 0o600
    # the layered resolver now sees it with no env vars at all
    resolved = channel_config.resolved_config({})
    assert "logic.azure.com" in resolved["AXIOM_HERALD_TEAMS_WEBHOOK_URL"]
    assert "teams" in resolved["AXIOM_HERALD_EXPECTED_CHANNELS"]


def test_apply_rejects_wrong_host(config_path):
    out = setup_channel.run(
        {"channel": "teams", "webhook_url": "https://evil.example/hook"},
        ctx=None,
    )
    assert not out.ok
    assert "logic.azure.com" in out.errors[0]
    assert not config_path.exists()  # nothing persisted on refusal


def test_env_still_overrides_installed(config_path, monkeypatch):
    setup_channel._write_installed({"teams_webhook_url": "https://a.logic.azure.com/1"})
    monkeypatch.setenv("AXIOM_HERALD_TEAMS_WEBHOOK_URL", "https://b.logic.azure.com/2")
    resolved = channel_config.resolved_config(None)
    assert resolved["AXIOM_HERALD_TEAMS_WEBHOOK_URL"].endswith("/2")


def test_unknown_channel_is_actionable(config_path):
    out = setup_channel.run({"channel": "carrier-pigeon"}, ctx=None)
    assert not out.ok
    assert "supported" in out.errors[0]


def test_pasted_url_is_sanitized(config_path):
    pytest.importorskip("httpx")
    # typographic quotes + whitespace from a chat-mangled paste must not break it
    out = setup_channel.run(
        {
            "channel": "teams",
            "webhook_url": " \u2018https://x.logic.azure.com/w/1/paths/invoke?a=1&sig=s\u2019 ",
        },
        ctx=None,
    )
    assert out.ok, out.errors
    resolved = channel_config.resolved_config({})
    assert resolved["AXIOM_HERALD_TEAMS_WEBHOOK_URL"].startswith("https://")
    assert "\u2018" not in resolved["AXIOM_HERALD_TEAMS_WEBHOOK_URL"]


def test_apply_accepts_powerplatform_host(config_path):
    pytest.importorskip("httpx")
    # Microsoft's newer Workflows URLs live on *.api.powerplatform.com
    out = setup_channel.run(
        {
            "channel": "teams",
            "webhook_url": (
                "https://defaultabc.e1.environment.api.powerplatform.com:443/"
                "powerautomate/automations/direct/workflows/b7/triggers/manual/paths/invoke"
                "?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=SECRET"
            ),
        },
        ctx=None,
    )
    assert out.ok, out.errors


def test_links_mode_persists_identity(config_path):
    setup_channel.run(
        {"channel": "teams", "tenant_id": "t1", "group_id": "g1",
         "channel_id": "19:x@thread.tacv2", "channel_name": "Alerts"},
        ctx=None,
    )
    out = setup_channel.run({"channel": "teams"}, ctx=None)  # no flags this time
    urls = [link["url"] for link in out.value["links"]]
    assert any("teams.microsoft.com/l/channel/" in u and "g1" in u for u in urls)
