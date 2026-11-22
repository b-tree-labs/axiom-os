# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""KEEP's capability tokens as ``act`` and ``run`` grants for the ``grant`` noun (ADR-250 D4).

A capability with a budget and a parent is a ``run`` grant; any other is an ``act`` grant. The
capability store stays the store: this adapter reads :func:`list_capabilities` and revokes with
:func:`revoke_capability`, so :func:`get_capability_by_id` refuses the token afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from axiom.governance.grants import Grant

from .capability_store import VaultContext, list_capabilities, revoke_capability

_DEFAULT: VaultContext | None = None


class CapabilityGrants:
    store = "vault"

    def __init__(self, ctx: VaultContext) -> None:
        self.ctx = ctx

    @classmethod
    def default(cls) -> CapabilityGrants:
        """The node's capability store (the ``vault`` schema, ADR-052)."""
        global _DEFAULT
        if _DEFAULT is None:
            from axiom.infra.db import session_for

            _DEFAULT = VaultContext(session_factory=lambda: session_for("vault"))
        return cls(_DEFAULT)

    def list(self) -> Iterator[Grant]:
        for t in list_capabilities(self.ctx):
            run = t.budget is not None and t.parent_capability is not None
            scope: dict[str, Any] = {
                "intent": t.intent_pattern.value,
                "resource": t.resource_pattern.value,
                "classification": t.classification_ceiling.value,
                "delegation_depth": t.delegation_depth,
            }
            if t.budget is not None:
                scope["budget"] = t.budget.as_dict()
            yield Grant(
                id=t.id,
                kind="run" if run else "act",
                principal=t.subject.handle,
                scope=scope,
                expires_at=t.not_after,
                issuer=t.issuer.handle,
                parent=t.parent_capability,
                store=self.store,
            )

    def revoke(self, grant_id: str, *, by: str, reason: str) -> dict[str, Any]:
        receipt = revoke_capability(self.ctx, grant_id, f"{reason} (by {by})")
        return {"revocation_id": receipt, "by": by, "reason": reason}


__all__ = ["CapabilityGrants"]
