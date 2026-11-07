# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Directory skill functions (ADR-056 shape: ``(params, ctx) -> SkillResult``).

``directory.sync``   — one projection run (what the heartbeat fires).
``directory.status`` — configuration + last run, no network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.directory.config import load_directory_config
from axiom.extensions.builtins.directory.protocol import GroupRef
from axiom.extensions.builtins.directory.sync import run_sync
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult

LAST_SYNC_FILE = "last-sync.json"


def _state(ctx: SkillContext) -> Path:
    return Path(ctx.state_dir) / "directory"


def _write_last(ctx: SkillContext, report: dict[str, Any]) -> None:
    d = _state(ctx)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (LAST_SYNC_FILE + ".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    tmp.replace(d / LAST_SYNC_FILE)


def _read_last(ctx: SkillContext) -> dict[str, Any] | None:
    p = _state(ctx) / LAST_SYNC_FILE
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sync(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Project configured groups into the tuple store.

    ``params``: ``dry_run`` (bool), ``groups`` (list[str] — override the
    configured set), ``env`` (mapping — tests), ``graph_client`` (injected).
    A dry run never writes and is ``ok`` even when it would change things; a
    real run is ``ok`` only when every group synced cleanly.
    """
    cfg = load_directory_config(
        params.get("env"), state_dir=Path(ctx.state_dir), graph_client=params.get("graph_client")
    )
    if cfg.errors:
        return SkillResult(ok=False, value={"config": cfg.summary()}, errors=list(cfg.errors))
    groups = cfg.sync_groups
    override = params.get("groups")
    if override:
        groups = tuple(GroupRef(id=str(g), provider=cfg.provider_name) for g in override)
    dry_run = bool(params.get("dry_run", False))
    revoked = None if dry_run else cfg.revoked_set()
    report = run_sync(
        provider=cfg.provider,
        provider_name=cfg.provider_name,
        groups=groups,
        store=cfg.tuple_store,
        revoked=revoked,
        dry_run=dry_run,
    )
    value = report.to_dict()
    value["config"] = cfg.summary()
    if not dry_run:
        _write_last(ctx, value)
    errors = list(report.errors) + [f"{g.group_id}: {g.error}" for g in report.groups if g.error]
    actions = []
    if not dry_run:
        actions.extend(
            f"{g.group_id}: +{len(g.added)} -{len(g.removed)}" for g in report.groups if g.changed
        )
    return SkillResult(ok=not errors, value=value, errors=errors, actions_taken=actions)


def status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    cfg = load_directory_config(
        params.get("env"), state_dir=Path(ctx.state_dir), graph_client=params.get("graph_client")
    )
    last = _read_last(ctx)
    revoked = None
    try:
        revoked = len(cfg.revoked_set())
    except Exception:  # noqa: BLE001 — a bad file is reported, not fatal
        pass
    value = {
        "config": cfg.summary(),
        "revoked_entries": revoked,
        "last_sync": last,
        "last_sync_age_s": (
            round(time.time() - last["started_at"], 1) if last and last.get("started_at") else None
        ),
    }
    return SkillResult(ok=not cfg.errors, value=value, errors=list(cfg.errors))


_SKILLS = {"sync": sync, "status": status}


def bind(registry: SkillRegistry) -> None:
    for name, fn in _SKILLS.items():
        registry.register(f"directory.{name}", fn)


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


def verbs() -> list[str]:
    return list(_SKILLS)


__all__ = ["LAST_SYNC_FILE", "bind", "bind_default", "status", "sync", "verbs"]
