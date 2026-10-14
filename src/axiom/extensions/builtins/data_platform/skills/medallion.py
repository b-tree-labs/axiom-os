# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.catalog`` / ``describe`` / ``freshness`` / ``sample`` — any tier.

Four verbs, one vocabulary, a ``tier`` argument. They replace six that split
the same questions across two naming schemes with the tier baked into the
name, so a caller had to know which medallion they were on before they could
phrase the question.

Every verb returns the same envelope whatever answered it, which is the half
that matters downstream: a consumer branching on tier to read a result was
paying for the inconsistency.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..medallion import NotAvailableOnTier, UnknownTier, resolver_for


def _run(verb: str, params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        resolver = resolver_for(params.get("tier"))
    except UnknownTier as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except NotAvailableOnTier as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    try:
        return SkillResult(ok=True, value=getattr(resolver, verb)(params, ctx))
    except NotAvailableOnTier as exc:
        # The verb is real and this tier cannot answer it. A typed refusal
        # that says WHY beats an empty result, which is indistinguishable
        # from a tier that answered and had nothing.
        return SkillResult(ok=False, errors=[str(exc)])
    except FileNotFoundError as exc:
        return SkillResult(ok=False, errors=[f"not found: {exc}"])
    except (ValueError, OSError) as exc:
        return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])


def catalog(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """What this tier holds."""
    return _run("catalog", params, ctx)


def describe(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The shape of one object in this tier."""
    return _run("describe", params, ctx)


def freshness(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """How current this tier is, and what has stopped advancing."""
    return _run("freshness", params, ctx)


def sample(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Some records, to see the shape of what is arriving."""
    return _run("sample", params, ctx)


__all__ = ["catalog", "describe", "freshness", "sample"]
