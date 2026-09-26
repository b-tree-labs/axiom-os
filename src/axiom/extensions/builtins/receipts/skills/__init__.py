# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Receipts skills — thin, registry-bound entry points (ADR-056).

``receipts.decide`` was declared in the manifest and registered nowhere,
and the HTTP route imported its ``run`` and called it directly. So a
decision taken in a browser skipped the gateway entirely: no GUARD
consult, no site rules, no audit record. That was survivable while a
decision only wrote a row. It is not survivable now that a decision can
RUN something.

A decision that runs a fix needs the fix's capability in the same
registry, so :func:`bind_remedy_capabilities` pulls in whatever the
declared remedies name — from the extension that owns each one, by
namespace, never a second copy. The guard test in
``tests/test_remedies_exist.py`` fails the build if a declared remedy
names a capability that cannot be bound, because a Fix button wired to
nothing is the worst dead affordance there is: it looks like the product
works.
"""

from __future__ import annotations

import importlib

from axiom.infra.skills import SkillRegistry

# The skill modules are imported INSIDE bind_default, not here. This
# package used to be empty, so importing it cost nothing; pulling the
# verbs in at module scope dragged SQLAlchemy and the receipts store
# behind them — 285 modules — into every walker that merely imports an
# extension's skills package to see what is there.


def bind_remedy_capabilities(registry: SkillRegistry) -> list[str]:
    """Bind every capability a declared remedy names. Returns the ones
    that could NOT be bound, so a caller (and the guard test) can say
    which, rather than discovering it when someone presses the button."""
    from axiom.extensions.builtins.receipts.conditions import CONDITIONS
    from axiom.infra.skill_dispatch import capability_namespace

    wanted = {c.remedy.capability for c in CONDITIONS.values() if c.remedy is not None}
    missing: list[str] = []
    for capability in sorted(wanted):
        if registry.has(capability):
            continue
        namespace = capability_namespace(capability)
        try:
            owner = importlib.import_module(f"axiom.extensions.builtins.{namespace}.skills")
            adopted = registry.adopt(owner.bind_default(), capability)
        except Exception:  # noqa: BLE001 — an absent extension is a missing capability
            adopted = False
        if not adopted:
            missing.append(capability)
    return missing


def bind_default() -> SkillRegistry:
    from . import decide, digest, direct, receipt, today

    registry = SkillRegistry()
    registry.register("receipts.today", today.run)
    registry.register("receipts.receipt", receipt.run)
    registry.register("receipts.direct", direct.run)
    registry.register("receipts.decide", decide.run)
    registry.register("receipts.digest", digest.run)
    bind_remedy_capabilities(registry)
    return registry


__all__ = ["bind_default", "bind_remedy_capabilities"]
