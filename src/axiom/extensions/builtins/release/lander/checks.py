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

Pending has one case that never ends by itself: no check reported, no workflow
run queued or running on the head, and the head older than a grace period. No
run will ever report those checks, so waiting is not an answer. That case is
``ci_never_ran`` and it is judged here from facts the forge adapter reads.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

SUCCESS = "success"
FAILURE = "failure"
PENDING = "pending"
#: Report reasons for a head no workflow run will ever report on.
CI_NEVER_RAN = "ci_never_ran"
CI_NEVER_RAN_PERSON = "ci_never_ran_needs_person"

_FAILED = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
)
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


def _stamp(item: dict) -> str:
    """When a rollup row last changed. A run still going is the newest thing there is."""
    status = str(item.get("status") or "").upper()
    if status in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"}:
        return "9999"
    return str(item.get("completedAt") or item.get("startedAt") or "")


def latest_runs(rows: object) -> list[dict]:
    """Each check's latest run on the head, in first-seen order.

    Marking a draft ready, or repeating a run, starts a new run of the same workflow on
    the same commit and cancels the first, and the rollup keeps both. A check is judged by
    its latest run: the forge calls that pull request CLEAN, and counting the cancelled
    first run as a failure kept every such pull request from landing. The same check name
    in two workflows is two checks. A later row wins a tie.
    """
    if not isinstance(rows, list):
        return []
    best: dict[tuple[str, str], tuple[str, int, dict]] = {}
    order: list[tuple[str, str]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            continue
        key = (str(item.get("workflowName") or ""), _name(item, index))
        stamp = (_stamp(item), index)
        if key not in best:
            order.append(key)
        elif stamp < (best[key][0], best[key][1]):
            continue
        best[key] = (stamp[0], index, item)
    return [best[key][2] for key in order]


def _state(item: dict) -> str:
    """``success``, ``failure`` or ``pending`` for one rollup row. Only SUCCESS is success."""
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if conclusion in _FAILED:
        return FAILURE
    if conclusion == "SUCCESS" and status in {"", "COMPLETED"}:
        return SUCCESS
    # A completed, skipped job is how a path-filtered required check reports; GitHub's
    # merge button counts it as passing, and so does RIVET (#2147). Neutral does not.
    if conclusion == "SKIPPED" and status == "COMPLETED":
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


_SELF_CHECK = re.compile(r"^\s*rivet\b", re.IGNORECASE)


def is_self_check(item: dict) -> bool:
    """RIVET's own job or hold check. It must not decide the pull request it reports on (#2147)."""
    return any(
        _SELF_CHECK.match(str(item.get(key) or "")) for key in ("name", "context", "workflowName")
    )


def check_verdict(rollup: object, required: Sequence[str]) -> CheckVerdict:
    """Reduce a head's check rollup against the required set. Explicit success only.

    RIVET's own checks are left out unless the branch requires one by name: with
    protection unreadable, its neutral hold check once kept every pull request pending.
    """
    required = tuple(required)
    rows = latest_runs(rollup)
    rows = [item for item in rows if not (is_self_check(item) and _name(item) not in required)]
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


def active_runs(rows: object) -> int | None:
    """Workflow runs on a head that have not finished, RIVET's own left out. None when unreadable."""
    if not isinstance(rows, list):
        return None
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and not is_self_check(row)
        and str(row.get("status") or "").lower() != "completed"
    )


def ci_never_ran(
    verdict: CheckVerdict,
    *,
    active_runs: int | None,
    head_age_s: float | None,
    grace_s: float,
) -> bool:
    """True when nothing reported, nothing is running, and the head is past the grace period.

    A fact that could not be read (``None``) keeps the pull request pending: this
    reason leads to an action, so it is claimed only on evidence.
    """
    if verdict.state != PENDING or verdict.seen or verdict.pending or verdict.failing:
        return False
    if active_runs is None or head_age_s is None:
        return False
    return active_runs == 0 and head_age_s > grace_s
