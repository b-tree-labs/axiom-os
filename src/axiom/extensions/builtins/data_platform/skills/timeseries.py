# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""``data.timeseries_*`` — PLINTH's partition lifecycle for time-series tables.

Three verbs over the module in ``..timeseries``: say where rows actually are,
create the partitions that should exist, and drop the ones past the window.

**They report the difference, not the outcome.** "Partitions ensured" is the
kind of statement that was true every day a 337-million-row DEFAULT partition
was growing. These return the partitions they actually created or dropped, read
back from the catalog, so a log line is checkable.

Both mutating verbs are dry-run unless ``apply`` is passed.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..timeseries.config import configured_specs
from ..timeseries.lifecycle import ensure, retire
from ..timeseries.partitions import PartitionSpec, report


def _specs(params: dict[str, Any]) -> tuple[list[PartitionSpec], list[str]]:
    """The tables to act on: a single ``--table schema.table`` if given,
    otherwise every table this node declares."""
    declared = configured_specs()
    wanted = params.get("table")
    if not wanted:
        return declared, (
            []
            if declared
            else [
                "no time-series tables declared; add a [[data_platform.timeseries]] "
                "entry to data_platform.toml (nothing is managed by default, on purpose)"
            ]
        )
    match = [s for s in declared if f"{s.schema}.{s.table}" == wanted or s.table == wanted]
    if match:
        return match, []
    return [], [
        f"{wanted} is not declared in data_platform.timeseries; declare it before managing it"
    ]


def _connect(params: dict[str, Any]):
    from .._dsn import resolve_dsn

    dsn = resolve_dsn(params)
    if not dsn:
        return None, "DP1_RAG_DSN / DATABASE_URL unset"
    try:
        import psycopg2
    except ImportError:
        return None, "psycopg2 is not installed"
    try:
        return psycopg2.connect(dsn), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not connect: {exc}"


def _run(params: dict[str, Any], op) -> SkillResult:
    specs, errors = _specs(params)
    if errors and not specs:
        return SkillResult(ok=False, errors=errors)
    conn, err = _connect(params)
    if err:
        return SkillResult(ok=False, errors=[err])
    values, actions, problems = [], [], []
    try:
        with conn:
            with conn.cursor() as cur:
                for spec in specs:
                    value, acts, errs = op(cur, spec, params)
                    values.append(value)
                    actions.extend(acts)
                    problems.extend(errs)
    finally:
        conn.close()
    return SkillResult(ok=not problems, value=values, actions_taken=actions, errors=problems)


def run_report(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Where the rows are, which partitions are missing, and what is retirable."""

    def op(cur, spec, _params):
        rep = report(cur, spec)
        d = rep.as_dict()
        acts = [
            f"{rep.parent}: {len(rep.partitions)} partitions, "
            f"{rep.default_rows:,} rows in DEFAULT, {len(rep.missing)} missing"
        ]
        return d, acts + [f"  note: {n}" for n in rep.notes], []

    return _run(params, op)


def run_ensure(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Create the partitions that should exist. Dry-run unless ``apply``."""
    do_apply = bool(params.get("apply"))

    def op(cur, spec, _params):
        out = ensure(cur, spec, apply=do_apply)
        verb = "created" if do_apply else "would create"
        acts = [
            f"{spec.schema}.{spec.table}: {verb} {len(out.applied or out.planned)} partition(s)"
        ]
        acts += [f"  {verb} {n}" for n in (out.applied or out.planned)]
        return out.as_dict(), acts, out.errors

    return _run(params, op)


def run_retire(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Drop partitions entirely older than the retention window. Dry-run unless
    ``apply``. A DROP returns the space at once, indexes included, with no
    VACUUM and no bloat — the reason to partition a time-series table."""
    do_apply = bool(params.get("apply"))

    def op(cur, spec, _params):
        out = retire(cur, spec, apply=do_apply)
        verb = "dropped" if do_apply else "would drop"
        acts = [
            f"{spec.schema}.{spec.table}: {verb} {len(out.applied or out.planned)} partition(s)"
        ]
        acts += [f"  {verb} {n}" for n in (out.applied or out.planned)]
        return out.as_dict(), acts, out.errors

    return _run(params, op)


__all__ = ["run_ensure", "run_report", "run_retire"]
