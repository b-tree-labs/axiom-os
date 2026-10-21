# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""webapp skills — invocable through the platform SkillRegistry (ADR-056).

``webapp.project`` refreshes the serving catalog from the gold tier. A host
schedules it at ingest cadence; the API then reads the projection rather than
aggregating gold per request.
"""

from __future__ import annotations


def bind(registry) -> None:
    from axiom.extensions.builtins.webapp.skills import project

    registry.register("webapp.project", project.run)


__all__ = ["bind"]
