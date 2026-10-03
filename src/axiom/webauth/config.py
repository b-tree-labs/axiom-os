# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Ported from SoilMetrix, Inc (dba Aiterra.ag) by Benjamin Booth, CEO.

"""Authentication configuration.

The default signing algorithm is **ES256** (ADR-085); keys live in
``keys.py``. HS256 remains only as an explicit compatibility path (see
``get_signing_secret`` below and ``jwt.py``), used by tokens minted before the
ES256 cutover.

Lift note vs. the SoilMetrix original: the HS256 signing secret is resolved
*lazily* (``get_signing_secret()``) rather than at import time, and it flows
through Axiom's secrets provider before falling back to the environment. This
keeps ``import axiom.webauth`` side-effect-free — a missing key never explodes at
module load (which would break extension discovery); it fail-closes only when a
token is actually signed in a deployed environment.
"""

from __future__ import annotations

import os
import secrets as _secrets

from axiom.setup.secrets import get_secret

#: Primary JWT signing algorithm — ES256 (ECDSA P-256), asymmetric, verified
#: from the published JWKS. The ES256 keys themselves live in ``keys.py``; this
#: constant names the default alg. HS256 is reachable only via an explicit
#: ``secret_key`` (the migration-window compat path). Deliberately NOT the node's
#: Ed25519 vega identity key — see ``keys.py`` for why (ADR-085 / ADR-022).
ALGORITHM = "ES256"

# TODO(dedup): source these from an Axiom settings section (ConfigRegistry)
# instead of raw env, so they surface in `axi` settings like other tunables.
#: Access tokens: 24h (refreshed automatically).
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
#: Refresh tokens: 30d sliding session.
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

_DEV_ENVIRONMENTS = {"development", "test"}
_ephemeral_secret: str | None = None


def get_signing_secret() -> str:
    """Return the legacy **HS256** signing secret, fail-closed in deployed envs.

    Used only by the HS256 compatibility path (``jwt.py`` when an explicit
    ``secret_key`` is not passed by the caller but HS256 is still in play). The
    default ES256 path does not call this — it uses ``keys.py``.

    Resolution order: ``SECRET_KEY`` env → Axiom secrets provider → (dev/test
    only) a per-process ephemeral key. In any non-dev ``ENVIRONMENT`` a missing
    secret raises, rather than silently minting an ephemeral key that would
    invalidate every token on the next replica or restart.
    """
    secret = os.getenv("SECRET_KEY") or get_secret("SECRET_KEY")
    if secret:
        return secret

    environment = os.getenv("ENVIRONMENT", "development")
    if environment in _DEV_ENVIRONMENTS:
        global _ephemeral_secret
        if _ephemeral_secret is None:
            _ephemeral_secret = _secrets.token_urlsafe(32)
        return _ephemeral_secret

    raise RuntimeError(
        "SECRET_KEY must be set in deployed environments "
        f"(ENVIRONMENT={environment!r}); refusing to sign with an ephemeral key."
    )
