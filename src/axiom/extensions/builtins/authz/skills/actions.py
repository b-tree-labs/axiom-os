# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.actions`` — query the agent action-provenance ledger (#665).

CLI shape::

    axi audit actions [--agent tidy] [--op-class git.branch.delete]
                      [--outcome proceeded|refused|failed]
                      [--since 7d] [--until 1h] [--limit 50]
                      [--verify] [--json]

Thin wrapper over ``axiom.policy.action_ledger`` — the guard-emitted
ledger of every autonomous agent action. Filters AND-compose; most
recent first. ``--verify`` checks the EC-mode HMAC chain instead of
listing records.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.policy.action_ledger import search_actions, verify_actions

    if params.get("verify"):
        out = verify_actions(state_dir=ctx.state_dir)
        return SkillResult(
            ok=bool(out["ok"]),
            value={"resource": "actions_verify", **out},
            errors=(
                [] if out["ok"]
                else [f"HMAC chain broken at record {out['broken_at']}"]
            ),
        )

    raw_limit = params.get("limit")
    limit = int(raw_limit) if raw_limit is not None else _DEFAULT_LIMIT
    if limit < 1 or limit > _MAX_LIMIT:
        return SkillResult(
            ok=False,
            errors=[f"--limit must be 1..{_MAX_LIMIT}, got {limit}"],
        )

    out = search_actions(
        agent=params.get("agent"),
        op_class=params.get("op_class"),
        outcome=params.get("outcome"),
        since=params.get("since"),
        until=params.get("until"),
        text=params.get("text"),
        limit=limit,
        state_dir=ctx.state_dir,
    )
    if out.get("error"):
        return SkillResult(ok=False, errors=[out["error"]])

    return SkillResult(
        ok=True,
        value={
            "resource": "actions",
            "backend": out["backend"],
            "count": out["count"],
            "limit": limit,
            "filters": {
                "agent": params.get("agent"),
                "op_class": params.get("op_class"),
                "outcome": params.get("outcome"),
                "since": params.get("since"),
                "until": params.get("until"),
                "text": params.get("text"),
            },
            "items": out["actions"],
        },
    )
