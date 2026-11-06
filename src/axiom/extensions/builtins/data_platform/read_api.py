# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``POST /data/query`` — a data read on the node that holds the medallion.

A bound install sends the verb and its parameters. It does not send a
database URL. This route runs only where this install holds the medallion.
Anywhere else it answers 404, which the caller reports as "does not serve
data reads yet."
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from axiom.infra.skills import SkillContext, SkillRegistry

from .read_target import FOLLOWS_THE_BINDING, KIT_READS, holds_medallion
from .skills import gold

#: Every read a bound install sends here (``read_target.FOLLOWS_THE_BINDING``), served by the gold
#: skill of the same name, which holds the answer to the caller's verified site. One list on both
#: sides: a verb the laptop forwards and the node did not serve came back as HTTP 400.
_VERBS = {verb: verb.split(".", 1)[1] for verb in FOLLOWS_THE_BINDING}

#: ADR-203: promoted kits, served through the same door (``read_target.KIT_READS``, the list a
#: bound laptop forwards). The site comes from the verified credential inside each skill, never
#: from the request.
_KIT_VERBS = {verb: verb.split(".", 1)[1] for verb in KIT_READS}


#: The writes through this door, each an invoke at the gate: a bundle for review, and
#: a contributor's request to withdraw their objects (a row; nothing is dropped).
_SUBMIT_VERBS = {"data.kit_submit": "kit_submit", "data.kit_withdraw": "kit_withdraw"}


#: The one act of governance through this door (ADR-151 §2, ADR-222): the review queue, every
#: bundle and withdrawal waiting for an operator. The mount declares its path as governing, so
#: only a role holding ``govern`` (operator, admin, owner) reaches it, whatever the method.
_GOVERN_VERBS = {"data.kit_review": "kit_review"}


class QueryBody(BaseModel):
    verb: str = Field(max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)


def build_data_read_router() -> APIRouter:
    router = APIRouter()

    def _handle(request: Request) -> str:
        principal = getattr(getattr(request, "state", None), "principal", None)
        return str(
            (principal if isinstance(principal, str) else getattr(principal, "handle", None)) or ""
        )

    def _credential(request: Request) -> str | None:
        value = getattr(getattr(request, "state", None), "credential_id", None)
        return str(value) if value else None

    def _run(fn: Any, params: dict[str, Any], handle: str, credential_id: str | None = None) -> dict[str, Any]:
        from axiom.infra.principal import PrincipalContext

        # The caller the gate verified, and the key it presented, so the serving budget
        # (ADR-157) meters each credential on its own. Without it every bound reader shared
        # this process's identity and the anonymous class: one call at a time, for everyone.
        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=_state_dir(),
            logger=logging.getLogger("axi.data.read"),
            principal=PrincipalContext(handle=handle or "@anonymous", posture="attested" if handle else "open",
                                       assured=bool(handle),
                                       credential_id=credential_id if handle else None),
            surface="web" if handle else None,
        )
        result = fn(params, ctx)
        body: dict[str, Any] = {"ok": result.ok, "value": result.value, "errors": list(result.errors)}
        if result.unmet:
            # A refusal's machine-readable fixes (table_door.refusal_hints): additive.
            body["unmet"] = list(result.unmet)
        return body

    @router.post("/data/query")
    def query(body: QueryBody, request: Request) -> dict[str, Any]:
        """Questions only. The mount declares this path a read, so a read-only key may ask."""
        if not holds_medallion():
            raise HTTPException(status_code=404, detail="this node does not hold the medallion")
        params = {k: v for k, v in body.params.items() if k not in ("dsn", "_principal")}
        from .tiers import serving_refusal

        refusal = serving_refusal(body.verb, params)
        if refusal is not None:
            # The served door reads gold; a request naming silver is answered, with why.
            return {"ok": False, "value": None, "errors": [refusal]}
        handle = _handle(request)
        name = _VERBS.get(body.verb)
        if name is not None:
            fn = getattr(gold, name)
        elif body.verb in _KIT_VERBS:
            from .skills import kit_promotion

            fn = getattr(kit_promotion, _KIT_VERBS[body.verb])
            params["_principal"] = handle
        else:
            raise HTTPException(status_code=400, detail=f"not a data read: {body.verb}")
        return _run(fn, params, handle, _credential(request))

    @router.post("/data/submit")
    def submit(body: QueryBody, request: Request) -> dict[str, Any]:
        """The one write through this door: a kit bundle for review (ADR-203). It stays
        ``invoke`` at the gate, so a read-only key cannot submit."""
        if not holds_medallion():
            raise HTTPException(status_code=404, detail="this node does not hold the medallion")
        if body.verb not in _SUBMIT_VERBS:
            raise HTTPException(status_code=400, detail=f"not a data submission: {body.verb}")
        from .kit.host import RegistryNotReady
        from .skills import kit_promotion

        params = {k: v for k, v in body.params.items() if k not in ("dsn", "_principal")}
        handle = _handle(request)
        params["_principal"] = handle
        try:
            return _run(getattr(kit_promotion, _SUBMIT_VERBS[body.verb]), params, handle,
                        _credential(request))
        except RegistryNotReady as exc:
            # The node is not ready (an operator step is missing), which is not the
            # caller's fault and not a crash: 503, with the step named.
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @router.post("/data/review")
    def review(body: QueryBody, request: Request) -> dict[str, Any]:
        """The operator's queue, from anywhere the operator's key is. Declared governing by the
        mount, so a reader's or a contributor's key is refused before this runs."""
        if not holds_medallion():
            raise HTTPException(status_code=404, detail="this node does not hold the medallion")
        if body.verb not in _GOVERN_VERBS:
            raise HTTPException(status_code=400, detail=f"not a review verb: {body.verb}")
        from .kit.host import RegistryNotReady
        from .skills import kit_promotion

        params = {k: v for k, v in body.params.items() if k not in ("dsn", "_principal")}
        handle = _handle(request)
        params["_principal"] = handle
        try:
            return _run(getattr(kit_promotion, _GOVERN_VERBS[body.verb]), params, handle,
                        _credential(request))
        except RegistryNotReady as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    return router


def _state_dir():
    from axiom.infra.paths import get_platform_state_dir

    return get_platform_state_dir()


__all__ = ["build_data_read_router"]
