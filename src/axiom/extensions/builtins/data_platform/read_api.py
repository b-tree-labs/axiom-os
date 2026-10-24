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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from axiom.infra.skills import SkillContext, SkillRegistry

from .read_target import holds_medallion
from .skills import gold

_VERBS = {
    "data.tables": "tables",
    "data.describe": "describe",
    "data.aggregate": "aggregate",
    "data.series": "series",
    "data.roles": "roles",
    "data.compare": "compare",
}


class QueryBody(BaseModel):
    verb: str = Field(max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)


def build_data_read_router() -> APIRouter:
    router = APIRouter()

    @router.post("/data/query")
    def query(body: QueryBody) -> dict[str, Any]:
        if not holds_medallion():
            raise HTTPException(status_code=404, detail="this node does not hold the medallion")
        name = _VERBS.get(body.verb)
        if name is None:
            raise HTTPException(status_code=400, detail=f"not a data read: {body.verb}")
        fn = getattr(gold, name)
        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=_state_dir(),
            logger=logging.getLogger("axi.data.read"),
        )
        result = fn(body.params, ctx)
        return {"ok": result.ok, "value": result.value, "errors": list(result.errors)}

    return router


def _state_dir():
    from axiom.infra.paths import get_platform_state_dir

    return get_platform_state_dir()


__all__ = ["build_data_read_router"]
