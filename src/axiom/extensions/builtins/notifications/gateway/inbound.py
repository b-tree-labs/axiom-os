# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Inbound webhook receiver — route a vendor webhook to a channel (ADR-074, B3).

Socket-Mode channels (Slack) get inbound for free over their websocket. Request/
response channels (SMS, email reply-ingest) need a webhook endpoint. This is the
transport-agnostic seam: a thin HTTP entry validates the payload signature and
hands the raw payload to the registered channel's ``dispatch``. The
framework-specific glue (Flask/FastAPI route) stays at the edge; this owns the
reusable validate→route step every non-socket channel shares.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

# verify(payload, signature, headers) -> bool
Verifier = Callable[[dict, str | None, dict | None], bool]


class _Dispatchable(Protocol):
    def dispatch(self, payload: dict) -> None: ...


class InboundReceiver:
    """Registry of ``route -> (channel, verifier)``. The HTTP edge calls
    ``handle(route, payload, signature=…)``; we validate then dispatch."""

    def __init__(self) -> None:
        self._routes: dict[str, tuple[_Dispatchable, Verifier | None]] = {}

    def register(self, route: str, channel: _Dispatchable, *, verify: Verifier | None = None) -> None:
        self._routes[route] = (channel, verify)

    def routes(self) -> list[str]:
        return sorted(self._routes)

    def handle(
        self,
        route: str,
        payload: dict,
        *,
        signature: str | None = None,
        headers: dict | None = None,
    ) -> bool:
        """Validate + dispatch one inbound webhook. Returns False for an unknown
        route; raises ``PermissionError`` on signature failure."""
        entry = self._routes.get(route)
        if entry is None:
            return False
        channel, verify = entry
        if verify is not None and not verify(payload, signature, headers):
            raise PermissionError(f"inbound signature rejected for route {route!r}")
        channel.dispatch(payload)
        return True


__all__ = ["InboundReceiver", "Verifier"]
