# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Explicit success only.

A pull request lands when every check branch protection requires reports
``success`` on the head that will merge. An empty check list is pending, not
"not failed": a required test that has not registered yet reports nothing, and
a script that read nothing as green merged a pull request before its test ran
(#1782). A required check that is missing, queued or running is pending.
``skipped`` and ``neutral`` are not ``success``. With no protection to read,
every check the head reports must be ``success``, and none reported is still
pending.

The required set comes from the branch's protection (``required_status_checks``
``contexts`` or ``checks[].context``) and from rulesets of type
``required_status_checks``. Both are read once per base branch by the effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

SUCCESS = "success"
FAILURE = "failure"
PENDING = "pending"

_FAILED = frozenset({"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"})
_ACTIVE = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"})


@dataclass(frozen=True)
class CheckVerdict:
    state: str
    required: tuple[str, ...]
    failing: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    seen: int = 0

    def reason(self) -> str:
        if self.state == SUCCESS:
            return "every required check reports success"
        if self.state == FAILURE:
            return "failing: " + ", ".join(self.failing)
        parts = []
        if self.seen == 0:
            parts.append("no check has reported yet")
        if self.missing:
            parts.append("not reported: " + ", ".join(self.missing))
        if self.pending:
            parts.append("not finished: " + ", ".join(self.pending))
        return "; ".join(parts) or "pending"

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "required": list(self.required),
            "failing": list(self.failing),
            "pending": list(self.pending),
            "missing": list(self.missing),
            "seen": self.seen,
            "reason": self.reason(),
        }


def _name(item: dict, index: int = 0) -> str:
    """A check run's ``name`` or a commit status's ``context``; a nameless row is judged by position."""
    return str(item.get("name") or item.get("context") or f"check {index + 1}")


def _state(item: dict) -> str:
    """``success``, ``failure`` or ``pending`` for one rollup row. Only SUCCESS is success."""
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if conclusion in _FAILED:
        return FAILURE
    if conclusion == "SUCCESS" and status in {"", "COMPLETED"}:
        return SUCCESS
    return PENDING


def required_contexts(protection: dict | None, rules: Sequence[dict] | None) -> tuple[str, ...]:
    """The check names a merge must satisfy, from protection and rulesets, sorted."""
    names: set[str] = set()
    checks = protection.get("required_status_checks") if isinstance(protection, dict) else None
    if isinstance(checks, dict):
        for context in checks.get("contexts") or []:
            if isinstance(context, str) and context:
                names.add(context)
        for row in checks.get("checks") or []:
            if isinstance(row, dict) and row.get("context"):
                names.add(str(row["context"]))
    for rule in rules or []:
        if not isinstance(rule, dict) or str(rule.get("type") or "") != "required_status_checks":
            continue
        parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        for row in parameters.get("required_status_checks") or []:
            if isinstance(row, dict) and row.get("context"):
                names.add(str(row["context"]))
    return tuple(sorted(names))


def check_verdict(rollup: object, required: Sequence[str]) -> CheckVerdict:
    """Reduce a head's check rollup against the required set. Explicit success only."""
    rows = [item for item in rollup if isinstance(item, dict)] if isinstance(rollup, list) else []
    required = tuple(required)
    by_name: dict[str, list[str]] = {}
    for index, item in enumerate(rows):
        by_name.setdefault(_name(item, index), []).append(_state(item))
    failing: list[str] = []
    pending: list[str] = []
    missing: list[str] = []
    names = required if required else tuple(sorted(by_name))
    for name in names:
        states = by_name.get(name)
        if not states:
            missing.append(name)
        elif FAILURE in states:
            failing.append(name)
        elif PENDING in states:
            pending.append(name)
    if failing:
        state = FAILURE
    elif pending or missing or not rows:
        state = PENDING
    else:
        state = SUCCESS
    return CheckVerdict(
        state=state,
        required=required,
        failing=tuple(failing),
        pending=tuple(pending),
        missing=tuple(missing),
        seen=len(rows),
    )
