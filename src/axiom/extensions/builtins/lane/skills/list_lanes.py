# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``lane.list`` — every claimed lane, and whether it is actually up."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..doctor import listening_ports
from ..registry import RESERVED, Registry
from . import lanes_path


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    reg = Registry(params.get("registry") or lanes_path())
    heard = params.get("listening")
    heard = listening_ports() if heard is None else heard

    rows = []
    for name, lane in sorted(reg.all().items()):
        rows.append(
            {
                "lane": name,
                "up": all(p in heard for p in lane.ports),
                "front": lane.front,
                "api": lane.api,
                "database": lane.database or None,
                "isolated": lane.isolated,
                "dsn_var": lane.dsn_var,
                "branch": lane.branch,
                "owner": lane.owner,
                "trees": lane.trees,
            }
        )
    return SkillResult(
        value={
            "lanes": rows,
            "reserved": [{"port": p, "what": w} for p, w in sorted(RESERVED.items())],
        }
    )
