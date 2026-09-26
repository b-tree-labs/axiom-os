# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fleet skills — thin, registry-bound entry points (ADR-056)."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry

from . import report, status


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register("fleet.status", status.run)
    registry.register("fleet.report", report.run)
    return registry


__all__ = ["bind_default", "status", "report"]
