# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Synthetic skills: thin, registry-bound entry points (ADR-056)."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry

from . import run


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register("synthetic.run", run.run)
    return registry


__all__ = ["bind_default", "run"]
