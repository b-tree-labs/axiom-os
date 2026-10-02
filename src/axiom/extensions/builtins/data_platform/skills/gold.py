# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Generic answering over the served tiers (ADR-115).

Read-only verbs that let a base install answer quantitative questions about
whatever it has ingested, with no domain code:

``aggregate``  one deterministic aggregate
``series``     a bucketed series (the analytics tool's input shape)
``roles``      which quantities the fleet can answer, and where
``compare``    one quantity across sites, on a shared time axis

What exists and what shape it has are ``catalog`` and ``describe``, which
answer for every tier and live in :mod:`.medallion`.

The executor (validation, filter grammar, tier guard, envelope) lives in
:mod:`..gold_query` and is unit-tested without a database. These wrappers own
only connection handling and the ``SkillResult`` shape, so the security
properties are proven where they are implemented rather than through the DB.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import gold_query as gq


def _access_tiers(params: dict[str, Any], ctx: SkillContext) -> tuple[str, ...]:
    """Access tiers this caller may read.

    Named for the access-control axis, not the medallion one: ``tier`` on
    these verbs means bronze/silver/gold, and one word cannot mean both.

    Defaults to ``public`` only. A caller cannot widen its own tiers by passing
    a parameter — that would make the guard decorative — so an explicit
    ``tiers`` is honoured only when the principal is the node owner, and even
    then the table's own tier column is what does the filtering.
    """
    declared = params.get("tiers")
    if declared and getattr(getattr(ctx, "principal", None), "assured", False):
        return tuple(str(t) for t in declared)
    return ("public",)


def _restricted(params: dict[str, Any]) -> frozenset[str]:
    return frozenset(str(t) for t in (params.get("restricted_tables") or ()))


def _group_by(params: dict[str, Any]) -> tuple[str, ...]:
    """``group_by`` as a tuple, accepting a comma-separated string from the CLI."""
    raw = params.get("group_by") or ()
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    return tuple(str(c) for c in raw if str(c).strip())


def _allow_mixed(params: dict[str, Any]) -> bool:
    """Whether the caller has stated that a blended population is intended.

    Default false. Summarising a measurement and a prediction into one number
    has to be something a caller asked for in words, not something they get by
    omitting an argument.
    """
    return bool(params.get("allow_mixed", False))


def _run(params: dict[str, Any], ctx: SkillContext, fn) -> SkillResult:
    """Open a read-only connection, hand the cursor to ``fn``, wrap the result."""
    from .._dsn import resolve_dsn

    try:
        import psycopg2
    except ImportError:
        return SkillResult(ok=False, errors=["psycopg2 is not installed"])

    try:
        dsn = resolve_dsn(params)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"no database: {exc}"])

    conn = None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=15)
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            value = fn(cur)
        return SkillResult(ok=True, value=value)
    except gq.GoldQueryError as exc:
        # Caller error (unknown table/column, bad filter, tier refusal) — a
        # typed message, not a database traceback.
        return SkillResult(ok=False, errors=[str(exc)])
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def tables(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """List the gold tier's tables/views."""
    return _run(params, ctx, lambda cur: gq.list_tables(cur))


def describe(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Columns + types for one gold table."""
    table = params.get("table")
    if not table:
        return SkillResult(ok=False, errors=["`table` is required"])
    return _run(params, ctx, lambda cur: gq.describe(cur, str(table)))


def aggregate(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """One deterministic aggregate (sum/mean/min/max/std/count) of a column."""
    table, column, fn = params.get("table"), params.get("column"), params.get("fn")
    missing = [k for k, v in (("table", table), ("column", column), ("fn", fn)) if not v]
    if missing:
        return SkillResult(ok=False, errors=[f"required: {', '.join(missing)}"])
    return _run(
        params,
        ctx,
        lambda cur: gq.aggregate(
            cur,
            table=str(table),
            column=str(column),
            fn=str(fn),
            window=params.get("window"),
            filter=params.get("filter"),
            group_by=_group_by(params),
            allow_mixed=_allow_mixed(params),
            access_tiers=_access_tiers(params, ctx),
            restricted_tables=_restricted(params),
            include_synthetic=bool(params.get("include_synthetic")),
            allow_mixed_units=bool(params.get("allow_mixed_units")),
        ),
    )


def series(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """A bucketed series from a gold table."""
    table, column = params.get("table"), params.get("column")
    bucket, time_column = params.get("bucket"), params.get("time_column")
    missing = [
        k
        for k, v in (
            ("table", table),
            ("column", column),
            ("bucket", bucket),
            ("time_column", time_column),
        )
        if not v
    ]
    if missing:
        return SkillResult(ok=False, errors=[f"required: {', '.join(missing)}"])
    return _run(
        params,
        ctx,
        lambda cur: gq.series(
            cur,
            table=str(table),
            column=str(column),
            bucket=str(bucket),
            time_column=str(time_column),
            fn=str(params.get("fn") or "mean"),
            window=params.get("window"),
            filter=params.get("filter"),
            group_by=_group_by(params),
            allow_mixed=_allow_mixed(params),
            access_tiers=_access_tiers(params, ctx),
            restricted_tables=_restricted(params),
            include_synthetic=bool(params.get("include_synthetic")),
            allow_mixed_units=bool(params.get("allow_mixed_units")),
        ),
    )


def roles(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Which quantities the fleet can answer, and which sites answer each."""
    return _run(
        params, ctx,
        lambda cur: gq.roles(
            cur, include_synthetic=bool(params.get("include_synthetic"))
        ),
    )


def compare(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """One quantity across sites, on a shared time axis."""
    role, bucket = params.get("role"), params.get("bucket")
    missing = [k for k, v in (("role", role), ("bucket", bucket)) if not v]
    if missing:
        return SkillResult(ok=False, errors=[f"missing required param(s): {', '.join(missing)}"])
    raw = params.get("sites") or ()
    sites = tuple(s.strip() for s in raw.split(",")) if isinstance(raw, str) else tuple(raw)
    return _run(
        params, ctx,
        lambda cur: gq.compare(
            cur,
            role=str(role),
            bucket=str(bucket),
            sites=tuple(s for s in sites if s),
            fn=str(params.get("fn") or "mean"),
            window=params.get("window"),
            tiers=_access_tiers(params, ctx),
            include_synthetic=bool(params.get("include_synthetic")),
            allow_mixed_units=bool(params.get("allow_mixed_units")),
        ),
    )


def uncertainty_coverage(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """How much of the served surface can say how well it is known.

    A verb rather than a view, because a surface nobody knows to query is a
    surface that does not exist. The apparatus for carrying uncertainty can
    be complete and correct while nothing upstream declares anything, and in
    that state every aggregate reports `claimable: false` and no check
    anywhere fails.
    """
    return _run(
        params,
        ctx,
        lambda cur: gq.uncertainty_coverage(
            cur,
            site=str(params["site"]) if params.get("site") else None,
            include_sources=params.get("include_sources", True) is not False,
        ),
    )

__all__ = ['aggregate', 'compare', 'describe', 'roles', 'series', 'tables', 'uncertainty_coverage']
