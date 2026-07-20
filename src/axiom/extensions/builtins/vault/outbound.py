# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The outbound_call chokepoint — the ONLY plaintext-credential site.

Per ADR-055 D3 + spec-governance-fabric §2.3 + §9.2: every authenticated
outbound HTTP call routes through this module. The caller presents a
capability token; the vault dereferences it to the underlying credential
(via the existing ``axiom.infra.connections.get_credential`` chain),
attaches the credential to the request, and performs the call. No
credential ever leaves this process boundary in cleartext.

Phase 1: dereferences via ``get_credential`` (env / settings / 0600 file).
Phase 2: routes to OS-keychain backend for higher-attestation hosts.
Phase 4: federation-bound capability handoff.
Phase 5: HashiCorp Vault / AWS Secrets Manager / 1Password.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select

from axiom.extensions.builtins.vault.capability_store import (
    VaultContext,
    is_revoked,
    verify_capability,
)
from axiom.extensions.builtins.vault.db_models import (
    Capability as CapabilityRow,
    OutboundReceipt,
)
from axiom.governance import CapabilityToken


# ---------------------------------------------------------------------------
# Outbound request shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpRequest:
    """A pending outbound HTTP call.

    Headers are partial — the auth header is **not** present when the
    request is handed to ``outbound_call``. The vault attaches it.
    """

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None = None


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


# ---------------------------------------------------------------------------
# The chokepoint
# ---------------------------------------------------------------------------


def outbound_call(
    capability: CapabilityToken,
    request: HttpRequest,
    ctx: VaultContext,
    *,
    transport: Callable[[HttpRequest], HttpResponse] | None = None,
    credential_resolver: Callable[[str], str | None] | None = None,
    require_mfa: bool = False,
    min_posture: str = "open",
) -> HttpResponse:
    """Execute an authenticated outbound HTTP call under a capability.

    The capability is validated (lifecycle, revocation, scope), the
    underlying secret is dereferenced (and never returned to the caller),
    the request is sent, and a receipt is written.

    Args:
        capability: token presented by the calling agent
        request: the partial HTTP request (headers must NOT include auth)
        ctx: vault context (session factory + cache)
        transport: HTTP transport; injectable for tests. Default is httpx.
        credential_resolver: secret-name → cleartext fn; injectable for tests.
            Default routes through ``axiom.infra.connections.get_credential``.

    Returns:
        HttpResponse from the upstream service.

    Raises:
        ValueError: if capability is invalid or revoked.
    """
    receipt_id = f"out-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)

    # Lifecycle + revocation check.
    if not capability.is_valid_at(now):
        _write_receipt(
            ctx,
            receipt_id=receipt_id,
            capability=capability,
            request=request,
            status_code=None,
            outcome="capability_invalid",
            latency_ms=None,
            error="capability not valid at call time",
        )
        raise ValueError("capability not valid at call time")
    if is_revoked(ctx, capability.id):
        _write_receipt(
            ctx,
            receipt_id=receipt_id,
            capability=capability,
            request=request,
            status_code=None,
            outcome="capability_invalid",
            latency_ms=None,
            error="capability revoked",
        )
        raise ValueError(f"capability {capability.id} revoked")

    # Signature verification (IDENT-6, ADR-074). Enforced at attested+ (the
    # signature is a stable, custodied key); advisory at `open` (signing is
    # ephemeral, so cross-context verification is expected to fail).
    if not verify_capability(ctx, capability):
        from axiom.infra.principal import node_posture

        if node_posture() in ("attested", "sso", "service"):
            _write_receipt(
                ctx,
                receipt_id=receipt_id,
                capability=capability,
                request=request,
                status_code=None,
                outcome="capability_unverified",
                latency_ms=None,
                error="capability signature verification failed",
            )
            raise ValueError(
                f"capability {capability.id} signature verification failed"
            )

    # Posture-floor enforcement (ENF-4, AEOS-ID-2). A floored credential is not
    # released below its required posture. (Interactive step-up to elevate
    # instead of deny is the next increment — the step-up UX.)
    if min_posture != "open":
        from axiom.infra.principal import open_principal

        principal = getattr(ctx, "principal", None) or open_principal()
        if not principal.meets(min_posture):
            _write_receipt(
                ctx,
                receipt_id=receipt_id,
                capability=capability,
                request=request,
                status_code=None,
                outcome="posture_insufficient",
                latency_ms=None,
                error=f"requires posture '{min_posture}', principal is '{principal.posture}'",
            )
            raise ValueError(
                f"credential release requires posture '{min_posture}'; "
                f"principal is '{principal.posture}'"
            )

    # Fresh second factor for high-value releases (IDENT-9, ADR-074 §5b). A
    # require_mfa credential demands a fresh confirmation at *use* time (a Touch
    # ID / Badge tap), not merely a session unlock. Fail-closed: no confirmer or a
    # declined factor denies the release.
    if require_mfa:
        confirm = getattr(ctx, "mfa_confirm", None)
        if confirm is None or not confirm():
            _write_receipt(
                ctx,
                receipt_id=receipt_id,
                capability=capability,
                request=request,
                status_code=None,
                outcome="mfa_required",
                latency_ms=None,
                error="fresh second factor (MFA) required to release this credential",
            )
            raise ValueError("fresh second factor (MFA) required to release this credential")

    # Dereference the secret. THIS IS THE ONLY SITE THAT TOUCHES PLAINTEXT.
    secret_ref = _secret_ref_for(ctx, capability)
    if credential_resolver is None:
        credential_resolver = _default_credential_resolver
    cleartext = credential_resolver(secret_ref) if secret_ref else None

    # Build the auth-bearing request locally; never escape this scope.
    headers = dict(request.headers)
    if cleartext:
        headers["Authorization"] = f"Bearer {cleartext}"
    bearer_request = HttpRequest(
        method=request.method,
        url=request.url,
        headers=headers,
        body=request.body,
    )

    # Send.
    if transport is None:
        transport = _default_transport
    start = time.perf_counter()
    try:
        response = transport(bearer_request)
        latency_ms = int((time.perf_counter() - start) * 1000)
        _write_receipt(
            ctx,
            receipt_id=receipt_id,
            capability=capability,
            request=request,
            status_code=response.status_code,
            outcome="succeeded" if response.status_code < 400 else "failed",
            latency_ms=latency_ms,
            error=None,
        )
        return response
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        _write_receipt(
            ctx,
            receipt_id=receipt_id,
            capability=capability,
            request=request,
            status_code=None,
            outcome="failed",
            latency_ms=latency_ms,
            error=str(exc),
        )
        raise
    finally:
        # Wipe local references; prevent accidental persistence.
        del cleartext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _secret_ref_for(
    ctx: VaultContext, capability: CapabilityToken
) -> str | None:
    """Look up the capability's bound secret ref.

    In-memory map wins (it's authoritative when set by the caller's last
    issue_capability). Falls back to the Postgres store when persisted.
    The asymmetry is fine: production always persists; tests + in-memory
    dev mode use the in-memory map.
    """
    if capability.id in ctx.secret_refs:
        return ctx.secret_refs[capability.id]
    if ctx.session_factory is None:
        return None
    try:
        with ctx.session_factory() as session:  # type: ignore[misc]
            row = session.execute(
                select(CapabilityRow).where(CapabilityRow.id == capability.id)
            ).scalar_one_or_none()
            return row.secret_ref if row else None
    except Exception:
        return None


def _default_credential_resolver(secret_ref: str) -> str | None:
    """Phase 1: route to the existing ``axiom.infra.connections`` chain."""
    try:
        from axiom.infra.connections import get_credential

        return get_credential(secret_ref)
    except Exception:
        return None


def _default_transport(request: HttpRequest) -> HttpResponse:
    """Phase 1: synchronous httpx call.

    Phase 5 introduces connection pooling per vendor + retry policy
    from the connector manifest.
    """
    import httpx

    with httpx.Client(timeout=30.0) as client:
        response = client.request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.body,
        )
        return HttpResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=response.content,
        )


def _write_receipt(
    ctx: VaultContext,
    *,
    receipt_id: str,
    capability: CapabilityToken,
    request: HttpRequest,
    status_code: int | None,
    outcome: str,
    latency_ms: int | None,
    error: str | None,
) -> None:
    if ctx.session_factory is None:
        return
    try:
        with ctx.session_factory() as session:  # type: ignore[misc]
            session.add(
                OutboundReceipt(
                    id=receipt_id,
                    capability_id=capability.id,
                    actor=capability.subject.handle,
                    url=request.url,
                    method=request.method,
                    status_code=status_code,
                    outcome=outcome,
                    latency_ms=latency_ms,
                    error=error,
                )
            )
            session.commit()
    except Exception:
        pass


__all__ = ["HttpRequest", "HttpResponse", "outbound_call"]
