# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``connector.status`` skill: every configured connector, and the last delivery outcomes.

Two halves. ``configured`` is what each connector family reports about the
connectors declared here, with ADR-220 §3 health (``connector.configured``); a
fresh process always has it. ``connectors`` is the last delivery outcome per
connector from the in-process status store, which a fresh process finds empty.

ADR-056 thin wrapper: the CLI verb ``axi notifications connector status``
dispatches to this function. Returns a structured table that operators
or downstream tools can render. Includes the ``reconnect_pending``
subset prominently so the friction-killing "what needs my attention"
question has a one-call answer.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.connector.status_store import (
    ConnectorStatusStore,
    get_default_store,
)
from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Return per-connector latest status + reconnect-pending subset.

    Params:
      - ``store`` (optional) — inject a ConnectorStatusStore for tests;
        otherwise the process-default in-memory store is read.
      - ``connector`` (optional) — filter to one connector by name.
    """
    store: ConnectorStatusStore = params.get("store") or get_default_store()
    one = params.get("connector")
    configured, problems = _configured(ctx)

    if one:
        outcome = store.latest(one)
        row = next((r for r in configured if r.get("connector") == one), None)
        if row is not None:
            return SkillResult(
                ok=True,
                value={
                    "connector": one,
                    "found": True,
                    "configured": row,
                    "outcome": _to_dict(outcome) if outcome is not None else None,
                },
            )
        if outcome is None:
            return SkillResult(
                ok=True,
                value={
                    "connector": one,
                    "found": False,
                    "note": (
                        f"no outcomes recorded for {one!r} — has it sent "
                        "anything yet?"
                    ),
                },
            )
        return SkillResult(
            ok=True,
            value={"connector": one, "found": True, "outcome": _to_dict(outcome)},
        )

    latest = store.all_latest()
    pending = store.reconnect_pending()
    return SkillResult(
        ok=True,
        value={
            "count": len(latest),
            "reconnect_pending_count": len(pending),
            "reconnect_pending": [_to_dict(o) for o in pending],
            "connectors": [_to_dict(o) for o in latest.values()],
            "configured": configured,
            "configured_problems": problems,
        },
    )


def _configured(ctx: SkillContext) -> tuple[list[dict[str, Any]], list[str]]:
    """Every family's configured connectors (connector.configured), for this state directory."""
    from axiom.extensions.builtins.connector.configured import configured_connectors

    state_dir = getattr(ctx, "state_dir", None)
    if not state_dir:
        from axiom.infra.paths import get_user_state_dir

        state_dir = get_user_state_dir()
    return configured_connectors(state_dir)


def _to_dict(outcome) -> dict[str, Any]:
    return {
        "connector": outcome.connector,
        "ok": outcome.ok,
        "observed_at": outcome.observed_at.isoformat(),
        "recipient": outcome.recipient,
        "status_code": outcome.status_code,
        "error": outcome.error,
        "retry_attempts": outcome.retry_attempts,
        "reconnect_required": outcome.reconnect_required,
        "message_id": outcome.message_id,
        "vendor_code": outcome.vendor_code,
    }


__all__ = ["run"]
