# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``app.acceptance`` (read) and ``app.acceptance_run`` (CLI-only run), per ADR-056.

The read scans the configured repositories for tests carrying a row ID and
folds in the results it knows: the last ``--run`` (cached under the state
directory) and any results file a repository's own run wrote. It never runs a
test, so it is safe on MCP and in chat. The run executes the tagged tests and
writes the cache.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

from .. import acceptance as acc
from . import variants as _variants


def _cache_path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir()) / "app" / "acceptance-results.json"


def _read_cache(cfg: acc.HarnessConfig) -> tuple[dict[str, dict[str, str]], str]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, ""
    # A cache from another workspace's config would mark the wrong tests proven.
    if data.get("config") != cfg.source:
        return {}, ""
    return data.get("results") or {}, data.get("ran_at", "")


def _load(params: dict[str, Any]) -> acc.HarnessConfig:
    return acc.resolve_config(params.get("config") or None)


def _phase(params: dict[str, Any]) -> str:
    return str(params.get("phase") or "all").strip() or "all"


def acceptance(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """How far is the MVP? Every spec row, the tests that carry its ID, pass / fail / unproven."""
    try:
        cfg = _load(params)
        cached, ran_at = _read_cache(cfg)
        report = acc.build_report(cfg, phase=_phase(params), cached=cached, cached_at=ran_at)
    except (OSError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"acceptance: {exc}"])
    return SkillResult(ok=True, value=report, actions_taken=acc.render(report))


def run_tests(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Run every tagged acceptance test in each configured repo and cache the results."""
    try:
        cfg = _load(params)
    except (OSError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"acceptance: {exc}"])
    results: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for repo in cfg.repos:
        found, errs = acc.scan_repo(repo)
        errors += errs
        if not found:
            continue
        try:
            results[repo.name] = acc.run_repo(repo, found)
        except (OSError, ValueError) as exc:
            errors.append(f"{repo.name}: could not run its tests: {exc}")
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ran_at = datetime.now(UTC).isoformat(timespec="seconds")
    path.write_text(json.dumps({"config": cfg.source, "ran_at": ran_at, "results": results},
                               indent=2, sort_keys=True), encoding="utf-8")
    ran = sum(len(v) for v in results.values())
    return SkillResult(ok=True, value={"ran": ran, "ran_at": ran_at, "errors": errors},
                       actions_taken=[f"ran {ran} tagged test(s); results cached at {path}",
                                      *[f"  note: {e}" for e in errors]])


_READ_DESCRIPTION = (
    "How far is the MVP? Lists every acceptance row of the web app build-out spec (P0, P1 or "
    "all) with the tests that carry its ID across the configured repositories, and a verdict "
    "of pass, fail or unproven. Read-only; it reports the last test run's results."
)


def bind(registry: SkillRegistry) -> list[str]:
    if not registry.has("app.acceptance"):
        registry.register_skill(
            SkillSpec(
                name="app.acceptance",
                fn=acceptance,
                description=_READ_DESCRIPTION,
                inputs={"phase": "P0 | P1 | all (default all)",
                        "config": "optional path to an acceptance.toml"},
                side_effects=False,
                idempotent=True,
                risk="read",
                surfaces=("cli", "mcp", "agent_tool", "skill_md"),
            ),
            mutating=False,
        )
    if not registry.has("app.acceptance_run"):
        registry.register_skill(
            SkillSpec(
                name="app.acceptance_run",
                fn=run_tests,
                description=("Run every tagged acceptance test in each configured repository "
                             "and cache the results the acceptance report reads"),
                inputs={"config": "optional path to an acceptance.toml"},
                side_effects=True,
                idempotent=False,
                # Runs tests and rewrites a results cache; nothing to undo.
                risk="reversible-write",
                requires=(),
                surfaces=("cli",),
            ),
            mutating=True,
        )
    if not registry.has("app.variants"):
        registry.register_skill(
            SkillSpec(
                name="app.variants",
                fn=_variants.variants,
                description=("The web app's variants (bare Axiom, the bare product, the site): "
                             "each one's port, path, session realm and cookie, app-state "
                             "tenants and flag layer. Read-only."),
                inputs={"config": "optional path to a variants.toml"},
                side_effects=False,
                idempotent=True,
                risk="read",
                surfaces=("cli", "mcp", "agent_tool", "skill_md"),
            ),
            mutating=False,
        )
    if not registry.has("app.run"):
        registry.register_skill(
            SkillSpec(
                name="app.run",
                fn=_variants.run,
                description=("Start one web app variant, or all three, as supervised copies of "
                             "the served app, each on its own port with its own realm; "
                             "dry_run returns the plan and starts nothing"),
                inputs={"variant": "axiom | product | site", "all": "start every variant",
                        "dry_run": "plan only", "host": "bind address (default 127.0.0.1)",
                        "config": "optional path to a variants.toml"},
                side_effects=True,
                idempotent=False,
                # Starts long-running processes; stopping them undoes it.
                risk="reversible-write",
                requires=(),
                surfaces=("cli",),
            ),
            mutating=True,
        )
    return ["app.acceptance", "app.acceptance_run", "app.variants", "app.run"]


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["acceptance", "bind", "bind_default", "register", "run_tests"]
