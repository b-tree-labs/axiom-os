# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What every video skill does the same way: name the source, find the state, say the fix."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def cli() -> str:
    """The active distribution's program, for fixes that name another noun."""
    from ..naming import cli_name

    return cli_name()


def command() -> str:
    """``<cli> instrument video``, for fixes that name a video verb (ADR-256 D4)."""
    from ..naming import command as _command

    return _command()


def state_dir(ctx: SkillContext | None) -> Path:
    found = getattr(ctx, "state_dir", None)
    if found:
        return Path(found)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def source_name(params: dict[str, Any]) -> str:
    return str(params.get("name") or "").strip()


def refuse(message: str, fix: str = "", **value: Any) -> SkillResult:
    return SkillResult(
        ok=False, value=value or None, errors=[f"{message}. Fix: {fix}" if fix else message]
    )


def need_name(verb: str) -> SkillResult:
    return refuse(f"name the source to {verb}", f"`{command()} list` shows the declared sources")


__all__ = ["cli", "command", "need_name", "refuse", "source_name", "state_dir"]
