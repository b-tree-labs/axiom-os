# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``vault.list`` — names + metadata of stored credentials. Never values.

Reads the one credential index (ADR-218), so each row carries the same
``host``, ``purposes`` and ``health`` that ``vault.resolve`` and
``vault.audit`` see. ``host`` narrows the list exactly as resolve does:
``list --host H`` and ``resolve --host H`` name the same credentials.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.extensions.builtins.vault.index import CredentialIndex

    index = CredentialIndex(
        state_dir=getattr(ctx, "state_dir", None), store=params.get("_store")
    )
    host = str(params.get("host") or "").strip()
    try:
        items = index.for_host(host) if host else index.rows()
    except Exception as exc:  # noqa: BLE001 — surface backend problems cleanly
        return SkillResult(ok=False, errors=[f"list failed: {exc}"])
    value: dict[str, Any] = {"count": len(items), "items": items}
    if host:
        value["host"] = host.lower()
    return SkillResult(ok=True, value=value)


__all__ = ["run"]
