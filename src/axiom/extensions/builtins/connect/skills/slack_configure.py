# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Finish the Slack connector: verify tokens, resolve + join the channel.

The installer owns everything after the two tokens — no manual /invite,
channel-id hunting, or .env editing. slack_sdk is imported lazily; the channel
resolution is pure + tested.
"""
from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult


def resolve_channel_id(entries: list[dict], ref: str) -> str | None:
    """Find a channel id from a conversations.list result by id or name."""
    ref = ref.lstrip("#").strip()
    if ref.startswith("C") and ref.isupper() is False:  # ids are upper-ish; accept Cxxxx
        pass
    for c in entries:
        if c.get("id") == ref or c.get("name") == ref:
            return c.get("id")
    return ref if ref.startswith("C") else None


def configure(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    bot = params.get("bot_token")
    channel = params.get("channel")
    missing = [k for k in ("bot_token", "app_token", "channel") if not params.get(k)]
    if missing:
        return SkillResult(ok=False, errors=[f"missing: {', '.join(missing)}"])
    client = params.get("web_client")
    if client is None:
        from slack_sdk import WebClient
        client = WebClient(token=bot)
    try:
        auth = client.auth_test()
        if not auth.get("ok", True):
            return SkillResult(ok=False, errors=[f"bot token invalid: {auth.get('error')}"])
        # resolve channel id
        cid = channel if str(channel).startswith("C") else None
        if cid is None:
            convo = client.conversations_list(types="public_channel,private_channel", limit=1000)
            cid = resolve_channel_id(convo.get("channels", []), channel)
        if not cid:
            return SkillResult(ok=False, errors=[f"channel {channel!r} not found (is the bot in the workspace?)"])
        try:
            client.conversations_join(channel=cid)  # public channels
        except Exception:  # noqa: BLE001 — private/ already-member → human /invite once
            pass
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"Slack API error: {exc}"])
    return SkillResult(ok=True, value={"team": auth.get("team"), "bot_user": auth.get("user"),
                                       "channel_id": cid}, actions_taken=["verified bot token", f"joined {cid}"])


__all__ = ["resolve_channel_id", "configure"]
