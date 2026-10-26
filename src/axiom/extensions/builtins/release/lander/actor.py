# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Diagnose, propose, and land. Execution waits on an approval.

The loop asks a ``FixProvider`` what is blocking a pull request and for
a diff. It files that diff with the existing approval gate. A human on
``raci.approvers`` approves protected paths. ``raci.auto_approver``,
when set, may approve a routine fix, and that identity must differ from
the fix writer. The approval is bound to the head SHA and the diff. A
new push or a passed TTL invalidates it. Force-push is not a command
this module can build. After approval the loop applies the diff, re-runs
CI, and lands only when that run is green, pinning the merge to that SHA.

A claim label is applied before the pass acts and removed when the pass
is finished or the claim is older than the TTL. A foreign claim label
skips the pull request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from axiom.agents.raci import ProposalDecision, RACILedger
from axiom.extensions.builtins.release.fixes import Fix, FixProvider, resolve_fix_provider
from axiom.extensions.builtins.release.lander.claim_label import (
    claim_expired,
    claim_record,
    record_claim,
    release_claim,
)
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.protect import protected_categories
from axiom.extensions.builtins.release.lander.reviews import hold_for_pull
from axiom.infra.orchestrator.actions import Action, ActionCategory, ActionStatus
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.raci import check_raci
from axiom.infra.switchplan.ledger import FileLedger, SwitchRecord, new_record, now as ledger_now
from axiom.policy.agent_action_guard import human_route

_FORCE_FLAGS = {"--force", "--force-with-lease", "-f"}


def _refuse_force(args: list[str] | tuple[str, ...]) -> None:
    for arg in args:
        if arg in _FORCE_FLAGS or arg.startswith("--force") or arg.startswith("+"):
            raise LanderRefused("force-push is not allowed")


_RACI_ACTION = {
    "repair": "code.commit",
    "batch": "code.commit",
    "update_branch": "code.commit",
    "merge": "code.commit",
    "enqueue": "code.commit",
    "close": "issue.close",
    "rerun": "code.commit",
}


def push_command(remote: str, ref: str) -> list[str]:
    """A normal push. A ref that would force-update is refused."""
    if not remote or not ref or ref.startswith("+") or ref.startswith("-"):
        raise LanderRefused("refusing a push that is not a plain ref update")
    command = ["git", "push", remote, ref]
    _refuse_force(command)
    return command


def merge_command(number: int, method: str, sha: str) -> list[str]:
    """Merge or enqueue, pinned to ``sha``. There is no force flag."""
    if method not in {"merge", "enqueue"}:
        raise LanderRefused(f"unknown land method {method!r}")
    if not sha:
        raise LanderRefused("refusing to land without the approved head")
    flag = "--auto" if method == "enqueue" else "--merge"
    command = ["gh", "pr", "merge", str(number), flag, "--match-head-commit", sha]
    _refuse_force(command)
    if "--match-head-commit" not in command or command[-1] != sha:
        raise LanderRefused("refusing to merge without the sha that passed CI")
    return command


def label_command(repo: str, number: int, label: str, *, remove: bool) -> list[str]:
    """Add or remove one claim label. There is no force flag."""
    if not repo or not label or label.startswith("-"):
        raise LanderRefused("refusing a claim label command without a repo and a label")
    flag = "--remove-label" if remove else "--add-label"
    command = ["gh", "pr", "edit", str(number), "--repo", repo, flag, label]
    _refuse_force(command)
    return command


def commands_for_side_branch(target_ref: str, fix_branch: str, head_sha: str) -> tuple[tuple[str, ...], ...]:
    """Fetch a side branch and merge it onto the approved head.

    The sequence is fetch, an ancestor check, detach at the approved
    head, fast-forward, and — only when fast-forward is refused — a
    merge commit. The push updates the pull-request branch and carries
    no force flag. ``merge-base --is-ancestor`` must succeed before
    either merge; a stale branch is not applied.
    """
    if not target_ref or not fix_branch or not head_sha:
        raise LanderRefused("refusing to apply a side branch without a target, a branch, and the approved head")
    if target_ref.startswith(("-", "+")) or fix_branch.startswith(("-", "+")):
        raise LanderRefused("refusing a push that is not a plain ref update")
    commands = (
        ("git", "fetch", "origin", fix_branch),
        ("git", "merge-base", "--is-ancestor", head_sha, "FETCH_HEAD"),
        ("git", "checkout", "--detach", head_sha),
        ("git", "merge", "--ff-only", "FETCH_HEAD"),
        ("git", "merge", "--no-ff", "--no-edit", "FETCH_HEAD"),
        tuple(push_command("origin", f"HEAD:{target_ref}")),
    )
    for command in commands:
        _refuse_force(command)
    return commands


class GitEffects:
    """Apply an approved fix with git and gh. Never force-pushes."""

    def __init__(self, *, repo: str, token_env: str) -> None:
        self.repo = repo
        self.token_env = token_env

    def _run(self, args: list[str]) -> str:
        import os
        import subprocess

        _refuse_force(args)
        env = os.environ.copy()
        token = env.get(self.token_env, "")
        if token and self.token_env != "GH_TOKEN":
            env["GH_TOKEN"] = token
        done = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False, env=env)
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "command failed").strip()
            if token:
                detail = detail.replace(token, "[redacted]")
            raise LanderRefused(detail[:300])
        return done.stdout

    def head(self, number: int) -> str:
        import json

        raw = self._run(["gh", "pr", "view", str(number), "--repo", self.repo, "--json", "headRefOid"])
        return str(json.loads(raw)["headRefOid"])

    def apply(self, number: int, fix: Fix, head_sha: str) -> str:
        del number
        if fix.force or self.head_matches_force(fix):
            raise LanderRefused("force-push is not allowed")
        if fix.branch:
            return self._apply_side_branch(fix, head_sha)
        for command in fix.commands:
            if "--force" in command.split() or command.strip().split()[:2] == ["git", "push"]:
                raise LanderRefused("a fix may not push itself")
            self._run(["sh", "-c", command])
        current = self._run(["git", "rev-parse", "HEAD"]).strip()
        if current != head_sha and fix.commands:
            self._run(push_command("origin", "HEAD"))
        return self._run(["git", "rev-parse", "HEAD"]).strip()

    @staticmethod
    def head_matches_force(fix: Fix) -> bool:
        parts = fix.diff.split()
        return "--force" in parts or (len(parts) >= 2 and parts[0] == "push" and parts[1] in {"-f", "--force"})

    def _apply_side_branch(self, fix: Fix, head_sha: str) -> str:
        target = fix.target_ref or "HEAD"
        fetch, ancestor, detach, fast_forward, merge_commit, push = commands_for_side_branch(
            target, fix.branch, head_sha
        )
        self._run(list(fetch))
        self._run(list(ancestor))
        self._run(list(detach))
        try:
            self._run(list(fast_forward))
        except LanderRefused:
            self._run(list(merge_commit))
        self._run(list(push))
        return self._run(["git", "rev-parse", "HEAD"]).strip()

    def add_label(self, number: int, label: str) -> None:
        self._run(label_command(self.repo, number, label, remove=False))

    def remove_label(self, number: int, label: str) -> None:
        self._run(label_command(self.repo, number, label, remove=True))

    def rerun(self, number: int, sha: str) -> str:
        """The check rollup on ``sha``. Not passing means do not land."""
        import json

        from axiom.extensions.builtins.release.lander.adapters import checks_from_rollup

        try:
            raw = self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    self.repo,
                    "--json",
                    "headRefOid,statusCheckRollup",
                ]
            )
        except LanderRefused:
            return "unknown"
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return "unknown"
        if not isinstance(data, dict) or str(data.get("headRefOid") or "") != sha:
            return "unknown"
        return checks_from_rollup(data.get("statusCheckRollup"))

    def land(self, number: int, sha: str, method: str) -> None:
        self._run(merge_command(number, method, sha))

    def close(self, number: int, sha: str) -> None:
        del sha
        self._run(["gh", "pr", "close", str(number), "--repo", self.repo])


class Effects(Protocol):
    def head(self, number: int) -> str: ...

    def apply(self, number: int, fix: Fix, head_sha: str) -> str: ...

    def rerun(self, number: int, sha: str) -> str: ...

    def land(self, number: int, sha: str, method: str) -> None: ...

    def close(self, number: int, sha: str) -> None: ...

    def add_label(self, number: int, label: str) -> None: ...

    def remove_label(self, number: int, label: str) -> None: ...


@dataclass
class ActorOutcome:
    number: int
    status: str
    reason: str = ""
    action_id: str = ""
    head_sha: str = ""
    steps: tuple[str, ...] = ()


@dataclass
class ActorResult:
    outcomes: list[ActorOutcome] = field(default_factory=list)
    stopped: str = ""

    def to_dict(self) -> dict:
        return {
            "stopped": self.stopped,
            "outcomes": [
                {
                    "number": item.number,
                    "status": item.status,
                    "reason": item.reason,
                    "action_id": item.action_id,
                    "head_sha": item.head_sha,
                    "steps": list(item.steps),
                }
                for item in self.outcomes
            ],
        }


def killed(config: LanderConfig, environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(config.kill_switch_env, "") == "1"


def fixer_block(pull: OpenPull, config: LanderConfig) -> str:
    """Ineligibility reported before a fixer is asked. Empty means ask.

    A draft, a human review hold, and a protected path are the reason.
    They are not handed to the fixer, even when checks are red and no
    webhook is configured.
    """
    hold = hold_for_pull(pull)
    if hold:
        return hold
    if pull.draft:
        return "draft"
    categories = protected_categories(
        pull.files,
        extra=config.extra_path_rules or None,
        dependency_patterns=config.dependency_patterns,
        test_setup_patterns=config.test_setup_patterns,
    )
    if categories:
        return "protected:" + ",".join(categories)
    return ""


def landing_block(pull: OpenPull, config: LanderConfig) -> str:
    """Why a green pull request must not be landed. Empty means it may.

    A pending human review request and a human changes-requested review
    block landing. Protected paths are flagged and not landed. Checks
    that are not passing are not landed.
    """
    blocked = fixer_block(pull, config)
    if blocked:
        return blocked
    if pull.mergeable is False:
        return "conflict"
    if pull.checks != "passing":
        return "checks not green"
    return ""


def _hold_status(reason: str) -> str:
    if reason.startswith("waiting on") or reason.startswith("changes requested"):
        return "waiting"
    return "escalated"


def posture(action_type: str, paths: tuple[str, ...], config: LanderConfig, ledger: RACILedger) -> str:
    """``approve``, ``auto``, or ``skip``. Human-routed paths are always approve."""
    if human_route(paths, extra=config.extra_path_rules or None):
        return "approve"
    if action_type not in config.auto_allowed:
        return "approve"
    level = check_raci(_RACI_ACTION.get(action_type, "code.commit"))
    if level == "approve":
        return "approve"
    decision = ledger.propose(f"release.{action_type}")
    if decision is ProposalDecision.SKIP:
        return "skip"
    if decision is ProposalDecision.AUTO:
        return "auto"
    return "approve"


def answer(
    gate: ApprovalGate,
    action_id: str,
    *,
    who: str,
    roster: tuple[str, ...],
    accept: bool,
    reason: str = "",
) -> Action | None:
    """Record a human decision. The person must be on the Accountable roster."""
    if who not in roster:
        raise LanderRefused(f"{who} is not an Accountable approver")
    if accept:
        return gate.approve(action_id, decided_by=who)
    return gate.reject(action_id, reason, decided_by=who)


def _matching(gate: ApprovalGate, number: int, head_sha: str, diff_sha: str) -> Action | None:
    found: list[Action] = []
    for action in gate.all_actions():
        params = action.params or {}
        if params.get("pr") == number and params.get("head_sha") == head_sha and params.get("diff_sha") == diff_sha:
            found.append(action)
    if not found:
        return None
    return found[-1]


def _touched(pull: OpenPull, fix: Fix) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*pull.files, *fix.paths)))


def _categories(paths: tuple[str, ...], config: LanderConfig) -> tuple[str, ...]:
    return protected_categories(
        paths,
        extra=config.extra_path_rules or None,
        dependency_patterns=config.dependency_patterns,
        test_setup_patterns=config.test_setup_patterns,
    )


def approval_route(
    action_type: str,
    paths: tuple[str, ...],
    config: LanderConfig,
    ledger: RACILedger,
    writer: str,
) -> tuple[str, str]:
    """``approve``, ``auto``, ``auto-reviewer``, or ``skip``, plus a rationale.

    ``auto_approver`` may approve a routine fix. Protected paths stay with
    ``raci.approvers``. The reviewer must differ from the fix writer.
    """
    if _categories(paths, config):
        return "approve", ""
    if config.auto_approver and config.auto_approver != writer:
        who = writer or "built-in"
        rationale = (
            f"non-protected {action_type}; reviewer {config.auto_approver} differs from fix writer {who}"
        )
        return "auto-reviewer", rationale
    return posture(action_type, paths, config, ledger), ""


def _approval_current(action: Action) -> bool:
    until = str((action.params or {}).get("approved_until") or "")
    if not until:
        return True
    try:
        deadline = datetime.fromisoformat(until)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC) <= deadline


def _add_label(effects: Effects, number: int, label: str) -> None:
    fn = getattr(effects, "add_label", None)
    if fn is not None and label:
        fn(number, label)


def _remove_label(effects: Effects, number: int, label: str) -> None:
    fn = getattr(effects, "remove_label", None)
    if fn is not None and label:
        fn(number, label)


def _submit(
    gate: ApprovalGate,
    *,
    config: LanderConfig,
    number: int,
    action_type: str,
    head_sha: str,
    diff: str,
    diff_sha: str,
    evidence: str,
    paths: tuple[str, ...],
    branch: str = "",
    target_ref: str = "",
    writer: str = "",
) -> Action:
    accountable = config.approvers[0] if config.approvers else ""
    action = Action(
        name=f"release.{action_type}",
        category=ActionCategory.WRITE,
        reason=evidence[:500],
        params={
            "pr": number,
            "action_type": action_type,
            "head_sha": head_sha,
            "diff": diff,
            "diff_sha": diff_sha,
            "accountable": accountable,
            "land_if_green": action_type in {"repair", "rerun", "merge", "enqueue", "update_branch"},
            "paths": list(paths),
            "branch": branch,
            "target_ref": target_ref,
            "writer": writer,
        },
    )
    return gate.submit(action)


def _ledger(state_dir: Path) -> FileLedger:
    return FileLedger(Path(state_dir) / "agents" / "rivet" / "lander-records")


def _audit(state_dir: Path, repo: str, step: str, detail: str) -> None:
    ledger = _ledger(state_dir)
    record = new_record("release.actor", repo)
    record.phase = "planned"
    record.event(step, detail)
    record.state = {"step": step}
    ledger.save(record)


def _audit_auto(
    state_dir: Path,
    repo: str,
    *,
    reviewer: str,
    rationale: str,
    number: int,
    head_sha: str,
    diff_sha: str,
) -> None:
    """One ledger record per automatic approval. Names the reviewer and why."""
    moment = ledger_now().isoformat()
    record = SwitchRecord(
        id=new_record("release.auto-approve", repo).id,
        kind="release.auto-approve",
        subject=f"{repo}#{number}",
        phase="committed",
        created_at=moment,
        updated_at=moment,
        approver=reviewer,
        state={
            "reviewer": reviewer,
            "rationale": rationale,
            "pr": number,
            "head_sha": head_sha,
            "diff_sha": diff_sha,
        },
    )
    record.event("auto-approve", f"{reviewer}: {rationale}"[:500])
    _ledger(state_dir).save(record)


def _land_pinned(
    *,
    effects: Effects,
    action: Action,
    number: int,
    sha: str,
    method: str,
    steps: list[str],
) -> ActorOutcome:
    """Land only when CI is green on ``sha``, and pin the merge to that sha."""
    state = effects.rerun(number, sha)
    steps.append("verify")
    if state != "passing":
        action.complete({"applied": False, "sha": sha, "checks": state})
        return ActorOutcome(
            number,
            "verified",
            state or "checks not green",
            action.action_id,
            sha,
            tuple(steps),
        )
    current = effects.head(number)
    if current != sha:
        return ActorOutcome(
            number, "invalidated", "head moved", action.action_id, current, tuple(steps + ["invalidated"])
        )
    effects.land(number, sha, method)
    steps.append("land")
    action.complete({"landed": True, "sha": sha})
    return ActorOutcome(number, "landed", "", action.action_id, sha, tuple(steps))


def _execute(
    *,
    effects: Effects,
    action: Action,
    method: str,
    steps: list[str],
) -> ActorOutcome:
    params = action.params
    number = int(params["pr"])
    sha = str(params["head_sha"])
    current = effects.head(number)
    if current != sha:
        return ActorOutcome(number, "invalidated", "head moved", action.action_id, current, tuple(steps + ["invalidated"]))
    action_type = str(params.get("action_type"))
    steps.append("apply")
    if action_type in {"merge", "enqueue"}:
        return _land_pinned(
            effects=effects,
            action=action,
            number=number,
            sha=sha,
            method=action_type if action_type == "enqueue" else method,
            steps=steps,
        )
    if action_type == "close":
        effects.close(number, sha)
        steps.append("close")
        action.complete({"closed": True})
        return ActorOutcome(number, "landed", "closed", action.action_id, sha, tuple(steps))
    fix = Fix(
        summary=action_type,
        diff=str(params.get("diff") or ""),
        kind=action_type,
        branch=str(params.get("branch") or ""),
        target_ref=str(params.get("target_ref") or ""),
        writer=str(params.get("writer") or ""),
        paths=tuple(params.get("paths") or ()),
    )
    if fix.force:
        raise LanderRefused("force-push is not allowed")
    new_sha = effects.apply(number, fix, sha)
    steps.append("verify")
    state = effects.rerun(number, new_sha)
    if state == "passing" and params.get("land_if_green"):
        if effects.head(number) != new_sha:
            return ActorOutcome(
                number, "invalidated", "head moved", action.action_id, effects.head(number), tuple(steps + ["invalidated"])
            )
        effects.land(number, new_sha, method)
        steps.append("land")
        action.complete({"landed": True, "sha": new_sha})
        return ActorOutcome(number, "landed", "", action.action_id, new_sha, tuple(steps))
    action.complete({"applied": True, "sha": new_sha, "checks": state})
    return ActorOutcome(
        number,
        "applied" if state != "passing" else "verified",
        state,
        action.action_id,
        new_sha,
        tuple(steps),
    )


def run_actor(
    pulls: list[OpenPull],
    *,
    repo: str,
    config: LanderConfig,
    state_dir: Path,
    effects: Effects,
    gate: ApprovalGate,
    provider: FixProvider | None = None,
    ledger: RACILedger | None = None,
    environ: dict[str, str] | None = None,
) -> ActorResult:
    """One pass over ``pulls``. Unapproved work is proposed and not executed."""
    if not config.approvers:
        raise LanderRefused("name an Accountable approver before RIVET can act")
    if killed(config, environ):
        return ActorResult(stopped="kill-switch")
    provider = provider or resolve_fix_provider(config.fix_provider)
    bind = getattr(provider, "bind", None)
    if bind is not None:
        bind(
            config=config,
            state_dir=state_dir,
            repo=repo,
            environ=os.environ if environ is None else environ,
        )
    ledger = ledger or RACILedger.load(Path(state_dir) / "agents" / "rivet" / "raci-ledger.json")
    method = config.land_method
    result = ActorResult()

    def settle(outcome: ActorOutcome) -> None:
        if outcome.status != "waiting":
            _remove_label(effects, outcome.number, config.claim_label)
            release_claim(state_dir, repo=repo, number=outcome.number)
        result.outcomes.append(outcome)

    for pull in pulls:
        if killed(config, environ):
            result.stopped = "kill-switch"
            return result
        foreign = [label for label in pull.labels if label in set(config.foreign_claim_labels)]
        if foreign:
            if claim_record(state_dir, repo, pull.number) is not None:
                _remove_label(effects, pull.number, config.claim_label)
                release_claim(state_dir, repo=repo, number=pull.number)
            result.outcomes.append(
                ActorOutcome(
                    pull.number,
                    "skipped",
                    "foreign-claim:" + ",".join(foreign),
                    "",
                    pull.head_sha,
                    ("skip",),
                )
            )
            continue
        if claim_expired(claim_record(state_dir, repo, pull.number), ttl_s=config.claim_ttl_s):
            _remove_label(effects, pull.number, config.claim_label)
            release_claim(state_dir, repo=repo, number=pull.number)
            result.outcomes.append(
                ActorOutcome(pull.number, "claim-expired", config.claim_label, "", pull.head_sha, ("claim-expired",))
            )
            continue
        _add_label(effects, pull.number, config.claim_label)
        try:
            claimed_sha = effects.head(pull.number)
        except Exception:
            claimed_sha = pull.head_sha
        record_claim(
            state_dir,
            repo=repo,
            number=pull.number,
            label=config.claim_label,
            head_sha=claimed_sha,
        )
        attempts = 0
        steps: list[str] = []
        while attempts < config.max_rounds:
            if killed(config, environ):
                result.stopped = "kill-switch"
                return result
            sha = effects.head(pull.number)
            stale = [
                action
                for action in gate.all_actions()
                if action.params.get("pr") == pull.number
                and action.status == ActionStatus.APPROVED
                and action.params.get("head_sha") not in ("", sha)
            ]
            if stale:
                settle(
                    ActorOutcome(
                        pull.number,
                        "invalidated",
                        "head moved",
                        stale[-1].action_id,
                        sha,
                        tuple(steps + ["invalidated"]),
                    )
                )
                break
            diagnosis = provider.diagnose(pull)
            steps.append("diagnose")
            fix = provider.draft(diagnosis, pull)
            if fix.force:
                raise LanderRefused("force-push is not allowed")
            if fix.pending:
                _audit(state_dir, repo, "handoff", f"{pull.number} {fix.branch}")
                settle(ActorOutcome(pull.number, "waiting", "handoff", "", sha, tuple(steps + ["handoff"])))
                break
            if diagnosis.kind == "clear":
                action_type = method
                diff = ""
                if action_type in config.auto_allowed:
                    sha = effects.head(pull.number)
                    block = landing_block(pull, config)
                    if block:
                        settle(
                            ActorOutcome(
                                pull.number,
                                _hold_status(block),
                                block,
                                "",
                                sha,
                                tuple(steps + ["hold"]),
                            )
                        )
                        break
                    submitted = _submit(
                        gate,
                        config=config,
                        number=pull.number,
                        action_type=action_type,
                        head_sha=sha,
                        diff="",
                        diff_sha=Fix(summary="land").diff_sha,
                        evidence=diagnosis.evidence or fix.summary,
                        paths=pull.files,
                        writer=fix.writer,
                    )
                    submitted.approve(decided_by=config.approvers[0] if config.approvers else "lander")
                    gate.record(submitted)
                    try:
                        outcome = _land_pinned(
                            effects=effects,
                            action=submitted,
                            number=pull.number,
                            sha=sha,
                            method=method,
                            steps=steps,
                        )
                    except LanderRefused as exc:
                        settle(
                            ActorOutcome(
                                pull.number,
                                "escalated",
                                str(exc)[:300],
                                submitted.action_id,
                                sha,
                                tuple(steps + ["hold"]),
                            )
                        )
                        break
                    settle(outcome)
                    break
            elif fix.needs_human:
                _audit(state_dir, repo, "escalate", f"{pull.number} {fix.reason}")
                settle(
                    ActorOutcome(pull.number, "escalated", fix.reason or diagnosis.kind, "", sha, tuple(steps + ["escalate"]))
                )
                break
            else:
                action_type = "rerun" if fix.kind == "rerun" else "repair"
                diff = fix.diff
            paths = _touched(pull, fix)
            route, rationale = approval_route(action_type, paths, config, ledger, fix.writer)
            if route == "skip":
                settle(ActorOutcome(pull.number, "escalated", "raci-skip", "", sha, tuple(steps)))
                break
            diff_sha = fix.diff_sha if diff else Fix(summary="land").diff_sha
            existing = _matching(gate, pull.number, sha, diff_sha)
            if existing is None:
                submitted = _submit(
                    gate,
                    config=config,
                    number=pull.number,
                    action_type=action_type,
                    head_sha=sha,
                    diff=diff,
                    diff_sha=diff_sha,
                    evidence=diagnosis.evidence or fix.summary,
                    paths=paths,
                    branch=fix.branch,
                    target_ref=fix.target_ref,
                    writer=fix.writer,
                )
                steps.append("propose")
                _audit(state_dir, repo, "propose", f"{pull.number} {action_type} {sha[:12]}")
                existing = submitted
                if route == "auto":
                    existing.approve(decided_by=config.approvers[0])
                    gate.record(existing)
                elif route != "auto-reviewer":
                    settle(ActorOutcome(pull.number, "waiting", action_type, submitted.action_id, sha, tuple(steps)))
                    break
            if route == "auto-reviewer" and existing.status == ActionStatus.PENDING:
                approved = gate.approve(existing.action_id, decided_by=config.auto_approver)
                if approved is None or approved.status != ActionStatus.APPROVED:
                    settle(
                        ActorOutcome(pull.number, "escalated", "auto-approve failed", existing.action_id, sha, tuple(steps))
                    )
                    break
                approved.params["approved_until"] = (
                    datetime.now(UTC) + timedelta(hours=config.approval_ttl_hours)
                ).isoformat()
                approved.params["reviewer"] = config.auto_approver
                approved.params["rationale"] = rationale
                gate.record(approved)
                _audit_auto(
                    state_dir,
                    repo,
                    reviewer=config.auto_approver,
                    rationale=rationale,
                    number=pull.number,
                    head_sha=sha,
                    diff_sha=str(approved.params.get("diff_sha") or diff_sha),
                )
                existing = approved
            if existing.status == ActionStatus.PENDING:
                settle(ActorOutcome(pull.number, "waiting", action_type, existing.action_id, sha, tuple(steps)))
                break
            if existing.status == ActionStatus.REJECTED:
                reason = existing.error or "rejected"
                kind = "expired" if reason.startswith("expired") else "rejected"
                settle(ActorOutcome(pull.number, kind, reason, existing.action_id, sha, tuple(steps)))
                break
            if existing.status != ActionStatus.APPROVED:
                settle(
                    ActorOutcome(pull.number, "escalated", existing.status.value, existing.action_id, sha, tuple(steps))
                )
                break
            allowed = set(config.approvers)
            if config.auto_approver and not _categories(tuple(existing.params.get("paths") or ()), config):
                allowed.add(config.auto_approver)
            if existing.decided_by not in allowed:
                settle(
                    ActorOutcome(pull.number, "escalated", "approver not on the roster", existing.action_id, sha, tuple(steps))
                )
                break
            if not _approval_current(existing):
                settle(ActorOutcome(pull.number, "expired", "approval ttl", existing.action_id, sha, tuple(steps)))
                break
            if effects.head(pull.number) != str(existing.params.get("head_sha")):
                settle(
                    ActorOutcome(
                        pull.number,
                        "invalidated",
                        "head moved",
                        existing.action_id,
                        effects.head(pull.number),
                        tuple(steps + ["invalidated"]),
                    )
                )
                break
            if str(existing.params.get("action_type") or "") in {"merge", "enqueue"}:
                hold = hold_for_pull(pull)
                if hold:
                    settle(
                        ActorOutcome(
                            pull.number,
                            "waiting",
                            hold,
                            existing.action_id,
                            sha,
                            tuple(steps + ["hold"]),
                        )
                    )
                    break
            steps.append("approve")
            outcome = _execute(effects=effects, action=existing, method=method, steps=steps)
            gate.record(existing)
            _audit(state_dir, repo, outcome.status, f"{pull.number} {outcome.head_sha[:12]}")
            attempts += 1
            if outcome.status in {"landed", "invalidated"}:
                settle(outcome)
                break
            if outcome.status == "applied":
                if attempts >= config.max_rounds:
                    settle(
                        ActorOutcome(pull.number, "escalated", "round cap", outcome.action_id, outcome.head_sha, outcome.steps)
                    )
                    break
                pull = OpenPull(
                    number=pull.number,
                    title=pull.title,
                    head_sha=outcome.head_sha,
                    head_ref=pull.head_ref,
                    base_ref=pull.base_ref,
                    files=pull.files,
                    labels=pull.labels,
                    checks=outcome.reason or "failing",
                    failure_log=pull.failure_log,
                    mergeable=pull.mergeable,
                    ci_attempts=pull.ci_attempts + 1,
                    failing_checks=pull.failing_checks,
                )
                continue
            settle(outcome)
            break
        else:
            settle(ActorOutcome(pull.number, "escalated", "round cap", "", pull.head_sha, tuple(steps)))
    try:
        ledger.save(Path(state_dir) / "agents" / "rivet" / "raci-ledger.json")
    except Exception:
        pass
    return result
