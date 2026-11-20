# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``journal.query``, ``journal.lanes`` and ``journal.verify``: thin wrappers over the service,
each building the caller from the skill context so the one rights check runs on every surface."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import rights, service


def _fail(message: str) -> SkillResult:
    return SkillResult(ok=False, value=None, errors=[message])


def query(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    caller = rights.caller_from_ctx(ctx)
    try:
        value = service.query(
            caller,
            lanes=params.get("lanes") or params.get("lane"),
            subjects=params.get("subjects") or params.get("subject_id"),
            subject=params.get("subject") or None,
            since=params.get("since"),
            until=params.get("until"),
            limit=int(params.get("limit") or 50),
            cursor=params.get("cursor"),
            mine=params.get("mine") or False,
            agent=params.get("agent") or None,
            levels=params.get("levels") or params.get("level") or None,
        )
    except ValueError as exc:
        return _fail(str(exc))
    return SkillResult(ok=True, value=value)


def lanes(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    caller = rights.caller_from_ctx(ctx)
    return SkillResult(ok=True, value=service.lanes_report(caller))


def verify(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    report = service.verify(limit=int(params.get("limit") or 5000),
                            state_dir=getattr(ctx, "state_dir", None))  # fmt: skip
    if report["ok"]:
        return SkillResult(ok=True, value=report)
    errors = [
        f"journal row {m['seq']} ({m['lane']}) does not match its record: {m['reason']}"
        for m in report["mismatches"]
    ]
    return SkillResult(ok=False, value=report, errors=errors)
