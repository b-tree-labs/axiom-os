# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axiom.extensions.builtins.connector`` — cross-cutting connector primitive.

Connectors are the substrate every extension that touches a vendor surface
rides on: HERALD outbound channels (Slack/Teams/Mattermost/Email/Twilio),
the upcoming Calendar primitive (Microsoft Graph / Google Calendar /
CalDAV), RAG-side artifact stores (Box, OneDrive, SharePoint), and any
future external vendor surface.

This extension owns the cross-cutting concerns:

- **Add** — interactive wizard (``axi connector add <vendor>``)
- **Status** — last-known outcome per connector (``axi connector status``)
- **Reconnect** — surface what needs operator attention
- **Observability** — structured ``connector.*`` events on the EventBus
- **Status store** — in-process + (future Postgres) backing for status

The actual per-vendor adapters live in their consumer extensions today
(``notifications/channels/slack.py``, etc.); a follow-up generalizes the
adapter Protocol so vendor implementations are owned by this extension
and consumed by HERALD / Calendar / RAG.
"""

from __future__ import annotations

from axiom.extensions.builtins.connector.detect import (
    ConnectorState,
    DetectResult,
    default_detect,
    detect_connector,
)
from axiom.extensions.builtins.connector.observability import (
    SUBJECT_DELIVERED,
    SUBJECT_FAILED,
    SUBJECT_RECONNECT_REQUIRED,
    ConnectorOutcome,
    publish_outcome,
)
from axiom.extensions.builtins.connector.status_store import (
    ConnectorStatusStore,
    InMemoryStatusStore,
    StatusStoreSubscriber,
    get_default_store,
    reset_default_store_for_testing,
)
from axiom.extensions.builtins.connector.tunnel import (
    CloudflaredProvider,
    TunnelHandle,
    TunnelProvider,
    TunnelUnavailable,
    open_tunnel,
)

__all__ = [
    "ConnectorState",
    "DetectResult",
    "default_detect",
    "detect_connector",
    "TunnelHandle",
    "TunnelProvider",
    "CloudflaredProvider",
    "TunnelUnavailable",
    "open_tunnel",
    "ConnectorOutcome",
    "ConnectorStatusStore",
    "InMemoryStatusStore",
    "SUBJECT_DELIVERED",
    "SUBJECT_FAILED",
    "SUBJECT_RECONNECT_REQUIRED",
    "StatusStoreSubscriber",
    "get_default_store",
    "publish_outcome",
    "reset_default_store_for_testing",
]
