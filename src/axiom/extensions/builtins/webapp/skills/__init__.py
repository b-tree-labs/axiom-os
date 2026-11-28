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

    if not registry.has("webapp.project"):
        registry.register("webapp.project", project.run)


def bind_default():
    """Bind into the process-local default registry, as the skill loader expects.

    Without it the loader never bound ``webapp.project``: the conform watcher
    looked for it after every pass, found nothing, and the serving catalog
    stopped moving while gold kept filling (on one node, for two months).
    """
    from axiom.infra.skills import default_registry

    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["bind", "bind_default"]
