# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The public sign-in client a site node publishes for local development.

A contributor's local node that is bound to this site asks
``GET /api/v1/site/sign-in`` with their developer key and gets back the one
thing it needs to offer the site's single sign-on: the issuer and a **public**
client id (authorization code + PKCE, no secret). Nothing else is ever in the
answer: an allow-list of four fields, built here, never a filtered copy of the
node's configuration.

Off unless the node's configuration says so::

    AXIOM_GATE_OIDC_DEV_CLIENT_ID=<the public client's id>
    AXIOM_GATE_OIDC_DEV_ISSUER=<https issuer>     # optional: default is the
                                                  # node's own sign-in issuer
    AXIOM_GATE_OIDC_DEV_CLIENT_PUBLIC_SAFE=1      # only when the id is the same
                                                  # app the node signs in with

A client id is not a secret, but the id of an app that also holds a secret (the
node's own confidential client) is not published unless the configuration
marks that app public-safe: its registration must carry a public-client
platform with the loopback redirects, and somebody has to have decided so.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from .oidc import LOOPBACK_REDIRECTS

_LOG = logging.getLogger("axiom.webgate.dev_client")

ENV_CLIENT_ID = "AXIOM_GATE_OIDC_DEV_CLIENT_ID"
ENV_ISSUER = "AXIOM_GATE_OIDC_DEV_ISSUER"
ENV_PUBLIC_SAFE = "AXIOM_GATE_OIDC_DEV_CLIENT_PUBLIC_SAFE"

#: The only fields the endpoint ever returns.
FIELDS = ("issuer_url", "client_id", "client_type", "allowed_redirects")

_PRIMARY = "AXIOM_GATE_OIDC_"
_SECRET_KEYS = ("CLIENT_SECRET", "CLIENT_SECRET_FILE", "CLIENT_SECRET_VAULT")
_TRUE = {"1", "true", "yes", "on"}


def _primary_issuer(env: Mapping[str, str]) -> str:
    """The node's own sign-in issuer, from its primary block, without a network call."""
    provider = (env.get(_PRIMARY + "PROVIDER") or "").strip()
    if provider.lower() == "entra":
        tenant = (env.get(_PRIMARY + "TENANT") or "").strip()
        return f"https://login.microsoftonline.com/{tenant}/v2.0" if tenant else ""
    if provider.lower() == "google":
        return "https://accounts.google.com"
    return provider if provider.startswith("https://") else ""


def published(env: Mapping[str, str] | None = None) -> dict | None:
    """What the endpoint answers, or ``None`` when this node publishes nothing."""
    e = os.environ if env is None else env
    client_id = (e.get(ENV_CLIENT_ID) or "").strip()
    if not client_id:
        return None
    issuer = (e.get(ENV_ISSUER) or "").strip() or _primary_issuer(e)
    parts = urlsplit(issuer)
    if parts.scheme != "https" or not parts.hostname:
        _LOG.warning("%s is set but no https issuer is configured; publishing nothing", ENV_CLIENT_ID)
        return None
    same_app = client_id == (e.get(_PRIMARY + "CLIENT_ID") or "").strip()
    has_secret = any((e.get(_PRIMARY + k) or "").strip() for k in _SECRET_KEYS)
    if same_app and has_secret and (e.get(ENV_PUBLIC_SAFE) or "").strip().lower() not in _TRUE:
        _LOG.warning(
            "%s names the node's confidential client; set %s=1 once its registration "
            "has a public-client platform with the loopback redirects; publishing nothing",
            ENV_CLIENT_ID,
            ENV_PUBLIC_SAFE,
        )
        return None
    return {
        "issuer_url": issuer,
        "client_id": client_id,
        "client_type": "public",
        "allowed_redirects": list(LOOPBACK_REDIRECTS),
    }


def register_route(router) -> None:
    """``GET /site/sign-in`` on the ``/api/v1`` router (a read: ``*:read``)."""
    from fastapi.responses import JSONResponse

    @router.get("/site/sign-in", tags=["system"])
    def site_sign_in() -> JSONResponse:
        """This site's public sign-in client for local development, or 404."""
        body = published()
        if body is None:
            return JSONResponse(
                {"detail": "this site has not enabled SSO for local development"},
                status_code=404,
            )
        return JSONResponse({k: body[k] for k in FIELDS})


__all__ = [
    "ENV_CLIENT_ID",
    "ENV_ISSUER",
    "ENV_PUBLIC_SAFE",
    "FIELDS",
    "published",
    "register_route",
]
