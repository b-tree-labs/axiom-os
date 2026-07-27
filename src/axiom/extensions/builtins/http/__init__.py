# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom's core HTTP surface — FastAPI app factory + threaded runner +
chat HTTP API.

Every built-in extension that needs to serve HTTP (today: the
classroom coordinator; tomorrow: federation peers, the memory
surface, the portal, anything) mounts its routes on an app produced
by :func:`create_app` and runs it via :func:`ThreadedServer`.

This replaces the hand-rolled stdlib :class:`BaseHTTPRequestHandler`
pattern that the classroom coordinator used through its prototype
phase. Consequences of picking FastAPI as the Axiom-core HTTP layer:

- Native asyncio concurrency — no more single-threaded-by-accident
  correctness.
- Dependency-injection — extensions can plug alternative backends
  (filesystem / SQLite / Postgres) into the same routes via
  :func:`fastapi.Depends`.
- Auto-generated OpenAPI at ``/docs`` — the portal can consume it,
  human inspectors can browse it.
- Starlette middleware stack — logging, CORS, error handling done
  once, reused everywhere.

Per ADR-031 extension self-containment, the HTTP extension owns its
lifecycle primitives; consumer extensions import from here rather
than depending on uvicorn/FastAPI directly.

Also bundles ``axi serve`` (the chat HTTP API absorbed from the now-
deprecated ``web_api/`` extension) and the federation-RAG endpoint
(``federation_endpoint``). Those modules use a stdlib server today;
the migration to FastAPI alongside ``create_app`` is a follow-up.
"""

from .compose import compose_app, route_table
from .middleware import (
    AuthzDecision,
    AuthzHook,
    MiddlewareConfig,
    PeerSigHook,
    PeerVerifyResult,
)
from .registry import (
    MountSpec,
    PrefixConflictError,
    RouterRegistry,
    default_registry,
    register_router,
)
from .server import (
    ThreadedServer,
    create_app,
    run_server,
)

__all__ = [
    "AuthzDecision",
    "AuthzHook",
    "MiddlewareConfig",
    "MountSpec",
    "PeerSigHook",
    "PeerVerifyResult",
    "PrefixConflictError",
    "RouterRegistry",
    "ThreadedServer",
    "compose_app",
    "create_app",
    "default_registry",
    "register_router",
    "route_table",
    "run_server",
]
