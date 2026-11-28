# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A thin landing queue: a pure, deterministic state machine (#2399, slice 1).

It tests the merged state, serializes landings (a concurrency limit), removes a
failing candidate in order with a reason code, and has a cost-labelled hotfix lane.
It does no network and no file access, and it imports no forge: the caller feeds
it facts (the release lock, the owner pause, a test result) and reads its events.
Batching and bisection are only planned here (``plan_batch``, ``bisect_plan``): the
plan is data for the shadow record. Nothing tests a batch or removes a member yet; that
waits for the owner's decisions on the replay's cost report.

A candidate carries only a number, two shas and a lane. A title, a body or a
branch name is never an input, so pull request text cannot change the order.
Fail closed: a green result with no tests run leaves the queue as ``no_tests_run``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

LANES = ("normal", "hotfix")
MODES = ("off", "shadow", "on")
#: Why a candidate leaves the queue, or waits in it.
REASONS = (
    "conflict",
    "red_check",
    "stale_base",
    "hold",
    "hotfix_ahead",
    "release_lock",
    "owner_pause",
    "superseded",
    "no_tests_run",
)
#: Reasons a caller may give for a failed result or an outside removal.
_LEAVE_REASONS = ("conflict", "red_check", "stale_base", "hold", "no_tests_run")


@dataclass(frozen=True)
class Candidate:
    pr: int
    head_sha: str
    base_sha: str
    lane: str = "normal"

    def __post_init__(self) -> None:
        if self.lane not in LANES:
            raise ValueError("unknown lane")


@dataclass(frozen=True)
class QueueConfig:
    concurrency: int = 1
    mode: str = "off"

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.mode not in MODES:
            raise ValueError("unknown queue mode")


@dataclass(frozen=True)
class Event:
    """One append-only record. No clock, so the same inputs give the same list."""

    seq: int
    kind: str
    pr: int
    head_sha: str = ""
    reason: str = ""
    base_sha: str = ""
    #: PRs whose merged work this candidate was tested on top of.
    ahead: tuple[int, ...] = ()
    #: Hotfix cost label: how many candidates it forced to be tested again.
    invalidated: int = 0
    detail: str = ""


@dataclass
class _Entry:
    candidate: Candidate
    status: str = "queued"  # queued | testing | green
    ahead: tuple[int, ...] = ()
    held_for: str = ""


@dataclass
class LandingQueue:
    config: QueueConfig
    base_sha: str
    events: list[Event] = field(default_factory=list)
    _entries: list[_Entry] = field(default_factory=list)

    # -- reading ---------------------------------------------------------------

    def order(self) -> list[Candidate]:
        return [entry.candidate for entry in self._entries]

    def _emit(self, kind: str, entry: _Entry | Candidate, **fields: object) -> Event:
        candidate = entry.candidate if isinstance(entry, _Entry) else entry
        event = Event(
            seq=len(self.events),
            kind=kind,
            pr=candidate.pr,
            head_sha=candidate.head_sha,
            **fields,  # type: ignore[arg-type]
        )
        self.events.append(event)
        return event

    def _find(self, pr: int) -> _Entry | None:
        return next((e for e in self._entries if e.candidate.pr == pr), None)

    # -- changing --------------------------------------------------------------

    def enqueue(self, candidate: Candidate) -> None:
        existing = self._find(candidate.pr)
        if existing is not None:
            if existing.candidate.head_sha == candidate.head_sha:
                return
            self._remove(existing, "superseded")
        entry = _Entry(candidate)
        if candidate.lane == "hotfix":
            index = 0
            while index < len(self._entries) and self._entries[index].candidate.lane == "hotfix":
                index += 1
            # Work already tested without the hotfix is stale: it is tested again.
            stale = [e for e in self._entries[index:] if e.status != "queued"]
            self._entries.insert(index, entry)
            self._emit("enqueued", entry, base_sha=self.base_sha, detail="hotfix")
            for behind in stale:
                self._reset(behind, "hotfix_ahead")
            self._emit("hotfix_jump", entry, invalidated=len(stale))
        else:
            self._entries.append(entry)
            self._emit("enqueued", entry, base_sha=self.base_sha)

    def _reset(self, entry: _Entry, reason: str) -> None:
        entry.status = "queued"
        entry.ahead = ()
        self._emit("retest", entry, reason=reason)

    def _remove(self, entry: _Entry, reason: str) -> None:
        index = self._entries.index(entry)
        self._entries.pop(index)
        self._emit("dequeued", entry, reason=reason)
        # Candidates behind were tested on top of this one: test them again.
        for behind in self._entries[index:]:
            if behind.status != "queued" and entry.candidate.pr in behind.ahead:
                self._reset(behind, reason)

    def remove(self, pr: int, reason: str) -> None:
        """An outside fact (a conflict, a hold) takes a candidate out, with its code."""
        if reason not in _LEAVE_REASONS:
            raise ValueError("unknown reason")
        entry = self._find(pr)
        if entry is not None:
            self._remove(entry, reason)

    def next_to_test(
        self, *, release_lock: bool = False, owner_pause: bool = False
    ) -> list[Candidate]:
        """Start tests for the next candidates, up to the concurrency limit.

        A held release lock or an owner pause starts nothing. Each waiting
        candidate gets one ``held`` event with the code, and stays queued.
        """
        block = "release_lock" if release_lock else "owner_pause" if owner_pause else ""
        if block:
            for entry in self._entries:
                if entry.status == "queued" and entry.held_for != block:
                    entry.held_for = block
                    self._emit("held", entry, reason=block)
            return []
        started: list[Candidate] = []
        busy = sum(1 for e in self._entries if e.status != "queued")
        stack: list[int] = []
        for entry in self._entries:
            if entry.status == "queued":
                if busy + len(started) >= self.config.concurrency:
                    break
                entry.status = "testing"
                entry.held_for = ""
                entry.ahead = tuple(stack)
                self._emit("testing", entry, base_sha=self.base_sha, ahead=entry.ahead)
                started.append(entry.candidate)
            stack.append(entry.candidate.pr)
        return started

    def report(
        self,
        pr: int,
        head_sha: str,
        *,
        passed: bool,
        tests_run: int,
        reason: str = "red_check",
        merged_sha: str = "",
    ) -> None:
        """A test result. Green lands in order; red leaves with a code."""
        entry = self._find(pr)
        if entry is None or entry.candidate.head_sha != head_sha:
            raise ValueError("result for a candidate that is not in the queue at that head")
        if entry.status == "queued":
            raise ValueError("result for a candidate that was not being tested")
        if passed and tests_run <= 0:
            passed, reason = False, "no_tests_run"
        if not passed:
            if reason not in _LEAVE_REASONS:
                raise ValueError("unknown reason")
            self._remove(entry, reason)
            return
        entry.status = "green"
        entry.held_for = ""
        self._land_front(merged_sha)

    def _land_front(self, merged_sha: str) -> None:
        while self._entries and self._entries[0].status == "green":
            entry = self._entries.pop(0)
            if merged_sha:
                self.base_sha = merged_sha
            self._emit("landed", entry, base_sha=self.base_sha)
            merged_sha = ""


def candidates_from_pulls(
    pulls: Iterable[object],
    *,
    base_sha: str,
    lanes: Mapping[int, str] | None = None,
) -> list[Candidate]:
    """Build candidates from pull requests, reading only number and head sha.

    The lane comes from the caller's own mapping and nowhere else.
    """
    chosen = lanes or {}
    return [
        Candidate(
            pr=int(pull.number),  # type: ignore[attr-defined]
            head_sha=str(pull.head_sha),  # type: ignore[attr-defined]
            base_sha=base_sha,
            lane=chosen.get(int(pull.number), "normal"),  # type: ignore[attr-defined]
        )
        for pull in pulls
    ]


#: The branch RIVET would push; ci.yml's `push` trigger lists `rivet-queue/**`.
QUEUE_BRANCH_PREFIX = "rivet-queue/"
#: Extra runs a red batch may cost before the rest go back (owner decision, #2399).
MAX_BISECT_EXTRA_RUNS = 3


@dataclass(frozen=True)
class BatchPlan:
    """The merged state a batch would be tested as, described and never created.

    ``ref`` is the BRANCH name ``rivet-queue/<n>``: a push workflow fires for a branch
    that matches ci.yml's filter and for nothing outside refs/heads and refs/tags.
    """

    ref: str
    base_sha: str
    members: tuple[int, ...]
    head_shas: tuple[str, ...]
    #: Why the plan has no members, or empty.
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "base_sha": self.base_sha,
            "members": list(self.members),
            "head_shas": list(self.head_shas),
            "created": False,
            "reason": self.reason,
            "describes": "base_sha with each member's head merged in this order",
        }


def plan_batch(candidates: Iterable[Candidate], *, base_sha: str, size: int) -> BatchPlan:
    """The first ``size`` candidates, in the order given, on one throwaway ref.

    An empty ``base_sha`` is unknown: fail closed with no batch. The ref is named
    after the first member's number, so the same input names the same ref.
    """
    if size < 1:
        raise ValueError("batch size must be at least 1")
    ordered = list(candidates)
    if not base_sha:
        return BatchPlan("", "", (), (), reason="base_unknown")
    # A hotfix leads, whatever order the caller gave the rest.
    ordered.sort(key=lambda c: 0 if c.lane == "hotfix" else 1)
    chosen = ordered[:size]
    if not chosen:
        return BatchPlan("", base_sha, (), (), reason="nothing_ready")
    return BatchPlan(
        ref=f"{QUEUE_BRANCH_PREFIX}{chosen[0].pr}",
        base_sha=base_sha,
        members=tuple(c.pr for c in chosen),
        head_shas=tuple(c.head_sha for c in chosen),
    )


def bisect_plan(members: Iterable[int]) -> dict:
    """What would be tested, in order, if the whole batch came back red.

    Halve: test the first half alone; red narrows into it, green lands it and the
    rest is tested on top. A single member that is red is the culprit and leaves with
    ``red_check``. When the last two are red together and the first passes, the
    second is the culprit by elimination, with no extra run. For one culprit this
    needs at most ``2 * ceil(log2 n) - 1`` extra runs. A member whose own checks were
    already red, or that conflicts, never joins a batch, so costs no run.
    """
    ordered = tuple(members)
    steps: list[dict] = []

    def walk(group: tuple[int, ...]) -> None:
        if len(group) <= 1:
            return
        half = (len(group) + 1) // 2
        steps.append({"test": list(group[:half]), "if_green": "land", "if_red": "narrow"})
        walk(group[:half])
        walk(group[half:])

    walk(ordered)
    n = len(ordered)
    worst = 0 if n <= 1 else 2 * (n - 1).bit_length() - 1
    capped = min(worst, MAX_BISECT_EXTRA_RUNS)
    return {
        "members": list(ordered),
        "steps": steps,
        "worst_case_extra_runs": worst,
        "max_extra_runs": MAX_BISECT_EXTRA_RUNS,
        "extra_runs_if_capped": capped,
        "after_cap": "members still unresolved go back to the lander, none is blamed",
        "infrastructure_failure": (
            "never blamed on a member: with no named failing test the run is re-run once "
            "(the rule of #2454); a second infrastructure failure holds the batch and reports"
        ),
        "removal_reason": "red_check",
    }


def describe_queue_branch(plan: BatchPlan, merged_state: dict[int, str] | None = None) -> dict:
    """The branch RIVET WOULD push for ``plan``, and the run it would expect. Never pushes.

    ``sha`` is known only for a single member, whose forge merge commit is the merged
    state; for several members it is the result of merging them in order on ``base_sha``,
    which does not exist until the push, so it is left empty and named as such.
    """
    merged_state = merged_state or {}
    single = plan.members[0] if len(plan.members) == 1 else None
    sha = merged_state.get(single, "") if single is not None else ""
    return {
        "branch": plan.ref,
        "ref": f"refs/heads/{plan.ref}" if plan.ref else "",
        "base_sha": plan.base_sha,
        "members": list(plan.members),
        "head_shas": list(plan.head_shas),
        "sha": sha,
        "sha_basis": (
            "forge merge commit of the single member"
            if sha
            else "merge of the members in order on base_sha; exists only after the push"
        ),
        "trigger": {
            "workflow": "ci.yml",
            "event": "push",
            "branches_filter": f"{QUEUE_BRANCH_PREFIX}**",
        },
        "expected_run": {
            "path": "full (same jobs as main, sharded unit suite included)",
            "writes_main_caches": False,
            "publishes": False,
            "concurrency": "own group per branch, cancel-in-progress within the branch",
            "on_green": "main push of an identical tree skips its heavy jobs (tree match)",
        },
        "created": False,
        "pushed": False,
    }


__all__ = [
    "BatchPlan",
    "Candidate",
    "Event",
    "LANES",
    "LandingQueue",
    "MODES",
    "QueueConfig",
    "REASONS",
    "MAX_BISECT_EXTRA_RUNS",
    "QUEUE_BRANCH_PREFIX",
    "bisect_plan",
    "describe_queue_branch",
    "candidates_from_pulls",
    "plan_batch",
]
