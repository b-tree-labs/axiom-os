# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``hygiene`` skills — invocable through the platform SkillRegistry.

Each skill maps 1:1 to a CLI verb in ``axi hygiene``. The business
logic stays in ``_legacy_cli.py`` (kept intact during the verb-grammar
migration to minimize churn risk); these skills are thin
``(params, ctx) -> SkillResult`` adapters per ADR-056.

A future PR can lift formatting out of the legacy ``_cmd_X``
functions into structured ``SkillResult.value`` + a CLI emitter
(matching the data_platform pattern). Doing it now multiplies the
PR's blast radius unnecessarily; the verb-grammar + noun rename are
the load-bearing changes here.

Namespace: ``hygiene.<verb>``.
"""

from __future__ import annotations

import argparse
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

from .. import _legacy_cli as _legacy
# Imported before any package-level name `reclaim` is bound, so the submodule
# resolves to the module and not to a shadowing attribute.
from . import reclaim as _reclaim_mod
from . import artifacts as _artifacts_mod
from . import report as _report_mod

_NAMESPACE = "hygiene"


def _skill(legacy_cmd):
    """Wrap a legacy `_cmd_X(args)` int handler as a SkillResult-returning skill."""

    def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        args = argparse.Namespace(**params)
        try:
            rc = legacy_cmd(args)
        except SystemExit as e:
            rc = int(e.code) if e.code is not None else 0
        except Exception as exc:
            return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
        return SkillResult(ok=(rc == 0))

    run.__name__ = f"run_{legacy_cmd.__name__.removeprefix('_cmd_')}"
    return run


# Direct 1:1 wrappers for the imperative-leaf verbs.
status = _skill(_legacy._cmd_status)
ls = _skill(_legacy._cmd_ls)
clean = _skill(_legacy._cmd_clean)
purge = _skill(_legacy._cmd_purge)
diagnose = _skill(_legacy._cmd_diagnose)
discover = _skill(_legacy._cmd_discover)
propose = _skill(_legacy._cmd_propose)
approve = _skill(_legacy._cmd_approve)
deny = _skill(_legacy._cmd_deny)


# Grammar-restructured: `axi hygiene <noun>` → `axi hygiene <verb> <resource>`.
# `stat` consolidates: vitals / health / ci / drift / retention / git.
# `list` consolidates: worktrees / branches.

_STAT_RESOURCES = {
    "vitals": _legacy._cmd_vitals,
    "health": _legacy._cmd_health,
    "ci": _legacy._cmd_ci,
    "drift": _legacy._cmd_drift,
    "retention": _legacy._cmd_retention,
    "git": _legacy._cmd_git,
    "docs": _legacy._cmd_docs,
    # The repo standard (ADR-221): required files, AGENTS.md sections, and the
    # context extension's managed-block findings.
    "repo": _legacy._cmd_repo,
    # Declaration drift: capabilities that are registered but not DECLARED, so
    # nothing can discover them. Lives here because hygiene is the drift surface
    # and — the practical half — the one daemon operators actually consent to
    # run. A check on a disabled daemon is the failure this came from.
    "capabilities": None,
    # The disk-pressure ladder: margin to the kubelet's eviction line on every
    # volume the node depends on (MVP-19). A structured read, safe on MCP.
    "disk": None,
    "storage": None,
}


def stat(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi hygiene stat <vitals|health|ci|drift|retention|git|docs|repo|capabilities|disk|storage>``."""
    resource = params.get("resource")
    if resource not in _STAT_RESOURCES:
        return SkillResult(
            ok=False,
            errors=[f"unknown stat resource {resource!r}; "
                    f"supported: {sorted(_STAT_RESOURCES)}"],
        )
    if resource == "capabilities":
        from axiom.extensions.builtins.hygiene.skills import capabilities

        return capabilities.run(params, ctx)
    if resource == "disk":
        from axiom.extensions.builtins.hygiene.skills import disk

        return disk.report(params, ctx)
    if resource == "storage":
        params = dict(params)
        params["watch_only"] = True  # stat is a report; the artifacts verb acts
        return _artifacts_mod.artifacts(params, ctx)
    return _skill(_STAT_RESOURCES[resource])(params, ctx)


_LIST_RESOURCES = {
    "worktrees": _legacy._cmd_worktrees,
    "branches": _legacy._cmd_branches,
}


def list_(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi hygiene list <worktrees|branches>``."""
    resource = params.get("resource")
    if resource not in _LIST_RESOURCES:
        return SkillResult(
            ok=False,
            errors=[f"unknown list resource {resource!r}; "
                    f"supported: {sorted(_LIST_RESOURCES)}"],
        )
    return _skill(_LIST_RESOURCES[resource])(params, ctx)


_SKILLS = {
    "status": status,
    "ls": ls,
    "list": list_,
    "clean": clean,
    "purge": purge,
    "stat": stat,
    "diagnose": diagnose,
    "discover": discover,
    "propose": propose,
    "approve": approve,
    "deny": deny,
    "reclaim": _reclaim_mod.run,
    "artifacts": _artifacts_mod.artifacts,
    "storage_tick": _artifacts_mod.storage_tick,
    "storage_digest": _artifacts_mod.storage_digest,
    "report": _report_mod.run,
    "github_tick": _report_mod.github_tick,
}


def _disk_watch(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Take the disk-ladder reading, notify on a change of rung, save it for status."""
    from axiom.extensions.builtins.hygiene.skills import disk

    return disk.watch(params, ctx)


#: Writes (state + a notification), so CLI-only: ``axi hygiene stat disk --notify``.
_SKILLS["disk_watch"] = _disk_watch


# Read-only verbs safe to project onto agent + MCP (ADR-072 §4.9.4); writes
# stay CLI-only until a per-verb decision (a write on MCP must declare its effect).
_MCP_READS = frozenset({"status", "ls", "list", "stat", "diagnose", "discover", "reclaim", "artifacts", "report"})

# Pulse ticks and the weekly digest: they write (deletes, closes, HERALD), so
# they stay CLI-only. A SkillSpec is required so they do not raise the
# undiscoverable-capability ratchet.
_CLI_WRITES = {
    "storage_tick": (
        "PULSE daily tick: reclaim in-policy Actions artifacts and caches "
        "unless the kill switch or --dry-run is on"
    ),
    "storage_digest": (
        "Weekly TIDY storage report through HERALD (always a report, never a delete)"
    ),
    "github_tick": (
        "PULSE tick: act on stale drafts and merged/closed notifications, then digest"
    ),
}


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        if verb == "disk_watch":
            registry.register_skill(
                SkillSpec(
                    name=name, fn=fn,
                    description=("Take the disk-pressure ladder reading, notify a person on a "
                                 "change of rung, and save it for the status line (the node "
                                 "timer's verb)"),
                    inputs={"config": "optional path to the site's disk-ladder.toml"},
                    idempotent=False, side_effects=True, surfaces=("cli",),
                    # Saves a reading and writes an inbox alert; nothing destructive.
                    risk="reversible-write", requires=(),
                ),
                mutating=True,
            )
        elif verb in _CLI_WRITES:
            registry.register_skill(
                SkillSpec(
                    name=name, fn=fn,
                    description=_CLI_WRITES[verb],
                    idempotent=False, side_effects=True, surfaces=("cli",),
                    risk="reversible-write", requires=(),
                ),
                mutating=True,
            )
        elif verb in _MCP_READS:
            registry.register_skill(
                SkillSpec(name=name, fn=fn, idempotent=True, side_effects=False,
                          surfaces=("cli", "mcp", "agent_tool"))
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
