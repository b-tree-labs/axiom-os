# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``inbox`` channel adapter — the always-available baseline.

Per spec-axiom-notifications §9: ``inbox`` is the only channel adapter
that ships in SEC-1. Its ceiling is ``CONTROLLED`` (i.e. it admits every
classification within the recipient's tier). Every send fall-through
routes here when no higher-fidelity channel is admitted.

Real-vendor adapters (slack, email-smtp) land in HERALD-2 as external
packages that register their provider at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.extensions.builtins.notifications.sender import SenderIdentity

from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelCapabilities,
    Direction,
)
from axiom.extensions.builtins.notifications.inbox import (
    InboxStore,
    InMemoryInboxStore,
)
from axiom.governance import Classification


@dataclass
class InboxDispatchResult:
    ok: bool
    row_id: str | None = None
    error: str | None = None


class InboxChannelAdapter:
    """Runtime adapter — writes through to the configured ``InboxStore``."""

    name = "inbox"

    def __init__(self, store: InboxStore) -> None:
        self._store = store

    def deliver_sync(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
        sender: SenderIdentity | None = None,
    ) -> InboxDispatchResult:
        try:
            row_id = self._store.write(
                recipient=recipient,
                receipt_id=receipt_id,
                classification=classification,
                priority=priority,
                summary=summary,
            )
            return InboxDispatchResult(ok=True, row_id=row_id)
        except Exception as exc:  # noqa: BLE001 — adapter boundary
            return InboxDispatchResult(ok=False, error=str(exc))


class InboxChannelAdapterProvider:
    """Factory; mirrors the secrets-extension SecretBackendProvider shape."""

    name = "inbox"

    def __init__(self, store: InboxStore | None = None) -> None:
        self._store = store

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="inbox",
            direction=Direction.BIDIRECTIONAL,
            priority_levels=("low", "normal", "high", "urgent"),
            classification_ceiling=Classification.CONTROLLED,
            supports_threading=True,
            supports_acknowledge=True,
            delivery_sla_p95_ms=200,
        )

    def build(self, config: dict[str, Any] | None = None) -> InboxChannelAdapter:
        store = (config or {}).get("store") or self._store or InMemoryInboxStore()
        return InboxChannelAdapter(store=store)


__all__ = [
    "InboxChannelAdapter",
    "InboxChannelAdapterProvider",
    "InboxDispatchResult",
]
