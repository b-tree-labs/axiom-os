# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``connector.roles`` skill: which connector serves each storage role (ADR-220 §6).

ADR-056 thin wrapper behind ``axi connector roles``. Read-only. For every
declared role it says whether a configured connector serves it, the default
store serves it (and what that leaves missing, such as an off-site copy), or the
feature is absent on this install.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``manager`` (tests)."""
    from axiom.extensions.builtins.connector.storage.manager import default_storage_manager

    manager = params.get("manager") or default_storage_manager()
    rows = [s.as_dict() for s in manager.role_status().values()]
    return SkillResult(ok=True, value={"resource": "storage_roles", "roles": rows})
