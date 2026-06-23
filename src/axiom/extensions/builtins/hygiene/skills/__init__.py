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

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, default_registry

from .. import _legacy_cli as _legacy


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
# `stat` consolidates: vitals / health / ci / drift / retention.
# `list` consolidates: worktrees / branches.

_STAT_RESOURCES = {
    "vitals": _legacy._cmd_vitals,
    "health": _legacy._cmd_health,
    "ci": _legacy._cmd_ci,
    "drift": _legacy._cmd_drift,
    "retention": _legacy._cmd_retention,
}


def stat(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``axi hygiene stat <vitals|health|ci|drift|retention>``."""
    resource = params.get("resource")
    if resource not in _STAT_RESOURCES:
        return SkillResult(
            ok=False,
            errors=[f"unknown stat resource {resource!r}; "
                    f"supported: {sorted(_STAT_RESOURCES)}"],
        )
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
