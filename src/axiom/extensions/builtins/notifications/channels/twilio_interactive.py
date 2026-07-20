# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Twilio SMS as a bidirectional ``InteractiveChannel`` (ADR-074, B3).

SMS proves the generalization: the same DT gate + control plane that run over
Slack run over a phone. SMS has no buttons and no Socket Mode — approvals
degrade to text ("reply YES / NO / a number"), and inbound replies arrive via a
webhook (the ``InboundReceiver`` seam) rather than a socket loop.

Outbound reuses the hardened ``TwilioSmsChannelAdapter`` (backoff + secret
redaction). ``parse_twilio_inbound`` is a pure function (testable without HTTP):
YES/NO → approval confirm/reject; anything else → a message (a bare number is
read by the gate as a measured value).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from .interactive import (
    ActionHandler,
    ApprovalOutcome,
    ApprovalRequest,
    ChannelMessage,
    MessageHandler,
)

_YES = {"yes", "y", "confirm", "approve", "ok", "ack"}
_NO = {"no", "n", "reject", "deny", "cancel", "stop"}


def parse_twilio_inbound(payload: dict) -> ChannelMessage | ApprovalOutcome | None:
    """Map a Twilio inbound webhook payload to a vendor-neutral type.

    YES/NO → ``ApprovalOutcome`` (confirm/reject); any other text → a
    ``ChannelMessage`` (the gate treats a bare number as the measured value).
    """
    body = (payload.get("Body") or "").strip()
    frm = payload.get("From") or "unknown"
    if not body:
        return None
    low = body.lower()
    if low in _YES:
        return ApprovalOutcome(action_id="confirm", actor=frm, thread_id=None)
    if low in _NO:
        return ApprovalOutcome(action_id="reject", actor=frm, thread_id=None)
    return ChannelMessage(text=body, author=frm, thread_id=None, is_agent=False)


def _approval_as_text(request: ApprovalRequest) -> str:
    # Render buttons as a text instruction (SMS has no interactive components).
    return (
        f"{request.prompt}\n"
        "Reply YES to confirm, NO to reject, or send the measured value (a number)."
    )


class TwilioInteractiveChannel:
    """Bidirectional SMS channel. Implements ``InteractiveChannel``.

    ``send`` is injectable ``(to, body) -> Any`` (tests pass a fake); the default
    wraps ``TwilioSmsChannelAdapter.deliver_sync``. Inbound is driven by
    ``dispatch(payload)`` from the webhook receiver — there is no ``run()`` loop.
    """

    def __init__(
        self,
        *,
        account_sid: str = "",
        auth_token: str = "",
        from_number: str = "",
        to_number: str,
        send: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._to = to_number
        self._send = send or self._default_send(account_sid, auth_token, from_number)
        self._msg_handlers: list[MessageHandler] = []
        self._action_handlers: list[ActionHandler] = []

    @staticmethod
    def _default_send(account_sid: str, auth_token: str, from_number: str):  # pragma: no cover - live HTTP
        from axiom.governance import Classification

        from .twilio_sms import TwilioSmsChannelAdapter

        adapter = TwilioSmsChannelAdapter(
            account_sid=account_sid, auth_token=auth_token, from_number=from_number
        )

        def _send(to: str, body: str):
            return adapter.deliver_sync(
                recipient=to, receipt_id=uuid.uuid4().hex[:12],
                classification=Classification.PUBLIC, priority="normal", summary=body,
            )

        return _send

    def post(self, text: str, *, thread_id: str | None = None, author: str = "agent",
             icon_url: str | None = None) -> str:
        # SMS carries author inline (no avatar); brief possessive prefix.
        body = f"{author}: {text}" if author and author != "agent" else text
        self._send(self._to, body)
        return thread_id or self._to  # SMS has no threads; the recipient is the "thread"

    def request_approval(self, request: ApprovalRequest) -> str:
        self._send(self._to, _approval_as_text(request))
        return request.thread_id or self._to

    def on_message(self, handler: MessageHandler) -> None:
        self._msg_handlers.append(handler)

    def on_action(self, handler: ActionHandler) -> None:
        self._action_handlers.append(handler)

    def dispatch(self, payload: dict) -> None:
        """Route one inbound Twilio webhook payload to handlers."""
        parsed = parse_twilio_inbound(payload)
        if isinstance(parsed, ChannelMessage):
            for h in list(self._msg_handlers):
                h(parsed)
        elif isinstance(parsed, ApprovalOutcome):
            for h in list(self._action_handlers):
                h(parsed)


def make_twilio_channel(*, env: dict) -> TwilioInteractiveChannel:
    """Factory for the connector resolver (ADR-074 ``provider_entry``)."""
    return TwilioInteractiveChannel(
        account_sid=env.get("TWILIO_ACCOUNT_SID", ""),
        auth_token=env.get("TWILIO_AUTH_TOKEN", ""),
        from_number=env.get("TWILIO_FROM", ""),
        to_number=env["TWILIO_TO"],
    )


__all__ = ["TwilioInteractiveChannel", "parse_twilio_inbound", "make_twilio_channel"]
