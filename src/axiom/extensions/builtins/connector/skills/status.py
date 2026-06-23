# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.connector_status`` skill — read the connector status store.

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

    if one:
        outcome = store.latest(one)
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
        },
    )


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
