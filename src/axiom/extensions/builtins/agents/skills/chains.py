# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``agents.chains`` and ``agents.chain_stop``: see the chains of hand-offs in flight, and end one.

A chain is the sequence one request sets off as agents hand work to each other
(the baton, :mod:`axiom.infra.chain_ledger`). Chains may loop and may run long;
these two skills are how a person or AXI sees what is running and stops it. A
stop is a request the dispatcher honours before the chain's next hop, so the
chain ends between steps, never in the middle of one.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from axiom.infra import chain_ledger
from axiom.infra.skills import SkillContext, SkillResult


def _state_dir(ctx: SkillContext | None) -> Path:
    if ctx is not None and getattr(ctx, "state_dir", None):
        return Path(ctx.state_dir)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def chains(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    now = time.time()
    orphaned = chain_ledger.sweep(_state_dir(ctx), now)
    records = chain_ledger.active(_state_dir(ctx), now)
    include_ended = bool(params.get("all"))
    rows = [
        r.summary(now)
        for r in records
        if include_ended or r.status in (*chain_ledger.ACTIVE, "orphaned")
    ]
    rows.sort(key=lambda r: -r["running_for_s"])
    return SkillResult(
        ok=True,
        value={
            "chains": rows,
            "active": sum(1 for r in rows if r["status"] in chain_ledger.ACTIVE),
            "orphaned_now": orphaned,
        },
        actions_taken=["listed chains of hand-offs"],
    )


def chain_stop(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    chain_id = str(params.get("id") or "").strip()
    if not chain_id:
        return SkillResult(
            ok=False, errors=["name the chain to stop: id=<chain id> (agents.chains lists them)"]
        )
    who = str(params.get("by") or getattr(ctx, "actor", "") or "an operator")
    rec = chain_ledger.request_stop(
        _state_dir(ctx), chain_id, by=who, reason=str(params.get("reason") or "")
    )
    if rec is None:
        return SkillResult(ok=False, errors=[f"no chain {chain_id}"])
    if rec.status != "stopping":
        return SkillResult(
            ok=True,
            value={"chain": rec.summary()},
            actions_taken=[f"chain {chain_id} is already {rec.status}"],
        )
    return SkillResult(
        ok=True,
        value={"chain": rec.summary()},
        actions_taken=[f"asked chain {chain_id} to stop; its next hand-off will be refused"],
    )


def chain_priority(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    chain_id = str(params.get("id") or "").strip()
    priority = str(params.get("priority") or "").strip()
    if not chain_id or not priority:
        return SkillResult(
            ok=False,
            errors=[
                f"name the chain and the priority: id=<chain id> priority=<{'|'.join(chain_ledger.PRIORITIES)}>"
            ],
        )
    who = str(params.get("by") or getattr(ctx, "actor", "") or "an operator")
    try:
        rec = chain_ledger.set_priority(_state_dir(ctx), chain_id, priority, by=who)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if rec is None:
        return SkillResult(ok=False, errors=[f"no chain {chain_id}"])
    return SkillResult(
        ok=True,
        value={"chain": rec.summary()},
        actions_taken=[f"chain {chain_id} priority is {rec.priority}"],
    )


__all__ = ["chain_priority", "chain_stop", "chains"]
