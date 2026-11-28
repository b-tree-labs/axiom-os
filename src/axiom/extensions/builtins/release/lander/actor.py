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

A claim label is applied only when the pass is about to act on a pull
request (land it, start its CI, propose or apply a fix, update its branch)
and removed when that action ends or the claim is older than the TTL. A
draft, and a pull request the pass only reports on, is never claimed: a
label added and removed on every open pull request each sweep moved every
one's update time and said nothing. A foreign claim label skips the pull
request.

A head no workflow run ever started on (``lander.ci_start``) gets its CI
started once, by a close and a reopen, and goes to a person after that.

A pull request held for its owner (``lander.owner_hold``: a hold label, or a
pending review request to an owner) is passed over before any of that, draft
or ready and whatever its checks say. The pass makes no call on it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from axiom.agents.raci import ProposalDecision, RACILedger
from axiom.extensions.builtins.release.ci_health.budget import LandingPolicy, is_release_pull
from axiom.extensions.builtins.release.ci_health.update_branch import BEHIND, plan_update_branch
from axiom.extensions.builtins.release.fixes import Fix, FixProvider, resolve_fix_provider
from axiom.extensions.builtins.release.lander import ci_start, owner_hold
from axiom.extensions.builtins.release.lander.checks import not_run_fix
from axiom.extensions.builtins.release.lander.claim_label import (
    claim_expired,
    claim_record,
    record_claim,
    release_claim,
)
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.drift import DriftOutcome, DriftWiring, resolve_drift
from axiom.extensions.builtins.release.lander.drift import enabled as drift_enabled
from axiom.extensions.builtins.release.lander.drift_ledger import DriftLedger
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.protect import config_categories
from axiom.extensions.builtins.release.lander.reviews import hold_for_pull
from axiom.infra.orchestrator.actions import Action, ActionCategory, ActionStatus
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.raci import check_raci
from axiom.infra.switchplan.ledger import FileLedger, SwitchRecord, new_record, now as ledger_now
from axiom.policy.agent_action_guard import human_route

_log = logging.getLogger(__name__)
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


def update_branch_command(repo: str, number: int, sha: str) -> list[str]:
    """Update one pull request from its base. Refused without the head it was planned on."""
    if not repo or not sha:
        raise LanderRefused("refusing to update a branch without a repo and the planned head")
    command = [
        "gh",
        "api",
        "-X",
        "PUT",
        f"repos/{repo}/pulls/{int(number)}/update-branch",
        "--raw-field",
        f"expected_head_sha={sha}",
    ]
    _refuse_force(command)
    return command


def label_command(repo: str, number: int, label: str, *, remove: bool) -> list[str]:
    """Add or remove one claim label. There is no force flag."""
    if not repo or not label or label.startswith("-"):
        raise LanderRefused("refusing a claim label command without a repo and a label")
    flag = "--remove-label" if remove else "--add-label"
    command = ["gh", "pr", "edit", str(number), "--repo", repo, flag, label]
    _refuse_force(command)
    return command


def close_command(repo: str, number: int) -> list[str]:
    """Close one pull request, with no comment and no branch delete. Half of a CI start."""
    if not repo:
        raise LanderRefused("refusing to close a pull request without a repo")
    command = ["gh", "pr", "close", str(int(number)), "--repo", repo]
    _refuse_force(command)
    return command


def reopen_command(repo: str, number: int) -> list[str]:
    """Reopen it. The forge then delivers ``reopened`` and the pull-request workflows start."""
    if not repo:
        raise LanderRefused("refusing to reopen a pull request without a repo")
    command = ["gh", "pr", "reopen", str(int(number)), "--repo", repo]
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
        self._required: dict[str, tuple[str, ...]] = {}

    def _run(self, args: list[str], *, token_env: str = "") -> str:
        import os
        import subprocess

        _refuse_force(args)
        env = os.environ.copy()
        token_name = token_env or self.token_env
        token = env.get(token_name, "")
        if token and token_name != "GH_TOKEN":
            env["GH_TOKEN"] = token
        done = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False, env=env)
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "command failed").strip()
            if token:
                detail = detail.replace(token, "[redacted]")
            raise LanderRefused(detail[:300])
        return done.stdout

    def main_green_at(self, base: str) -> datetime | None:
        """When the base branch last had a commit whose checks all passed: that commit's last
        check to finish. None when it cannot be read. Used to tell a pull request's stale red
        result (main has been green since) from a fresh one."""
        import json

        owner, _, name = self.repo.partition("/")
        query = (
            "query($o:String!,$r:String!,$b:String!){repository(owner:$o,name:$r){ref(qualifiedName:$b)"
            "{target{...on Commit{history(first:15){nodes{oid statusCheckRollup{state contexts(first:100)"
            "{nodes{__typename ...on CheckRun{completedAt}}}}}}}}}}}"
        )
        try:
            raw = self._run(
                ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"o={owner}", "-f", f"r={name}",
                 "-f", f"b=refs/heads/{base}"]
            )
            nodes = json.loads(raw)["data"]["repository"]["ref"]["target"]["history"]["nodes"]
        except (LanderRefused, ValueError, KeyError, TypeError):
            return None
        for node in nodes or []:
            rollup = (node or {}).get("statusCheckRollup") or {}
            if rollup.get("state") != "SUCCESS":
                continue
            stamps = [
                _parse_iso(item.get("completedAt"))
                for item in (rollup.get("contexts") or {}).get("nodes") or []
                if item.get("__typename") == "CheckRun"
            ]
            stamps = [stamp for stamp in stamps if stamp is not None]
            if stamps:
                return max(stamps)
        return None

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

    def required_checks(self, base: str) -> tuple[str, ...]:
        """The checks the base branch's protection and rulesets require. Read once per base."""
        import json

        from axiom.extensions.builtins.release.lander.checks import required_contexts

        if base in self._required:
            return self._required[base]
        protection = None
        rules: list = []
        try:
            protection = json.loads(self._run(["gh", "api", f"repos/{self.repo}/branches/{base}/protection"]) or "null")
        except (LanderRefused, json.JSONDecodeError, RuntimeError):
            protection = None
        try:
            rules = json.loads(self._run(["gh", "api", f"repos/{self.repo}/rules/branches/{base}"]) or "[]")
        except (LanderRefused, json.JSONDecodeError, RuntimeError):
            rules = []
        found = required_contexts(protection if isinstance(protection, dict) else None, rules if isinstance(rules, list) else [])
        self._required[base] = found
        return found

    def rerun(self, number: int, sha: str) -> str:
        """The checks on ``sha``: ``passing`` only when every required check reports success.

        An empty rollup, a required check that has not registered, or one still
        running is ``pending``. Anything not ``passing`` is not landed (#1782).
        """
        import json

        from axiom.extensions.builtins.release.lander.checks import FAILURE, NOT_RUN, SUCCESS, check_verdict

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
                    "headRefOid,baseRefName,isDraft,statusCheckRollup,labels",
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
        required = self.required_checks(str(data.get("baseRefName") or "main"))
        rollup = data.get("statusCheckRollup")
        draft = bool(data.get("isDraft"))
        labels = [
            str(item.get("name") if isinstance(item, dict) else item)
            for item in (data.get("labels") or [])
        ]
        verdict = check_verdict(rollup, required, draft=draft, labels=labels)
        if verdict.state == SUCCESS:
            # Green on its face. A draft's run is green without running the suite (#2410), so
            # the evidence is a run that started after the last ready-for-review event.
            ready_at, known = self._ready_at(number)
            verdict = check_verdict(
                rollup, required, ready_at=ready_at, ready_known=known, draft=draft, labels=labels
            )
            if verdict.unreadable:
                return "unknown"
        if verdict.not_run:
            return NOT_RUN
        return {SUCCESS: "passing", FAILURE: "failing"}.get(verdict.state, "pending")

    def _ready_at(self, number: int):
        """``(time, known)``: the last ready-for-review event of one pull request."""
        import json

        from axiom.extensions.builtins.release.lander.adapters import _READY, ready_time

        owner, _, name = self.repo.partition("/")
        try:
            raw = self._run(
                ["gh", "api", "graphql", "-f", f"query={_READY}", "-f", f"owner={owner}", "-f", f"name={name}",
                 "-F", f"number={int(number)}"]
            )
            return ready_time(json.loads(raw)), True
        except (LanderRefused, ValueError, TypeError):
            return None, False

    def land(self, number: int, sha: str, method: str) -> None:
        self._run(merge_command(number, method, sha))

    def update_branch(self, number: int, sha: str) -> str:
        """Merge the base into one pull request, pinned to the head we saw."""
        self._run(update_branch_command(self.repo, number, sha))
        return self.head(number)

    def close(self, number: int, sha: str) -> None:
        del sha
        self._run(["gh", "pr", "close", str(number), "--repo", self.repo])

    def start_ci(self, number: int, sha: str, *, token_env: str = "") -> None:
        """Close and reopen one pull request so the forge starts its pull-request workflows.

        Pinned to the head that was judged. The reopen is tried twice, and a pull
        request left closed is named in the error so a person reopens it.
        """
        if self.head(number) != sha:
            raise LanderRefused("head moved before CI could be started")
        self._run(close_command(self.repo, number), token_env=token_env)
        try:
            self._run(reopen_command(self.repo, number), token_env=token_env)
        except LanderRefused:
            try:
                self._run(reopen_command(self.repo, number), token_env=token_env)
            except LanderRefused as exc:
                raise LanderRefused(
                    f"closed #{number} to start CI and could not reopen it; reopen it by hand: {exc}"[:300]
                ) from exc


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


class _NoLanding:
    """Every effect of the wrapped one except the merge, which is refused (``land = false``)."""

    def __init__(self, effects: Effects) -> None:
        self._effects = effects

    def __getattr__(self, name: str):
        return getattr(self._effects, name)

    def land(self, number: int, sha: str, method: str) -> None:
        raise LanderRefused("land = false in the config: this run lands nothing")


def guard_landing(effects: Effects, config: LanderConfig) -> Effects:
    """``effects`` unchanged, or with the merge refused when the config says ``land = false``."""
    return effects if config.land else _NoLanding(effects)  # type: ignore[return-value]


def killed(config: LanderConfig, environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(config.kill_switch_env, "") == "1"


def fixer_block(pull: OpenPull, config: LanderConfig) -> str:
    """Ineligibility reported before a fixer is asked. Empty means ask.

    A draft, a human review hold, and a protected path are the reason.
    They are not handed to the fixer, even when checks are red and no
    webhook is configured. A pull request held for its owner is named
    first: nothing touches it until the hold is released.
    """
    waiting = owner_hold.hold_for(pull, config)
    if waiting is not None:
        return waiting.detail
    hold = hold_for_pull(pull)
    if hold:
        return hold
    if pull.draft:
        return "draft"
    categories = config_categories(pull.files, config)
    if categories:
        return "protected:" + ",".join(categories)
    return ""


def tests_not_run_reason(pull: OpenPull) -> str:
    """The hold reason for a head whose required checks have no run that counts."""
    names = ", ".join(pull.tests_not_run) or "its required checks"
    return f"tests not run: {names}; {not_run_fix(pull.tests_not_run_cause, pull.number)}"


def landing_block(pull: OpenPull, config: LanderConfig) -> str:
    """Why a green pull request must not be landed. Empty means it may.

    A pending human review request and a human changes-requested review
    block landing. Protected paths are flagged and not landed. Checks
    that are not passing are not landed.

    The risk-tier review is computed and logged in shadow. The string
    returned here is the live hold, unchanged by that tier.
    """
    reason = fixer_block(pull, config)
    if not reason and not config.land:
        reason = "land = false in the config: this run lands nothing"
    if not reason and pull.mergeable is False:
        reason = "conflict"
    if not reason and pull.checks == "not_run":
        reason = tests_not_run_reason(pull)
    if not reason and pull.checks != "passing":
        reason = "checks not green"
    _shadow(pull, config, action="merge", live_block=reason, kind="hold")
    return reason


def _shadow(pull: OpenPull, config: LanderConfig, *, action: str, live_block: str, kind: str) -> None:
    """Record the shadow tier. A failure here leaves the live decision alone."""
    try:
        from axiom.infra.raci_review import observe_landing

        observe_landing(pull, config, action=action, live_block=live_block, kind=kind)
    except Exception:
        _log.debug("risk-tier shadow skipped", exc_info=True)


def _shadow_paths(action_type: str, paths: tuple[str, ...], config: LanderConfig, *, live_route: str) -> None:
    pull = OpenPull(number=0, title="", head_sha="", head_ref="", files=paths)
    _shadow(pull, config, action=action_type, live_block=live_route, kind="approval")


def _agreement_shadow(pull: OpenPull, repo: str, state_dir: Path) -> None:
    """Record whether the owner's GitHub review would have been skipped (#2471, ADR-291).

    Shadow only: it returns nothing, and nothing in this module reads what it wrote. A
    failure here leaves the live decision alone.
    """
    try:
        from axiom.extensions.builtins.release.agreement import shadow

        shadow.observe(pull, repo=repo, state_dir=state_dir)
    except Exception:
        _log.debug("agreement shadow skipped", exc_info=True)


def _hold_status(reason: str) -> str:
    if reason.startswith(owner_hold.HELD_FOR_OWNER_REVIEW):
        return "held"
    if reason.startswith("waiting on") or reason.startswith("changes requested"):
        return "waiting"
    return "escalated"


def posture(action_type: str, paths: tuple[str, ...], config: LanderConfig, ledger: RACILedger) -> str:
    """``approve``, ``auto``, or ``skip``. Human-routed paths are always approve."""
    if human_route(paths, extra=config.extra_path_rules or None, replace=config.replaces_human_paths):
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
    return config_categories(paths, config)


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
        result = ("approve", "")
    elif config.auto_approver and config.auto_approver != writer:
        who = writer or "built-in"
        rationale = (
            f"non-protected {action_type}; reviewer {config.auto_approver} differs from fix writer {who}"
        )
        result = ("auto-reviewer", rationale)
    else:
        result = (posture(action_type, paths, config, ledger), "")
    _shadow_paths(action_type, paths, config, live_route=result[0])
    return result


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
            (
                "tests not run: a required check has no run that started after the pull request "
                "was marked ready; nothing is landed on a draft's green"
                if state == "not_run"
                else state or "checks not green"
            ),
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


def _budget_refusal(action_type: str, budget: LandingPolicy | None) -> str:
    if budget is None:
        return ""
    if action_type == "rerun" and not budget.allow_rerun:
        return f"budget-{budget.state}: no blind re-run; fix the cause"
    if action_type == "repair" and not budget.allow_repair:
        return f"budget-{budget.state}: repairs that re-run CI are paused"
    return ""


def _start_ci(
    pull: OpenPull,
    *,
    config: LanderConfig,
    state_dir: Path,
    repo: str,
    effects: Effects,
    environ: dict[str, str] | None,
    take,
) -> ActorOutcome:
    """A head CI never ran on: start it once, leave a draft to its author, else raise it."""
    chosen = ci_start.plan(pull, config, state_dir=state_dir, repo=repo, environ=environ)
    steps = ("ci-never-ran",)
    if chosen.next == ci_start.AUTHOR:
        return ActorOutcome(pull.number, "skipped", f"ci never ran: {chosen.why}", "", pull.head_sha, steps)
    starter = getattr(effects, "start_ci", None)
    why = chosen.why
    if chosen.next == ci_start.RIVET and starter is None:
        why = "this forge adapter cannot start CI"
    if chosen.next == ci_start.RIVET and starter is not None:
        take(pull)
        # Recorded before the attempt: a crash or a failure must not earn a second start.
        ci_start.record_start(state_dir, repo=repo, number=pull.number, head_sha=pull.head_sha)
        try:
            starter(pull.number, pull.head_sha, token_env=config.ci_start_token_env)
        except LanderRefused as exc:
            why = f"the start failed: {exc}"[:300]
        else:
            _audit(state_dir, repo, "ci-start", f"{pull.number} {pull.head_sha[:12]}")
            missing = ", ".join(pull.missing_checks) or "its checks"
            return ActorOutcome(
                pull.number,
                "ci-started",
                f"ci never ran: closed and reopened to start {missing}",
                "",
                pull.head_sha,
                steps + ("start",),
            )
    reason = ci_start.raise_to_person(state_dir, repo, pull, why)
    return ActorOutcome(pull.number, "escalated", reason, "", pull.head_sha, steps + ("escalate",))


def _behind(
    pull: OpenPull,
    plan,
    config: LanderConfig,
    effects,
    state_dir: Path,
    repo: str,
    steps: list[str],
    take=lambda pull: None,
) -> ActorOutcome:
    """A green pull request behind its base: update it only if it lands next."""
    if plan.next != pull.number:
        reason = f"behind: waits for #{plan.next} to land" if plan.next else f"behind: {plan.reason}"
        return ActorOutcome(pull.number, "waiting", reason, "", pull.head_sha, tuple(steps + ["behind"]))
    updater = getattr(effects, "update_branch", None)
    if "update_branch" not in config.auto_allowed or updater is None:
        return ActorOutcome(
            pull.number,
            "waiting",
            "behind: next to land; update_branch needs approval",
            "",
            pull.head_sha,
            tuple(steps + ["behind"]),
        )
    take(pull)
    try:
        new_sha = updater(pull.number, pull.head_sha)
    except LanderRefused as exc:
        return ActorOutcome(pull.number, "escalated", str(exc)[:300], "", pull.head_sha, tuple(steps + ["update"]))
    _audit(state_dir, repo, "update-branch", f"{pull.number} {pull.head_sha[:12]} -> {str(new_sha)[:12]}")
    return ActorOutcome(pull.number, "updated", "next to land", "", str(new_sha), tuple(steps + ["update"]))


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


#: A red result is stale only when it is this old and main has been green since: at most one
#: refresh per pull request per this long, so a pull request that really fails is never looped.
STALE_RED_AFTER = timedelta(hours=6)


#: Blockers a fresh run cannot clear, or that have their own move.
_NOT_STALE_RED = frozenset(
    {"clear", "cancelled_checks", "merge_conflict", "dependency_conflict", "batching_gate"}
)


def _stale_red(pull: OpenPull, effects) -> bool:
    """Every red check finished at least six hours ago, and main has been green since the newest."""
    finder = getattr(effects, "main_green_at", None)
    if finder is None or pull.mergeable is False or not pull.failing_checks:
        return False
    finished = [_parse_iso(check.completed_at) for check in pull.failing_checks]
    if any(stamp is None for stamp in finished):
        return False
    newest = max(finished)  # type: ignore[type-var]
    if datetime.now(UTC) - newest < STALE_RED_AFTER:
        return False
    green = finder(pull.base_ref or "main")
    return green is not None and green > newest


#: Pull requests refreshed in one pass. Each refresh starts a CI run, so a backlog of
#: stale pull requests is worked through a few at a time and never storms CI.
STALE_REFRESH_PER_PASS = 3


def _refresh_cancelled(
    pull: OpenPull,
    config: LanderConfig,
    effects,
    state_dir: Path,
    repo: str,
    steps: list[str],
    take,
    budget,
    spent: list[int],
    why: str = "cancelled checks",
) -> ActorOutcome:
    """A pull request whose only red checks were cancelled: bring its branch up to date.

    A cancelled check reported no result, so there is nothing to repair. Merging the base
    into the head gives a new head, and fresh CI on it decides: green lands, red is a real
    failure the fixer or the author then sees. Pinned to the head that was judged.
    """
    steps = [*steps, "cancelled"]
    updater = getattr(effects, "update_branch", None)
    if "update_branch" not in config.auto_allowed or updater is None:
        return ActorOutcome(
            pull.number, "waiting", f"{why}: update_branch needs approval", "",
            pull.head_sha, tuple(steps),
        )
    if budget is not None and not budget.allow_update_branch:
        return ActorOutcome(
            pull.number, "waiting", f"{why}: branch updates are paused", "",
            pull.head_sha, tuple(steps),
        )
    if spent[0] >= STALE_REFRESH_PER_PASS:
        return ActorOutcome(
            pull.number, "waiting", f"{why}: queued, refreshed on a next pass", "",
            pull.head_sha, tuple(steps),
        )
    take(pull)
    try:
        new_sha = updater(pull.number, pull.head_sha)
    except LanderRefused as exc:
        return ActorOutcome(pull.number, "escalated", str(exc)[:300], "", pull.head_sha, tuple(steps + ["update"]))
    spent[0] += 1
    _audit(state_dir, repo, "refresh-cancelled", f"{pull.number} {pull.head_sha[:12]} -> {str(new_sha)[:12]}")
    return ActorOutcome(
        pull.number, "updated", f"{why}: branch brought up to date for a fresh run", "",
        str(new_sha), tuple(steps + ["update"]),
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
    budget: LandingPolicy | None = None,
    drift: DriftWiring | None = None,
) -> ActorResult:
    """One pass over ``pulls``. Unapproved work is proposed and not executed.

    ``drift`` wires the base-drift path (``lander.drift``): a red pull
    request whose failure the repair catalog knows as drift is repaired,
    made to wait on another pull request's repair, or given the base once
    that repair landed, with no approval step, when ``raci.auto_allowed``
    names ``drift_repair``. Without it nothing changes.

    ``budget`` is the CI-budget landing policy. At warn, blind re-runs are
    refused. At stop, only release pull requests are considered and repairs
    and branch updates pause. Either way only the next pull request to land
    may have its branch updated.
    """
    if not config.approvers:
        raise LanderRefused("name an Accountable approver before RIVET can act")
    if killed(config, environ):
        return ActorResult(stopped="kill-switch")
    effects = guard_landing(effects, config)
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
    #: Held for their owner. Left out of the landing order too, or the one branch
    #: update would go to a pull request that is not going to land.
    waiting_for_owner = {
        pull.number: found
        for pull in pulls
        if (found := owner_hold.hold_for(pull, config, repo=repo)) is not None
    }
    updates = plan_update_branch(
        [pull for pull in pulls if pull.number not in waiting_for_owner],
        allow=budget.allow_update_branch if budget else True,
    )
    drift_ledger = DriftLedger(state_dir) if drift is not None and drift_enabled(config) else None
    if drift_ledger is not None:
        drift_ledger.sync(drift.host)

    #: Pull requests that carry the claim: taken in this pass, or left by an earlier one.
    held: set[int] = set()
    #: Branch refreshes spent on cancelled checks in this pass.
    refreshed = [0]

    def take(pull: OpenPull) -> None:
        """Claim one pull request, just before the pass acts on it. A draft is never claimed."""
        if pull.draft:
            return
        if pull.number not in held and config.claim_label not in pull.labels:
            _add_label(effects, pull.number, config.claim_label)
        held.add(pull.number)
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

    def settle(outcome: ActorOutcome) -> None:
        # Only a claim that exists is given back: an unclaimed pull request costs no call.
        if outcome.status != "waiting" and outcome.number in held:
            _remove_label(effects, outcome.number, config.claim_label)
            release_claim(state_dir, repo=repo, number=outcome.number)
            held.discard(outcome.number)
        result.outcomes.append(outcome)

    for pull in pulls:
        if killed(config, environ):
            result.stopped = "kill-switch"
            return result
        _agreement_shadow(pull, repo, state_dir)
        waiting = waiting_for_owner.get(pull.number)
        if waiting is not None:
            if config.claim_label in pull.labels or claim_record(state_dir, repo, pull.number) is not None:
                # A claim from before the hold arrived is given back; nothing else is touched.
                held.add(pull.number)
            reason = owner_hold.raise_to_person(state_dir, repo, pull, waiting)
            settle(ActorOutcome(pull.number, "held", reason, "", pull.head_sha, ("hold",)))
            continue
        if budget is not None and budget.release_only and not is_release_pull(pull, budget):
            result.outcomes.append(
                ActorOutcome(pull.number, "skipped", "budget-stop: only release pull requests land", "", pull.head_sha, ("budget",))
            )
            continue
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
        if config.claim_label in pull.labels or claim_record(state_dir, repo, pull.number) is not None:
            # A claim an earlier pass left (a wait that has since ended, or a runner
            # that keeps no state): it is given back when this pass settles.
            held.add(pull.number)
        if pull.checks == "not_run" and not pull.ci_never_ran:
            # No run that counts, and none is overdue: a draft, or a ready run still to start.
            # Nothing is landed, repaired or diagnosed on a green that is not evidence.
            settle(
                ActorOutcome(
                    pull.number, "skipped", tests_not_run_reason(pull), "", pull.head_sha, ("tests-not-run",)
                )
            )
            continue
        if pull.ci_never_ran:
            settle(
                _start_ci(
                    pull,
                    config=config,
                    state_dir=state_dir,
                    repo=repo,
                    effects=effects,
                    environ=environ,
                    take=take,
                )
            )
            continue
        if drift_ledger is not None and drift is not None and not _budget_refusal("repair", budget):
            try:
                handled = resolve_drift(pull, config=config, wiring=drift, ledger=drift_ledger)
            except (LanderRefused, OSError) as exc:
                handled = DriftOutcome("escalated", f"drift: {exc}"[:300], pull.head_sha, ("drift", "escalate"))
            if handled is not None:
                take(pull)
                _audit(state_dir, repo, f"drift-{handled.status}", f"{pull.number} {handled.reason}"[:300])
                settle(
                    ActorOutcome(
                        pull.number,
                        handled.status,
                        handled.reason,
                        "",
                        handled.head_sha or pull.head_sha,
                        handled.steps,
                    )
                )
                continue
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
            if diagnosis.kind not in _NOT_STALE_RED and _stale_red(pull, effects):
                settle(
                    _refresh_cancelled(
                        pull, config, effects, state_dir, repo, steps, take, budget, refreshed,
                        why="stale failure, main has been green since",
                    )
                )
                break
            if diagnosis.kind == "cancelled_checks":
                settle(
                    _refresh_cancelled(
                        pull, config, effects, state_dir, repo, steps, take, budget, refreshed
                    )
                )
                break
            fix = provider.draft(diagnosis, pull)
            if fix.force:
                raise LanderRefused("force-push is not allowed")
            if fix.pending:
                take(pull)
                _audit(state_dir, repo, "handoff", f"{pull.number} {fix.branch}")
                settle(ActorOutcome(pull.number, "waiting", "handoff", "", sha, tuple(steps + ["handoff"])))
                break
            if diagnosis.kind == "clear" and pull.merge_state == BEHIND:
                outcome = _behind(pull, updates, config, effects, state_dir, repo, steps, take)
                settle(outcome)
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
                    take(pull)
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
                refusal = _budget_refusal(action_type, budget)
                if refusal:
                    _audit(state_dir, repo, "budget-hold", f"{pull.number} {action_type}: {refusal}")
                    settle(ActorOutcome(pull.number, "escalated", refusal, "", sha, tuple(steps + ["budget"])))
                    break
            paths = _touched(pull, fix)
            route, rationale = approval_route(action_type, paths, config, ledger, fix.writer)
            if route == "skip":
                settle(ActorOutcome(pull.number, "escalated", "raci-skip", "", sha, tuple(steps)))
                break
            take(pull)
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
