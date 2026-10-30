# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The Box storage provider (ADR-220).

Everything that knows this vendor lives in this package: the REST client
(``session_api``), the browser-session client (``api``), the three server auth
flows (``ccg_auth``, ``jwt_auth``, ``oauth_auth``), how a credential blob selects
one of them (``auth``), and :class:`~.provider.BoxStorageProvider`, the
``StorageConnectorProvider`` the rest of the platform uses. Core code reaches it
only through the storage registry, by a connector's ``kind``.

Registered through the ``axiom.storage_providers`` entry-point group, the same
way an extension ships a provider. Importing this package is cheap: the client
and auth modules load when a connector is built.
"""

from __future__ import annotations

KIND = "box"


def register(registry) -> None:
    """Entry point (``axiom.storage_providers``): register this provider's factory."""
    from .provider import build

    registry.register(KIND, build)


__all__ = ["KIND", "register"]
