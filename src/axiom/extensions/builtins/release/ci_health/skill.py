# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skills: ``release.ci_health`` (read), ``release.ci_sweep`` (scheduled),
``release.ci_lint`` (read) and ``release.ci_digest`` (weekly).

``ci_health`` and ``ci_lint`` only read, so they are on the MCP and chat
surfaces. ``ci_sweep`` cancels superseded runs and sends alerts; it runs from
the schedule and the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.release.ci_health.client import CIClient, GhClient
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig, load_ci_health
from axiom.extensions.builtins.release.ci_health.lint import lint_dir, lint_protection
from axiom.extensions.builtins.release.ci_health.report import (
    gather,
    render_digest,
    render_text,
    sweep,
    to_json,
)
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.infra.skills import SkillContext, SkillResult


def _config(params: dict[str, Any]) -> CIHealthConfig:
    path = Path(params["config"]) if params.get("config") else None
    return load_ci_health(path)


def _repos(params: dict[str, Any], config: CIHealthConfig, state_dir: Path) -> tuple[str, ...]:
    raw = params.get("repo") or params.get("repos")
    if isinstance(raw, str) and raw.strip():
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(raw, (list, tuple)) and raw:
        return tuple(str(part) for part in raw)
    if config.repos:
        return config.repos
    from axiom.extensions.builtins.release.actions_spend import watched_repos

    return watched_repos(state_dir)


def _client(params: dict[str, Any]) -> CIClient:
    client = params.get("_client")
    return client if client is not None else GhClient()


def _emit(report, params: dict[str, Any]) -> None:
    fmt = params.get("format") or params.get("fmt") or "text"
    print(to_json(report) if fmt == "json" else render_text(report), end="")


def ci_health(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Read-only CI health: spend, projection, minutes by repo/workflow/event/job, waste signals."""
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    try:
        config = _config(params)
    except (LanderRefused, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    repos = _repos(params, config, state_dir)
    if not repos:
        return SkillResult(ok=False, errors=["no repo: pass --repo owner/name or set ci_health.repos"])
    report = gather(_client(params), config, repos=repos, state_dir=state_dir)
    if not params.get("quiet"):
        _emit(report, params)
    return SkillResult(ok=True, value=report.to_dict())


def ci_sweep(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The scheduled check: cancel superseded runs, record budget state, alert on crossings.

    ``act`` defaults to true from the schedule. ``--dry-run`` proposes the
    cancels and sends nothing.
    """
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    try:
        config = _config(params)
    except (LanderRefused, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    repos = _repos(params, config, state_dir)
    if not repos:
        return SkillResult(ok=False, errors=["no repo: pass --repo owner/name or set ci_health.repos"])
    dry = bool(params.get("dry_run"))
    act = not dry and params.get("act", True) not in (False, "false", "0", 0)
    notify = not dry and params.get("notify", True) not in (False, "false", "0", 0)
    report = sweep(
        _client(params),
        config,
        repos=repos,
        state_dir=state_dir,
        act=act,
        notify=notify,
        sender=params.get("_sender"),
    )
    if not params.get("quiet"):
        _emit(report, params)
    return SkillResult(ok=True, value=report.to_dict())


def ci_lint(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Warn on workflow shapes that waste minutes. Never a blocker."""
    try:
        config = _config(params)
    except (LanderRefused, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    root = Path(params.get("path") or (Path.cwd() / ".github" / "workflows"))
    findings = lint_dir(root, long_job_minutes=config.long_job_minutes)
    repo = str(params.get("repo") or "").strip()
    if repo and params.get("protection", True) not in (False, "false", "0"):
        client = _client(params)
        findings.extend(
            lint_protection(
                client.branch_protection(repo, config.default_branch),
                client.branch_rules(repo, config.default_branch),
                branch=f"{repo}@{config.default_branch}",
            )
        )
    rows = [f.to_dict() for f in findings]
    if not params.get("quiet"):
        if (params.get("format") or "text") == "json":
            print(json.dumps(rows, indent=2))
        else:
            for finding in findings:
                print(finding.line())
            print(f"ci lint: {len(findings)} warning(s) in {root}")
    return SkillResult(ok=True, value={"findings": rows, "path": str(root)})


def ci_digest(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The weekly digest, sent through HERALD to the configured recipients."""
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    try:
        config = _config(params)
    except (LanderRefused, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    repos = _repos(params, config, state_dir)
    if not repos:
        return SkillResult(ok=False, errors=["no repo: pass --repo owner/name or set ci_health.repos"])
    report = gather(_client(params), config, repos=repos, state_dir=state_dir)
    text = render_digest(report)
    if not params.get("quiet"):
        print(text, end="")
    sent: list[dict] = []
    if not params.get("dry_run"):
        from axiom.extensions.builtins.release.ci_health.alerts import Alert, send_alerts

        digest = Alert(
            "digest",
            "normal",
            "RIVET weekly CI digest",
            "Read-only summary; no action taken.",
            "Pick the top waste signal to act on this week.",
            f"digest:{report.checked_at[:10]}",
            detail=text,
        )
        sent = send_alerts([digest], config.notify_recipients, already_sent=set(), sender=params.get("_sender"))
    return SkillResult(ok=True, value={"digest": text, "sent": sent})
