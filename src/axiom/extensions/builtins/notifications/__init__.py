# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HERALD — multi-channel notification primitive.

Per ADR-055 + prd-axiom-notifications + spec-axiom-notifications: every
agent on the platform reaches recipients through HERALD. The send
façade routes through classification (§4 of spec) + the channel-adapter
registry (§2, §7) + the receipt write (§8).

SEC-1 ships:

- ``send()`` façade with classification-routing site
- ``inbox`` channel adapter (always available; ``CONTROLLED`` ceiling)
- ``ChannelAdapterRegistry`` (factory/provider; mirrors secrets PR #296)
- ``InboxStore`` protocol + in-memory implementation
- ``axi notifications {send|list|channels}`` CLI (skill-fn backed per ADR-056)

Real channel adapters (email-smtp, slack) ship as HERALD-2 packages
that register their provider at import time. See
``channels/base.py::ChannelAdapterRegistry``.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.notifications.channels import (
    ChannelAdapter,
    ChannelAdapterProvider,
    ChannelAdapterRegistry,
    ChannelCapabilities,
    Direction,
    InboxChannelAdapter,
    InboxChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.inbox import (
    InboxQuery,
    InboxRow,
    InboxStore,
    InMemoryInboxStore,
    list_unread,
    mark_read,
)
from axiom.extensions.builtins.notifications.send import (
    ChannelPreferences,
    DeliveryReceipt,
    NotificationPayload,
    Priority,
    SendContext,
    send,
)

herald_persona_path = str(
    Path(__file__).parent / "agents" / "herald" / "persona.md"
)


__all__ = [
    "ChannelAdapter",
    "ChannelAdapterProvider",
    "ChannelAdapterRegistry",
    "ChannelCapabilities",
    "ChannelPreferences",
    "DeliveryReceipt",
    "Direction",
    "InboxChannelAdapter",
    "InboxChannelAdapterProvider",
    "InboxQuery",
    "InboxRow",
    "InboxStore",
    "InMemoryInboxStore",
    "NotificationPayload",
    "Priority",
    "SendContext",
    "herald_persona_path",
    "list_unread",
    "mark_read",
    "send",
]
