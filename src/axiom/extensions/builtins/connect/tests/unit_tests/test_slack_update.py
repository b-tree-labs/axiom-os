# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""In-place connector update — evolve a deployed Slack app without teardown
or new tokens (ADR-074)."""

from __future__ import annotations

from axiom.extensions.builtins.connect.skills.slack_install import build_app_manifest
from axiom.extensions.builtins.connect.skills.slack_update import (
    classify_manifest_change,
    slack_update,
)


def _manifest(scopes, events, name="Axiom · Example-Site"):
    return {
        "display_information": {"name": name},
        "oauth_config": {"scopes": {"bot": scopes}},
        "settings": {"event_subscriptions": {"bot_events": events}},
    }


def test_classify_config_only_when_event_added_no_new_scope():
    cur = _manifest(["chat:write", "groups:history"], ["app_mention"])
    des = _manifest(["chat:write", "groups:history"], ["app_mention", "message.groups"])
    c = classify_manifest_change(cur, des)
    assert c["kind"] == "config_only"
    assert c["needs_reconsent"] is False
    assert c["added_events"] == ["message.groups"]


def test_classify_scope_add_needs_reconsent():
    cur = _manifest(["chat:write"], ["app_mention"])
    des = _manifest(["chat:write", "chat:write.customize"], ["app_mention"])
    c = classify_manifest_change(cur, des)
    assert c["kind"] == "scope_add"
    assert c["needs_reconsent"] is True
    assert c["added_scopes"] == ["chat:write.customize"]


def test_classify_name_change_is_config_only():
    cur = _manifest(["chat:write"], [], name="Axiom-example-sysadmin")
    des = _manifest(["chat:write"], [], name="Axiom · Example-Site")
    c = classify_manifest_change(cur, des)
    assert c["kind"] == "config_only" and c["name_changed"] is True


def test_classify_noop_when_identical():
    m = _manifest(["chat:write"], ["app_mention"])
    assert classify_manifest_change(m, dict(m))["kind"] == "noop"


def test_update_applies_in_place_and_reports_reconsent_for_new_scope():
    # current app predates chat:write.customize + message.groups + new name
    current = build_app_manifest(site="Example-Site", channel="example-sysadmin")
    current["display_information"]["name"] = "Axiom-example-sysadmin"
    current["oauth_config"]["scopes"]["bot"] = [
        s for s in current["oauth_config"]["scopes"]["bot"] if s != "chat:write.customize"
    ]
    current["settings"]["event_subscriptions"]["bot_events"] = [
        e for e in current["settings"]["event_subscriptions"]["bot_events"] if e != "message.groups"
    ]

    calls = {}

    def export_api(token, app_id):
        calls["export"] = (token, app_id)
        return {"ok": True, "manifest": current}

    def update_api(token, app_id, manifest):
        calls["update"] = (token, app_id, manifest)
        return {"ok": True, "app_id": app_id}

    res = slack_update(
        {"config_token": "xoxe-tok", "app_id": "A123", "site": "Example-Site",
         "channel": "example-sysadmin", "export_api": export_api, "update_api": update_api},
        ctx=None,
    )
    assert res.ok
    assert res.value["applied"] is True
    # adding chat:write.customize -> scope_add -> one re-consent, same token
    assert res.value["change"]["kind"] == "scope_add"
    assert "chat:write.customize" in res.value["change"]["added_scopes"]
    assert "reconsent_url" in res.value
    assert "tokens stay valid" in res.value["next_steps"]
    # update was applied in place with the SAME app_id
    assert calls["update"][1] == "A123"


def test_update_noop_when_already_current():
    desired = build_app_manifest(site="Example-Site", channel="example-sysadmin")
    res = slack_update(
        {"config_token": "xoxe", "app_id": "A1", "site": "Example-Site", "channel": "example-sysadmin",
         "export_api": lambda t, a: {"ok": True, "manifest": desired},
         "update_api": lambda t, a, m: {"ok": True}},
        ctx=None,
    )
    assert res.ok and res.value["applied"] is False


def test_update_requires_config_token_and_app_id():
    assert not slack_update({"site": "Example-Site"}, ctx=None).ok
