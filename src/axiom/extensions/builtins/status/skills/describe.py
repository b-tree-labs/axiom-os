# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``status.describe`` — the node describes itself, live.

One skill behind every surface (chat tool, MCP, CLI): the profile is
derived from the real registries at call time. ``section`` narrows the
answer (node | routes | chat_tools | skills | gate | rag | db) so an
agent asks small questions and answers fast.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..selfknowledge import node_profile


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    section = params.get("section") or None
    profile = node_profile(section)
    if section and not profile["sections"]:
        return SkillResult(
            ok=False,
            errors=[
                f"unknown section {section!r}; one of: node, routes, "
                "chat_tools, skills, gate, rag, db"
            ],
        )
    return SkillResult(ok=True, value=profile, actions_taken=["described the node (live)"])


__all__ = ["run"]
