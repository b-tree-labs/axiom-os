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


_GUARD = None


def _guard():
    """The process's serving guard (ADR-157), built on first use."""
    global _GUARD
    if _GUARD is None:
        from axiom.infra import serving_guard as sgm

        _GUARD = sgm.ServingGuard(sgm.load_policy())
    return _GUARD


def _run(
    params: dict[str, Any],
    ctx: SkillContext,
    fn,
    *,
    verb: str = "data.gold",
    cost_class: str | None = None,
    pre=None,
) -> SkillResult:
    """Admit the call, open a read-only connection, hand the cursor to ``fn``.

    The limits live HERE, at the one door every gold verb passes through
    (ADR-157): admission (suspension, declared cost class, per-principal rate
    and concurrency) comes first, then ``pre(budget)`` — the cheap preflight
    that can refuse or reshape BEFORE a connection is paid for — and only
    then the query. The connection string comes from the deployment
    (environment or platform config), never from ``params``: a caller cannot
    redirect the database (prd-gold-serving-safeguards Phase 0.2).
    """
    from axiom.infra import serving_guard as sgm

    from .._dsn import dsn_source, resolve_dsn

    guard = _guard()
    outcome = guard.admit(
        verb=verb,
        cost_class=cost_class,
        caller_class=sgm.caller_class(
            surface=getattr(ctx, "surface", None),
            assured=getattr(getattr(ctx, "principal", None), "assured", False),
        ),
        principal=(
            getattr(getattr(ctx, "principal", None), "handle", None) or "@anonymous"
        ),
    )
    if not outcome.allowed:
        return SkillResult(ok=False, errors=[f"refused: {outcome.reason}"])

    try:
        if pre is not None:
            refusal = pre(outcome.budget)
            if refusal is not None:
                return refusal

        try:
            import psycopg2
        except ImportError:
            return SkillResult(ok=False, errors=["psycopg2 is not installed"])

        try:
            dsn = resolve_dsn()
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
            # WHERE the DSN came from, not just what it was (#1064). A request
            # can no longer carry one (ADR-157 Phase 0.2), so the source is
            # always the deployment's: an env var belongs to whoever set it,
            # the platform default means nothing was configured at all.
            return SkillResult(
                ok=False,
                errors=[f"{type(exc).__name__}: {exc}", f"database: {dsn_source()}"],
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        outcome.release()


def tables(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """List the gold tier's tables/views."""
    return _run(
        params, ctx, lambda cur: gq.list_tables(cur),
        verb="data.tables", cost_class="lookup",
    )


def describe(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Columns + types for one gold table."""
    table = params.get("table")
    if not table:
        return SkillResult(ok=False, errors=["`table` is required"])
    return _run(
        params, ctx, lambda cur: gq.describe(cur, str(table)),
        verb="data.describe", cost_class="lookup",
    )


def aggregate(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """One deterministic aggregate (sum/mean/min/max/std/count) of a column."""
    table, column, fn = params.get("table"), params.get("column"), params.get("fn")
    missing = [k for k, v in (("table", table), ("column", column), ("fn", fn)) if not v]
    if missing:
        return SkillResult(ok=False, errors=[f"required: {', '.join(missing)}"])
    from axiom.infra import serving_guard as sgm

    def _agg_pre(budget) -> SkillResult | None:
        required = sgm.missing_required_filters(
            required=_guard().policy.table_policy(str(table)).required_filters,
            filter_text=params.get("filter"),
            group_by=_group_by(params),
        )
        if required:
            col = required[0]
            return SkillResult(
                ok=False,
                errors=[
                    f"refused: {table} requires a filter on "
                    f"{', '.join(required)} (the index prefix; measured about "
                    f"3,000x cheaper); add filter=\"{col} = '...'\" or "
                    f"group_by={col}"
                ],
            )
        return None

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
        verb="data.aggregate",
        cost_class="aggregate",
        pre=_agg_pre,
    )


def _bound_series(
    value: dict[str, Any],
    *,
    req_window: float | None,
    req_bucket: float | None,
    eff_bucket: str,
    reshaped: bool,
    max_points: int,
) -> dict[str, Any]:
    """The served block leads, and the points fit the budget (ADR-157).

    The preflight widened the bucket from the window estimate; this is the
    backstop for what an estimate cannot see (grouped series multiply the
    points by the group count). Sampling is even across the whole result —
    never its oldest slice — and the block says what was done, first.
    """
    from axiom.infra import serving_guard as sgm

    data = value.get("data") or {}
    series_val = data.get("series") or []
    grouped = bool(series_val) and isinstance(series_val[0], dict) and "points" in series_val[0]
    lists = [g["points"] for g in series_val] if grouped else [series_val]
    total = sum(len(pts) for pts in lists)
    sampled = False
    if total > max_points and lists:
        share = max(1, max_points // len(lists))
        if grouped:
            for g in series_val:
                g["points"] = sgm.sample_evenly(g["points"], share)
        else:
            data["series"] = sgm.sample_evenly(series_val, max_points)
            lists = [data["series"]]
        sampled = True
        total = sum(len(g["points"]) for g in series_val) if grouped else len(data["series"])

    stamps = [p["t"] for pts in lists for p in pts if p.get("t") is not None]
    covered = (str(min(stamps)), str(max(stamps))) if stamps else None
    served = sgm.served_block(
        requested_window_seconds=req_window,
        requested_bucket_seconds=req_bucket,
        effective_bucket_seconds=sgm.interval_seconds(eff_bucket),
        returned_points=total,
        covered=covered,
        reduced=reshaped or sampled,
    )
    return {"served": served, **value}


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

    from axiom.infra import serving_guard as sgm

    req_bucket = sgm.interval_seconds(str(bucket))
    req_window = sgm.window_seconds(params.get("window"))
    cell: dict[str, Any] = {}

    def _pre(budget) -> SkillResult | None:
        cell["budget"] = budget
        required = sgm.missing_required_filters(
            required=_guard().policy.table_policy(str(table)).required_filters,
            filter_text=params.get("filter"),
            group_by=_group_by(params),
        )
        if required:
            col = required[0]
            return SkillResult(
                ok=False,
                errors=[
                    f"refused: {table} requires a filter on "
                    f"{', '.join(required)} (the index prefix; measured about "
                    f"3,000x cheaper); add filter=\"{col} = '...'\" or "
                    f"group_by={col}"
                ],
            )
        if req_bucket is None:
            return None  # an invalid bucket gets gq.series' grammar error
        plan = sgm.preflight_series(
            window_seconds=req_window, bucket_seconds=req_bucket, budget=budget
        )
        if plan.refused:
            return SkillResult(
                ok=False, errors=[f"refused: {plan.reason}; {plan.cheaper_call}"]
            )
        cell["plan"] = plan
        return None

    def _fn(cur):
        plan = cell.get("plan")
        eff_bucket = str(bucket)
        if plan is not None and plan.reshaped:
            eff_bucket = sgm.format_interval(plan.bucket_seconds)
        # A coarse ask reads the declared rollup, so wide windows stop
        # re-reading raw rows the deployment already summarised (Phase 3.1).
        # Reshaping first means a widened bucket can newly qualify.
        eff_table = sgm.choose_rollup(
            policy=_guard().policy,
            table=str(table),
            bucket_seconds=sgm.interval_seconds(eff_bucket),
        ) or str(table)
        value = gq.series(
            cur,
            table=eff_table,
            column=str(column),
            bucket=eff_bucket,
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
        )
        budget = cell.get("budget")
        bounded = _bound_series(
            value,
            req_window=req_window,
            req_bucket=req_bucket,
            eff_bucket=eff_bucket,
            reshaped=bool(plan is not None and plan.reshaped),
            max_points=budget.max_points if budget is not None else 10_000,
        )
        if eff_table != str(table):
            bounded["served"]["source_table"] = eff_table
        return bounded

    return _run(
        params, ctx, _fn, verb="data.series", cost_class="series", pre=_pre
    )


def roles(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Which quantities the fleet can answer, and which sites answer each."""
    return _run(
        params, ctx,
        lambda cur: gq.roles(
            cur, include_synthetic=bool(params.get("include_synthetic"))
        ),
        verb="data.roles", cost_class="lookup",
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
        verb="data.compare", cost_class="aggregate",
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
        verb="data.uncertainty_coverage", cost_class="lookup",
    )

__all__ = ['aggregate', 'compare', 'describe', 'roles', 'series', 'serving_report', 'tables', 'uncertainty_coverage']


def serving_report(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Who is using the serving tier, and who met a limit (ADR-157 Phase 4).

    Counts are per serving process since its start — the same lifetime as the
    connections the guard protects. No conversation content and no query text:
    principals, counts and the policy digest, which is what a weekly report
    and a suspension decision need.
    """
    guard = _guard()
    policy = guard.policy
    meter = guard.metering()
    heaviest = sorted(
        meter.items(), key=lambda kv: kv[1].get("admitted", 0), reverse=True
    )
    return SkillResult(
        ok=True,
        value={
            "data": {
                "principals": [
                    {"principal": p, **counts} for p, counts in heaviest
                ],
                "suspended": sorted(policy.suspended_principals),
                "limits": {
                    caller: {
                        "rate_per_second": policy.limits(caller).rate_per_second,
                        "burst": policy.limits(caller).burst,
                        "max_concurrent": policy.limits(caller).max_concurrent,
                    }
                    for caller in ("agent", "interactive", "service", "anonymous")
                },
            },
            "provenance": {
                "source": "serving guard",
                "method": "in-process metering since service start",
            },
        },
    )
