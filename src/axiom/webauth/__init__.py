# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Ported from SoilMetrix, Inc (dba Aiterra.ag) by Benjamin Booth, CEO.

"""Human/web authentication for Axiom — login, passwords, JWT sessions.

``webauth`` is the *authentication* layer: it establishes **who** a browser or
mobile client is and issues/verifies the session tokens that carry that claim.
It is deliberately distinct from ``axiom.vega`` authorization (GUARD), which
decides **what** an already-identified principal may do. The seam between them
is small and one-directional: webauth resolves a request to an
``axiom.vega.identity.principal.Principal``; the HTTP authz hook then asks GUARD
``decide(...)``. webauth never re-implements authorization.

Ported from the SoilMetrix ``libs/auth`` library (self-contained, factory-based)
as the first "replace a SoilMetrix piece with an Axiom equivalent" step. Lift
adaptations: JWT rides Axiom's existing PyJWT dependency; the signing secret is
resolved lazily through Axiom's secrets provider; password hashing uses stdlib
scrypt to avoid new dependencies. Opportunistic dedup against Axiom primitives
(GUARD for roles, settings for expiries, telemetry for audit) is marked inline
with ``TODO(dedup)`` and lands as each piece wires in.
"""

from __future__ import annotations

from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    REFRESH_TOKEN_EXPIRE_DAYS,
    get_signing_secret,
)
from .api_keys import (
    ApiKeyIdentity,
    ApiKeysFileError,
    JsonFileApiKeyStore,
    append_api_key_record,
    load_api_key_records,
    mint_api_key,
    revoke_api_key_record,
    save_api_key_records,
)
from .jwt import create_access_token, create_refresh_token, verify_token
from .keys import KeyStore, SigningKey, get_key_store, load_key_store
from .file_store import (
    AccountsFileError,
    JsonFileUserStore,
    load_user_records,
    save_user_records,
    upsert_user_record,
)
from .password import get_password_hash, validate_password, verify_password
from .session import (
    SESSION_COOKIE,
    issue_session_token,
    session_from_cookies,
    verify_session_token,
)
from .users import (
    InMemoryUserStore,
    User,
    UserStore,
    authenticate,
    get_user_store,
    set_user_store,
)

__all__ = [
    "ALGORITHM",
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "REFRESH_TOKEN_EXPIRE_DAYS",
    "get_signing_secret",
    "create_access_token",
    "create_refresh_token",
    "verify_token",
    "SigningKey",
    "KeyStore",
    "get_key_store",
    "load_key_store",
    "get_password_hash",
    "verify_password",
    "validate_password",
    # accounts + browser session (shared with the OIDC fast-follow)
    "User",
    "UserStore",
    "InMemoryUserStore",
    "JsonFileUserStore",
    "AccountsFileError",
    "load_user_records",
    "save_user_records",
    "upsert_user_record",
    "authenticate",
    "get_user_store",
    "set_user_store",
    "SESSION_COOKIE",
    "issue_session_token",
    "verify_session_token",
    "session_from_cookies",
    # non-human API principals (bearer keys the HTTP authz hook resolves)
    "ApiKeyIdentity",
    "ApiKeysFileError",
    "JsonFileApiKeyStore",
    "append_api_key_record",
    "load_api_key_records",
    "mint_api_key",
    "revoke_api_key_record",
    "save_api_key_records",
]
