# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``receipts.sealed``: the receipts console's name for ``attest.sealed_and_packaged`` (#1814),
the one skill chat and MCP reach. Same function, same result."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.attest.skills.read import sealed_and_packaged

    return sealed_and_packaged(params or {}, ctx)
