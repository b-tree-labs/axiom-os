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
from datetime import datetime, timedelta
from pathlib import Path

from axiom.extensions.builtins.release.actions_spend import SUITE_MINUTES, learn_suite_minutes
from axiom.extensions.builtins.release.ci_flow import choose_reviewer, plan_repair
from axiom.extensions.builtins.release.lander import ci_start, owner_hold
from axiom.extensions.builtins.release.lander.checks import (
    CAUSE_DRAFT,
    CAUSE_NEVER_REPORTED,
    NOT_RUN,
    TESTS_NOT_RUN,
    not_run_fix,
    not_run_sentence,
)
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
from axiom.extensions.builtins.release.lander.protect import config_categories
from axiom.extensions.builtins.release.lander.reviews import hold_for_pull
from axiom.policy.agent_action_guard import human_route

_DISABLED = ("merge", "enqueue", "force_push", "status_post")

#: Reasons RIVET works through on its own (ADR-269): it repairs failing checks until the
#: try cap, waits on CI it cannot read yet, and lands overlapping changes one at a time.
_RIVET_REASONS = frozenset({"failing_checks", "checks_unknown"})
_RIVET_PREFIXES = ("file_overlap:",)
#: Reasons that are the author's own next step, not anyone's decision.
_AUTHOR_REASONS = frozenset({"draft", f"{TESTS_NOT_RUN}:{CAUSE_DRAFT}"})


def _owner(reasons: tuple[str, ...]) -> str:
    """Who acts next on a held pull request: ``person``, ``author`` or ``rivet``.

    Any reason not known to be RIVET's or the author's needs a person, so a new
    reason is held for a person until it is classified.
    """
    if not reasons:
        return "person"
    rest = [r for r in reasons if r not in _RIVET_REASONS and not r.startswith(_RIVET_PREFIXES)]
    if not rest:
        return "rivet"
    if all(r in _AUTHOR_REASONS for r in rest):
        return "author"
    return "person"


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
    #: The loop tier (ADR-257 D2) from the tier map; None when the repository declares none.
    tier: int | None = None

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
            "tier": self.tier,
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
    #: Heads no workflow run ever started on: the missing checks, who moves next, one sentence.
    ci_never_ran: list[dict] = field(default_factory=list)
    #: Heads whose required checks have no run that counts (#2410, #2354): the checks, the
    #: cause, one sentence, and the exact fix. Listed before any count.
    tests_not_run: list[dict] = field(default_factory=list)
    #: Required checks with no row yet while a run for the head is queued or in progress.
    waiting_for_run: list[dict] = field(default_factory=list)
    #: Finished and held for their owner (``lander.owner_hold``): why, and for how long.
    held_for_owner_review: list[dict] = field(default_factory=list)
    needs_human: list[PullView] = field(default_factory=list)
    waiting_on_ci: list[PullView] = field(default_factory=list)
    rivet_handling: list[PullView] = field(default_factory=list)
    with_author: list[PullView] = field(default_factory=list)
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
            "ci_never_ran": list(self.ci_never_ran),
            "tests_not_run": list(self.tests_not_run),
            "waiting_for_run": list(self.waiting_for_run),
            "held_for_owner_review": list(self.held_for_owner_review),
            "batches": [batch.to_dict() for batch in self.batches],
            "families": [batch.to_dict() for batch in self.families],
            "superseded": [item.to_dict() for item in self.superseded],
            "ready": [item.to_dict() for item in self.ready],
            "needs_human": [item.to_dict() for item in self.needs_human],
            "waiting_on_ci": [item.to_dict() for item in self.waiting_on_ci],
            "rivet_handling": [item.to_dict() for item in self.rivet_handling],
            "with_author": [item.to_dict() for item in self.with_author],
            "blocked_by_closer": list(self.blocked_by_closer),
            "notes": list(self.notes),
        }


def _not_run_row(pull: OpenPull) -> dict:
    """One head whose required checks have no run that counts, in the report's JSON form."""
    cause = pull.tests_not_run_cause or CAUSE_NEVER_REPORTED
    names = pull.tests_not_run or pull.missing_checks
    when = pull.ready_at.strftime("%Y-%m-%d %H:%M UTC") if pull.ready_at else ""
    return {
        "number": pull.number,
        "head_sha": pull.head_sha,
        "checks": list(names),
        "cause": cause,
        "draft": pull.draft,
        #: ``draft`` (the author's step), ``overdue`` (nothing will start it: RIVET or a person
        #: acts) or ``waiting`` (a run is due to start; nobody acts yet).
        "status": "draft" if pull.draft else ("overdue" if pull.ci_never_ran else "waiting"),
        "sentence": not_run_sentence(names, cause, head=pull.head_sha, ready_at=when),
        "fix": not_run_fix(cause, pull.number),
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
    tier: int | None = None,
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
        tier=tier,
    )


_FROM_CWD = object()


def _tier_map(given: object):
    """The tier map handed in, or this checkout's. A map that cannot be read reports no tier."""
    if given is not _FROM_CWD:
        return given
    from axiom.extensions.builtins.release.tiers import load_tier_map

    try:
        return load_tier_map(Path.cwd())
    except (ValueError, OSError):
        return None


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
    tier_map: object = _FROM_CWD,
    now: datetime | None = None,
) -> BacklogReport:
    """Classify ``pulls``. ``state_dir`` lets the auto-closer's claims exclude a PR.

    Each view carries the pull request's loop tier from the tier map (LOOP-S2). The
    tier is reported; it does not hold or land anything in this phase.
    """
    config = config or LanderConfig()
    tiers_in_force = _tier_map(tier_map)
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
    never_ran: list[dict] = []
    not_run: list[dict] = []
    waiting_run: list[dict] = []
    # Every held pull request is listed, also one that is superseded or that the
    # auto-closer holds: the owner is still the one it waits for.
    owner_holds = {
        pull.number: found
        for pull in pulls
        if (found := owner_hold.hold_for(pull, config, repo=repo)) is not None
    }
    held_rows = [owner_hold.row(pull, owner_holds[pull.number], now) for pull in pulls if pull.number in owner_holds]
    human: list[PullView] = []
    waiting: list[PullView] = []
    rivet: list[PullView] = []
    author: list[PullView] = []
    rules = config.extra_path_rules or None
    for pull in live:
        if pull.number in superseded_ids:
            continue
        reasons: list[str] = list(human_route(pull.files, extra=rules, replace=config.replaces_human_paths))
        for category in config_categories(pull.files, config):
            if category not in reasons:
                reasons.append(category)
        if pull.number in owner_holds:
            # First, so the reason a person reads is the one that decides it.
            reasons.insert(0, owner_hold.HELD_FOR_OWNER_REVIEW)
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
        if pull.checks == NOT_RUN or (pull.ci_never_ran and not pull.tests_not_run):
            # Its own state, listed above any count: no run of the required checks counts.
            if pull.checks != NOT_RUN:
                pull = replace(
                    pull, tests_not_run=pull.missing_checks, tests_not_run_cause=CAUSE_NEVER_REPORTED
                )
            not_run.append(_not_run_row(pull))
            if pull.draft:
                reasons.append(f"{TESTS_NOT_RUN}:{CAUSE_DRAFT}")
        if pull.waiting_for_run:
            listed = ", ".join(pull.waiting_for_run[:5])
            waiting_run.append(
                {
                    "number": pull.number,
                    "head_sha": pull.head_sha,
                    "checks": list(pull.waiting_for_run),
                    "sentence": f"waiting for its run: {listed} has no result yet, and a run for head "
                    f"{pull.head_sha[:7]} is queued or in progress. Nothing to do yet.",
                }
            )
        if pull.ci_never_ran:
            # Not "still running": no run will ever report these checks (lander.ci_start).
            chosen = ci_start.plan(pull, config, state_dir=state_dir, repo=repo)
            reasons.append(chosen.reason)
            never_ran.append(ci_start.row(pull, chosen))
        elif pull.checks == "unknown":
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
            tier=tiers_in_force.classify(pull.files).tier if tiers_in_force is not None else None,  # type: ignore[attr-defined]
        )
        if reasons:
            {"rivet": rivet, "author": author}.get(_owner(tuple(reasons)), human).append(view)
        elif pull.checks in {"pending", NOT_RUN}:
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
        ci_never_ran=never_ran,
        tests_not_run=not_run,
        waiting_for_run=waiting_run,
        held_for_owner_review=held_rows,
        needs_human=human,
        waiting_on_ci=waiting,
        rivet_handling=rivet,
        with_author=author,
        minutes_saved=saved,
        blocked_by_closer=blocked,
        notes=notes,
        open_count=len(pulls),
    )
