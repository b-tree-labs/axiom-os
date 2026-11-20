# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Automated Slack connector install (ADR-074 Phase 2).

Slack apps are declarative *manifests*, so we generate the full manifest
(Socket Mode, scopes, events, interactivity) and automate everything Slack's
security model allows:

- **With** an App-Configuration token (``xoxe-…``): call ``apps.manifest.create``
  to build the app programmatically — zero console clicking.
- **Without** one: emit a prefilled *"create from manifest"* deep link (2 clicks).

Either way the connector then registers a PENDING ``slack`` connection bound to
a keystore ``secret_ref`` (never inline creds); workspace-install consent (the
one step Slack mandates a human for) is reduced to a deep link, after which the
bot auto-joins the channel and the connection is health-verified to ACTIVE
(those live steps run in the connect CLI, not this pure skill).
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from typing import Any

from axiom.infra.skills import SkillResult

from .registry_ops import install as _register_connection

_BOT_SCOPES = [
    "chat:write",
    "chat:write.customize",  # per-message agent attribution (AgentCard.name as username)
    "app_mentions:read",
    "channels:history",
    "groups:history",
    "channels:read",   # resolve channel name -> id (conversations.list)
    "groups:read",     # private channels
    "channels:join",
    "reactions:write",
]
# message.channels = public; message.groups = private. A bidirectional comms
# connector must hear both, since incident channels are routinely private.
_BOT_EVENTS = ["app_mention", "message.channels", "message.groups"]


def build_app_manifest(*, site: str, channel: str | None = None) -> dict:
    """The Slack app manifest for an Axiom bidirectional comms connector.

    The app is the *install presence* (``Axiom · <site>``), not an agent and
    not keyed to one channel — agents speak through it with per-message
    attribution (``chat:write.customize``). ``site`` is the install/site
    identity (e.g. ``example-host``); ``channel`` is only used for the description.
    """
    presence = f"Axiom · {site}"
    where = f" for #{channel}" if channel else ""
    return {
        "display_information": {"name": presence, "description": f"Axiom agent presence{where}"},
        # Bot fallback name is the neutral install presence; individual agents
        # override `username` per message from their AgentCard.
        "features": {"bot_user": {"display_name": "Axiom", "always_online": True}},
        "oauth_config": {"scopes": {"bot": list(_BOT_SCOPES)}},
        "settings": {
            "socket_mode_enabled": True,
            "interactivity": {"is_enabled": True},
            "event_subscriptions": {"bot_events": list(_BOT_EVENTS)},
            "org_deploy_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def _create_from_manifest_url(manifest: dict) -> str:
    return "https://api.slack.com/apps?new_app=1&manifest_json=" + urllib.parse.quote(
        json.dumps(manifest)
    )


def _default_manifest_api(token: str, manifest: dict) -> dict:  # pragma: no cover - live HTTP
    import urllib.request

    data = urllib.parse.urlencode({"token": token, "manifest": json.dumps(manifest)}).encode()
    req = urllib.request.Request("https://slack.com/api/apps.manifest.create", data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def slack_install(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    channel = params.get("channel")
    owner = params.get("owner")
    secret_ref = params.get("secret_ref")
    missing = [k for k in ("channel", "owner", "secret_ref") if not params.get(k)]
    if missing:
        return SkillResult(ok=False, errors=[f"missing required params: {', '.join(missing)}"])

    site = params.get("site") or "Axiom"
    manifest = build_app_manifest(site=site, channel=channel)
    actions = [f"built Slack app manifest for #{channel}"]

    value: dict[str, Any] = {"channel": channel, "manifest": manifest}
    config_token = params.get("config_token")
    if config_token:
        api: Callable[[str, dict], dict] = params.get("manifest_api") or _default_manifest_api
        result = api(config_token, manifest)
        if not result.get("ok", True) or "app_id" not in result:
            return SkillResult(ok=False, errors=[f"apps.manifest.create failed: {result}"], actions_taken=actions)
        app_id = result["app_id"]
        value["app_id"] = app_id
        # Exact per-app deep links — one click each from the CLI output.
        value["install_url"] = f"https://api.slack.com/apps/{app_id}/install-on-team"
        value["token_urls"] = {
            "Bot token (xoxb-)": f"https://api.slack.com/apps/{app_id}/oauth",
            "App-level token (xapp-, connections:write)": f"https://api.slack.com/apps/{app_id}/general",
        }
        actions.append(f"created Slack app {app_id} via apps.manifest.create")
    else:
        value["create_url"] = _create_from_manifest_url(manifest)
        actions.append("emitted create-from-manifest deep link (no config token)")

    # Register the PENDING connection through the same fabric path as any connector.
    reg = _register_connection(
        {
            "fabric": params.get("fabric"),
            "connections": params.get("connections"),
            "connector": "ai.axiom.connector.slack",
            "name": f"slack-{channel}",
            "owner": owner,
            "secret_ref": secret_ref,
        },
        ctx=ctx,
    )
    if not reg.ok:
        return SkillResult(ok=False, errors=reg.errors, actions_taken=actions)
    value["connection"] = reg.value["connection"]
    value["next_steps"] = (
        "open install_url/create_url to grant the workspace; the bot then "
        "auto-joins the channel and the connection is health-verified to active"
    )
    return SkillResult(ok=True, value=value, actions_taken=actions + reg.actions_taken)


__all__ = ["build_app_manifest", "slack_install"]
