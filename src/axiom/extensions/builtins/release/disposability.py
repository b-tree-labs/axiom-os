# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET writes artifact disposability verdicts for TIDY.

This is the writer half of ``axiom.infra.artifact_verdicts``. The CI-health
sweep and the lander call :func:`publish_from_ci_report` and
:func:`publish_from_lander` after they have already decided what is in
flight, failing, flaky, superseded, or merged. A write failure is logged
and never raises into those loops.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from axiom.infra.artifact_verdicts import ArtifactVerdict, put_verdicts

_log = logging.getLogger("axiom.release.disposability")

_QUEUE_STATES = frozenset({"QUEUED", "IN_QUEUE", "MERGE_QUEUE", "BEHIND"})
_MERGE_EVENTS = frozenset({"merge_group", "merge_queue"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _verdict(
    kind: str,
    reason: str,
    *,
    repo: str,
    run_id: str | int = "",
    head_sha: str = "",
    artifact_id: str = "",
    now: datetime | None = None,
) -> ArtifactVerdict:
    return ArtifactVerdict.make(
        kind,
        reason,
        repo=repo,
        run_id=run_id,
        head_sha=head_sha,
        artifact_id=artifact_id,
        now=now,
    )


def hints_from_ci_report(report: Any, *, now: datetime | None = None) -> list[ArtifactVerdict]:
    """Translate a CI-health report into keep/disposable verdicts."""
    now = now or _now()
    out: list[ArtifactVerdict] = []

    for decision in getattr(report, "cancels", None) or []:
        run = getattr(decision, "run", None)
        reason = str(getattr(decision, "reason", "") or "")
        if run is None:
            continue
        if getattr(decision, "cancel", False) or "superseded" in reason.lower():
            out.append(_verdict("disposable", "superseded_green", repo=run.repo, run_id=run.id, head_sha=getattr(run, "head_sha", ""), now=now))

    for reading in getattr(report, "flakes", None) or []:
        repo = str(getattr(reading, "repo", "") or "")
        run_id = getattr(reading, "run_id", "") or ""
        sha = str(getattr(reading, "sha", "") or "")
        status = str(getattr(reading, "verdict", "") or "")
        if status in {"candidate", "rerun-requested", "unclassified"}:
            out.append(_verdict("keep", "flaky_investigation", repo=repo, run_id=run_id, head_sha=sha, now=now))
        elif status == "flake":
            out.append(_verdict("disposable", "rerun_succeeded", repo=repo, run_id=run_id, head_sha=sha, now=now))
        elif status == "real":
            out.append(_verdict("keep", "failing_run_diagnosed", repo=repo, run_id=run_id, head_sha=sha, now=now))

    for culprit in getattr(report, "culprits", None) or []:
        if not getattr(culprit, "open", False):
            continue
        run = getattr(culprit, "run", None)
        if run is None:
            continue
        out.append(_verdict("keep", "failing_run_diagnosed", repo=run.repo, run_id=run.id, head_sha=getattr(run, "head_sha", ""), now=now))

    for repo, head in (getattr(report, "heads", None) or {}).items():
        run_id = getattr(head, "run_id", 0) or ""
        sha = str(getattr(head, "sha", "") or "")
        verdict = str(getattr(head, "verdict", "") or "")
        if not run_id and not sha:
            continue
        if verdict == "pending":
            reason = "retry_pending" if getattr(head, "run_attempt", 1) > 1 else "pr_in_flight"
            out.append(_verdict("keep", reason, repo=str(repo), run_id=run_id, head_sha=sha, now=now))
        elif verdict == "failure":
            out.append(_verdict("keep", "failing_run_diagnosed", repo=str(repo), run_id=run_id, head_sha=sha, now=now))

    for run in getattr(report, "pipeline_runs", None) or []:
        repo = run.repo
        sha = getattr(run, "head_sha", "") or ""
        event = str(getattr(run, "event", "") or "")
        branch = str(getattr(run, "head_branch", "") or "")
        workflow = f"{getattr(run, 'workflow', '')} {getattr(run, 'path', '')}".lower()
        if event in _MERGE_EVENTS or branch.startswith("gh-readonly-queue"):
            out.append(_verdict("keep", "merge_queue", repo=repo, run_id=run.id, head_sha=sha, now=now))
            continue
        if getattr(run, "active", False) and (
            "release" in workflow or "publish" in workflow or "tag" in workflow or event in {"release", "workflow_dispatch"}
        ):
            reason = "tag_pending" if ("tag" in workflow or branch.startswith("refs/tags/") or branch.startswith("v")) else "release_pending"
            out.append(_verdict("keep", reason, repo=repo, run_id=run.id, head_sha=sha, now=now))
            continue
        if getattr(run, "active", False) and getattr(run, "pr_numbers", ()):
            out.append(_verdict("keep", "pr_in_flight", repo=repo, run_id=run.id, head_sha=sha, now=now))

    return out


def hints_from_lander(
    *,
    repo: str,
    pulls: Iterable[Any] | None = None,
    now: datetime | None = None,
    lock_held: bool = False,
    open_fix_shas: Iterable[str] | None = None,
    bisect_shas: Iterable[str] | None = None,
    merged: Iterable[Any] | None = None,
    closed: Iterable[Any] | None = None,
) -> list[ArtifactVerdict]:
    """Verdicts from the lander's view of open, queued, merged, and closed PRs."""
    now = now or _now()
    out: list[ArtifactVerdict] = []
    for pull in pulls or []:
        sha = str(getattr(pull, "head_sha", "") or "")
        state = str(getattr(pull, "merge_state", "") or "").upper()
        reason = "merge_queue" if state in _QUEUE_STATES else "pr_in_flight"
        if sha:
            out.append(_verdict("keep", reason, repo=repo, head_sha=sha, now=now))
    if lock_held:
        out.append(_verdict("keep", "release_pending", repo=repo, now=now))
    for sha in open_fix_shas or []:
        if sha:
            out.append(_verdict("keep", "open_fix_attempt", repo=repo, head_sha=str(sha), now=now))
    for sha in bisect_shas or []:
        if sha:
            out.append(_verdict("keep", "bisect", repo=repo, head_sha=str(sha), now=now))
    for pull in merged or []:
        sha = str(getattr(pull, "head_sha", "") or pull if isinstance(pull, str) else "")
        if sha:
            out.append(_verdict("disposable", "pr_merged", repo=repo, head_sha=sha, now=now))
    for pull in closed or []:
        sha = str(getattr(pull, "head_sha", "") or pull if isinstance(pull, str) else "")
        if sha:
            out.append(_verdict("disposable", "pr_closed", repo=repo, head_sha=sha, now=now))
    return out


def publish_verdicts(state_dir: Path, verdicts: Iterable[ArtifactVerdict], *, now: datetime | None = None) -> int:
    """Write verdicts, swallowing errors so a lander/sweep cannot fail closed."""
    items = list(verdicts)
    if not items:
        return 0
    try:
        put_verdicts(state_dir, items, now=now)
        return len(items)
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not write artifact verdicts: %s", exc)
        return 0


def publish_from_ci_report(state_dir: Path, report: Any, *, now: datetime | None = None) -> int:
    return publish_verdicts(state_dir, hints_from_ci_report(report, now=now), now=now)


def publish_from_lander(state_dir: Path, **kwargs: Any) -> int:
    return publish_verdicts(state_dir, hints_from_lander(**{k: v for k, v in kwargs.items() if k != "state_dir"}), now=kwargs.get("now"))


__all__ = [
    "hints_from_ci_report",
    "hints_from_lander",
    "publish_from_ci_report",
    "publish_from_lander",
    "publish_verdicts",
]
