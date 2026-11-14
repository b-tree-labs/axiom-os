# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi release land`` — read a backlog, or propose without merging."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from axiom.extensions.builtins.release.lander.adapters import ForgeUnavailable, GitHubForge
from axiom.extensions.builtins.release.lander.config import load_config
from axiom.extensions.builtins.release.lander.loop import load_approvals, run_once
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import pull_from_mapping
from axiom.extensions.builtins.release.lander.render import render_text
from axiom.extensions.builtins.release.lander.report import build_report
from axiom.extensions.builtins.release.lander.tickets import TicketRouter


def _pulls_param(raw: Any) -> list:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raise LanderRefused("pulls must be a list")
    router = TicketRouter()
    pulls = []
    for row in raw:
        if not isinstance(row, dict):
            raise LanderRefused("each pull must be an object")
        pull = pull_from_mapping(row)
        pulls.append(replace(pull, ticket_ids=router.ids_for(pull)))
    return pulls


def _ci_lint_warnings(params: dict[str, Any]) -> list[dict]:
    """``ci lint`` before landing. Printed as warnings; never blocks."""
    if params.get("no_ci_lint"):
        return []
    try:
        from axiom.extensions.builtins.release.ci_health.lint import lint_dir

        findings = lint_dir(Path(params.get("workflows") or (Path.cwd() / ".github" / "workflows")))
    except Exception:  # noqa: BLE001 - a lint problem never stops a land
        return []
    for finding in findings:
        print(f"ci lint (warning): {finding.line()}", file=sys.stderr)
    return [finding.to_dict() for finding in findings]


def _budget_policy(state_dir: Path, explicit: Path | None):
    from axiom.extensions.builtins.release.ci_health.budget import current_state, landing_policy
    from axiom.extensions.builtins.release.ci_health.config import load_ci_health

    ci_config = load_ci_health(explicit)
    return landing_policy(current_state(state_dir, ci_config), ci_config)


def _main_head(params: dict[str, Any], repo: str, explicit: Path | None, *, read: bool):
    """Main's verdict for its CURRENT head (#1782), or None when no forge is being read.

    Never "the last completed run": a superseded head is untested, not green or
    red. In-memory pull requests (tests, ``--pulls``) read no forge unless a
    client is handed in.
    """
    if not read or "/" not in repo:
        return None
    from axiom.extensions.builtins.release.ci_health.client import GhClient
    from axiom.extensions.builtins.release.ci_health.config import load_ci_health
    from axiom.extensions.builtins.release.ci_health.head import head_verdict

    client = params.get("_client") or GhClient()
    return head_verdict(client, load_ci_health(explicit), repo)


def _lock_forge(params: dict[str, Any], repo: str, *, read: bool):
    forge = params.get("_lock_forge")
    if forge is not None:
        return forge
    from axiom.extensions.builtins.release.release_lock import GhLockForge, NullLockForge

    return GhLockForge(repo) if read and "/" in repo else NullLockForge()


def _lock_recipients(explicit: Path | None) -> tuple[str, ...]:
    from axiom.extensions.builtins.release.ci_health.config import load_ci_health

    try:
        return load_ci_health(explicit).notify_recipients
    except Exception:  # noqa: BLE001 - a bad config never stops the lift
        return ()


def land(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Report batches, closes, ready pull requests, and human decisions.

    ``--propose`` records the proposal locally. ``--act`` lands a green,
    unprotected, non-draft pull request with no pending human review and
    no changes-requested review. A protected path is held. The report
    path does not merge, enqueue, force-push, or post a check.

    Two gates stand before acting (#1782): a held release lock means nothing
    is armed and anything found armed is disarmed; a red main head (judged by
    its own run) means nothing lands on top of it.
    """
    from axiom.extensions.builtins.release import release_lock

    repo = str(params.get("repo") or "").strip()
    raw_pulls = params.get("pulls")
    if not repo and not raw_pulls:
        print("repo is required", file=sys.stderr)
        return SkillResult(ok=False, errors=["repo is required"])
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    explicit = Path(params["config"]) if params.get("config") else None
    lint = _ci_lint_warnings(params)
    # A forge is read when the pull requests are not handed in, or a client is.
    read_forge = raw_pulls is None or params.get("_client") is not None
    head = None
    lock = None
    try:
        config = load_config(state_dir, repo or "local", explicit=explicit)
        if params.get("window_hours") is not None:
            config = replace(config, window_hours=float(params["window_hours"]))
        if params.get("suite_minutes"):
            config = replace(config, suite_minutes=int(params["suite_minutes"]))
        pulls = _pulls_param(raw_pulls) if raw_pulls is not None else None
        samples = params.get("samples")
        if isinstance(samples, str):
            samples = [int(part) for part in samples.split(",") if part.strip()]
        if params.get("propose"):
            result = run_once(
                repo=repo or "local",
                state_dir=state_dir,
                config=config,
                pulls=pulls,
                samples=samples,
            )
            if result.report is None:
                print(result.reason or "skipped", file=sys.stderr)
                return SkillResult(ok=False, errors=[result.reason or "skipped"], value=result.to_dict())
            report = result.report
        elif params.get("act"):
            from axiom.extensions.builtins.release.lander.actor import GitEffects, run_actor
            from axiom.infra.orchestrator.approval import ApprovalGate
            from axiom.infra.orchestrator.approval_store import FileActionStore

            now = datetime.now(UTC)
            lock_forge = _lock_forge(params, repo or "local", read=read_forge or params.get("_lock_forge") is not None)
            # A timed-out lock lifts here, re-arming what it disarmed, before anything else.
            release_lock.expire_due(
                state_dir,
                repo or "local",
                forge=lock_forge,
                now=now,
                sender=params.get("_sender"),
                recipients=_lock_recipients(explicit),
            )
            lock = release_lock.current(state_dir, repo or "local", lock_forge, now=now)
            if lock is not None:
                disarmed = release_lock.sweep_armed(state_dir, lock, lock_forge, now=now)
                held = {
                    "stopped": "release-lock",
                    "reason": lock.describe(),
                    "release_lock": lock.to_dict(),
                    "disarmed": [item.number for item in disarmed],
                    "outcomes": [],
                }
                print(json.dumps(held, indent=2))
                return SkillResult(ok=True, value={**held, "ci_lint": lint})
            head = _main_head(params, repo or "local", explicit, read=read_forge)
            if head is not None and head.red:
                held = {
                    "stopped": "main-red",
                    "reason": f"main is red on its current head; nothing lands on top of it: {head.line()}",
                    "main_head": head.to_dict(),
                    "outcomes": [],
                }
                print(json.dumps(held, indent=2))
                return SkillResult(ok=True, value={**held, "ci_lint": lint})
            if pulls is None:
                forge = GitHubForge(limit=config.pr_limit, self_landers=config.self_landers)
                pulls = forge.list_open(repo)
            from axiom.extensions.builtins.release.ci_health.budget import batch_gate

            policy = _budget_policy(state_dir, explicit)
            if policy.reason:
                print(policy.reason, file=sys.stderr)
            proceed, why = batch_gate(pulls, policy)
            if not proceed:
                held = {"stopped": "batching", "reason": why, "budget": policy.to_dict(), "outcomes": []}
                print(json.dumps(held, indent=2))
                return SkillResult(ok=True, value={**held, "ci_lint": lint})
            gate = ApprovalGate(
                FileActionStore(
                    state_dir / "agents" / "rivet" / "approvals.json",
                    ttl_hours=config.approval_ttl_hours,
                )
            )
            acted = run_actor(
                pulls,
                repo=repo or "local",
                config=config,
                state_dir=state_dir,
                effects=GitEffects(repo=repo, token_env=config.token_env),
                gate=gate,
                budget=policy,
            )
            print(json.dumps(acted.to_dict(), indent=2))
            value = {
                **acted.to_dict(),
                "budget": policy.to_dict(),
                "ci_lint": lint,
                "main_head": head.to_dict() if head else None,
            }
            return SkillResult(ok=acted.stopped != "kill-switch", value=value)
        else:
            if pulls is None:
                forge = GitHubForge(limit=config.pr_limit, self_landers=config.self_landers)
                pulls = forge.list_open(repo)
                if samples is None and config.suite_minutes is None:
                    try:
                        samples = forge.suite_samples(repo)
                    except ForgeUnavailable:
                        samples = []
            head = _main_head(params, repo or "local", explicit, read=read_forge)
            lock = release_lock.current(
                state_dir,
                repo or "local",
                _lock_forge(params, repo or "local", read=read_forge),
                now=datetime.now(UTC),
            )
            report = build_report(
                pulls,
                repo=repo or "local",
                config=config,
                samples=samples,
                approvals=load_approvals(state_dir, repo or "local"),
                state_dir=state_dir,
            )
    except (OSError, ValueError, LanderRefused, ForgeUnavailable) as exc:
        print(str(exc), file=sys.stderr)
        return SkillResult(ok=False, errors=[str(exc)])
    extra = {
        "main_head": head.to_dict() if head is not None else None,
        "release_lock": lock.to_dict() if lock is not None else None,
    }
    as_json = (params.get("format") or params.get("fmt")) == "json"
    if as_json:
        rendered = json.dumps({**report.to_dict(), **extra}, indent=2)
    else:
        lines = []
        if head is not None:
            lines.append(f"Main head: {head.line()}")
        if lock is not None:
            lines.append(f"Release lock: {lock.describe()}; the lander arms nothing until it lifts.")
        rendered = ("\n".join(lines) + "\n" if lines else "") + render_text(report)
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    return SkillResult(ok=True, value={**report.to_dict(), **extra, "ci_lint": lint})
