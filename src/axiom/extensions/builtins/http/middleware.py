# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Shared middleware for the composed HTTP app (spec-serve §5–6).

One place for the cross-cutting behavior every consumer used to
re-implement (or skip): structured request logging, a single error
envelope, and seams for authorization + peer-signature verification.

Middleware order is fixed and documented (SRV-024), outermost first::

    request → [logging] → [error-normalization] → [peer-sig] → [authz] → route

The authz and peer-sig stages are **seams**: they run only when an
injected hook is supplied on :class:`MiddlewareConfig`. ``serve`` has no
hard dependency on the ``authz`` or ``federation`` extensions — the hook
is a plain callable the composing caller provides. The peer-sig call
site is defined here but left as a seam (PRD §7.2); the authz call site
is built now (PRD §7.1).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_LOGGER = logging.getLogger("axi.serve")

# Stable machine tokens for the error envelope (spec §6).
_STATUS_CODE_TOKEN = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    422: "bad_request",
    500: "internal",
}


@dataclass(frozen=True)
class AuthzDecision:
    """Result of an :data:`AuthzHook` call.

    ``allow`` gates the request; ``reason`` is surfaced in the error
    envelope on deny. Deliberately minimal — the ``authz`` extension owns
    the richer ``Verdict``; ``serve`` only needs allow/deny + a reason.
    """

    allow: bool
    reason: str = ""


@dataclass(frozen=True)
class PeerVerifyResult:
    """Result of a :data:`PeerSigHook` call (seam).

    ``ok`` gates the request; ``peer`` is the resolved peer identity
    attached to ``request.state.peer`` on success.
    """

    ok: bool
    peer: str | None = None
    reason: str = ""


# Seams — injected callables, mirroring the federation gateway's injected
# signer/verifier discipline.
AuthzHook = Callable[[Request], AuthzDecision]
"""Calls the authz decision (e.g. an adapter over
``axiom.extensions.builtins.authz.decide``). Injected so ``serve`` has no
hard dep on authz."""

PeerSigHook = Callable[[Request], PeerVerifyResult]
"""Calls federation Ed25519 verification on inbound peer requests.
Injected so ``serve`` has no hard dep on federation. SEAM."""


@dataclass
class MiddlewareConfig:
    """Configures the shared middleware chain (spec §5)."""

    request_logging: bool = True  # SRV-020
    error_normalization: bool = True  # SRV-021
    authz: AuthzHook | None = None  # SRV-022 seam; None = off
    peer_sig: PeerSigHook | None = None  # SRV-023 seam; None = off


def _token_for(status_code: int) -> str:
    if status_code in _STATUS_CODE_TOKEN:
        return _STATUS_CODE_TOKEN[status_code]
    if 400 <= status_code < 500:
        return "bad_request"
    return "internal"


def _envelope(
    *,
    status_code: int,
    message: str,
    request: Request,
    code: str | None = None,
) -> JSONResponse:
    """Build the one error envelope (spec §6) for every route."""
    extension = getattr(request.state, "mount_extension", None)
    request_id = getattr(request.state, "request_id", None)
    body = {
        "error": {
            "code": code or _token_for(status_code),
            "message": message,
            "request_id": request_id,
            "extension": extension,
        }
    }
    return JSONResponse(status_code=status_code, content=body)


def _resolve_mount(request: Request, specs) -> object | None:
    """Find the registered MountSpec whose prefix matches this request
    path, so the envelope/authz can name the owning extension.

    ``specs`` is a callable returning the current spec list (so the live
    registry is consulted per request without a hard import cycle).
    """
    path = request.url.path
    best = None
    for spec in specs():
        if path == spec.prefix or path.startswith(
            spec.prefix.rstrip("/") + "/"
        ):
            if best is None or len(spec.prefix) > len(best.prefix):
                best = spec
    return best


def install_middleware(
    app: FastAPI,
    config: MiddlewareConfig,
    *,
    specs: Callable[[], list] | None = None,
) -> None:
    """Install the middleware chain on ``app`` in the documented order.

    ``specs`` returns the current MountSpec list (used to resolve the
    owning extension for the envelope + the authz ``requires_authz``
    gate). When ``None``, mount resolution is skipped (the envelope's
    ``extension`` field is then ``None``).
    """
    specs = specs or (lambda: [])

    if config.error_normalization:
        _install_error_handlers(app)

    # Starlette runs middleware in reverse registration order, so the
    # LAST registered is the OUTERMOST. Register inner-to-outer to land
    # the documented outermost-first order:
    #   logging (outermost) → error → peer-sig → authz → route
    if config.authz is not None:
        _install_authz(app, config.authz, specs)
    if config.peer_sig is not None:
        _install_peer_sig(app, config.peer_sig)
    if config.request_logging:
        _install_logging(app, specs)


def _install_logging(app: FastAPI, specs) -> None:
    @app.middleware("http")
    async def _logging(request: Request, call_next):  # SRV-020
        request_id = request.headers.get("x-request-id") or (
            "req_" + uuid.uuid4().hex[:12]
        )
        request.state.request_id = request_id
        mount = _resolve_mount(request, specs)
        if mount is not None:
            request.state.mount_extension = mount.extension
            request.state.mount_spec = mount
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
        _LOGGER.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": round(latency_ms, 2),
                "extension": getattr(
                    request.state, "mount_extension", None
                ),
            },
        )
        response.headers["x-request-id"] = request_id
        return response


def _install_peer_sig(app: FastAPI, hook: PeerSigHook) -> None:
    @app.middleware("http")
    async def _peer_sig(request: Request, call_next):  # SRV-023 (seam)
        result = hook(request)
        if not result.ok:
            return _envelope(
                status_code=401,
                message=result.reason or "peer signature verification failed",
                request=request,
                code="unauthorized",
            )
        request.state.peer = result.peer
        return await call_next(request)


def _install_authz(app: FastAPI, hook: AuthzHook, specs) -> None:
    @app.middleware("http")
    async def _authz(request: Request, call_next):  # SRV-022
        mount = getattr(request.state, "mount_spec", None) or _resolve_mount(
            request, specs
        )
        # Honor the per-mount opt-out; default-on for unresolved mounts.
        requires = True if mount is None else mount.requires_authz
        if requires:
            decision = hook(request)
            if not decision.allow:
                return _envelope(
                    status_code=403,
                    message=decision.reason or "forbidden",
                    request=request,
                    code="forbidden",
                )
        return await call_next(request)


def _install_error_handlers(app: FastAPI) -> None:  # SRV-021
    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        return _envelope(
            status_code=exc.status_code,
            message=str(exc.detail),
            request=request,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        return _envelope(
            status_code=422,
            message="request validation failed",
            request=request,
            code="bad_request",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        _LOGGER.exception(
            "unhandled error",
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        return _envelope(
            status_code=500,
            message="internal server error",
            request=request,
            code="internal",
        )


__all__ = [
    "AuthzDecision",
    "AuthzHook",
    "MiddlewareConfig",
    "PeerSigHook",
    "PeerVerifyResult",
    "install_middleware",
]
