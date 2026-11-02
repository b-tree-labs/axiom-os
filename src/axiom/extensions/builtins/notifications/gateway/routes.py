# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""HERALD Gateway router — the single inbound surface (ADR-067 PR-1).

One FastAPI route, ``POST /herald/inbound/{vendor}``, mounted on the
existing ``http`` extension factory (no separate server; the
``signals/serve.py`` BaseHTTPRequestHandler precedent is the anti-pattern
this avoids). The route does only steps 1-3 + emission of the 10-step
pipeline: verify signature → decode + dedup → publish
``herald.inbound.<vendor>`` on the bus. TRIAGE's classifier (PR-9)
subscribes and runs steps 4-10.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from fastapi import APIRouter, Request, Response

from axiom.extensions.builtins.notifications.gateway.decode import DecoderRegistry
from axiom.extensions.builtins.notifications.gateway.dedup import DedupCache
from axiom.extensions.builtins.notifications.gateway.verify import VerifierRegistry

_GATEWAY_SOURCE = "herald.gateway"


class _BusLike(Protocol):
    def publish(
        self, subject: str, payload: dict[str, Any] | None = ..., source: str = ...
    ) -> Any: ...


def build_gateway_router(
    *,
    bus: _BusLike,
    verifiers: VerifierRegistry,
    dedup: DedupCache | None = None,
    decoders: DecoderRegistry | None = None,
) -> APIRouter:
    """Build the inbound router.

    ``verifiers`` is authoritative: a vendor with no registered verifier
    is rejected (404), never silently accepted. ``dedup``/``decoders``
    default to the in-memory scaffold implementations.
    """
    router = APIRouter()
    _dedup = dedup or DedupCache()
    _decoders = decoders or DecoderRegistry()

    @router.post("/herald/inbound/{vendor}")
    async def inbound(vendor: str, request: Request) -> Response:
        verifier = verifiers.get(vendor)
        if verifier is None:
            return _json(404, {"status": "unknown_vendor", "vendor": vendor})

        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not verifier.verify(headers=headers, body=body):
            return _json(401, {"status": "bad_signature"})

        try:
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                data = {"value": data}
        except json.JSONDecodeError:
            return _json(400, {"status": "bad_payload"})

        # Slack URL-verification handshake: echo the challenge, never publish.
        if data.get("type") == "url_verification" and isinstance(
            data.get("challenge"), str
        ):
            return _json(200, {"challenge": data["challenge"]})

        decoder = _decoders.get(vendor)
        # Loop guard: drop self-authored / echo events (e.g. the bot's own
        # reply) before they re-enter the pipeline.
        ignore = getattr(decoder, "ignore", None)
        if callable(ignore) and ignore(vendor, data):
            return _json(200, {"status": "ignored"})

        event = decoder.decode(vendor, data)
        if _dedup.seen_or_add(vendor, event.event_id):
            return _json(200, {"status": "duplicate", "event_id": event.event_id})

        bus.publish(
            f"herald.inbound.{vendor}",
            payload=event.as_payload(),
            source=_GATEWAY_SOURCE,
        )
        return _json(202, {"status": "accepted", "event_id": event.event_id})

    return router


def mount_gateway(
    app: Any,
    *,
    bus: _BusLike,
    verifiers: VerifierRegistry,
    dedup: DedupCache | None = None,
    decoders: DecoderRegistry | None = None,
) -> None:
    """Mount the inbound router on an ``http`` extension FastAPI app.

    The serve path calls this after ``create_app()`` with the live bus +
    the per-vendor verifier registry (Slack signing secret, etc.).
    """
    app.include_router(
        build_gateway_router(
            bus=bus, verifiers=verifiers, dedup=dedup, decoders=decoders
        )
    )


def _json(status: int, body: dict[str, Any]) -> Response:
    return Response(
        content=json.dumps(body), status_code=status, media_type="application/json"
    )


__all__ = ["build_gateway_router", "mount_gateway"]
