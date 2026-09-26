# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The fleet console's slice of ``/api/v1`` (spec-fleet-console §3, §8).

Registered through the webapp contribution registry (lazy entry; webapp
never imports this package at load time).

Ingest auth is authoritative, not best-effort: a push carries a Bearer
API key minted with a bound site (``bind_principal_to_site``), and the
site attributed to every stored report comes from that credential —
never from the payload. No key → 401; a key without a bound site →
403; both fail closed. Reads are bounded by the deployment site scope
narrowed to the credential's site when one is presented.
"""

# NOTE: no `from __future__ import annotations` here on purpose (same rule as
# chat/api.py): FastAPI resolves handler parameter annotations against module
# globals to decide body-vs-query; a stringized annotation for the
# locally-imported Request resolves to nothing and the route silently 422s.

import os
from collections.abc import Callable
from typing import Any

KEY_STORE_ENV = "AXIOM_GATE_API_KEYS_FILE"


def _default_key_store():
    from axiom.webauth.api_keys import JsonFileApiKeyStore

    return JsonFileApiKeyStore(os.environ.get(KEY_STORE_ENV, "").strip() or None)


_key_store_factory: Callable[[], Any] = _default_key_store


def set_key_store_factory(factory: Callable[[], Any]) -> None:
    """Test seam: inject a key store."""
    global _key_store_factory
    _key_store_factory = factory


def reset_key_store_factory() -> None:
    global _key_store_factory
    _key_store_factory = _default_key_store


def _bearer_identity(request):
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None
    return _key_store_factory().resolve(token)


def _default_session_resolver():
    """The webgate cookie-session leg of ``chain_resolvers(session, bearer)``
    (ADR-123 D3). Reads only — ingest never consults it. ``None`` when the
    http extension (which owns the resolver) isn't importable."""
    try:
        from axiom.extensions.builtins.http.authz_hook import build_session_resolver
    except Exception:
        return None
    return build_session_resolver()


_session_resolver_factory: Callable[[], Any] = _default_session_resolver


def set_session_resolver_factory(factory: Callable[[], Any]) -> None:
    """Test seam: inject the session-cookie resolver."""
    global _session_resolver_factory
    _session_resolver_factory = factory


def reset_session_resolver_factory() -> None:
    global _session_resolver_factory
    _session_resolver_factory = _default_session_resolver


def _session_site(request) -> str | None:
    """The ``site`` claim of a resolved webgate session, if any.

    A session that resolves without a site claim intentionally yields
    ``None`` — the viewer is authenticated (the gate's front door already
    enforced that) and reads fall back to the deployment scope, exactly as
    before D3. Site scoping still derives from a credential, never the
    client's request."""
    resolver = _session_resolver_factory()
    if resolver is None:
        return None
    # A raising resolver propagates (same rule as chain_resolvers): an
    # unverifiable cookie is None, but a broken resolver must fail the
    # request rather than quietly widen the view to deployment scope.
    credential = resolver(request)
    if credential is None:
        return None
    claims = getattr(credential, "claims", None) or {}
    site = claims.get("site")
    return str(site) if site else None


def register_routes(router: Any, *, subpath: str = "/fleet") -> None:
    """Attach the fleet endpoints under ``/api/v1<subpath>``."""
    from fastapi import HTTPException, Request

    from axiom.extensions.builtins.fleet import store
    from axiom.extensions.builtins.fleet.ingest import IngestRefused, ingest_reports
    from axiom.extensions.builtins.fleet.view import fleet_status
    from axiom.infra import site_scope

    @router.post(subpath + "/reports", tags=["fleet"])
    async def post_reports(request: Request) -> dict:
        """Push-only ingestion: nodes push out, the console never reaches in."""
        identity = _bearer_identity(request)
        if identity is None:
            raise HTTPException(401, "fleet ingest requires a Bearer API key")
        if not identity.site:
            raise HTTPException(
                403,
                "this key has no bound site; mint with --site (site never comes from the payload)",
            )

        body = await request.json()
        node_id = str(body.get("node_id") or "").strip()
        if not node_id:
            raise HTTPException(422, "node_id is required")
        reports = body.get("reports")
        if not isinstance(reports, list):
            raise HTTPException(422, "reports must be a list of {kind, payload}")

        cadences = body.get("cadences")
        try:
            with store.session_scope() as session:
                outcome = ingest_reports(
                    session,
                    site=identity.site,
                    node_id=node_id,
                    reporter_principal=identity.principal,
                    reports=reports,
                    cadences=cadences if isinstance(cadences, dict) else None,
                )
                session.commit()
        except IngestRefused as exc:
            raise HTTPException(422, str(exc)) from exc

        return {"accepted": outcome.accepted, "report_ids": list(outcome.report_ids)}

    @router.get(subpath + "/status", tags=["fleet"])
    def get_status(request: Request) -> dict:
        """The console view: per-node effect-checked status with evidence.

        Reads ride ``chain_resolvers(session, bearer)`` (ADR-123 D3): the
        webgate cookie session answers first — same order as the gate's
        front door — then the Bearer key. Either way the site narrowing
        derives from the resolved credential, never the client; out-of-
        scope stays 404, never 403. Writes remain bearer-only."""
        scope = site_scope.resolve()
        credential_site = _session_site(request)
        if credential_site is None:
            identity = _bearer_identity(request)
            if identity is not None and identity.site:
                credential_site = identity.site
        if credential_site is not None:
            if not scope.permits(credential_site):
                # Out-of-scope reads 404, never 403 (site_scope rule).
                raise HTTPException(404, "not found")
            sites: list[str] | None = [credential_site]
        elif scope.unbounded:
            sites = None
        else:
            sites = sorted(scope.sites)

        with store.session_scope() as session:
            return fleet_status(session, sites=sites)


__all__ = ["register_routes", "set_key_store_factory", "reset_key_store_factory"]
