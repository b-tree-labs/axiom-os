# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The receipts slice of ``/api/v1`` — the web projection of the brief.

One composer, three projections (spec-receipts-surface §1d): the CLI
renders text, MCP relays the courier form, and this route serves the
structured payload the web views (`BriefToday` / `BriefDocket` in
appkit) render verbatim.

Read-only by construction: a web GET composes with ``snapshot=False``
so browsing never advances the trust-delta baseline — only the
deliberate surfaces (CLI ``axi receipts today``, the MCP courier)
consume deltas. Site scoping mirrors fleet's reads: the resolved
credential narrows the view, never the client's request; out-of-scope
is 404, never 403.
"""

# NOTE: no `from __future__ import annotations` (same rule as fleet/api.py):
# FastAPI resolves handler annotations against module globals.

from typing import Any

from pydantic import BaseModel


class _Decision(BaseModel):
    """What a person decided about a case. The DECIDER is never in this
    body — it comes from the resolved session, so a decision cannot be
    attributed to someone who did not make it."""

    chosen: str
    note: str = ""


def _session_principal(request) -> str | None:
    """The signed-in principal, from the gate session. ``None`` when the
    request carries no resolvable session, or none this node can name.

    Built through the platform's own handle builder, never by hand. An
    email subject like ``ben@local`` interpolated straight into
    ``f"@{sub}"`` yields ``@ben@local`` — a double-@ that the ADR-020
    grammar does not parse, written permanently into an append-only
    decision record. A subject too malformed to name is no decider at
    all, and the caller answers 401 rather than recording a handle
    nothing can resolve later.
    """
    from axiom.extensions.builtins.fleet.api import _session_resolver_factory
    from axiom.infra.principal import principal_from_idp_subject

    resolver = _session_resolver_factory()
    if resolver is None:
        return None
    credential = resolver(request)
    if credential is None:
        return None
    claims = getattr(credential, "claims", None) or {}
    sub = claims.get("sub") or claims.get("email")
    if not sub:
        return None
    try:
        return principal_from_idp_subject(str(sub), str(claims.get("site") or ""))
    except ValueError:
        return None


def _session_label(request) -> str:
    """What to call the signed-in person on screen.

    The display name the provider gave, else the email's local part,
    else nothing — and nothing is honest: the surface falls back to the
    handle rather than to a blank. Never the raw subject, which is the
    handle's job.
    """
    from axiom.extensions.builtins.fleet.api import _session_resolver_factory

    resolver = _session_resolver_factory()
    if resolver is None:
        return ""
    credential = resolver(request)
    if credential is None:
        return ""
    claims = getattr(credential, "claims", None) or {}
    name = str(claims.get("name") or "").strip()
    if name:
        return name[:200]
    email = str(claims.get("email") or "").strip()
    return email.split("@", 1)[0][:200] if email else ""


def _this_node() -> str:
    """Which node this process IS — not one it merely watches.

    A fix like ``fleet.report`` acts on the node it runs on, so the
    question "can we fix this from here" is really "is this case about
    us". Same source the reporter uses, so the two can never disagree.
    """
    import os
    import platform

    return os.environ.get("AXIOM_FLEET_NODE_ID") or platform.node()


def register_routes(router: Any, *, subpath: str = "/receipts") -> None:
    """Attach the receipts endpoints under ``/api/v1<subpath>``."""
    from fastapi import HTTPException, Request

    from axiom.extensions.builtins.fleet import store as fleet_store
    from axiom.extensions.builtins.fleet.api import _bearer_identity, _session_site
    from axiom.extensions.builtins.receipts import store
    from axiom.extensions.builtins.receipts.brief import (
        brief_payload,
        compose_brief,
        fleet_source,
    )
    from axiom.extensions.builtins.receipts.cases import case_payload, compose_cases
    from axiom.infra import site_scope

    @router.get(subpath + "/today", tags=["receipts"])
    def get_today(request: Request) -> dict:
        """The oversight brief, composed now, without consuming deltas."""
        scope = site_scope.resolve()
        credential_site = _session_site(request)
        if credential_site is None:
            identity = _bearer_identity(request)
            if identity is not None and identity.site:
                credential_site = identity.site
        if credential_site is not None:
            if not scope.permits(credential_site):
                raise HTTPException(404, "not found")
            sites: list[str] | None = [credential_site]
            site_label: str | None = credential_site
        elif scope.unbounded:
            sites = None
            site_label = None
        else:
            sites = sorted(scope.sites)
            site_label = sites[0] if len(sites) == 1 else None

        with fleet_store.session_scope() as fsession:
            items = fleet_source(fsession, sites=sites)
        with store.session_scope() as session:
            with fleet_store.session_scope() as f2:
                brief = compose_brief(
                    session,
                    items,
                    site=site_label,
                    snapshot=False,
                    this_node=_this_node(),
                    fleet_session=f2,
                )

        return {"brief": brief_payload(brief)}

    @router.get(subpath + "/case/{case_id}", tags=["receipts"])
    def get_case(case_id: str, request: Request) -> dict:
        """One case, recomposed now from the same evaluator items — the
        drill-down's data. Same scoping as the brief; an unknown or
        out-of-scope case is 404, never distinguished."""
        scope = site_scope.resolve()
        credential_site = _session_site(request)
        if credential_site is None:
            identity = _bearer_identity(request)
            if identity is not None and identity.site:
                credential_site = identity.site
        if credential_site is not None:
            if not scope.permits(credential_site):
                raise HTTPException(404, "not found")
            sites: list[str] | None = [credential_site]
        elif scope.unbounded:
            sites = None
        else:
            sites = sorted(scope.sites)

        with fleet_store.session_scope() as fsession:
            items = fleet_source(fsession, sites=sites)
        from axiom.extensions.builtins.receipts.cases import (
            attach_blast,
            attach_decisions,
            attach_handling,
        )

        for case in compose_cases(items):
            if case.case_id == case_id:
                with store.session_scope() as session:
                    [decorated] = attach_decisions(session, [case])
                    with fleet_store.session_scope() as f2:
                        [decorated] = attach_blast(f2, session, [decorated])
                    # Whether anybody needs to be involved, and why. The
                    # drill-down is where that question gets answered,
                    # because answering it reads the decision record.
                    [decorated] = attach_handling(session, [decorated], this_node=_this_node())
                return {"case": case_payload(decorated)}
        raise HTTPException(404, "not found")

    @router.get(subpath + "/entity/{kind}/{entity_id:path}", tags=["receipts"])
    def get_entity(kind: str, entity_id: str, request: Request) -> dict:
        """What the platform knows about one named thing.

        Every name on a case is something a reader may want to know more
        about. An entity kind nobody has recorded facts for is a 404, so
        the surface shows no drill-down rather than a page that
        apologises for being empty.
        """
        from axiom.extensions.builtins.receipts.entities import KNOWN_KINDS, profile_for

        if kind not in KNOWN_KINDS:
            raise HTTPException(404, "not found")
        with fleet_store.session_scope() as fsession, store.session_scope() as session:
            profile = profile_for(
                kind,
                entity_id,
                fleet_session=fsession,
                receipts_session=session,
            )
            if not profile.known:
                raise HTTPException(404, "not found")
            return {"entity": profile.payload()}

    @router.post(subpath + "/case/{case_id}/decide", tags=["receipts"])
    def decide_case(case_id: str, body: _Decision, request: Request) -> dict:
        """Record a decision about a case — the oversight loop's write.

        The decider comes from the SESSION, never the body: an
        unattributed decision is not a record (and an attributable one
        must be attributable to the right person). The case is
        recomposed here so the evidence stored is what was true at
        decision time.
        """
        decider = _session_principal(request)
        if not decider:
            raise HTTPException(401, "a decision requires a signed-in principal")
        # What to show for them, captured NOW: the handle is keyed on the
        # provider subject (usually a GUID), which is right to key on and
        # unreadable to show. Names change, so the record keeps the name
        # as it stood at decision time rather than looking one up later.
        decider_label = _session_label(request)

        scope = site_scope.resolve()
        credential_site = _session_site(request)
        if credential_site is not None and not scope.permits(credential_site):
            raise HTTPException(404, "not found")

        # Through the gateway, never around it. A decision taken in a
        # browser used to import the skill and call it — no GUARD consult,
        # no site rules, no audit record. That mattered less when a
        # decision only wrote a row; it now runs capabilities.
        from axiom.extensions.builtins.receipts.cli import _ctx
        from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

        ctx = _ctx()
        result = invoke_capability(
            ctx.registry,
            "receipts.decide",
            {
                "case_id": case_id,
                "chosen": body.chosen,
                "note": body.note,
                "decider": decider,
                "decider_label": decider_label,
                "site": credential_site,
            },
            ctx,
            surface=WEB_SURFACE,
        )
        if not result.ok:
            message = "; ".join(result.errors)
            # An unknown/stale case reads as 404; a bad choice as 422.
            raise HTTPException(404 if "no live case" in message else 422, message)
        return result.value


__all__ = ["register_routes"]
