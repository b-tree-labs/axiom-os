# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``press.publish`` — draft + upload + notify via the event bus.

Per ADR-060: publishing emits events; agent_bridge routes to HERALD.
First call-site implementing the contract that retires publishing's
direct NotificationProvider import (completed in M3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _get_bus():
    """Return the platform EventBus. Tests monkey-patch this."""
    from axiom.infra.bus import get_default_eventbus
    return get_default_eventbus()


def _publish_event(subject: str, payload: dict[str, Any]) -> None:
    try:
        bus = _get_bus()
        bus.publish(subject, payload, source="agent.press")
    except Exception:  # noqa: BLE001
        pass


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    src = params.get("source")
    if not src:
        return SkillResult(ok=False, errors=["missing required param: source"])
    source_path = Path(src).resolve()
    if not source_path.exists():
        return SkillResult(
            ok=False, errors=[f"source file not found: {src}"]
        )

    from axiom.extensions.builtins.publishing.engine import PublisherEngine

    draft_mode = bool(params.get("draft", False))
    storage_override = params.get("endpoint")
    force = bool(params.get("force", False))

    try:
        engine = PublisherEngine()
        result = engine.publish(
            source_path,
            storage_override=storage_override,
            draft=draft_mode,
            force=force,
        )
    except Exception as exc:  # noqa: BLE001
        _publish_event(
            "publishing.failed",
            {"source": str(source_path), "error": f"{type(exc).__name__}: {exc}", "draft": draft_mode},
        )
        return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])

    _publish_event(
        "publishing.draft_ready" if draft_mode else "publishing.succeeded",
        {"source": str(source_path), "draft": draft_mode,
         "result": result if isinstance(result, dict) else None},
    )

    return SkillResult(
        ok=True,
        value={"source": str(source_path), "result": result},
        actions_taken=[f"published {source_path.name}"],
    )


__all__ = ["run", "_get_bus", "_publish_event"]
