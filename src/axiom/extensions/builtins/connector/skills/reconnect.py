# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.connector_reconnect`` skill — guide an operator
through reconnecting a channel that returned ``ReconnectRequired``.

ADR-056 thin wrapper. v0 reports what's pending + the suggested next
action (re-run the add wizard with ``--reconnect``). The full inline
re-OAuth flow lands once the ``connector_add`` wizard (parallel work
in flight) ships. Until then, this skill is the operator-friendly
"what should I do next" surface that the agent-bus → HERALD bridge
already routes to inbox + Slack + SMS via the default
``*.reconnect_required`` rule.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.connector.status_store import (
    ConnectorStatusStore,
    get_default_store,
)
from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Resolve the reconnect surface.

    Params:
      - ``store`` (optional) — ConnectorStatusStore for tests.
      - ``connector`` (optional) — reconnect a specific connector by name;
        when absent, surface every connector with reconnect pending.
    """
    store: ConnectorStatusStore = params.get("store") or get_default_store()
    one = params.get("connector")

    if one is None:
        pending = store.reconnect_pending()
        if not pending:
            return SkillResult(
                ok=True,
                value={
                    "reconnect_pending_count": 0,
                    "note": "no connectors require reconnect right now",
                },
            )
        return SkillResult(
            ok=True,
            value={
                "reconnect_pending_count": len(pending),
                "pending": [_action_row(p.connector, p) for p in pending],
            },
        )

    outcome = store.latest(one)
    if outcome is None:
        return SkillResult(
            ok=False,
            errors=[
                f"no outcomes recorded for {one!r}; nothing to reconnect"
            ],
        )
    if not outcome.reconnect_required:
        return SkillResult(
            ok=True,
            value={
                "connector": one,
                "needs_reconnect": False,
                "note": f"{one} is healthy as of {outcome.observed_at.isoformat()}",
            },
        )
    return SkillResult(
        ok=True,
        value=_action_row(one, outcome),
    )


def _action_row(connector: str, outcome) -> dict[str, Any]:
    """One actionable row the operator can paste into the next command."""
    return {
        "connector": connector,
        "needs_reconnect": True,
        "last_observed_at": outcome.observed_at.isoformat(),
        "last_error": outcome.error,
        "last_status_code": outcome.status_code,
        "next_action": (
            f"axi notifications connector add {connector} --reconnect"
        ),
        "note": (
            "Re-running `connector add` with --reconnect re-prompts for "
            "the credential and rotates the registered provider in place. "
            "(Wizard ships in feat/herald-connector-add-wizard; until then, "
            "rotate the secret via `axi secrets` and re-register manually.)"
        ),
    }


__all__ = ["run"]
