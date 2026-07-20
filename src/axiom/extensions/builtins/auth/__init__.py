# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``auth`` — SSO & delegated auth (OIDC / OAuth2). See prd-axiom-auth-sso.md.

One sign-in with an org IdP (Entra, Google, generic OIDC) yields a
platform identity + delegated tokens for every connector. Axiom runs the
Authorization-Code + PKCE flow, refreshes silently, and hands connectors a
``TokenSource``. Phase 1 (here): the flow building blocks + token source.
Phase 2: the interactive loopback login, vault-keyed ``token_source(provider,
user, scopes)``, JWKS verification, device-code, and the ``axi auth`` CLI.
"""

from axiom.extensions.builtins.auth import flow, pkce, providers
from axiom.extensions.builtins.auth.providers import IdpConfig, entra, google
from axiom.extensions.builtins.auth.token_source import TokenSource

__all__ = [
    "IdpConfig",
    "TokenSource",
    "entra",
    "flow",
    "google",
    "pkce",
    "providers",
]
