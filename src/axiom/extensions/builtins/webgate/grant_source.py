# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gate's keys as ``read`` grants for the ``grant`` noun (ADR-250 D4, RW-G1).

A key is what a key may read: its roles (scopes) and the site it is held to. The keys file
stays the store; this adapter only reads it, and its revoke is ``gate revoke`` itself, so the
key store's own check (:meth:`JsonFileApiKeyStore.resolve`) refuses the key on its next use.

A key may record an ``issuer`` and an ``expires_at`` (#1817, ADR-205's shape); a key that
records neither projects both as ``None``. An expired key is refused by the store's own check.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from axiom.governance.grants import Grant
from axiom.webauth.api_keys import _expiry, load_api_key_records


class ApiKeyGrants:
    store = "gate-keys"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> ApiKeyGrants | None:
        from .skills._accounts import resolve_keys_path

        path = resolve_keys_path(params)
        return cls(path) if path is not None else None

    def list(self) -> Iterator[Grant]:
        if not self.path.is_file():
            return
        for r in load_api_key_records(self.path):
            if r.get("revoked_at"):
                continue
            yield Grant(
                id=r["key_id"],
                kind="read",
                principal=r["principal"],
                scope={
                    "roles": list(r.get("scopes") or []),
                    "where": f"site:{r['site']}" if r.get("site") else None,
                    "name": r.get("name") or "",
                },
                expires_at=_expiry(r.get("expires_at")),
                issuer=r.get("issuer") or None,
                parent=None,
                store=self.store,
            )

    def revoke(self, grant_id: str, *, by: str, reason: str) -> dict[str, Any]:
        from axiom.infra.skills import SkillContext, SkillRegistry

        from .skills import revoke_key

        ctx = SkillContext(
            registry=SkillRegistry(), state_dir=self.path.parent, logger=logging.getLogger(__name__)
        )
        result = revoke_key.run({"key_id": grant_id, "keys_file": str(self.path)}, ctx)
        if not result.ok:
            raise RuntimeError("; ".join(result.errors) or f"could not revoke {grant_id}")
        return {"revoked_at": result.value["revoked_at"], "by": by, "reason": reason}


__all__ = ["ApiKeyGrants"]
