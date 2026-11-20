# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Bot Framework messaging endpoint — the Teams inbound surface (ADR-067,
HERALD-2b).

Slack (Socket Mode) receives inbound over an outbound websocket, so it needs
no public route. Teams is push-to-endpoint: Azure Bot Service POSTs every
Activity to a messaging endpoint, signed with a JWT bearer token minted by
the Bot Connector. This module is that endpoint — modelled on
``gateway/routes.py`` (one FastAPI ``POST`` mounted on the ``http`` extension
app) but instead of publishing on the bus it (a) verifies the JWT, (b) parses
the Activity, and (c) routes it through ``TeamsInteractiveChannel.dispatch``.

Fail-closed: a missing or invalid JWT is rejected (401); the channel is never
touched. The JWT verification is behind an injectable ``TeamsJwtVerifier``
seam so tests pass a fake (accept/reject) with no network and no ``PyJWT``
import. The concrete :class:`BotFrameworkJwtVerifier` lazy-imports ``jwt`` +
fetches the Connector OpenID signing keys only when actually serving.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, Request, Response

# The Bot Connector's stable OpenID metadata + token issuer (public, not a
# secret). Emitter of the channel→bot JWTs Azure Bot Service signs.
_OPENID_METADATA = "https://login.botframework.com/v1/.well-known/openidconfiguration"
_BOTFRAMEWORK_ISSUER = "https://api.botframework.com"


@runtime_checkable
class TeamsJwtVerifier(Protocol):
    """Verify the inbound Bot Framework JWT. Returns True iff trusted.

    ``headers`` is the lower-cased request header map (carries
    ``authorization: Bearer <jwt>``); ``body`` is the raw request body (some
    verifiers cross-check the ``serviceUrl`` claim against it). Mirrors the
    ``gateway/verify.py`` ``WebhookVerifier`` shape so it slots into the same
    fail-closed pattern.
    """

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool: ...


def _bearer(headers: Mapping[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


class BotFrameworkJwtVerifier:
    """Concrete verifier: validate the Azure Bot Service JWT.

    Checks signature (RS256 against the Connector's published JWKS), issuer
    (``https://api.botframework.com``) and audience (== the bot's App ID).
    Everything network/crypto is lazy so importing this module is cheap and
    offline; tests inject a fake instead.
    """

    def __init__(
        self,
        *,
        app_id: str,
        openid_metadata_url: str = _OPENID_METADATA,
        issuer: str = _BOTFRAMEWORK_ISSUER,
        jwk_client: Any | None = None,
    ) -> None:
        self._app_id = app_id
        self._openid_metadata_url = openid_metadata_url
        self._issuer = issuer
        self._jwk_client = jwk_client  # injectable PyJWKClient-like for tests

    def _jwks_uri(self) -> str:
        import httpx

        meta = httpx.get(self._openid_metadata_url, timeout=10.0)
        meta.raise_for_status()
        return meta.json()["jwks_uri"]

    def _client(self):
        if self._jwk_client is None:
            from jwt import PyJWKClient

            self._jwk_client = PyJWKClient(self._jwks_uri())
        return self._jwk_client

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        token = _bearer(headers)
        if not token:
            return False
        try:
            import jwt

            signing_key = self._client().get_signing_key_from_jwt(token)
            jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._app_id,
                issuer=self._issuer,
            )
            return True
        except Exception:  # noqa: BLE001 — any failure ⇒ untrusted ⇒ fail-closed
            return False


class _ChannelLike(Protocol):
    def dispatch(self, activity: dict) -> None: ...


def build_teams_bot_router(
    *,
    channel: _ChannelLike,
    verifier: TeamsJwtVerifier,
) -> APIRouter:
    """Build the Teams messaging-endpoint router.

    ``verifier`` is authoritative and fail-closed: a request whose JWT is
    absent or invalid is rejected (401) and the channel is never touched.
    """
    router = APIRouter()

    @router.post("/herald/inbound/teams-bot")
    async def teams_bot(request: Request) -> Response:
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not verifier.verify(headers=headers, body=body):
            return _json(401, {"status": "bad_jwt"})
        try:
            activity = json.loads(body or b"{}")
            if not isinstance(activity, dict):
                return _json(400, {"status": "bad_payload"})
        except json.JSONDecodeError:
            return _json(400, {"status": "bad_payload"})
        channel.dispatch(activity)
        return _json(200, {"status": "accepted"})

    return router


def mount_teams_bot(
    app: Any,
    *,
    channel: _ChannelLike,
    verifier: TeamsJwtVerifier,
) -> None:
    """Mount the Teams messaging endpoint on an ``http`` extension app.

    The serve path calls this after ``create_app()`` with the live
    ``TeamsInteractiveChannel`` (wired to its conversation handler) and the
    ``BotFrameworkJwtVerifier`` for the bot's App ID.
    """
    app.include_router(build_teams_bot_router(channel=channel, verifier=verifier))


def _json(status: int, body: dict[str, Any]) -> Response:
    return Response(
        content=json.dumps(body), status_code=status, media_type="application/json"
    )


__all__ = [
    "TeamsJwtVerifier",
    "BotFrameworkJwtVerifier",
    "build_teams_bot_router",
    "mount_teams_bot",
]
