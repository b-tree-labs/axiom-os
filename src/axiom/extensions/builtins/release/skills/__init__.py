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
status = _wrap_int_handler(_rivet_handler("status"))
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
            return SkillResult(ok=False, errors=[f"--releases {releases}: no tags reachable from {to_ref}"])
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
            "sections": [
                {"title": t, "items": i} for t, i in (log.polished or log.sections())
            ],
            "curated": bool(log.curated),
        },
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
