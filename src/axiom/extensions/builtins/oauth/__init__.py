# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``oauth`` — Axiom's first-party OAuth 2.1 AS + OIDC provider + MCP Resource Server.

Per ADR-082, Axiom issues its own audience-bound tokens (ES256, ADR-085) so a web
app, a mobile app, and agents/MCP clients all authenticate on one standards
surface, while GUARD (ADR-055) stays the authorization decision point. This is
distinct from the ``auth`` relying-party (ADR-075), which federates *external*
IdPs upstream.

This cut ships the public discovery + JWKS surface. The ``/authorize`` and
``/token`` endpoints, resource-server enforcement (the first production
``AuthzHook``), and RFC 8693 delegation follow in later cuts (build P2–P4).
"""

from __future__ import annotations

from .api.routers import (
    build_oauth_endpoints_router,
    build_oauth_router,
    set_subject_resolver,
)
from .clients import (
    ClientRegistry,
    InMemoryClientRegistry,
    get_client_registry,
    set_client_registry,
)
from .codes import (
    AuthorizationCodeStore,
    InMemoryAuthorizationCodeStore,
    get_authorization_code_store,
    set_authorization_code_store,
)
from .errors import OAuthError
from .metadata import authorization_server_metadata, openid_configuration
from .models import OAuthClient
from .mount import endpoints_mount_spec, mount_spec
from .refresh import (
    InMemoryRefreshTokenStore,
    RefreshTokenStore,
    get_refresh_token_store,
    set_refresh_token_store,
)

__version__ = "0.1.0"

__all__ = [
    "AuthorizationCodeStore",
    "ClientRegistry",
    "InMemoryAuthorizationCodeStore",
    "InMemoryClientRegistry",
    "InMemoryRefreshTokenStore",
    "OAuthClient",
    "OAuthError",
    "RefreshTokenStore",
    "authorization_server_metadata",
    "build_oauth_endpoints_router",
    "build_oauth_router",
    "endpoints_mount_spec",
    "get_authorization_code_store",
    "get_client_registry",
    "get_refresh_token_store",
    "mount_spec",
    "openid_configuration",
    "set_authorization_code_store",
    "set_client_registry",
    "set_refresh_token_store",
    "set_subject_resolver",
]
