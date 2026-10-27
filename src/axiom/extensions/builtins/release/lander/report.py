# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The backlog report: batches, closes, ready, and human decisions.

Pure aside from the repair and review skills it calls. It does not
talk to a forge and does not write state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from axiom.extensions.builtins.release.actions_spend import SUITE_MINUTES, learn_suite_minutes
from axiom.extensions.builtins.release.ci_flow import choose_reviewer, plan_repair
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.coordination import held_by
from axiom.extensions.builtins.release.lander.grouping import (
    dependency_key,
    dependency_version,
    group_pulls,
    is_dependabot,
    shared_reasons,
)
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.selectors import (
    Selection,
    TestmonSelector,
    scope_id,
    suite_minutes_for,
)
from axiom.extensions.builtins.release.lander.protect import protected_categories
from axiom.extensions.builtins.release.lander.reviews import hold_for_pull
from axiom.policy.agent_action_guard import human_route

_DISABLED = ("merge", "enqueue", "force_push", "status_post")


@dataclass(frozen=True)
class BatchView:
    key: str
    reasons: tuple[str, ...]
    members: tuple[int, ...]
    span_hours: float

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "reasons": list(self.reasons),
            "members": list(self.members),
            "span_hours": round(self.span_hours, 2),
        }


@dataclass(frozen=True)
class CloseAdvice:
    number: int
    keeper: int
    reason: str

    def to_dict(self) -> dict:
        return {"number": self.number, "keeper": self.keeper, "reason": self.reason}


@dataclass(frozen=True)
class PullView:
    number: int
    title: str
    head_sha: str
    head_ref: str
    checks: str
    approval: str
    reasons: tuple[str, ...] = ()
    repair: str = ""
    reviewer: str = ""

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "head_sha": self.head_sha,
            "head_ref": self.head_ref,
            "checks": self.checks,
            "approval": self.approval,
            "reasons": list(self.reasons),
            "repair": self.repair,
            "reviewer": self.reviewer,
        }


@dataclass
class BacklogReport:
    repo: str
    mode: str
    suite_minutes: int
    suite_minutes_source: str
    batches: list[BatchView] = field(default_factory=list)
    families: list[BatchView] = field(default_factory=list)
    superseded: list[CloseAdvice] = field(default_factory=list)
    ready: list[PullView] = field(default_factory=list)
    needs_human: list[PullView] = field(default_factory=list)
    waiting_on_ci: list[PullView] = field(default_factory=list)
    minutes_saved: int = 0
    blocked_by_closer: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    open_count: int = 0

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "mode": self.mode,
            "read_only": True,
            "actions_enabled": ["report", "propose"],
            "actions_disabled": list(_DISABLED),
            "suite_minutes": self.suite_minutes,
            "suite_minutes_source": self.suite_minutes_source,
            "open_count": self.open_count,
            "minutes_saved": self.minutes_saved,
            "batches": [batch.to_dict() for batch in self.batches],
            "families": [batch.to_dict() for batch in self.families],
            "superseded": [item.to_dict() for item in self.superseded],
            "ready": [item.to_dict() for item in self.ready],
            "needs_human": [item.to_dict() for item in self.needs_human],
            "waiting_on_ci": [item.to_dict() for item in self.waiting_on_ci],
            "blocked_by_closer": list(self.blocked_by_closer),
            "notes": list(self.notes),
        }


def _keeper(group: list[OpenPull]) -> OpenPull:
    return max(group, key=lambda pull: (pull.checks == "passing", pull.updated_at, pull.number))


def _dependency_keeper(group: list[OpenPull]) -> OpenPull:
    return max(
        group,
        key=lambda pull: (dependency_version(pull.head_ref), pull.updated_at, pull.number),
    )


def find_superseded(pulls: list[OpenPull]) -> list[CloseAdvice]:
    """Duplicates (identical files) and older dependency updates."""
    advice: list[CloseAdvice] = []
    seen: set[int] = set()
    by_files: dict[tuple[str, ...], list[OpenPull]] = {}
    for pull in pulls:
        # Grouped Dependabot updates often touch the same manifest.
        # That is not a duplicate pull request.
        if is_dependabot(pull):
            continue
        files = pull.sorted_files()
        if files:
            by_files.setdefault(files, []).append(pull)
    for group in by_files.values():
        if len(group) < 2:
            continue
        keeper = _keeper(group)
        for other in group:
            if other.number == keeper.number or other.number in seen:
                continue
            seen.add(other.number)
            advice.append(CloseAdvice(other.number, keeper.number, "duplicate"))
    by_dep: dict[str, list[OpenPull]] = {}
    for pull in pulls:
        key = dependency_key(pull.head_ref)
        if key:
            by_dep.setdefault(key, []).append(pull)
    for group in by_dep.values():
        if len(group) < 2:
            continue
        keeper = _dependency_keeper(group)
        for other in group:
            if other.number == keeper.number or other.number in seen:
                continue
            seen.add(other.number)
            advice.append(CloseAdvice(other.number, keeper.number, "superseded_dependency"))
    advice.sort(key=lambda item: item.number)
    return advice


def _views(
    pulls: list[OpenPull],
    *,
    window: timedelta,
    ignore_window: bool,
    signals: frozenset[str] | set[str] | None = None,
) -> list[BatchView]:
    views: list[BatchView] = []
    for members in group_pulls(
        pulls, window=window, ignore_window=ignore_window, signals=signals,
    ):
        if len(members) < 2:
            continue
        reasons = shared_reasons(members)
        start = min(pull.updated_at for pull in members)
        end = max(pull.updated_at for pull in members)
        span = (end - start).total_seconds() / 3600
        key = reasons[0] if reasons else f"group-{members[0].number}"
        views.append(
            BatchView(
                key=key,
                reasons=reasons,
                members=tuple(pull.number for pull in members),
                span_hours=span,
            )
        )
    return views


def _approval(pull: OpenPull, approvals: dict[int, str]) -> str:
    if pull.number not in approvals:
        return "none"
    if approvals[pull.number] == pull.head_sha:
        return "bound"
    return "reset"


def _subset_partners(pull: OpenPull, pulls: list[OpenPull], skip: set[int]) -> list[int]:
    mine = set(pull.files)
    if not mine or is_dependabot(pull):
        return []
    partners: list[int] = []
    for other in pulls:
        if other.number == pull.number or other.number in skip or is_dependabot(other):
            continue
        theirs = set(other.files)
        if not theirs or mine == theirs:
            continue
        if mine < theirs or theirs < mine:
            partners.append(other.number)
    return partners


def _view_for(
    pull: OpenPull,
    *,
    approval: str,
    reasons: tuple[str, ...],
    repair: str,
    reviewer: str,
) -> PullView:
    return PullView(
        number=pull.number,
        title=pull.title,
        head_sha=pull.head_sha,
        head_ref=pull.head_ref,
        checks=pull.checks,
        approval=approval,
        reasons=reasons,
        repair=repair,
        reviewer=reviewer,
    )


def build_report(
    pulls: list[OpenPull],
    *,
    repo: str,
    config: LanderConfig | None = None,
    samples: list[int] | None = None,
    approvals: dict[int, str] | None = None,
    state_dir: Path | None = None,
    selector: object | None = None,
    base_sha: str = "",
) -> BacklogReport:
    """Classify ``pulls``. ``state_dir`` lets the auto-closer's claims exclude a PR."""
    config = config or LanderConfig()
    if config.suite_cost == "configured" and config.suite_minutes:
        suite = config.suite_minutes
        source = "configured"
    elif config.suite_minutes and config.suite_cost != "learned":
        suite = config.suite_minutes
        source = "configured"
    elif samples or config.suite_cost == "learned":
        suite = learn_suite_minutes(samples or [])
        source = "learned"
    else:
        suite = SUITE_MINUTES
        source = "default"
    window = timedelta(hours=config.window_hours)
    approvals = approvals or {}
    chooser = selector or TestmonSelector(
        database=config.selector_database,
        context="pull_request",
        test_setup=config.test_setup_patterns,
        dependencies=config.dependency_patterns,
        full_contexts=config.full_suite_contexts,
    )
    selections: dict[int, Selection] = {}
    annotated: list[OpenPull] = []
    for pull in pulls:
        try:
            chosen = chooser.select(pull.files, base_sha, pull.head_sha)  # type: ignore[attr-defined]
        except Exception:
            chosen = Selection(True, (), "selector-error")
        selections[pull.number] = chosen
        scope = "" if chosen.run_all else scope_id(chosen.tests)
        annotated.append(replace(pull, scope=scope))
    pulls = annotated
    blocked: list[int] = []
    if state_dir is not None:
        for pull in pulls:
            if held_by(
                state_dir,
                pull.number,
                actor="auto_closer",
                repo=repo,
                ttl_s=config.claim_ttl_s,
            ):
                blocked.append(pull.number)
    live = [pull for pull in pulls if pull.number not in set(blocked)]
    superseded = find_superseded(live)
    superseded_ids = {item.number for item in superseded}
    batches = _views(live, window=window, ignore_window=False, signals=config.signals)
    families = _views(pulls, window=window, ignore_window=True, signals=config.signals)
    ready: list[PullView] = []
    human: list[PullView] = []
    waiting: list[PullView] = []
    rules = config.extra_path_rules or None
    for pull in live:
        if pull.number in superseded_ids:
            continue
        reasons: list[str] = list(human_route(pull.files, extra=rules))
        for category in protected_categories(
            pull.files,
            extra=rules,
            dependency_patterns=config.dependency_patterns,
            test_setup_patterns=config.test_setup_patterns,
        ):
            if category not in reasons:
                reasons.append(category)
        hold = hold_for_pull(pull)
        if hold:
            reasons.append(hold)
        approval = _approval(pull, approvals)
        if approval == "reset":
            reasons.append("approval_reset")
        if pull.draft:
            reasons.append("draft")
        if pull.mergeable is False:
            reasons.append("conflict")
        repair = ""
        if pull.checks == "failing":
            planned = plan_repair(
                pull.failure_log or pull.title,
                attempts=pull.ci_attempts,
            )
            repair = str(planned.get("pattern") or "")
            if repair == "try-cap" or pull.ci_attempts >= config.repair_cap:
                reasons.append("try-cap")
            else:
                reasons.append("failing_checks")
        if pull.checks == "unknown":
            reasons.append("checks_unknown")
        partners = _subset_partners(pull, live, superseded_ids | {pull.number})
        if partners:
            joined = ",".join(str(number) for number in partners)
            reasons.append(f"file_overlap:{joined}")
        reviewer = ""
        if pull.author_model and config.review_roster:
            picked = choose_reviewer(pull.author_model, list(config.review_roster))
            reviewer = str(picked.get("reviewer") or "")
            if not picked.get("ok"):
                reasons.append("review_refused")
        view = _view_for(
            pull,
            approval=approval,
            reasons=tuple(reasons),
            repair=repair,
            reviewer=reviewer,
        )
        if reasons:
            human.append(view)
        elif pull.checks == "pending":
            waiting.append(view)
        elif pull.checks == "passing":
            ready.append(view)
        else:
            human.append(view)
    saved = 0
    by_number = {pull.number: pull for pull in pulls}
    for item in superseded:
        saved += suite_minutes_for(selections[item.number], suite)
    for batch in batches:
        alive = [
            by_number[number]
            for number in batch.members
            if number not in superseded_ids and number in by_number
        ]
        if len(alive) < 2:
            continue
        separate = sum(suite_minutes_for(selections[pull.number], suite) for pull in alive)
        union_paths = tuple(sorted({path for pull in alive for path in pull.files}))
        try:
            together = chooser.select(union_paths, base_sha, alive[-1].head_sha)  # type: ignore[attr-defined]
        except Exception:
            together = Selection(True, (), "selector-error")
        saved += max(0, separate - suite_minutes_for(together, suite))
    notes = list(config.notes)
    if "merge" in config.auto_allowed:
        notes.append(
            "a green pull request with no review hold and no protected path is eligible to land"
        )
    else:
        notes.append("warn mode proposes and reports; it does not merge or enqueue")
    if config.fix_provider == "handoff":
        url = os.environ.get(config.webhook_url_env, "") if config.webhook_url_env else ""
        key = os.environ.get(config.webhook_key_env, "") if config.webhook_key_env else ""
        if not url or not key:
            notes.append("no fixer configured")
    if config.post_status:
        notes.append("status posts stay disarmed; the advisory payload is neutral and non-blocking")
    return BacklogReport(
        repo=repo,
        mode=config.mode,
        suite_minutes=suite,
        suite_minutes_source=source,
        batches=batches,
        families=families,
        superseded=superseded,
        ready=ready,
        needs_human=human,
        waiting_on_ci=waiting,
        minutes_saved=saved,
        blocked_by_closer=blocked,
        notes=notes,
        open_count=len(pulls),
    )
