# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Inbound payload decoding — vendor JSON → ``InboundEvent`` (ADR-067 PR-1).

The gateway normalizes every vendor's webhook body into one struct so the
downstream dispatch pipeline (TRIAGE classifier, reply-bind-back) never
sees vendor shapes. Per-vendor decoders register here; a permissive
generic decoder covers the scaffold + tests until the real Slack/Teams
decoders land (PR-4 / PR-8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class InboundEvent:
    """A normalized inbound message from any human channel.

    ``event_id`` is the vendor's idempotency key (Slack ``event_id``,
    Twilio ``MessageSid``, …) used for dedup. ``thread_ref`` carries the
    vendor-native correlation handle when present (reply-bind-back, PR-9).
    """

    vendor: str
    event_id: str
    text: str = ""
    sender_ref: str = ""
    thread_ref: str | None = None
    channel: str | None = None
    attachments: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "event_id": self.event_id,
            "text": self.text,
            "sender_ref": self.sender_ref,
            "thread_ref": self.thread_ref,
            "channel": self.channel,
            "attachments": list(self.attachments),
        }


class Decoder(Protocol):
    def decode(self, vendor: str, body: dict[str, Any]) -> InboundEvent: ...


class GenericDecoder:
    """Best-effort decode used by the scaffold and as a fallback.

    Real vendor decoders (Slack Events API envelope, Twilio form body,
    Graph change-notification) override this per vendor.
    """

    def decode(self, vendor: str, body: dict[str, Any]) -> InboundEvent:
        event_id = str(
            body.get("event_id")
            or body.get("MessageSid")
            or body.get("id")
            or ""
        )
        text = str(body.get("text") or body.get("Body") or "")
        sender_ref = str(body.get("user") or body.get("From") or body.get("sender") or "")
        thread_ref = body.get("thread_ts") or body.get("thread_ref") or None
        channel = body.get("channel") or body.get("To") or None
        return InboundEvent(
            vendor=vendor,
            event_id=event_id,
            text=text,
            sender_ref=sender_ref,
            thread_ref=str(thread_ref) if thread_ref is not None else None,
            channel=str(channel) if channel is not None else None,
            raw=body,
        )


class DecoderRegistry:
    def __init__(self, default: Decoder | None = None) -> None:
        self._by_vendor: dict[str, Decoder] = {}
        self._default: Decoder = default or GenericDecoder()

    def register(self, vendor: str, decoder: Decoder) -> None:
        self._by_vendor[vendor] = decoder

    def get(self, vendor: str) -> Decoder:
        return self._by_vendor.get(vendor, self._default)


__all__ = ["InboundEvent", "Decoder", "GenericDecoder", "DecoderRegistry"]
