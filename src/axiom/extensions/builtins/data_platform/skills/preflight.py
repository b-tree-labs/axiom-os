# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``data.preflight`` — live-verify a connector and report actionable fixes.

Authenticates with the connector's stored credentials and confirms the
target is reachable, returning plain-language remediation for anything
wrong. The same shape for every source kind (the kind's provider supplies
``preflight``), so one command verifies Box, GDrive, S3, … identically —
turning the silent-crashloop-six-days-later failure mode into an instant
checklist a non-coder can act on.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..agents.plinth.connectors import load_connector
from ..sources import default_source_kind_registry


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["preflight requires a connector name"])

    try:
        config = load_connector(name, state_dir=ctx.state_dir)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"no such connector {name!r}: {exc}"])

    provider = default_source_kind_registry().get(config.kind)
    if not hasattr(provider, "preflight"):
        return SkillResult(
            ok=False,
            errors=[f"kind {config.kind!r} does not support preflight yet"],
        )

    result = provider.preflight(config)
    return SkillResult(
        ok=result.ok,
        value={
            "connector": result.connector,
            "kind": result.kind,
            "ok": result.ok,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "message": c.message,
                    "remediation": c.remediation,
                    "copy_value": c.copy_value,
                    "actor": c.actor,
                }
                for c in result.checks
            ],
        },
    )
