# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``GET /program/{status,changes}`` — the reads on the composed HTTP substrate.

A ``service`` mount discovered by ``compose_app`` (spec-serve §4): the
entry returns a :class:`MountSpec` with ``requires_authz=True``, so the
fail-closed substrate refuses to serve it without an authz hook, and the
hook refuses a request without a credential. There is no anonymous read.

The routes are thin projections. They dispatch ``program.status`` /
``program.changes`` through ``invoke_capability`` on the ``web`` surface —
the same door the CLI and the MCP tools use, so the gateway's
``tool.pre_invoke`` chain sees them — and map the skill's typed refusal onto
a status code: ``absent`` → 404, ``bad_request`` → 422, ``no_data`` → 503.
They read only the node's own data file; a ``data`` query param is refused,
exactly as on MCP.

``GET /program/changes`` is unconditionally a read: it forwards only
``principal`` and ``since`` and never ``advance``, so a GET can never move a
watermark. Advancing a watermark is reachable only where the caller can ask
for it explicitly (the CLI, or an MCP call that sets ``advance``), never as a
side effect of a plain HTTP read.
"""

# No ``from __future__ import annotations``: FastAPI resolves the route's
# ``Request`` annotation at decoration time, and Request is imported inside
# the builder so the module imports without the web extra installed.
import logging
from pathlib import Path
from typing import Any

#: The query params each route forwards; anything else is a 422, the HTTP
#: equivalent of the MCP projection refusing an undeclared argument. The
#: changes route deliberately omits ``advance`` (and ``peek``): a GET is a
#: read, so it never carries the one param that would make it advance.
_PARAMS = ("scope", "key", "fmt")
_CHANGES_PARAMS = ("principal", "since")

_STATUS_FOR_REFUSAL = {"absent": 404, "bad_request": 422, "no_data": 503}


def build_program_router(*, state_dir: Path | None = None):
    """Build the ``/program`` router.

    ``state_dir`` is the test seam; a node gets its user state dir, which
    is where every program read finds ``program/data.json`` by default.
    """
    from fastapi import APIRouter, HTTPException, Request

    router = APIRouter()

    def _dispatch(capability: str, query: dict, allowed: tuple[str, ...]) -> Any:
        unknown = sorted(set(query) - set(allowed))
        if unknown:
            raise HTTPException(
                422,
                f"{capability} does not accept parameter(s) over HTTP: {', '.join(unknown)}",
            )

        from axiom.infra.paths import get_user_state_dir
        from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
        from axiom.infra.skills import SkillContext, SkillRegistry

        from .skills import bind

        registry = SkillRegistry()
        bind(registry)
        ctx = SkillContext(
            registry=registry,
            state_dir=state_dir if state_dir is not None else get_user_state_dir(),
            logger=logging.getLogger("axiom.program.http"),
        )
        result = invoke_capability(registry, capability, query, ctx, surface=WEB_SURFACE)
        if not result.ok:
            refused = (
                (result.value or {}).get("refused") if isinstance(result.value, dict) else None
            )
            raise HTTPException(_STATUS_FOR_REFUSAL.get(refused, 403), "; ".join(result.errors))
        return result.value

    @router.get("/program/status", tags=["program"])
    def program_status(request: Request) -> Any:
        return _dispatch("program.status", dict(request.query_params), _PARAMS)

    @router.get("/program/changes", tags=["program"])
    def program_changes(request: Request) -> Any:
        # A GET is a read: only principal / since are forwarded, never
        # advance, so this route can never move a watermark.
        return _dispatch("program.changes", dict(request.query_params), _CHANGES_PARAMS)

    return router


def mount_spec(*, state_dir: Path | None = None):
    """The authz-required ``/program`` mount (discovered by ``compose_app``)."""
    from axiom.extensions.builtins.http.registry import MountSpec

    return MountSpec(
        prefix="/program",
        router=build_program_router(state_dir=state_dir),
        extension="program",
        requires_authz=True,
    )


__all__ = ["build_program_router", "mount_spec"]
