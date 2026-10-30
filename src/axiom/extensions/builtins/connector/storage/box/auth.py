# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which auth flow a credential selects. Provider-internal (ADR-220 §5).

A connector names its credential by reference (``credential_ref``, or the
historical ``params.jwt_secret_ref``); the blob behind it decides the flow, most
specific first: an OAuth refresh-token blob, then client credentials (CCG), then
a JWT keypair config. Every flow is duck-typed to ``authorization_header()``.
"""

from __future__ import annotations

import json
from typing import Any

#: The auth modes this provider supports (``developer-token`` and the browser
#: session are read by :class:`~.session_api.BoxSessionApiClient` itself).
AUTH_MODES = ("oauth", "ccg", "jwt", "developer-token", "browser-session")


def auth_from_blob(blob: dict[str, Any]):
    """The auth provider a credential blob selects."""
    from .oauth_auth import BoxOAuthAuth, BoxOAuthConfig

    if BoxOAuthConfig.is_oauth_blob(blob):
        return BoxOAuthAuth(BoxOAuthConfig.from_dict(blob))

    from .ccg_auth import BoxCcgAuth, BoxCcgConfig

    if BoxCcgConfig.is_ccg_blob(blob):
        return BoxCcgAuth(BoxCcgConfig.from_dict(blob))

    from .jwt_auth import BoxJwtAuth, BoxJwtConfig

    return BoxJwtAuth(BoxJwtConfig.from_dict(blob))


def auth_from_ref(ref_url: str | None):
    """The auth provider behind a SecretRef URL, or ``None`` when none is configured."""
    if not ref_url:
        return None
    from axiom.extensions.builtins.secrets import SecretRef, resolve

    with resolve(SecretRef.parse(ref_url)) as secret:
        blob = json.loads(secret.as_str())
    return auth_from_blob(blob)


__all__ = ["AUTH_MODES", "auth_from_blob", "auth_from_ref"]
