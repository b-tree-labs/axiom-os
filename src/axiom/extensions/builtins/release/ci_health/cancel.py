# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which running workflow runs are superseded, and the one safe fix: cancel.

A run is superseded when it is still going and a newer run of the same
workflow is testing a newer head:

* on a pull request or feature branch, any newer head on that branch;
* on the default branch, a newer push whose head descends from this one
  (so the newer run tests everything this one would).

Never cancelled: a release, publish, tag, or deploy workflow (names from
``ci_health.release_workflows``); a run on a tag; a run started by a
schedule, a manual dispatch, a release or a repository dispatch; the newest
run of its group; anything already finished. One sweep cancels at most
``ci_health.max_cancels`` runs. Each cancel writes an audit record.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.runs import Run

_CANCELLABLE_EVENTS = frozenset({"pull_request", "pull_request_target", "push"})


@dataclass(frozen=True)
class CancelDecision:
    run: Run
    cancel: bool
    reason: str
    newer: int = 0

    def to_dict(self) -> dict:
        return {
            "run": self.run.id,
            "repo": self.run.repo,
            "workflow": self.run.workflow,
            "branch": self.run.head_branch,
            "sha": self.run.head_sha[:12],
            "cancel": self.cancel,
            "reason": self.reason,
            "newer": self.newer,
            "url": self.run.url,
        }


def protected_reason(run: Run, config: CIHealthConfig) -> str:
    """Why a run must never be cancelled, or empty."""
    haystack = f"{run.workflow} {run.path}".lower()
    for word in config.release_workflows:
        # A whole word: "tag" protects "tag.yml" and "Tag release", not "staging".
        if re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", haystack):
            return f"release workflow ({word})"
    if run.event not in _CANCELLABLE_EVENTS:
        return f"{run.event or 'unknown'} event"
    if run.head_branch and re.match(config.tag_pattern, run.head_branch):
        return "tag ref"
    if run.head_branch.startswith("refs/tags/"):
        return "tag ref"
    return ""


def _group_key(run: Run, config: CIHealthConfig) -> tuple:
    if run.head_branch == config.default_branch and run.event == "push":
        return ("main", run.repo, run.workflow_id or run.workflow, run.head_branch)
    return ("branch", run.repo, run.workflow_id or run.workflow, run.event, run.head_branch)


def plan_cancels(runs: list[Run], config: CIHealthConfig, client: CIClient | None = None) -> list[CancelDecision]:
    """One decision per active run. Pure except the ancestor read on the default branch."""
    groups: dict[tuple, list[Run]] = defaultdict(list)
    for run in runs:
        groups[_group_key(run, config)].append(run)
    decisions: list[CancelDecision] = []
    for key, group in groups.items():
        ordered = sorted(group, key=lambda r: (r.created_at, r.id))
        newest = ordered[-1]
        for run in ordered:
            if not run.active:
                continue
            if run is newest:
                decisions.append(CancelDecision(run, False, "newest run"))
                continue
            guard = protected_reason(run, config)
            if guard:
                decisions.append(CancelDecision(run, False, guard))
                continue
            newer = [r for r in ordered if r.created_at > run.created_at or (r.created_at == run.created_at and r.id > run.id)]
            newer_heads = [r for r in newer if r.head_sha and r.head_sha != run.head_sha]
            if not newer_heads:
                decisions.append(CancelDecision(run, False, "no newer head"))
                continue
            candidate = newer_heads[-1]
            if key[0] == "main":
                if client is None:
                    decisions.append(CancelDecision(run, False, "ancestry unknown"))
                    continue
                try:
                    descends = client.is_ancestor(run.repo, run.head_sha, candidate.head_sha)
                except CIClientError:
                    descends = False
                if not descends:
                    decisions.append(CancelDecision(run, False, "newer head does not descend from this one"))
                    continue
                decisions.append(CancelDecision(run, True, "a newer default-branch run tests a descendant", candidate.id))
            else:
                decisions.append(CancelDecision(run, True, "a newer head on this branch is running", candidate.id))
    cancels = [d for d in decisions if d.cancel]
    if len(cancels) > config.max_cancels:
        keep = {id(d) for d in sorted(cancels, key=lambda d: d.run.created_at)[: config.max_cancels]}
        decisions = [
            d if not d.cancel or id(d) in keep else CancelDecision(d.run, False, "over the per-sweep cancel cap", d.newer)
            for d in decisions
        ]
    return decisions


def _audit(state_dir: Path, decision: CancelDecision, outcome: str) -> None:
    from axiom.infra.switchplan.ledger import FileLedger, new_record

    record = new_record("release.ci-cancel", f"{decision.run.repo}#run-{decision.run.id}")
    record.phase = "committed" if outcome == "cancelled" else "planned"
    record.state = {**decision.to_dict(), "outcome": outcome}
    record.event("cancel", f"{outcome}: {decision.reason}; newer run {decision.newer}"[:500])
    FileLedger(Path(state_dir) / "agents" / "rivet" / "ci-health-records").save(record)


def apply_cancels(
    decisions: list[CancelDecision],
    client: CIClient,
    *,
    state_dir: Path,
    act: bool,
    allow_cancel: bool = True,
) -> list[dict]:
    """Cancel each superseded run when ``act``. Every attempt is audited."""
    done: list[dict] = []
    for decision in decisions:
        if not decision.cancel:
            continue
        if not act or not allow_cancel:
            done.append({**decision.to_dict(), "outcome": "proposed"})
            continue
        try:
            client.cancel_run(decision.run.repo, decision.run.id)
            outcome = "cancelled"
        except CIClientError as exc:
            outcome = f"failed: {exc}"[:200]
        _audit(state_dir, decision, outcome)
        done.append({**decision.to_dict(), "outcome": outcome})
    return done
