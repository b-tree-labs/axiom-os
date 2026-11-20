# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Identity skill functions (ADR-056 shape: ``(params, ctx) -> SkillResult``).

- ``whoami`` — who is acting (the runtime principal).
- ``status`` — principal + posture + the node's floor + assurance.
- ``init`` — create/load the local ``attested`` principal (custodied keypair);
  ``params['custody']`` overrides the backend (tests use in-memory).
"""

from __future__ import annotations

from typing import Any

from axiom.infra.principal import local_handle, node_posture, principal_provenance
from axiom.infra.skills import SkillContext, SkillResult


def whoami(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return SkillResult(ok=True, value=principal_provenance(ctx.principal))


def status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    value = principal_provenance(ctx.principal)
    value["node_floor"] = node_posture()
    return SkillResult(ok=True, value=value)


def init(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Create or load the local principal's keypair (the attested identity)."""
    from axiom.vega.identity.local import load_or_create_local_keypair

    keypair = load_or_create_local_keypair(custody=params.get("custody"))
    return SkillResult(
        ok=True,
        value={"handle": local_handle(), "public_key": keypair.public_bytes.hex()},
        actions_taken=["created or loaded the local principal keypair"],
    )


__all__ = ["init", "status", "whoami"]
