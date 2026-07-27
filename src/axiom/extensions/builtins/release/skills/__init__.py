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

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, default_registry

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
    for flag in ("dry_run", "status", "changelog", "tag_only", "no_push",
                 "skip_tests", "yes"):
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
status     = _wrap_int_handler(_rivet_handler("status"))
mode       = _wrap_int_handler(_rivet_handler("mode"))
check      = _wrap_int_handler(_rivet_handler("check"))
plan       = _wrap_int_handler(_rivet_handler("plan"))
sync       = _wrap_int_handler(_rivet_handler("sync"))
watch      = _wrap_int_handler(_rivet_handler("watch"))
unwatch    = _wrap_int_handler(_rivet_handler("unwatch"))
pause      = _wrap_int_handler(_rivet_handler("pause"))
resume     = _wrap_int_handler(_rivet_handler("resume"))
# `heartbeat` is the load-bearing dispatcher target. Per the 2026-06-01
# autopsy: launchd / systemd fire `axi release heartbeat` every
# heartbeat_interval seconds. The previous omission of this skill
# bricked RIVET's heartbeat.jsonl writer for 28 hours. NEVER drop it
# from this dispatch chain again — TIDY's heartbeat_liveness_audit
# (added in this PR) catches recurrence by watching ``~/.axi/agents/
# <agent>/heartbeat.jsonl`` staleness.
heartbeat  = _wrap_int_handler(_rivet_handler("heartbeat"))


# Grammar-restructured (rivet → release):
#   patterns        → list patterns
#   watched         → list watched
#   paused          → list paused
#   close-stale     → close stale

_LIST_RESOURCES = {
    "patterns": _rivet_handler("patterns"),
    "watched":  _rivet_handler("watched"),
    "paused":   _rivet_handler("paused"),
}


def list_(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi release list <patterns|watched|paused>``."""
    resource = params.get("resource")
    h = _LIST_RESOURCES.get(resource)
    if h is None:
        return SkillResult(
            ok=False,
            errors=[f"unknown list resource {resource!r}; "
                    f"supported: {sorted(_LIST_RESOURCES)}"],
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


_SKILLS = {
    "cut":       cut,
    "status":    status,
    "mode":      mode,
    "check":     check,
    "plan":      plan,
    "sync":      sync,
    "watch":     watch,
    "unwatch":   unwatch,
    "pause":     pause,
    "resume":    resume,
    "heartbeat": heartbeat,
    "list":      list_,
    "close":     close,
    "cross_repo_pr_watch": cross_repo_pr_watch,
}


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
