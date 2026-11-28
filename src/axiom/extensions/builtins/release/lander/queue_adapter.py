# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The seam between the landing queue and a forge (#2399, slice 1).

The queue never imports a forge. Two Protocols: ``QueueReader`` is the read side
(slice 2: open pull requests, the base, the merged-state sha the forge computes, CI
by sha); ``QueueAdapter`` adds the writes (build a branch, land, delete) that only
mode ``on`` will use. A forge's own queue may be an optional adapter later, off by
default. ``FakeQueueAdapter`` keeps a call log for tests: shadow mode must leave it
empty. ``FakeQueueReader`` serves canned facts and can be told a read is unreadable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from axiom.extensions.builtins.release.lander.queue import Candidate


@dataclass(frozen=True)
class CheckOutcome:
    passed: bool
    tests_run: int = 0
    detail: str = ""


class QueueReadError(RuntimeError):
    """A value could not be read, or was not in the expected shape. Fail closed."""


class QueueWriteRefused(RuntimeError):
    """A read-only adapter was asked to write."""


@dataclass(frozen=True)
class PullFacts:
    """What the forge says about one open pull request. Text fields are never inputs."""

    number: int
    head_sha: str
    base_ref: str
    labels: tuple[str, ...] = ()
    #: ``passing``, ``failing``, ``pending`` or ``none`` (no check reported).
    checks: str = "none"
    #: True or False as the forge computed it; None when it has not computed yet.
    mergeable: bool | None = None
    #: The commit the forge built for the merged state; empty when it has none.
    merge_commit_sha: str = ""
    base_sha: str = ""


@dataclass(frozen=True)
class CiResult:
    """Workflow runs for one sha, summarised."""

    sha: str
    #: ``success``, ``failure``, ``pending`` or ``none`` (no run for that sha).
    state: str
    runs: int = 0
    minutes: float = 0.0


class QueueReader(Protocol):
    def open_pulls(self, repo: str) -> list[PullFacts]:
        """Open pull requests: head sha, base, labels, checks."""
        ...

    def base_sha(self, repo: str, base: str) -> str:
        """The tip of the base branch. Raises ``QueueReadError`` when unreadable."""
        ...

    def pull_facts(self, repo: str, number: int) -> PullFacts:
        """One pull request with the forge's merged-state sha where it has one."""
        ...

    def ci_results(self, repo: str, sha: str) -> CiResult:
        """CI by sha."""
        ...


class QueueAdapter(Protocol):
    def build_candidate(self, candidate: Candidate, ahead: tuple[int, ...]) -> str:
        """Main plus the PR (and those ahead) on a temporary branch. Returns its name."""
        ...

    def wait_for_checks(self, branch: str) -> CheckOutcome:
        """Wait for the required checks on that branch."""
        ...

    def land(self, candidate: Candidate) -> str:
        """Land the PR pinned to ``candidate.head_sha``. Returns the merge sha."""
        ...

    def delete_branch(self, branch: str) -> None: ...


@dataclass
class FakeQueueAdapter:
    outcomes: dict[int, CheckOutcome] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)

    def build_candidate(self, candidate: Candidate, ahead: tuple[int, ...]) -> str:
        self.calls.append(("build_candidate", candidate.pr, ahead))
        return f"queue/pr-{candidate.pr}"

    def wait_for_checks(self, branch: str) -> CheckOutcome:
        self.calls.append(("wait_for_checks", branch))
        pr = int(branch.rsplit("-", 1)[1])
        return self.outcomes.get(pr, CheckOutcome(True, 1))

    def land(self, candidate: Candidate) -> str:
        self.calls.append(("land", candidate.pr, candidate.head_sha))
        return f"merged-{candidate.pr}"

    def delete_branch(self, branch: str) -> None:
        self.calls.append(("delete_branch", branch))


@dataclass
class FakeQueueReader:
    """Canned reads for tests. A number in ``unreadable`` raises, as an unreadable value."""

    base: str = "b" * 40
    pulls: dict[int, PullFacts] = field(default_factory=dict)
    ci: dict[str, CiResult] = field(default_factory=dict)
    unreadable: set[int] = field(default_factory=set)
    base_unreadable: bool = False
    calls: list[tuple] = field(default_factory=list)

    def open_pulls(self, repo: str) -> list[PullFacts]:
        self.calls.append(("open_pulls", repo))
        return [self.pulls[n] for n in sorted(self.pulls)]

    def base_sha(self, repo: str, base: str) -> str:
        self.calls.append(("base_sha", repo, base))
        if self.base_unreadable:
            raise QueueReadError("base unreadable")
        return self.base

    def pull_facts(self, repo: str, number: int) -> PullFacts:
        self.calls.append(("pull_facts", repo, number))
        if number in self.unreadable or number not in self.pulls:
            raise QueueReadError(f"pull request {number} unreadable")
        return self.pulls[number]

    def ci_results(self, repo: str, sha: str) -> CiResult:
        self.calls.append(("ci_results", repo, sha))
        return self.ci.get(sha, CiResult(sha, "none"))
