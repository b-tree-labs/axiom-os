# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`process.stop`: stop one registered process. Dry-run unless `kill` is true."""

from __future__ import annotations

from typing import Any

from axiom.infra.process_containment import ContainmentPolicy, terminate
from axiom.infra.skills import SkillContext, SkillResult

from ._common import next_steps, registry_for


def stop(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    reg = registry_for(ctx)
    ident = str(params.get("id", "")).strip()
    rec = reg.get(ident) if ident else None
    if rec is None:
        return SkillResult(ok=False, errors=[f"no unique registered process matches {ident!r}"])
    execute = bool(params.get("kill", False))
    grace = float(params.get("grace", ContainmentPolicy.load().grace_s))
    res = terminate(rec, state_dir=reg.state_dir, why=str(params.get("why") or "operator stop"),
                    execute=execute, grace_s=grace, actor=str(params.get("actor") or "operator"))
    if res["action"] == "dry-run":
        steps = next_steps(f"{{cli}} process stop {rec.id} --kill   do it: {res['plan']}",
                           f"{{cli}} process show {rec.id}          inspect first")
    elif res["action"] == "refused":
        steps = next_steps("{cli} process ls             the record may be stale; reaped on sweep",
                           "{cli} process sweep          see why the record does not verify")
    else:
        steps = next_steps("{cli} process ls             confirm it is gone",
                           "{cli} process sweep          look for anything else")
    value = {**res, "next": steps}
    if res.get("id"):
        value["process_id"] = res["id"]
    return SkillResult(ok=res["action"] != "survived", value=value,
                       actions_taken=[res["action"]] if res.get("signalled") else [],
                       errors=[res["reason"]] if res["action"] == "refused" else [])
