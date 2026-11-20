# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/charts`` — the web face of ``charts.suggest`` and ``charts.render``.

``GET /charts/saved`` lists Chart's catalog and ``GET /charts/saved/{id}`` draws one
saved chart by its catalog id (``charts.saved``). ``POST /charts/render`` is the route appkit's ``specFigureSource`` draws
through, so a chart in a chat answer, in its expanded view and on the Charts
page is drawn by one renderer. ``GET /charts/data/{ref}`` hands back data an
answer was too large to carry inline (see ``block``).

No ``from __future__ import annotations``: FastAPI resolves handler
annotations at registration, and they are imported inside ``register_routes``.
"""

import logging
from typing import Any


def register_routes(router: Any, *, subpath: str = "/charts") -> None:
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from . import block
    from .skills import bind

    def _call(name: str, params: dict[str, Any], principal: str = "") -> Any:
        registry = SkillRegistry()
        bind(registry)
        extra: dict[str, Any] = {}
        if principal:
            from axiom.infra.principal import PrincipalContext

            # The signed-in person, so a saved chart is read from their catalog only.
            extra["principal"] = PrincipalContext(handle=principal)
        ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                           logger=logging.getLogger("axi.charts"), **extra)
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

    def _who(request: Request) -> str:
        from axiom.extensions.builtins.webapp.api.views import principal_of

        who = principal_of(request)
        if not who:
            raise HTTPException(status_code=401, detail="saved charts belong to somebody; sign in")
        return who

    @router.get(f"{subpath}/saved", tags=["charts"])
    def saved_list(request: Request) -> Any:
        """Chart's catalog: this person's saved charts, by catalog id (MVP-03-A2)."""
        return _call("charts.saved", {}, _who(request))

    @router.get(f"{subpath}/saved/{{chart_id}}", tags=["charts"])
    def saved_one(chart_id: str, request: Request, width: int | None = None) -> Any:
        """One saved chart drawn by id: the same document the CLI and MCP return."""
        params: dict[str, Any] = {"id": chart_id}
        if width:
            params["width"] = width
        return _call("charts.saved", params, _who(request))

    class KindsBody(BaseModel):
        site: str
        replay: str | None = None
        dry_run: bool = False
        save_copy: bool = False

    @router.get(f"{subpath}/groups", tags=["charts"])
    def groups(request: Request) -> Any:
        """The groups this node provides to every account that may use Charts: ``{"prebuilt": [...]}``.

        The same list for every signed-in account (nothing is copied per account); an account's
        own groups are its saved view (``/views/groups/last``).
        """
        return _call("charts.prebuilt_groups", {}, _who(request))

    @router.post(f"{subpath}/kinds-group", tags=["charts"])
    def kinds_group(body: KindsBody, request: Request) -> Any:
        """The chart-kinds group for a site whose config is registered (it is prebuilt, shared: see GET /groups).

        ``save_copy`` also saves a personal copy on the caller's page. A browser never names a file.
        """
        params = body.model_dump(exclude_none=True)
        if params.pop("save_copy", False):
            params["copy"] = True
        return _call("charts.kinds_group", params, _who(request))

    @router.get(f"{subpath}/data/{{ref}}", tags=["charts"])
    def data(ref: str) -> dict:
        held = block.fetch(ref)
        if held is None:
            raise HTTPException(status_code=404, detail="no data held under that reference; it may have expired")
        return {"datasets": held}
