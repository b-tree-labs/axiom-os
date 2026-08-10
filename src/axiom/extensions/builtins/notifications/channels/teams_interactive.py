# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Microsoft Teams (Bot Framework) provider for the vendor-neutral
``InteractiveChannel`` (ADR-074 Phase 2, HERALD-2b).

The Slack provider uses Socket Mode — an outbound websocket, so it owns a
blocking ``run()`` loop. Teams is the opposite shape: Azure Bot Service
*pushes* every inbound Activity to a public messaging endpoint (the
``/herald/inbound/teams-bot`` route), and the bot *posts* back proactively
through the Bot Connector REST API. So there is **no** ``run()`` loop here;
inbound arrives via :meth:`TeamsInteractiveChannel.dispatch` (the same seam
``SlackInteractiveChannel.dispatch`` exposes), and outbound is a thin REST
``POST``.

Mirrors ``slack_interactive.py`` structurally:

- ``parse_teams_activity``  ↔ ``parse_slack_event``   (pure, SDK-free)
- ``build_approval_card``   ↔ ``build_approval_blocks``
- ``TeamsInteractiveChannel`` ↔ ``SlackInteractiveChannel``
- ``make_teams_channel``    ↔ ``make_slack_channel``

Everything Microsoft-specific (the Connector REST client, the OAuth token
acquisition) is injected behind seams — an ``_HttpPoster`` (matching
``channels/teams.py``) and a ``token_provider`` callable — so the module
imports without ``botbuilder`` and the whole thing tests offline.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol

from .interactive import (
    ActionHandler,
    ApprovalOutcome,
    ApprovalRequest,
    ChannelMessage,
    MessageHandler,
)

# ---------------------------------------------------------------------------
# HTTP + token seams (injected; the module never imports an SDK at top level)
# ---------------------------------------------------------------------------


class _HttpPoster(Protocol):
    def post(self, url: str, json: dict, headers: dict, timeout: float): ...


def _default_poster() -> _HttpPoster:
    import httpx

    return httpx.Client(follow_redirects=False)


# token_provider() -> a bearer token for the Bot Connector API.
TokenProvider = Callable[[], str]


def _default_token_provider(
    *, app_id: str, app_password: str, tenant_id: str | None = None
) -> TokenProvider:
    """Client-credentials token for the Bot Connector API (lazy httpx).

    Single-tenant + multi-tenant bots both mint Connector tokens against the
    ``botframework.com`` authority with the ``api.botframework.com`` scope;
    ``tenant_id`` is carried for future single-tenant-authority use and does
    not change the default flow. Never called in tests — a fake provider is
    injected — so the httpx import stays lazy.
    """

    def _acquire() -> str:
        import httpx

        resp = httpx.post(
            "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_password,
                "scope": "https://api.botframework.com/.default",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    return _acquire


# ---------------------------------------------------------------------------
# Adaptive Card build — the approval affordance (mirrors build_approval_blocks)
# ---------------------------------------------------------------------------

_ACTION_STYLE = {"primary": "positive", "danger": "destructive"}


def build_approval_card(request: ApprovalRequest) -> dict:
    """Render an ``ApprovalRequest`` as a Teams message with an Adaptive Card.

    Buttons are ``Action.Submit`` carrying ``{"action_id": ...}`` in their
    ``data`` — so the submit round-trips the stable id the action handler
    matches on (the Teams analogue of Slack's ``action_id``). Card style
    reuses the envelope shape from ``channels/teams.py``.
    """
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": request.prompt,
                            "wrap": True,
                            "weight": "bolder",
                        }
                    ],
                    "actions": [
                        {
                            "type": "Action.Submit",
                            "title": o.label,
                            "style": _ACTION_STYLE.get(o.style, "default"),
                            "data": {"action_id": o.action_id},
                        }
                        for o in request.options
                    ],
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Activity parsing — Bot Framework Activity → vendor-neutral (pure function)
# ---------------------------------------------------------------------------

# Teams renders an @mention of the bot as ``<at>Display Name</at>`` in the
# message text (with a matching entities[] entry). Strip it so the agent sees
# the bare ask — the Teams analogue of Slack's ``<@U…>`` stripping.
_AT_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)


def _strip_mentions(text: str) -> str:
    return _AT_RE.sub("", text or "").strip()


def _actor(activity: dict) -> str:
    frm = activity.get("from") or {}
    if isinstance(frm, dict):
        return str(frm.get("id") or frm.get("name") or "unknown")
    return "unknown"


def _is_agent(activity: dict) -> bool:
    """True when the Activity originated from a bot (loop-guard signal)."""
    frm = activity.get("from") or {}
    return isinstance(frm, dict) and frm.get("role") == "bot"


def _thread_id(activity: dict) -> str | None:
    """The postable conversation handle for an in-thread reply.

    A Teams channel thread is addressed by ``conversation.id``, which for a
    reply already encodes the root message (``…;messageid=<root>``). When the
    Activity carries a bare conversation id plus a ``replyToId`` we splice the
    ``messageid`` on so the outbound post lands in the same thread.
    """
    conv = (activity.get("conversation") or {}).get("id")
    reply_to = activity.get("replyToId")
    if conv and reply_to and "messageid=" not in str(conv):
        return f"{conv};messageid={reply_to}"
    return conv or reply_to


def _card_action_id(activity: dict) -> str | None:
    """Extract an ``action_id`` from an Adaptive-Card submit/invoke Activity.

    Action.Submit surfaces as a ``message`` Activity with the card ``data``
    under ``activity['value']``; Action.Execute surfaces as an ``invoke``
    Activity (``value.action.data``). Cover both shapes.
    """
    value = activity.get("value")
    if not isinstance(value, dict):
        return None
    # invoke (adaptiveCard/action): the payload nests under value.action.data
    action = value.get("action")
    if isinstance(action, dict):
        data = action.get("data")
        if isinstance(data, dict) and data.get("action_id"):
            return str(data["action_id"])
    # Action.Submit on a message Activity: data merged into value directly,
    # or under value.data.
    data = value.get("data")
    if isinstance(data, dict) and data.get("action_id"):
        return str(data["action_id"])
    if value.get("action_id"):
        return str(value["action_id"])
    return None


def parse_teams_activity(activity: dict) -> ChannelMessage | ApprovalOutcome | None:
    """Map a Bot Framework Activity to a vendor-neutral type, or None.

    - ``invoke`` (Adaptive-Card action) → ``ApprovalOutcome``
    - ``message`` carrying a card submit ``value`` → ``ApprovalOutcome``
    - ``message`` with text → ``ChannelMessage`` (bot @mention stripped)
    - anything else (conversationUpdate, typing, …) → None
    """
    atype = activity.get("type")
    if atype == "invoke":
        action_id = _card_action_id(activity)
        if action_id is None:
            return None
        return ApprovalOutcome(
            action_id=action_id, actor=_actor(activity), thread_id=_thread_id(activity)
        )
    if atype == "message":
        # A card submit arrives as a message with a `value` payload and
        # (usually) no text — treat it as an approval outcome, not a chat turn.
        action_id = _card_action_id(activity)
        if action_id is not None:
            return ApprovalOutcome(
                action_id=action_id,
                actor=_actor(activity),
                thread_id=_thread_id(activity),
            )
        return ChannelMessage(
            text=_strip_mentions(activity.get("text", "")),
            author=_actor(activity),
            thread_id=_thread_id(activity),
            is_agent=_is_agent(activity),
        )
    return None


# ---------------------------------------------------------------------------
# The channel — implements InteractiveChannel over the Bot Connector REST API
# ---------------------------------------------------------------------------


class TeamsInteractiveChannel:
    """Bidirectional Teams channel over the Bot Framework Connector API.

    Implements ``InteractiveChannel``. Outbound is a proactive REST ``POST``
    to ``{service_url}/v3/conversations/{conversation}/activities``; inbound
    is pushed to the messaging endpoint and routed here via :meth:`dispatch`
    (there is no Socket-Mode-style ``run()`` loop — Teams is push).

    Pass ``poster`` / ``token_provider`` to inject fakes in tests; otherwise
    they're built lazily from httpx + the Connector OAuth flow.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_password: str,
        service_url: str,
        conversation_id: str | None = None,
        tenant_id: str | None = None,
        poster: _HttpPoster | None = None,
        token_provider: TokenProvider | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._app_id = app_id
        self._app_password = app_password
        # Normalize: the Connector URL is `{service_url}/v3/...`; drop any
        # trailing slash so the join is clean.
        self._service_url = service_url.rstrip("/")
        self._conversation_id = conversation_id
        self._tenant_id = tenant_id
        self._poster = poster
        self._token_provider = token_provider
        self._timeout = timeout
        self._msg_handlers: list[MessageHandler] = []
        self._action_handlers: list[ActionHandler] = []

    # -- lazy SDK-free wiring ------------------------------------------------
    def _http(self) -> _HttpPoster:
        if self._poster is None:
            self._poster = _default_poster()
        return self._poster

    def _token(self) -> str:
        if self._token_provider is None:
            self._token_provider = _default_token_provider(
                app_id=self._app_id,
                app_password=self._app_password,
                tenant_id=self._tenant_id,
            )
        return self._token_provider()

    def _post_activity(self, conversation: str, activity: dict) -> str:
        url = f"{self._service_url}/v3/conversations/{conversation}/activities"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }
        resp = self._http().post(
            url, json=activity, headers=headers, timeout=self._timeout
        )
        # Connector returns {"id": "<activityId>"}; be tolerant of stubs.
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("id"):
                return str(data["id"])
        except Exception:  # noqa: BLE001 - id is best-effort telemetry
            pass
        return conversation

    # -- InteractiveChannel --------------------------------------------------
    def post(
        self,
        text: str,
        *,
        thread_id: str | None = None,
        author: str = "agent",
        icon_url: str | None = None,
    ) -> str:
        """Post a proactive message to the channel/thread.

        A Bot Framework bot always speaks under its own registered identity —
        there is no per-message ``username``/``icon`` override like Slack's
        ``chat:write.customize``. So the speaking agent's identity (``author``,
        e.g. "AXI") is surfaced as a bold lead line in the message body,
        keeping "which agent spoke" visible. ``icon_url`` is accepted for
        protocol parity and currently unused on this surface.
        """
        conversation = thread_id or self._conversation_id
        if not conversation:
            raise ValueError(
                "TeamsInteractiveChannel.post needs a thread_id or a "
                "conversation_id configured on the channel"
            )
        body = text
        if author and author != "agent":
            body = f"**{author}**\n\n{text}"
        return self._post_activity(conversation, {"type": "message", "text": body})

    def request_approval(self, request: ApprovalRequest) -> str:
        conversation = request.thread_id or self._conversation_id
        if not conversation:
            raise ValueError(
                "TeamsInteractiveChannel.request_approval needs a thread_id or "
                "a conversation_id configured on the channel"
            )
        return self._post_activity(conversation, build_approval_card(request))

    def on_message(self, handler: MessageHandler) -> None:
        self._msg_handlers.append(handler)

    def on_action(self, handler: ActionHandler) -> None:
        self._action_handlers.append(handler)

    def dispatch(self, activity: dict) -> None:
        """Route one raw Bot Framework Activity to the registered handlers.

        The messaging endpoint (``/herald/inbound/teams-bot``) calls this
        after JWT verification; it is also the unit seam for event routing —
        exactly like ``SlackInteractiveChannel.dispatch``.
        """
        parsed = parse_teams_activity(activity)
        if isinstance(parsed, ChannelMessage):
            for h in list(self._msg_handlers):
                h(parsed)
        elif isinstance(parsed, ApprovalOutcome):
            for h in list(self._action_handlers):
                h(parsed)


def make_teams_channel(*, env: dict) -> TeamsInteractiveChannel:
    """Factory for the connector resolver (ADR-074 ``provider_entry``).

    Maps the Teams descriptor's declared env vars to the constructor. The
    resolver hands ``env`` with secrets already resolved from the keystore.
    """
    return TeamsInteractiveChannel(
        app_id=env["TEAMS_BOT_APP_ID"],
        app_password=env["TEAMS_BOT_APP_PASSWORD"],
        tenant_id=env.get("TEAMS_TENANT_ID"),
        service_url=env["TEAMS_SERVICE_URL"],
        conversation_id=env.get("TEAMS_CHANNEL_ID"),
    )


__all__ = [
    "TeamsInteractiveChannel",
    "build_approval_card",
    "parse_teams_activity",
    "make_teams_channel",
]
