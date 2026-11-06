# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""HERALD Gateway — the unified inbound surface (ADR-067).

Turns one verified vendor webhook into one ``herald.inbound.<vendor>``
bus event. Mounted on the ``http`` extension's FastAPI factory; per-vendor
verifiers + decoders register here, dedup guards idempotency.
"""

from __future__ import annotations

from axiom.extensions.builtins.notifications.gateway.classify import (
    Decision,
    classify_inbound,
)
from axiom.extensions.builtins.notifications.gateway.conversation import (
    ChatResponder,
    attach_chat_agent,
    default_chat_responder,
)
from axiom.extensions.builtins.notifications.gateway.decode import (
    DecoderRegistry,
    GenericDecoder,
    InboundEvent,
)
from axiom.extensions.builtins.notifications.gateway.dedup import DedupCache
from axiom.extensions.builtins.notifications.gateway.routes import (
    build_gateway_router,
    mount_gateway,
)
from axiom.extensions.builtins.notifications.gateway.slack import (
    SlackDecoder,
    SlackSigningVerifier,
    is_url_verification,
)
from axiom.extensions.builtins.notifications.gateway.teams_bot import (
    BotFrameworkJwtVerifier,
    TeamsJwtVerifier,
    build_teams_bot_router,
    mount_teams_bot,
)
from axiom.extensions.builtins.notifications.gateway.threads import (
    ThreadStore,
    embed_footer,
    mint_correlation_id,
    parse_token,
)
from axiom.extensions.builtins.notifications.gateway.triage import register_triage
from axiom.extensions.builtins.notifications.gateway.verify import (
    AllowAllVerifier,
    HmacSha256Verifier,
    VerifierRegistry,
    WebhookVerifier,
)

__all__ = [
    "InboundEvent",
    "GenericDecoder",
    "DecoderRegistry",
    "DedupCache",
    "build_gateway_router",
    "mount_gateway",
    "WebhookVerifier",
    "AllowAllVerifier",
    "HmacSha256Verifier",
    "VerifierRegistry",
    "SlackSigningVerifier",
    "SlackDecoder",
    "is_url_verification",
    "Decision",
    "classify_inbound",
    "ThreadStore",
    "embed_footer",
    "mint_correlation_id",
    "parse_token",
    "register_triage",
    "ChatResponder",
    "attach_chat_agent",
    "default_chat_responder",
    "TeamsJwtVerifier",
    "BotFrameworkJwtVerifier",
    "build_teams_bot_router",
    "mount_teams_bot",
]
