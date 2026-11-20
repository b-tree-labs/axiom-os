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

from ..medallion import NotAvailableOnTier, UnknownTier, envelope, resolver_for


def _bound(verb: str, params: dict[str, Any]) -> SkillResult | None:
    """On a bound install, the gold catalog and a gold table's shape come from the node.

    Everything else these verbs read (silver, bronze, freshness, samples) has no read across the
    binding, so it refuses and says where the data is instead of describing this machine's
    database. ``None``: this install answers itself.
    """
    from ..read_target import ask, refuse_when_bound, resolve_read_target

    refusal = refuse_when_bound(f"data.{verb}", params)
    if refusal is None:
        return None
    tier = str(params.get("tier") or "gold")
    if tier != "gold" or verb not in ("catalog", "describe"):
        return refusal
    target = resolve_read_target()
    if verb == "catalog":
        result = ask(target, verb="data.tables", params={})
        if not result.ok:
            return result
        tables = ((result.value or {}).get("data") or {}).get("tables") or []
        return SkillResult(ok=True, value=envelope(
            data={"tier": "gold", "objects": tables},
            tier="gold",
            source=f"gold on {target.site}",
            method="information_schema.tables, on the bound node",
            rows=len(tables),
        ))
    obj = params.get("object") or params.get("table")
    if not obj:
        return SkillResult(ok=False, errors=["`object` is required — which table to describe"])
    result = ask(target, verb="data.describe", params={"table": str(obj)})
    if not result.ok:
        return result
    columns = ((result.value or {}).get("data") or {}).get("columns") or []
    return SkillResult(ok=True, value=envelope(
        data={"tier": "gold", "object": obj, "columns": columns},
        tier="gold",
        source=f"gold.{obj} on {target.site}",
        method="information_schema.columns, on the bound node",
        rows=len(columns),
    ))


def _run(verb: str, params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    bound = _bound(verb, params)
    if bound is not None:
        return bound
    try:
        from axiom.infra.site_authority import tenant_scope

        if tenant_scope() and (params.get("tier") or "gold") == "bronze":
            # Bronze is every tenant's raw files in one tree. A credential bound to
            # a site reads served tiers, held to that site; raw bytes are the
            # operator's.
            return SkillResult(
                ok=False,
                errors=["refused: bronze holds every tenant's raw files; a site-bound credential reads the served tiers"],
            )
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
