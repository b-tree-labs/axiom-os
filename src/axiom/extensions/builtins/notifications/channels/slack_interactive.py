# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Slack Socket-Mode provider for the vendor-neutral ``InteractiveChannel``
(ADR-074 Phase 2). Socket Mode = an outbound websocket, so no public
Request URL / inbound firewall hole — works behind a restricted network.

The Block-Kit build and event parsing are pure functions (tested without
slack_sdk). The live ``WebClient`` + ``SocketModeClient`` wiring is the only
part that needs the SDK and is imported lazily, so importing this module is
cheap and the workflow above it stays vendor-agnostic.
"""

from __future__ import annotations

from typing import Any

from .interactive import (
    ActionHandler,
    ApprovalOutcome,
    ApprovalRequest,
    ChannelMessage,
    MessageHandler,
)


def build_approval_blocks(request: ApprovalRequest) -> list[dict]:
    """Render an ApprovalRequest as Slack Block Kit (section + buttons)."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": request.prompt}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": o.action_id,
                    "text": {"type": "plain_text", "text": o.label},
                    **({"style": o.style} if o.style in ("primary", "danger") else {}),
                }
                for o in request.options
            ],
        },
    ]


def _strip_mentions(text: str) -> str:
    """Drop leading ``<@U…>`` bot mentions so the agent sees the bare ask."""
    import re

    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def parse_slack_event(event: dict) -> ChannelMessage | ApprovalOutcome | None:
    """Map a Slack event to a vendor-neutral type, or None if irrelevant."""
    etype = event.get("type")
    if etype in ("message", "app_mention"):
        # Skip Slack's own subtype churn (edits/joins); only real text.
        if event.get("subtype") not in (None, "thread_broadcast"):
            return None
        return ChannelMessage(
            # @mention text carries the <@BOT> token — strip it so the agent
            # sees just the question; plain messages pass through unchanged.
            text=_strip_mentions(event.get("text", "")),
            author=event.get("user") or event.get("bot_id") or "unknown",
            thread_id=event.get("thread_ts") or event.get("ts"),
            is_agent=bool(event.get("bot_id")),
        )
    if etype == "block_actions":
        actions = event.get("actions") or []
        if not actions:
            return None
        return ApprovalOutcome(
            action_id=actions[0].get("action_id", ""),
            actor=(event.get("user") or {}).get("id", "unknown"),
            thread_id=(event.get("container") or {}).get("thread_ts")
            or (event.get("message") or {}).get("ts"),
        )
    return None


class SlackInteractiveChannel:
    """Bidirectional Slack channel over Socket Mode. Implements
    ``InteractiveChannel``. Pass ``web_client``/``socket_client`` to inject
    fakes in tests; otherwise they're built lazily from slack_sdk."""

    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        channel: str,
        web_client: Any | None = None,
        socket_client: Any | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._channel = channel
        self._web = web_client
        self._socket = socket_client
        self._msg_handlers: list[MessageHandler] = []
        self._action_handlers: list[ActionHandler] = []

    def _web_client(self):
        if self._web is None:
            from slack_sdk import WebClient  # lazy: only when actually serving

            self._web = WebClient(token=self._bot_token)
        return self._web

    def post(
        self,
        text: str,
        *,
        thread_id: str | None = None,
        author: str = "agent",
        icon_url: str | None = None,
    ) -> str:
        # `author` is the speaking agent's identity name (AgentCard.name, e.g.
        # "TRIAGE"/"AXI") and `icon_url` its avatar (AgentCard.icon_url) — surface
        # them as the Slack message username + icon so the channel shows *which*
        # agent spoke, with a face, not the neutral connector presence. Both
        # require the `chat:write.customize` bot scope.
        kwargs: dict[str, Any] = {"channel": self._channel, "text": text, "thread_ts": thread_id}
        if author and author != "agent":
            kwargs["username"] = author
        if icon_url:
            kwargs["icon_url"] = icon_url
        resp = self._web_client().chat_postMessage(**kwargs)
        return resp["ts"] if isinstance(resp, dict) else getattr(resp, "data", {}).get("ts", thread_id or "")

    def request_approval(self, request: ApprovalRequest) -> str:
        resp = self._web_client().chat_postMessage(
            channel=self._channel,
            text=request.prompt,
            blocks=build_approval_blocks(request),
            thread_ts=request.thread_id,
        )
        return resp["ts"] if isinstance(resp, dict) else request.thread_id or ""

    def on_message(self, handler: MessageHandler) -> None:
        self._msg_handlers.append(handler)

    def on_action(self, handler: ActionHandler) -> None:
        self._action_handlers.append(handler)

    def dispatch(self, event: dict) -> None:
        """Route one raw Slack event to the registered handlers (the Socket
        Mode loop calls this; also the unit seam for event routing)."""
        parsed = parse_slack_event(event)
        if isinstance(parsed, ChannelMessage):
            for h in list(self._msg_handlers):
                h(parsed)
        elif isinstance(parsed, ApprovalOutcome):
            for h in list(self._action_handlers):
                h(parsed)

    def run(self) -> None:  # pragma: no cover - live websocket loop
        """Start the Socket Mode loop (blocks). Requires slack_sdk."""
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse

        if self._socket is None:
            self._socket = SocketModeClient(app_token=self._app_token, web_client=self._web_client())

        def _handle(client: SocketModeClient, req: SocketModeRequest) -> None:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            payload = req.payload or {}
            event = payload.get("event", payload)  # events_api nests under "event"
            self.dispatch(event)

        self._socket.socket_mode_request_listeners.append(_handle)
        self._socket.connect()
        import threading

        threading.Event().wait()


def make_slack_channel(*, env: dict) -> SlackInteractiveChannel:
    """Factory for the connector resolver (ADR-074 ``provider_entry``).

    Maps the Slack descriptor's declared env vars to the constructor. The
    resolver hands `env` with secrets already resolved from the keystore."""
    return SlackInteractiveChannel(
        bot_token=env["SLACK_BOT_TOKEN"],
        app_token=env["SLACK_APP_TOKEN"],
        channel=env["SLACK_CHANNEL"],
    )


__all__ = [
    "SlackInteractiveChannel",
    "build_approval_blocks",
    "parse_slack_event",
    "make_slack_channel",
]
