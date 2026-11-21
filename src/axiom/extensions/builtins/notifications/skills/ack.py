# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`notifications ack` — record that someone took responsibility for an alert.

The `acknowledged_at` column has always existed and nothing ever wrote it, so
an alert could be delivered, read, and acted on with no record that anyone
picked it up. For a monitor that matters more than delivery does: "did somebody
see this" is the question asked after an incident, and an inbox that cannot
answer it is a log, not a duty roster.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    row_id = (params.get("id") or "").strip()
    if not row_id:
        return SkillResult(ok=False, errors=["missing required param: id"])

    recipient = (params.get("recipient") or "").strip() or _default_recipient()
    if not recipient:
        return SkillResult(
            ok=False,
            errors=[
                "cannot determine who is acknowledging — pass --recipient "
                "with the principal the alert was addressed to"
            ],
        )

    from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore

    store = DatabaseInboxStore()
    try:
        row_id = _resolve(store, row_id, recipient)
    except LookupError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    try:
        acknowledged = store.acknowledge(row_id=row_id, by=recipient)
    except PermissionError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])

    return SkillResult(
        ok=True,
        value={
            "id": row_id,
            "acknowledged_by": recipient,
            # False means it was ALREADY acknowledged — not a failure, and the
            # original timestamp stands.
            "newly_acknowledged": acknowledged,
        },
    )


def _default_recipient() -> str:
    """Whoever this machine is, so the common case needs no flag."""
    try:
        from axiom.infra.orchestrator.session import _default_principal

        return _default_principal()
    except Exception:  # noqa: BLE001
        return ""


def _resolve(store, prefix: str, recipient: str) -> str:
    """Accept the SHORT id an alert displays, not just the full uuid.

    The transcript shows eight characters because a full uuid is unreadable in
    a conversation; requiring the full one to acknowledge would mean going to
    look it up, which is exactly the friction that stops people acknowledging.
    """
    from axiom.extensions.builtins.notifications.inbox import InboxQuery

    rows = store.query(InboxQuery(recipient=recipient, limit=500))
    matches = [r for r in rows if r.id == prefix or r.id.startswith(prefix)]
    if not matches:
        raise LookupError(
            f"no alert for {recipient} starting {prefix!r} — "
            "`notifications list --unread` shows what is waiting"
        )
    if len(matches) > 1:
        raise LookupError(
            f"{prefix!r} matches {len(matches)} alerts; use more characters"
        )
    return matches[0].id
