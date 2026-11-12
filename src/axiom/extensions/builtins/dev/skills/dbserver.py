# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.db_server_*`` — the one shared local Postgres for every lane."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import server
from . import open_vault


def _registry(params: dict[str, Any]):
    from axiom.extensions.builtins.lane.registry import Registry
    from axiom.extensions.builtins.lane.skills import lanes_path

    return Registry(params.get("registry") or lanes_path())


def up(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        info = server.up(open_vault(ctx), _registry(params), say=lambda s: print(s, flush=True))
    except server.ServerError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    said = f"dev database server {info['state']} on 127.0.0.1:{info['port']}"
    if not info["started"]:
        said += " (already)"
    return SkillResult(value={**info, "container": server.NAME}, actions_taken=[said])


def down(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        info = server.down()
    except server.ServerError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    said = (
        "stopped the dev database server (data kept; lanes' databases are intact)"
        if info["stopped"]
        else f"not running ({info['state']})"
    )
    return SkillResult(value=info, actions_taken=[said])


def status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    info = server.status(open_vault(ctx), _registry(params))
    port = f" on 127.0.0.1:{info['port']}" if info["port"] else ""
    return SkillResult(value=info, actions_taken=[f"dev database server: {info['state']}{port}"])
