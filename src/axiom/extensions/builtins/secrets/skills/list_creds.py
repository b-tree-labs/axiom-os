# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.list`` — names + metadata of foreign credentials. Never values."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)
    try:
        items = store.list()
    except Exception as exc:  # noqa: BLE001 — surface backend problems cleanly
        return SkillResult(ok=False, errors=[f"list failed: {exc}"])
    return SkillResult(ok=True, value={"count": len(items), "items": items})


__all__ = ["run"]
