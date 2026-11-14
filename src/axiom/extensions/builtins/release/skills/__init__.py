# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``release`` skills — invocable through the platform SkillRegistry.

The release surface consolidates the pre-2026-05-30 ``axi rivet``
(verbose control surface — status / check / patterns / plan / watch /
…) and ``axi release`` (the simple positional bump). Per ADR-056 each
CLI verb maps 1:1 to a registered skill function.

Legacy implementations stay in ``_legacy_rivet_cli`` (most verbs) and
``_legacy_release_cli`` (the bump operation, renamed ``cut``); skill
modules here are thin ``(params, ctx) → SkillResult`` adapters.
"""

from __future__ import annotations

import argparse
from typing import Any

from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

from .. import _legacy_release_cli as _release_legacy
from .. import _legacy_rivet_cli as _rivet_legacy

_NAMESPACE = "release"


def _wrap_int_handler(legacy_fn):
    """Wrap an int-returning legacy `_cmd_X(args)` as a SkillResult skill."""

    def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        args = argparse.Namespace(**params)
        try:
            rc = legacy_fn(args)
        except SystemExit as e:
            rc = int(e.code) if e.code is not None else 0
        except Exception as exc:
            return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
        return SkillResult(ok=(rc == 0))

    run.__name__ = f"run_{getattr(legacy_fn, '__name__', 'unknown').removeprefix('_cmd_')}"
    return run


def cut(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release cut <part>`` — bump version + tag + push."""
    # The legacy release main() does its own argparse; we hand it
    # reconstructed argv. Simpler than threading params through.
    part = params.get("part")
    argv: list[str] = []
    if part:
        argv.append(part)
    for flag in ("dry_run", "status", "changelog", "tag_only", "no_push", "skip_tests", "yes"):
        if params.get(flag):
            argv.append("--" + flag.replace("_", "-"))
    try:
        rc = _release_legacy.main(argv)
    except SystemExit as e:
        rc = int(e.code) if e.code is not None else 0
    except Exception as exc:
        return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
    return SkillResult(ok=(rc == 0))


# Rivet's verb-to-handler map lives in _legacy_rivet_cli.main. We'd
# rather invoke its handlers directly. Lookups must match what
# _legacy_rivet_cli.main() dispatches.
def _rivet_handler(name):
    return getattr(_rivet_legacy, f"_cmd_{name}", None)


# Imperative-leaf verbs from rivet (1:1).
_status_legacy = _wrap_int_handler(_rivet_handler("status"))


def status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release status``: the legacy build status, plus this machine's release lock."""
    result = _status_legacy(params, ctx)
    try:
        from pathlib import Path

        from .. import release_lock as _rl

        state_dir = Path(params.get("state_dir") or ctx.state_dir)
        lock = _rl.load(state_dir)
        # Returned, never printed: this skill is on the MCP surface, where
        # stdout is the protocol channel. The CLI prints the line.
        line = f"Release lock: {_rl.render(lock, repo=lock.repo) if lock else 'none held here'}"
        value = dict(result.value) if isinstance(result.value, dict) else {}
        value["release_lock"] = line
        result.value = value
    except Exception:  # noqa: BLE001 - the lock line never fails the status
        pass
    return result


status.__name__ = "run_status"
mode = _wrap_int_handler(_rivet_handler("mode"))
check = _wrap_int_handler(_rivet_handler("check"))
plan = _wrap_int_handler(_rivet_handler("plan"))
sync = _wrap_int_handler(_rivet_handler("sync"))
watch = _wrap_int_handler(_rivet_handler("watch"))
unwatch = _wrap_int_handler(_rivet_handler("unwatch"))
pause = _wrap_int_handler(_rivet_handler("pause"))
resume = _wrap_int_handler(_rivet_handler("resume"))
# `heartbeat` is the load-bearing dispatcher target. Per the 2026-06-01
# autopsy: launchd / systemd fire `axi release heartbeat` every
# heartbeat_interval seconds. The previous omission of this skill
# bricked RIVET's heartbeat.jsonl writer for 28 hours. NEVER drop it
# from this dispatch chain again — TIDY's heartbeat_liveness_audit
# (added in this PR) catches recurrence by watching ``~/.axi/agents/
# <agent>/heartbeat.jsonl`` staleness.
heartbeat = _wrap_int_handler(_rivet_handler("heartbeat"))


# Grammar-restructured (rivet → release):
#   patterns        → list patterns
#   watched         → list watched
#   paused          → list paused
#   close-stale     → close stale

_LIST_RESOURCES = {
    "patterns": _rivet_handler("patterns"),
    "watched": _rivet_handler("watched"),
    "paused": _rivet_handler("paused"),
}


def list_(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release list <patterns|watched|paused>``."""
    resource = params.get("resource")
    h = _LIST_RESOURCES.get(resource)
    if h is None:
        return SkillResult(
            ok=False,
            errors=[f"unknown list resource {resource!r}; supported: {sorted(_LIST_RESOURCES)}"],
        )
    return _wrap_int_handler(h)(params, ctx)


def cross_repo_pr_watch(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release cross-repo-pr-watch`` — fan out trunk-CI watch
    across the configured repo list (``~/.axi/agents/rivet/watched-repos.toml``).

    Filled the gap behind the 2026-05-30 → 2026-06-01 silent domain-consumer
    red main. Skill returns a structured value so MCP/CLI consumers can render
    a per-repo summary.
    """
    from pathlib import Path

    from axiom.extensions.builtins.release.cross_repo_pr_watch import (
        cross_repo_pr_watch as _do,
    )
    from axiom.extensions.builtins.release.cross_repo_pr_watch import (
        default_config_path,
        load_watched_repos,
    )

    config_path = Path(params.get("config", default_config_path()))
    state_dir = Path(params.get("state_dir", Path.home() / ".axi"))
    targets = load_watched_repos(config_path)
    if not targets:
        return SkillResult(
            ok=True,
            value={
                "watched": 0,
                "findings": [],
                "config_path": str(config_path),
                "note": "no watched repos configured; edit the config to add some",
            },
        )
    findings, snapshots = _do(targets, state_dir=state_dir)
    return SkillResult(
        ok=True,
        value={
            "watched": len(targets),
            "polled": len(snapshots),
            "findings": [
                {
                    "repo": f.repo,
                    "ref": f.ref,
                    "severity": f.severity,
                    "detail": f.detail,
                    "url": f.run_url,
                }
                for f in findings
            ],
            "config_path": str(config_path),
        },
    )


def close(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release close stale`` — only one resource for now."""
    resource = params.get("resource")
    if resource != "stale":
        return SkillResult(
            ok=False,
            errors=[f"unknown close resource {resource!r}; supported: stale"],
        )
    return _wrap_int_handler(_rivet_handler("close_stale"))(params, ctx)


def changelog(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release changelog`` \u2014 the story between two refs, out of the box.

    Sources: curated CHANGELOG.md sections, else grouped commit subjects.
    ``--ai`` (default auto) polishes either through the platform LLM gateway:
    related changes grouped semantically, each item phrased
    "<what changed> \u2014 <benefit to a user type>" \u2014 falling back to
    the deterministic grouping whenever no tier resolves. ``--since`` accepts
    a ref or a YYYY-MM-DD date; ``--releases N`` scopes to the last N tagged
    releases; ``--user`` + ``--since-viewed``/``--mark-viewed`` give each
    logged-in principal a personal "what's new since I last looked" (state
    keyed by principal \u2014 web surfaces pass the gate identity).
    """
    from pathlib import Path

    from .. import changelog as _cl

    repo = Path(params.get("repo") or ".").expanduser()
    to_ref = params.get("to_ref") or "HEAD"
    from_ref = params.get("from_ref")
    state_file = params.get("state_file")
    principal = params.get("user")
    releases = params.get("releases")
    if releases:
        tags = _cl.recent_tags(repo, to_ref=to_ref)
        if not tags:
            return SkillResult(
                ok=False, errors=[f"--releases {releases}: no tags reachable from {to_ref}"]
            )
        if to_ref == "HEAD":
            to_ref = tags[0]  # curated sections match version headings, not HEAD
        n = int(releases)
        from_ref = tags[n] if len(tags) > n else None
    if not from_ref and not releases and principal and params.get("since_viewed"):
        from_ref = _cl.read_viewed(ctx.state_dir, principal)
    if not from_ref and not releases and state_file:
        from_ref = _cl.read_state(Path(state_file).expanduser())
    ai = params.get("ai") or "auto"
    try:
        log = _cl.build(
            repo,
            from_ref=from_ref,
            to_ref=to_ref,
            limit=int(params.get("limit") or 20),
            changelog_file=params.get("changelog_file", "auto"),
        )
        if ai != "off":
            log.polished = _cl.polish(log, gateway=params.get("_gateway"))
            if ai == "on" and log.polished is None and (log.commits or log.curated):
                return SkillResult(
                    ok=False,
                    errors=["--ai on: no LLM gateway available (or it returned garbage)"],
                )
        rendered = _cl.render(log, fmt=params.get("fmt") or "text")
    except Exception as exc:
        return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
    print(rendered)
    if state_file and params.get("update_state"):
        _cl.write_state(Path(state_file).expanduser(), to_ref)
    if principal and params.get("mark_viewed"):
        _cl.write_viewed(ctx.state_dir, principal, to_ref)
    return SkillResult(
        ok=True,
        value={
            "since": log.from_ref,
            "from": log.from_ref,
            "to": log.to_ref,
            "count": log.count,
            "rendered": rendered,
            "polished": log.polished is not None,
            "sections": [{"title": t, "items": i} for t, i in (log.polished or log.sections())],
            "curated": bool(log.curated),
        },
    )


def ci_repair(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """The mechanical repair for a CI failure, for any harness on the MCP."""
    from ..ci_flow import plan_repair

    log = str(params.get("log") or "").strip()
    if not log:
        return SkillResult(ok=False, errors=["log is required"])
    try:
        attempts = int(params.get("attempts") or 0)
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["attempts must be an integer"])
    return SkillResult(
        ok=True, value=plan_repair(log, str(params.get("check") or ""), attempts=attempts)
    )


def review(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Pick a review model that did not write the change."""
    from ..ci_flow import choose_reviewer

    raw = params.get("models") or []
    if isinstance(raw, str):
        models = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        models = [str(part) for part in raw]
    picked = choose_reviewer(str(params.get("author_model") or ""), models)
    if not picked["ok"]:
        return SkillResult(ok=False, errors=[picked["reason"]])
    value = dict(picked)
    pull = str(params.get("pull") or "").strip()
    if pull:
        value["pull"] = pull
    return SkillResult(ok=True, value=value)


def land(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Report how open pull requests would batch. Propose writes local state only."""
    from axiom.extensions.builtins.release.lander import skill as lander

    if params.get("act") and not params.get("propose"):
        return SkillResult(
            ok=False, errors=["release.land only reports; merging is release.land_act"]
        )
    return lander.land(params, ctx)


def land_act(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Land an eligible green pull request. The write half of ``axi release land --act``."""
    from axiom.extensions.builtins.release.lander import skill as lander

    return lander.land({**params, "act": True}, ctx)


def ci_health(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Actions spend, projection, minutes by repo/workflow/event/job, and waste signals. Reads only."""
    from axiom.extensions.builtins.release.ci_health.skill import ci_health as _do

    return _do(params, ctx)


def ci_head(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The CI verdict for one head: success, failure, pending or superseded. Reads only."""
    from axiom.extensions.builtins.release.ci_health.skill import ci_head as _do

    return _do(params, ctx)


# -- the release lock -------------------------------------------------------------


def _lock_repo(params: dict[str, Any]) -> str:
    repo = str(params.get("repo") or "").strip()
    if repo:
        return repo
    try:
        return _release_legacy.ReleaseManager().repo_slug() or "local"
    except Exception:  # noqa: BLE001 - outside a checkout there is no slug
        return "local"


def _lock_forge_for(params: dict[str, Any], repo: str):
    forge = params.get("_lock_forge")
    if forge is not None:
        return forge
    from ..release_lock import GhLockForge, NullLockForge

    return GhLockForge(repo) if "/" in repo else NullLockForge()


def _lock_recipients(params: dict[str, Any]) -> tuple[str, ...]:
    from pathlib import Path

    from ..ci_health.config import load_ci_health

    try:
        return load_ci_health(Path(params["config"]) if params.get("config") else None).notify_recipients
    except Exception:  # noqa: BLE001 - a bad config does not stop a lock
        return ()


def _emit_lock(value: dict, params: dict[str, Any]) -> None:
    import json

    if params.get("quiet"):
        return
    if (params.get("format") or params.get("fmt") or "text") == "json":
        print(json.dumps(value, indent=2, default=str))
    else:
        print(value.get("line") or value.get("why") or "")


def lock(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release lock``: take the release lock for a person's window."""
    from datetime import UTC, datetime
    from pathlib import Path

    from .. import release_lock as _rl

    repo = _lock_repo(params)
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    forge = _lock_forge_for(params, repo)
    now = datetime.now(UTC)
    try:
        minutes = int(params.get("minutes") or _rl.DEFAULT_MINUTES)
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["minutes must be an integer"])
    try:
        _rl.take(
            state_dir,
            repo,
            reason=str(params.get("reason") or "manual release window"),
            held_by=_rl.default_holder(),
            forge=forge,
            now=now,
            minutes=minutes,
            sender=params.get("_sender"),
            recipients=_lock_recipients(params),
        )
    except _rl.LockHeld as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    value = _rl.status(state_dir, repo, forge, now=now)
    _emit_lock(value, params)
    return SkillResult(ok=True, value=value)


def unlock(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release unlock``: lift the release lock now and re-arm what it disarmed."""
    from datetime import UTC, datetime
    from pathlib import Path

    from .. import release_lock as _rl

    repo = _lock_repo(params)
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    forge = _lock_forge_for(params, repo)
    result = _rl.lift(
        state_dir,
        repo,
        forge=forge,
        now=datetime.now(UTC),
        why="unlocked by a person",
        sender=params.get("_sender"),
        recipients=_lock_recipients(params),
    )
    value = result.to_dict()
    value["line"] = (
        f"release lock lifted on {repo}; re-armed {len(result.rearmed)} pull request(s)"
        + (f"; not re-armed: {', '.join(f'#{n}' for n, _ in result.not_rearmed)}" if result.not_rearmed else "")
        if result.lock
        else f"no release lock on {repo}"
    )
    _emit_lock(value, params)
    return SkillResult(ok=True, value=value)


def lock_status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release lock-status``: whether a release lock is held, by whom, and until when."""
    from datetime import UTC, datetime
    from pathlib import Path

    from .. import release_lock as _rl

    repo = _lock_repo(params)
    state_dir = Path(params.get("state_dir") or ctx.state_dir)
    value = _rl.status(state_dir, repo, _lock_forge_for(params, repo), now=datetime.now(UTC))
    _emit_lock(value, params)
    return SkillResult(ok=True, value=value)


def ci_sweep(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Scheduled: cancel superseded runs, record the budget state landing obeys, alert on crossings."""
    from axiom.extensions.builtins.release.ci_health.skill import ci_sweep as _do

    return _do(params, ctx)


def ci_lint(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Warn on workflow shapes that waste Actions minutes. Never blocks."""
    from axiom.extensions.builtins.release.ci_health.skill import ci_lint as _do

    return _do(params, ctx)


def ci_main_watch(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Forge-side watch: read the default branch and keep its one incident."""
    from axiom.extensions.builtins.release.ci_health.skill import ci_main_watch as _do

    return _do(params, ctx)


def ci_digest(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Weekly CI-health digest through HERALD."""
    from axiom.extensions.builtins.release.ci_health.skill import ci_digest as _do

    return _do(params, ctx)


def guards(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The cheap guards for a pushed range, and its tier (LOOP-S2, LOOP-S3)."""
    from axiom.extensions.builtins.release.loop_skill import guards as _do

    return _do(params, ctx)


def auto_revert(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Judge one main commit; revert it and ask its author only when turned on (LOOP-S4)."""
    from axiom.extensions.builtins.release.loop_skill import auto_revert as _do

    return _do(params, ctx)


def repo_lineage(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Classify repositories as origin, mirror, fork, archived or unverified, from evidence only.

    ``repos`` is a list of ``owner/name`` strings or forge objects the caller
    already read; ``declared`` the deployment's declared origin/mirror pairs
    (they always win). ``_history`` is an in-process ``(host, repo) -> [sha, ...]``
    reader for shared-commit evidence; over MCP there is none and the answer
    rests on metadata alone. Reads nothing itself.
    """
    from ..repo_lineage import classify

    repos = params.get("repos")
    if not isinstance(repos, list):
        return SkillResult(
            ok=False, errors=["repos must be a list of owner/name strings or repository objects"]
        )
    declared = params.get("declared") or []
    if not isinstance(declared, list):
        return SkillResult(ok=False, errors=["declared must be a list of {origin, mirror} pairs"])
    try:
        max_reads = int(params.get("max_history_reads") or 30)
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["max_history_reads must be an integer"])
    history = params.get("_history")
    return SkillResult(
        ok=True,
        value=classify(
            repos,
            declared=declared,
            history=history if callable(history) else None,
            default_host=str(params.get("default_host") or "github.com"),
            max_history_reads=max_reads,
        ),
    )


_SKILLS = {
    "cut": cut,
    "changelog": changelog,
    "status": status,
    "mode": mode,
    "check": check,
    "plan": plan,
    "sync": sync,
    "watch": watch,
    "unwatch": unwatch,
    "pause": pause,
    "resume": resume,
    "heartbeat": heartbeat,
    "list": list_,
    "close": close,
    "cross_repo_pr_watch": cross_repo_pr_watch,
    "ci_repair": ci_repair,
    "review": review,
    "land": land,
    "land_act": land_act,
    "ci_health": ci_health,
    "ci_head": ci_head,
    "lock": lock,
    "unlock": unlock,
    "lock_status": lock_status,
    "ci_sweep": ci_sweep,
    "ci_lint": ci_lint,
    "ci_main_watch": ci_main_watch,
    "ci_digest": ci_digest,
    "guards": guards,
    "auto_revert": auto_revert,
    "repo_lineage": repo_lineage,
}


# Read-only verbs safe to project onto agent + MCP (ADR-072 §4.9.4); the write
# verbs (cut/mode/sync/watch/pause/resume/close/...) stay CLI-only for now.
# ci_repair and review are reads: they name the repair and the reviewer.
# The harness runs the commands. They do not push.
# ci_health and ci_lint read the forge and the workflow files; they cancel
# nothing and send nothing. ci_sweep and ci_digest act, so they stay off.
_MCP_READS = frozenset(
    {
        "status",
        "check",
        "plan",
        "list",
        "ci_repair",
        "review",
        "land",
        "ci_health",
        "ci_head",
        "ci_lint",
        "lock_status",
    }
)


# The scheduled CI writes. A cancelled run can be re-run, an alert is a message
# and a merge can be reverted, so all are reversible. CLI and schedule only.
_CI_WRITES = {
    "land_act": (
        "Land a green, unprotected, non-draft pull request with no pending human "
        "review and no changes-requested review. A protected path is held."
    ),
    "ci_sweep": (
        "Hourly CI-health check: cancel superseded runs (never release, tag or "
        "the newest), record the budget state landing obeys, and alert on a crossing."
    ),
    "ci_digest": "Weekly CI-health digest through notifications.",
    "ci_main_watch": (
        "Read the default branch from outside the local watch and keep its one "
        "incident. Writes only with act. Never merges, re-runs or cancels."
    ),
    # The loop, phase 3 (ADR-257 D6). A revert is an ordinary commit that a later
    # commit can undo, a re-run can be repeated and an ask is a message.
    "auto_revert": (
        "Judge one default-branch commit whose required check failed: re-run a known "
        "flake once, then revert the commit and ask its author. Only with act and the "
        "repository variable AXIOM_AUTO_REVERT=1; never a revert, never during the "
        "release lock, never when the failure predates the commit."
    ),
    # The release lock (#1782). Both are reversible: a lock lifts, a lift can be
    # taken again, and auto-merge is re-armed at the head each pull request had.
    "lock": (
        "Take the release lock: landing pauses until unlock, the tag, or the "
        "timeout. Disarms auto-merge on armed pull requests with a comment, marks "
        "the repository, and tells the roster."
    ),
    "unlock": (
        "Lift the release lock now: re-arm the pull requests it disarmed, comment, "
        "clear the repository marker, and tell the roster."
    ),
}


#: RIVET hands to KEEL around a deploy (ADR-230): never deploy onto a node whose checks fail or
#: that has drifted from what it was tested as; afterwards, confirm what actually runs.
_KEEL_HANDOFFS = {
    "check": (
        "Pre-release checks for this repository: what a release would include and whether it is ready.",
        (
            NextStep(
                "node.doctor",
                "ok",
                "before the release lands on a node: never deploy onto a node whose checks fail",
            ),
            NextStep(
                "node.drift",
                "ok",
                "a drifted node receives the release differently from the one it was tested on",
            ),
        ),
    ),
    "status": (
        "Where the release stands: version, tags, what was published.",
        (
            NextStep(
                "node.inventory", "ok", "after a deploy, confirm what actually runs on the node"
            ),
        ),
    ),
}


#: Pure reads other agents compose over the registry (ADR-161): no CLI verb, no audit entry.
_COMPOSED_READS = {
    "repo_lineage": SkillSpec(
        name=f"{_NAMESPACE}.repo_lineage",
        fn=repo_lineage,
        description=(
            "Classify repositories as origin, mirror, fork, archived or unverified from "
            "forge metadata, declared pairs and shared-commit evidence. Never guesses."
        ),
        inputs={
            "repos": "list",
            "declared": "list",
            "default_host": "str",
            "max_history_reads": "int",
        },
        idempotent=True,
        side_effects=False,
        surfaces=("mcp", "agent_tool"),
        risk="read",
        requires=(),
    ),
}


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        if verb in _COMPOSED_READS:
            registry.register_skill(_COMPOSED_READS[verb], mutating=False)
        elif verb in _MCP_READS:
            inputs = {
                "ci_repair": {"log": "str", "check": "str", "attempts": "str"},
                "review": {"author_model": "str", "models": "str", "pull": "str"},
                "land": {
                    "repo": "str",
                    "window_hours": "str",
                    "config": "str",
                    "format": "str",
                    "suite_minutes": "str",
                    "propose": "str",
                },
                "ci_health": {"repo": "str", "config": "str", "format": "str"},
                "ci_head": {
                    "repo": "str",
                    "sha": "str",
                    "branch": "str",
                    "config": "str",
                    "format": "str",
                },
                "ci_lint": {"repo": "str", "path": "str", "config": "str", "format": "str"},
                "lock_status": {"repo": "str", "config": "str", "format": "str"},
            }.get(verb, {})
            extra = {}
            if verb in {"ci_repair", "review", "land", "ci_health", "ci_head", "ci_lint", "lock_status"}:
                extra = {"risk": "read", "requires": ()}
            registry.register_skill(
                SkillSpec(
                    name=name,
                    fn=fn,
                    description={
                        "ci_repair": (
                            "Name the mechanical repair for a CI failure and how to "
                            "resubmit it. Does not push. A harness runs the commands."
                        ),
                        "review": (
                            "Pick a model to review a change. It is never the model "
                            "that wrote the change."
                        ),
                        "land": (
                            "Report proposed batches, superseded pull requests, "
                            "what is ready to land, and what needs a person, "
                            "with estimated minutes saved. Propose does not merge."
                        ),
                        "ci_health": (
                            "CI spend and waste: month-to-date Actions minutes and cost, "
                            "run-rate projection, percent of budget, minutes by repo, "
                            "workflow, event and job, and waste signals (update-branch "
                            "re-runs, superseded runs, selection fallbacks, flakes, "
                            "chronic red, duplicate main runs). Reads only."
                        ),
                        "ci_head": (
                            "The CI verdict for one head of a branch, from that head's "
                            "own run and never from the last completed run: success, "
                            "failure (with the failing checks), pending, or superseded "
                            "(no run of its own; the newest completed head is named). "
                            "Default: the default branch's current head. Reads only."
                        ),
                        "ci_lint": (
                            "Warn on workflow shapes that waste CI minutes: no PR "
                            "concurrency cancel, long jobs with no fast stage, no "
                            "docs-only filter, daily matrices, strict protection "
                            "without a merge queue. Never blocks."
                        ),
                        "lock_status": (
                            "Whether a release lock is held on a repository, by whom, "
                            "why, until when, and which pull requests it disarmed. "
                            "Reads the local state file and the repo marker."
                        ),
                    }.get(verb, ""),
                    inputs=inputs,
                    idempotent=True,
                    side_effects=False,
                    surfaces=("cli", "mcp", "agent_tool"),
                    **extra,
                )
            )
        elif verb in _CI_WRITES:
            registry.register_skill(
                SkillSpec(
                    name=name,
                    fn=fn,
                    description=_CI_WRITES[verb],
                    inputs={
                        "ci_main_watch": {
                            "repo": "str",
                            "config": "str",
                            "format": "str",
                            "act": "bool",
                        },
                        "land_act": {
                            "repo": "str",
                            "window_hours": "str",
                            "config": "str",
                            "format": "str",
                            "suite_minutes": "str",
                        },
                        "lock": {
                            "repo": "str",
                            "reason": "str",
                            "minutes": "int",
                            "config": "str",
                            "format": "str",
                        },
                        "unlock": {"repo": "str", "config": "str", "format": "str"},
                        "auto_revert": {
                            "repo": "str",
                            "sha": "str",
                            "config": "str",
                            "format": "str",
                            "act": "bool",
                        },
                    }.get(
                        verb, {"repo": "str", "config": "str", "format": "str", "dry_run": "bool"}
                    ),
                    idempotent=False,
                    side_effects=True,
                    surfaces=("cli",),
                    risk="reversible-write",
                    requires=(),
                )
            )
        elif verb == "guards":
            registry.register_skill(
                SkillSpec(
                    name=name,
                    fn=fn,
                    description=(
                        "Run the cheap guards (root directories, public surface, naming, ADR "
                        "numbers, credentials in the pushed lines) on a pushed range and report "
                        "its tier from the tier map. The pre-push hook and the server run this."
                    ),
                    inputs={"base": "str", "head": "str", "root": "str", "only": "str", "format": "str"},
                    idempotent=True,
                    side_effects=False,
                    surfaces=("cli",),
                    risk="read",
                    requires=(),
                )
            )
        elif verb in _KEEL_HANDOFFS:
            registry.register_skill(
                SkillSpec(
                    name=name,
                    fn=fn,
                    description=_KEEL_HANDOFFS[verb][0],
                    surfaces=("cli",),
                    next_steps=_KEEL_HANDOFFS[verb][1],
                )
            )
        else:
            registry.register(name, fn)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
