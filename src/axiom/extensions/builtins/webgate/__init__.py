# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``webgate`` — a UI-agnostic forward-auth gate (ADR-003).

A browser-session authenticator that fronts any UI (Open WebUI, LibreChat, …) or
app through an edge proxy: the proxy subrequests ``/gate/verify``, which allows
(200 + ``X-Axiom-User-*`` headers) or denies (401), and unauthenticated users are
sent to ``/gate/login``. Built on ``axiom.webauth`` (scrypt passwords, ES256
session tokens, the shared user store), so the OIDC fast-follow reuses the same
accounts and the same browser session — no fork.
"""

from __future__ import annotations

from .api.routers import build_webgate_router
from .mount import mount_spec

__version__ = "0.1.0"

__all__ = ["build_webgate_router", "mount_spec"]
