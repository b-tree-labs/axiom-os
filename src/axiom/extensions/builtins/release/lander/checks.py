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
from datetime import datetime

SUCCESS = "success"
FAILURE = "failure"
PENDING = "pending"
#: Report reasons for a head no workflow run will ever report on.
CI_NEVER_RAN = "ci_never_ran"
CI_NEVER_RAN_PERSON = "ci_never_ran_needs_person"
#: ``OpenPull.checks`` when a required check has no run that counts: its own state, not pending.
NOT_RUN = "not_run"
#: Why a required check has no run that counts. A draft's run skips the suite and reports
#: success, so a run that started before the last ready-for-review event is not evidence.
CAUSE_DRAFT = "draft"
CAUSE_DRAFT_RUN_ONLY = "draft_run_only"
CAUSE_NEVER_REPORTED = "never_reported"
#: A head labelled ``ci:full`` has no full run on it: the shard jobs only exist in a run that took
#: main's path, so a narrowed run's green is not evidence (#2441).
CAUSE_FULL_RUN_MISSING = "full_run_missing"
#: The label that asks for main's full path on a pull request. ``scripts/ci_full_paths.json`` names
#: the same string; a test holds them together.
FULL_LABEL = "ci:full"
#: The check names only a full run reports: ``Unit Shard (1)`` and so on. A narrowed run reports
#: one SKIPPED row named ``Unit Shard`` with no number, which is why the number is required.
_SHARD = re.compile(r"^Unit Shard \(\d+\)$")
#: Reason codes in a report are ``tests_not_run:<cause>``.
TESTS_NOT_RUN = "tests_not_run"

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
    #: Required checks with no run that counts, and why (``CAUSE_*``). Empty when every
    #: required check has a real run on this head after it was ready for review.
    not_run: tuple[str, ...] = ()
    not_run_cause: str = ""
    #: The pull request's ready-for-review time could not be read, so nothing is concluded.
    unreadable: bool = False

    def reason(self) -> str:
        if self.state == SUCCESS:
            return "every required check reports success"
        if self.state == FAILURE:
            return "failing: " + ", ".join(self.failing)
        if self.unreadable:
            return "when the pull request was marked ready could not be read, so no run is counted"
        if self.not_run and self.not_run_cause != CAUSE_NEVER_REPORTED:
            return not_run_sentence(self.not_run, self.not_run_cause)
        parts = ["tests: not run"] if self.not_run else []
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
            "not_run": list(self.not_run),
            "not_run_cause": self.not_run_cause,
            "unreadable": self.unreadable,
            "reason": self.reason(),
        }


def not_run_sentence(names: Sequence[str], cause: str, *, head: str = "", ready_at: str = "") -> str:
    """The plain words for "tests: not run". One place, so the report, the note and the hold agree."""
    shown = list(names)[:5]
    listed = ", ".join(shown) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    if cause == CAUSE_NEVER_REPORTED and not names:
        where = f" on head {head[:7]}" if head else ""
        return (
            f"tests: not run. No check has reported{where}: a path filter, a workflow that did "
            "not fire, or a run waiting for approval."
        )
    listed = listed or "its required checks"
    if cause == CAUSE_FULL_RUN_MISSING:
        where = f" on head {head[:7]}" if head else ""
        return (
            f"tests: not run. It carries the {FULL_LABEL} label, which asks for main's full path, "
            f"and no full run (the Unit Shard checks) has reported{where}. A narrowed run's green "
            "does not count for it."
        )
    if cause == CAUSE_DRAFT:
        return (
            f"tests: not run. It is a draft, and a draft's run does not run the suite, so its "
            f"green checks ({listed}) are not evidence."
        )
    if cause == CAUSE_DRAFT_RUN_ONLY:
        when = f" ({ready_at})" if ready_at else ""
        return (
            f"tests: not run. {listed} last ran while the pull request was a draft, before it "
            f"was marked ready{when}. A draft's run skips the suite and reports success, so "
            "that green does not count."
        )
    where = f" on head {head[:7]}" if head else ""
    return (
        f"tests: not run. {listed} never ran{where}: a path filter, a workflow that did not "
        "fire, or a run waiting for approval."
    )


def not_run_fix(cause: str, number: int | str) -> str:
    """The exact action that starts the missing run."""
    start = (
        f"RIVET closes and reopens the pull request once when no run starts (ci_start); by hand: "
        f"`gh pr close {number} && gh pr reopen {number}`. If a run is waiting for approval, "
        "approve it in the Actions tab."
    )
    if cause == CAUSE_FULL_RUN_MISSING:
        return (
            f"Start the full run: `gh pr edit {number} --remove-label {FULL_LABEL} && "
            f"gh pr edit {number} --add-label {FULL_LABEL}` (the label change restarts CI on this "
            "head). RIVET lands it only after the Unit Shard checks are green on the current head."
        )
    if cause == CAUSE_DRAFT:
        return "Mark it ready for review. The run that starts then is the one that counts, and RIVET lands it only after that run passes."
    if cause == CAUSE_DRAFT_RUN_ONLY:
        return "Wait for the run that starts when it was marked ready; it is the one that counts. " + start
    return "Start the required checks. " + start


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


def _started(item: dict) -> datetime | None:
    value = item.get("startedAt") or item.get("completedAt")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _counts_after(items: Sequence[dict], ready_at: datetime) -> bool:
    """True when some run in ``items`` started at or after ``ready_at``. A row with no
    readable start time cannot be shown to be later, so it does not count."""
    for item in items:
        started = _started(item)
        if started is not None and started.tzinfo is not None and started >= ready_at:
            return True
    return False


def check_verdict(
    rollup: object,
    required: Sequence[str],
    *,
    ready_at: datetime | None = None,
    ready_known: bool = True,
    draft: bool = False,
    labels: Sequence[str] = (),
) -> CheckVerdict:
    """Reduce a head's check rollup against the required set. Explicit success only.

    RIVET's own checks are left out unless the branch requires one by name: with
    protection unreadable, its neutral hold check once kept every pull request pending.

    Evidence (#2410, #2354). A required check counts only with a run that completed on this
    head after the pull request was last marked ready for review (``ready_at``; None means it
    was never a draft). A draft's run is green without running the suite, and that green stays
    attached to the head after the pull request is marked ready. A required check with no run
    at all, or only a draft-time run, or any check on a draft, is ``not_run``: its own state,
    never success and never plain pending. ``ready_known=False`` means the time could not be
    read; nothing is then concluded.

    Labels (#2441). A head labelled ``ci:full`` also needs the evidence only a full run gives: the
    ``Unit Shard (n)`` checks, all success, on this head. None at all is ``not_run`` with its own
    cause; one still going is pending; a red one is failing. A head without the label is judged
    exactly as before.
    """
    required = tuple(required)
    rows = latest_runs(rollup)
    rows = [item for item in rows if not (is_self_check(item) and _name(item) not in required)]
    by_name: dict[str, list[str]] = {}
    rows_by_name: dict[str, list[dict]] = {}
    for index, item in enumerate(rows):
        name = _name(item, index)
        by_name.setdefault(name, []).append(_state(item))
        rows_by_name.setdefault(name, []).append(item)
    failing: list[str] = []
    pending: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    names = required if required else tuple(sorted(by_name))
    for name in names:
        states = by_name.get(name)
        if not states:
            missing.append(name)
        elif FAILURE in states:
            failing.append(name)
        elif PENDING in states:
            pending.append(name)
        elif ready_at is not None and not _counts_after(rows_by_name[name], ready_at):
            stale.append(name)
    full_wanted = any(str(label).casefold() == FULL_LABEL for label in labels)
    full_missing = False
    if full_wanted:
        shards = [(_name(item), _state(item)) for item in rows if _SHARD.match(_name(item))]
        if not shards:
            full_missing = True
        for name, state in shards:
            if state == FAILURE and name not in failing:
                failing.append(name)
            elif state == PENDING and name not in pending:
                pending.append(name)
    not_run: list[str] = []
    cause = ""
    unreadable = False
    if not failing:
        if draft:
            not_run, cause = list(names), CAUSE_DRAFT
        elif not ready_known and not (pending or missing):
            unreadable = True
        elif stale:
            not_run, cause = stale + missing, CAUSE_DRAFT_RUN_ONLY
        elif missing:
            not_run, cause = list(missing), CAUSE_NEVER_REPORTED
        elif full_missing:
            not_run, cause = ["Unit Shard"], CAUSE_FULL_RUN_MISSING
    if failing:
        state = FAILURE
    elif pending or missing or not_run or unreadable or not rows:
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
        not_run=tuple(not_run),
        not_run_cause=cause,
        unreadable=unreadable,
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


def tests_never_started(
    verdict: CheckVerdict,
    *,
    active_runs: int | None,
    since_age_s: float | None,
    grace_s: float,
) -> bool:
    """True when required checks have no counting run, nothing is running, and the wait is over.

    ``since_age_s`` is the age of the later of the head and the ready-for-review event: the
    earliest moment the run that counts could have started. Unreadable facts claim nothing.
    """
    if not verdict.not_run or verdict.not_run_cause == CAUSE_DRAFT:
        return False
    if verdict.failing or verdict.pending:
        return False
    if active_runs is None or since_age_s is None:
        return False
    return active_runs == 0 and since_age_s > grace_s
