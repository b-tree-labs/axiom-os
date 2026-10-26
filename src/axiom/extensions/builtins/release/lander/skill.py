# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi release land`` — read a backlog, or propose without merging."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
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


def land(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Report batches, closes, ready pull requests, and human decisions.

    ``--propose`` records the proposal locally. ``--act`` lands a green,
    unprotected, non-draft pull request with no pending human review and
    no changes-requested review. A protected path is held. The report
    path does not merge, enqueue, force-push, or post a check.
    """
    repo = str(params.get("repo") or "").strip()
    raw_pulls = params.get("pulls")
    if not repo and not raw_pulls:
        print("repo is required", file=sys.stderr)
        return SkillResult(ok=False, errors=["repo is required"])
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    explicit = Path(params["config"]) if params.get("config") else None
    try:
        config = load_config(state_dir, repo or "local", explicit=explicit)
        if params.get("window_hours") is not None:
            config = replace(config, window_hours=float(params["window_hours"]))
        if params.get("suite_minutes"):
            config = replace(config, suite_minutes=int(params["suite_minutes"]))
        pulls = _pulls_param(raw_pulls) if raw_pulls else None
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

            if not pulls:
                forge = GitHubForge(limit=config.pr_limit, self_landers=config.self_landers)
                pulls = forge.list_open(repo)
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
            )
            print(json.dumps(acted.to_dict(), indent=2))
            return SkillResult(ok=acted.stopped != "kill-switch", value=acted.to_dict())
        else:
            if pulls is None:
                forge = GitHubForge(limit=config.pr_limit, self_landers=config.self_landers)
                pulls = forge.list_open(repo)
                if samples is None and config.suite_minutes is None:
                    try:
                        samples = forge.suite_samples(repo)
                    except ForgeUnavailable:
                        samples = []
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
    rendered = (
        json.dumps(report.to_dict(), indent=2)
        if (params.get("format") or params.get("fmt")) == "json"
        else render_text(report)
    )
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    return SkillResult(ok=True, value=report.to_dict())
