# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``GET /api/v1/first-run``: the web face of ``sites.checklist`` with ``kind=first_run``.

The person is the gate session's; with no session the saved-chart step reads
``todo`` (it is somebody's catalogue). Through the dispatch chokepoint, so the
authority gate, audit and telemetry apply to a browser as to the CLI and MCP.

No ``from __future__ import annotations``: FastAPI resolves handler annotations
at registration and ``Request`` is imported inside ``register_routes``.
"""

import logging
from typing import Any


def _claims(request: Any) -> dict:
    try:
        from axiom.webauth import session_from_cookies

        return session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - no session: the person-bound step stays todo
        return {}


def register_routes(router: Any, *, subpath: str = "/first-run") -> None:
    from fastapi import HTTPException, Request

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext

    from .skills import bind_default

    @router.get(subpath, tags=["sites"])
    def first_run(request: Request) -> dict:
        claims = _claims(request)
        registry = bind_default()
        ctx = SkillContext(
            registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.sites")
        )
        result = invoke_capability(
            registry,
            "sites.checklist",
            {"kind": "first_run", "_principal": str(claims.get("sub") or "")},
            ctx,
            surface=WEB_SURFACE,
        )
        if not result.ok:
            raise HTTPException(500, "; ".join(result.errors))
        return {k: v for k, v in result.value.items() if k != "text"}
