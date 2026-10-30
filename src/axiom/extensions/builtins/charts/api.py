# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/charts`` — the web face of ``charts.suggest`` and ``charts.render``.

``POST /charts/render`` is the route appkit's ``specFigureSource`` draws
through, so a chart in a chat answer, in its expanded view and on the Charts
page is drawn by one renderer. ``GET /charts/data/{ref}`` hands back data an
answer was too large to carry inline (see ``block``).

No ``from __future__ import annotations``: FastAPI resolves handler
annotations at registration, and they are imported inside ``register_routes``.
"""

import logging
from typing import Any


def register_routes(router: Any, *, subpath: str = "/charts") -> None:
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from . import block
    from .skills import bind

    def _call(name: str, params: dict[str, Any]) -> Any:
        registry = SkillRegistry()
        bind(registry)
        ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                           logger=logging.getLogger("axi.charts"))
        # Through the dispatch chokepoint: the gate, audit and telemetry apply
        # to a browser as to the CLI and MCP.
        result = invoke_capability(registry, name, params, ctx, surface=WEB_SURFACE)
        if not result.ok:
            # appkit's renderSpec reads {detail, problems} off a 422 and shows
            # the reasons as a quiet note, not an alarm.
            return JSONResponse({"detail": f"{name} refused", "problems": list(result.errors)}, status_code=422)
        return result.value

    class SuggestBody(BaseModel):
        result: Any = None
        data_ref: str | None = None
        question: str = ""
        prefer: list[str] = []
        reference: dict | None = None
        title: str = ""
        width: int | None = None

    class RenderBody(BaseModel):
        spec: dict
        state: dict | None = None
        width: int | None = None
        height: int | None = None

    @router.post(f"{subpath}/suggest", tags=["charts"])
    def suggest(body: SuggestBody) -> Any:
        return _call("charts.suggest", body.model_dump(exclude_none=True))

    @router.post(f"{subpath}/render", tags=["charts"])
    def render(body: RenderBody) -> Any:
        return _call("charts.render", body.model_dump(exclude_none=True))

    @router.get(f"{subpath}/data/{{ref}}", tags=["charts"])
    def data(ref: str) -> dict:
        held = block.fetch(ref)
        if held is None:
            raise HTTPException(status_code=404, detail="no data held under that reference; it may have expired")
        return {"datasets": held}
