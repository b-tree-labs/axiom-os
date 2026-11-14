# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skills for the loop's phase 3: ``release.guards`` and ``release.auto_revert``.

``guards`` runs the cheap guards for a pushed range and reports the change's tier. The
pre-push hook and ``loop-guards.yml`` both call it through ``axi release guards``.

``auto_revert`` judges one main commit and, only with ``act`` and the repository
variable ``AXIOM_AUTO_REVERT=1``, reverts it and asks its author. Without both it is a
dry run that says what it would do.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

ENABLE_VARIABLE = "AXIOM_AUTO_REVERT"


def _truthy(value: Any) -> bool:
    return value not in (None, False, "", "false", "0", 0)


def _toplevel() -> Path:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
        )
        if done.returncode == 0 and done.stdout.strip():
            return Path(done.stdout.strip())
    except OSError:
        pass
    return Path.cwd()


def guards(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Run the cheap guards on ``base..head`` and report the tier. Fails when a guard fails."""
    from axiom.extensions.builtins.release.guards import run_guards

    root = Path(params["root"]) if params.get("root") else _toplevel()
    only = params.get("only")
    if isinstance(only, str):
        only = [part.strip() for part in only.split(",") if part.strip()]
    report = run_guards(
        root,
        base=str(params.get("base") or "").strip(),
        head=str(params.get("head") or "HEAD").strip() or "HEAD",
        only=only or None,
        runner=params.get("_runner"),
        stamp=bool(params.get("stamp")),
    )
    value = report.to_dict()
    if not params.get("quiet"):
        if (params.get("format") or "text") == "json":
            print(json.dumps(value, indent=2))
        else:
            print(report.render(), end="")
    failed = [r.id for r in report.results if not r.ok]
    return SkillResult(ok=report.ok, errors=[f"guard failed: {g}" for g in failed], value=value)


def auto_revert(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Judge one main commit; revert it and ask its author only when turned on."""
    from axiom.extensions.builtins.release import auto_revert as AR
    from axiom.extensions.builtins.release import release_lock
    from axiom.extensions.builtins.release.ci_health.client import GhClient
    from axiom.extensions.builtins.release.ci_health.config import load_ci_health
    from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused

    try:
        config = params.get("_config") or load_ci_health(Path(params["config"]) if params.get("config") else None)
    except (LanderRefused, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    repo = str(params.get("repo") or "").strip()
    if not repo:
        return SkillResult(ok=False, errors=["no repo: pass --repo owner/name"])
    client = params.get("_client") or GhClient()
    sha = str(params.get("sha") or "").strip()
    if not sha:
        try:
            sha = client.branch_head(repo, config.default_branch)
        except Exception as exc:  # noqa: BLE001 - reported, never guessed
            return SkillResult(ok=False, errors=[f"no sha given and the branch head could not be read: {exc}"])
    wanted = _truthy(params.get("act"))
    enabled = os.environ.get(ENABLE_VARIABLE, "") == "1"
    act = wanted and enabled
    if act:
        dry = ""
    elif wanted:
        dry = f"dry run: {ENABLE_VARIABLE} is not 1, so --act was ignored"
    else:
        dry = "dry run: pass --act with the repository variable set to turn it on"
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    forge = params.get("_lock_forge") or release_lock.GhLockForge(repo)
    lock = release_lock.current(state_dir, repo, forge)
    decision = AR.decide(client, config, repo, sha, lock=lock)
    if wanted and not enabled:
        decision.reasons.append(f"{ENABLE_VARIABLE} is not 1: nothing is reverted, re-run or asked")
    git = params.get("_git") or AR.GitRevert(_toplevel())
    AR.carry_out(client, config, decision, git=git, act=act, sender=params.get("_sender"), dry_reason=dry)
    value = decision.to_dict()
    if not params.get("quiet"):
        if (params.get("format") or "text") == "json":
            print(json.dumps(value, indent=2, default=str))
        else:
            print(decision.render(), end="")
    return SkillResult(ok=not decision.errors, errors=list(decision.errors), value=value)
