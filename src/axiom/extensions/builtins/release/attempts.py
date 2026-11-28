# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What one run attempt produced: a test result, a set of failing tests, or nothing (#2037).

A run whose runner was killed (exit 137), or whose job was cancelled or timed out by the
platform, produced **no test result**. It says nothing about the commit, so it is never counted
as a failure: the failed jobs are re-run, and only the re-run is judged. A revert also needs the
same failing test in the first run and in the re-run; two different failures are not a repeat.
This reads a run's jobs, their logs and their annotations through functions handed in, so the
decision is pure.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from axiom.extensions.builtins.release.ci_health.client import CIClientError
from axiom.extensions.builtins.release.ci_health.escape import failing_tests, failure_summaries, named_paths

NO_RESULT = "no_result"
TESTS = "tests"
OTHER = "other"
UNREADABLE = "unreadable"

_FAILED = frozenset({"failure", "timed_out", "cancelled"})
_PLATFORM_ENDED = frozenset({"timed_out", "cancelled"})
_KILLED = re.compile(r"exit code 137|received a shutdown \w+|lost communication|runner .{0,40}(?:killed|lost)", re.I)


@dataclass(frozen=True)
class AttemptReading:
    kind: str
    tests: tuple[str, ...] = ()
    detail: str = ""
    #: For each failing test, the failed jobs (shards) whose log named it: ``((test, (job, ...)), ...)``.
    sources: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: For each failing test, the file paths its own failure text names (what a scan guard found).
    paths: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def shards(self, test: str) -> tuple[str, ...]:
        return dict(self.sources).get(test, ())


def read_jobs(
    log: Callable[[dict], str],
    jobs: Sequence[dict],
    notes: Callable[[dict], Sequence[str]] | None = None,
) -> AttemptReading:
    """Classify the failed jobs of one attempt: ``tests``, ``no_result``, ``other`` or ``unreadable``."""
    failed = [job for job in jobs if str(job.get("conclusion") or "") in _FAILED]
    if not failed:
        return AttemptReading(UNREADABLE, detail="no failed job to read")
    tests: list[str] = []
    ended: list[str] = []
    wheres: list[str] = []
    sources: dict[str, list[str]] = {}
    paths: dict[str, list[str]] = {}
    for job in failed:
        name = str(job.get("name") or "a job")
        try:
            text = log(job) or ""
        except CIClientError:
            text = ""
        found = failing_tests(text)
        tests.extend(t for t in found if t not in tests)
        # Each failed job (a unit shard, or any other) is read on its own: the aggregator that says
        # "a shard failed" carries no test ids, the shards do, and the incident names which shard.
        for test in found:
            if name not in sources.setdefault(test, []):
                sources[test].append(name)
        for test, said in failure_summaries(text).items():
            for path in named_paths(said):
                if path not in paths.setdefault(test, []):
                    paths[test].append(path)
        if found:
            continue
        conclusion = str(job.get("conclusion") or "")
        if conclusion in _PLATFORM_ENDED:
            ended.append(f"{name} {conclusion.replace('_', ' ')} by the platform")
            continue
        try:
            messages = list(notes(job)) if notes is not None else []
        except CIClientError:
            messages = []
        hit = next((m for m in messages if _KILLED.search(m)), "")
        if hit:
            ended.append(f"{name}: {hit.strip()}")
            continue
        step = next(
            (str(s.get("name") or "") for s in job.get("steps") or [] if str(s.get("conclusion") or "") == "failure"), ""
        )
        said = next((m.strip() for m in messages if m.strip()), "")
        wheres.append(
            name
            + (f" failed in the step '{step}'" if step else " failed")
            + ("" if text.strip() else " (its log could not be read)")
            + (f" ({said})" if said else "")
        )
    if tests:
        return AttemptReading(
            TESTS,
            tuple(tests),
            sources=tuple((t, tuple(names)) for t, names in sources.items()),
            paths=tuple((t, tuple(found)) for t, found in paths.items() if found),
        )
    if ended:
        return AttemptReading(NO_RESULT, detail="; ".join(ended + wheres))
    return AttemptReading(OTHER, detail="; ".join(wheres))


def read_attempt(client: object, repo: str, run_id: int, attempt: int) -> AttemptReading:
    """One earlier attempt of a run. ``unreadable`` when the client cannot give its jobs."""
    reader = getattr(client, "attempt_jobs", None)
    if reader is None:
        return AttemptReading(UNREADABLE, detail="this client cannot read an earlier attempt")
    try:
        jobs = reader(repo, run_id, attempt)
    except CIClientError as exc:
        return AttemptReading(UNREADABLE, detail=str(exc))
    return _read(client, repo, jobs)


def read_current(client: object, repo: str, run_id: int) -> AttemptReading:
    """The run's latest attempt, from ``client.jobs``."""
    try:
        jobs = client.jobs(repo, run_id)  # type: ignore[attr-defined]
    except CIClientError as exc:
        return AttemptReading(UNREADABLE, detail=str(exc))
    return _read(client, repo, jobs)


def _read(client: object, repo: str, jobs: Sequence[dict]) -> AttemptReading:
    annotations = getattr(client, "annotations", None)
    return read_jobs(
        lambda job: client.job_log(repo, int(job.get("id") or 0)) or "",  # type: ignore[attr-defined]
        jobs,
        (lambda job: annotations(repo, int(job.get("id") or 0))) if annotations is not None else None,
    )


def describe(reading: AttemptReading, which: str) -> str:
    """One plain sentence for the incident: what ``which`` ("the first run", "the re-run") did."""
    if reading.kind == NO_RESULT:
        return f"{which} produced no test result ({reading.detail})"
    if reading.kind == TESTS:
        return f"{which} failed " + ", ".join(_with_shards(reading, test) for test in reading.tests)
    if reading.kind == OTHER:
        return f"{which} failed without a named failing test ({reading.detail or 'no failed step read'})"
    return f"{which} could not be read ({reading.detail})"


def _with_shards(reading: AttemptReading, test: str) -> str:
    shards = reading.shards(test)
    return f"{test} (named by {', '.join(shards)})" if shards else test
