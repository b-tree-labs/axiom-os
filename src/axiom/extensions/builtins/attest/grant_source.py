# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signing grants as ``sign`` grants, and reviewer grants as ``read`` grants, for the ``grant``
noun (ADR-250 D4, RW-G1; #1817).

A signing grant (ADR-146) signs one digest once and lives at most a minute. The attest store
records each one minted (``attest_grant_issues``), each one used (``attest_grant_uses``) and
each one withdrawn (``attest_grant_revocations``). A live grant is minted, unexpired, unused
and not withdrawn. A revoke writes a withdrawal, and :func:`service.sign` refuses the grant.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from axiom.governance.grants import Grant


class SigningGrants:
    store = "attest"

    def list(self) -> Iterator[Grant]:
        from . import store
        from .db_models import AttestGrantIssue, AttestGrantRevocation, AttestGrantUse

        now = datetime.now(UTC)
        try:
            with store.session_scope() as s:
                used = select(AttestGrantUse.grant_id)
                withdrawn = select(AttestGrantRevocation.grant_id)
                rows = list(
                    s.execute(
                        select(AttestGrantIssue)
                        .where(AttestGrantIssue.expires_at > now)
                        .where(AttestGrantIssue.grant_id.not_in(used))
                        .where(AttestGrantIssue.grant_id.not_in(withdrawn))
                        .order_by(AttestGrantIssue.expires_at)
                    ).scalars()
                )
                out = [
                    Grant(
                        id=r.grant_id,
                        kind="sign",
                        principal=r.principal,
                        scope={
                            "digest": r.digest,
                            "logbook": r.logbook,
                            "meaning": r.meaning,
                            "presentation_id": r.presentation_id,
                            "where": f"site:{r.site_id}",
                        },
                        expires_at=r.expires_at,
                        issuer=r.issuer,
                        parent=None,
                        store=self.store,
                    )
                    for r in rows
                ]
        except Exception:  # noqa: BLE001 - an attest store that cannot be read lists nothing
            return
        yield from out

    def revoke(self, grant_id: str, *, by: str, reason: str) -> dict[str, Any]:
        from . import store
        from .db_models import AttestGrantRevocation

        at = datetime.now(UTC)
        with store.session_scope() as s:
            if s.get(AttestGrantRevocation, grant_id) is None:
                s.add(AttestGrantRevocation(grant_id=grant_id, revoked_by=by, reason=reason,
                                            revoked_at=at))  # fmt: skip
                s.commit()
        return {"revoked_at": at.isoformat(), "by": by, "reason": reason}


class ReviewerGrants:
    """Reviewer grants as ``read`` grants (#1817): ADR-205's ``(principal, role, where,
    expires_at)`` with its issuer, one row per log role ``reviewer`` in the role table. A live
    grant is an unexpired row; a revoke removes the row and records who did it, and the one
    check (``doors.rule``) refuses the next read."""

    store = "attest"

    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir

    def _dir(self) -> Path:
        from .roles import default_state_dir

        return Path(self._state_dir) if self._state_dir is not None else default_state_dir()

    @staticmethod
    def grant_id(principal: str, site: str, logbook: str) -> str:
        key = "\x1f".join((principal, site, logbook, "reviewer"))
        return "rv_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def list(self) -> Iterator[Grant]:
        from .roles import REVIEWER, _load_log_roles, parse_until

        try:
            rows = [r for r in _load_log_roles(self._dir()) if r["role"] == REVIEWER]
        except Exception:  # noqa: BLE001 - a role table that cannot be read lists nothing
            return
        for r in rows:
            try:
                ends = parse_until(r["until"])
            except ValueError:
                continue  # an expiry nobody can read is no grant
            yield Grant(
                id=self.grant_id(r["principal"], r["site"], r["logbook"]),
                kind="read",
                principal=r["principal"],
                scope={"role": REVIEWER, "where": f"site:{r['site']}", "logbook": r["logbook"]},
                expires_at=ends,
                issuer=r.get("issuer"),
                parent=None,
                store=self.store,
            )

    def revoke(self, grant_id: str, *, by: str, reason: str) -> dict[str, Any]:
        from . import roles

        for g in self.list():
            if g.id == grant_id:
                site = str(g.scope["where"]).removeprefix("site:")
                roles.revoke(g.principal, site, roles.REVIEWER, by=by, state_dir=self._dir(),
                             logbook=g.scope["logbook"])  # fmt: skip
                return {"revoked_at": datetime.now(UTC).isoformat(), "by": by, "reason": reason}
        raise LookupError(f"no reviewer grant {grant_id}")


__all__ = ["ReviewerGrants", "SigningGrants"]
