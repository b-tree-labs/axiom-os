# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.readings_hypertable`` — convert an existing plain ``silver.signals``
into a compressed TimescaleDB hypertable, online, one phase at a time.

The logic is :mod:`..conformance.readings_migration`; this is its door. Never
run on startup: a runbook calls it. Phases (``phase=``): ``estimate``
(default, writes nothing), ``prepare``, ``copy`` (resumable; ``max_windows``
bounds one call), ``swap`` (needs ``conform_paused``), ``finish``. Every
changing phase needs ``confirm`` and either ``backup_file`` (a fresh
custom-format dump of silver.signals) or ``accept_rederive``.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .._dsn import resolve_dsn
    from ..conformance.readings_migration import MigrationRefused, migrate
    from ..conformance.timeseries import TimeseriesPolicy

    try:
        import psycopg
    except ImportError:  # pragma: no cover - deployment always has it
        return SkillResult(ok=False, errors=["psycopg is not installed"])
    dsn = resolve_dsn(params)
    if not dsn:
        return SkillResult(ok=False, errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL / AXIOM_DB_URL"])
    quiet = bool(params.get("quiet"))

    def say(line: str) -> None:
        if not quiet:
            print(line, flush=True)

    def on_progress(p) -> None:
        say(f"  {p.percent:5.1f}%  window {p.windows_done}/{p.windows_total}  {p.rows_copied:,} rows  "
            f"{p.rows_per_second or 0:,.0f} rows/s")

    policy = TimeseriesPolicy(compress_after_days=int(params.get("compress_after_days") or 7))
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
            out = migrate(
                conn, phase=str(params.get("phase") or "estimate"), confirm=bool(params.get("confirm")),
                backup_file=params.get("backup_file"), accept_rederive=bool(params.get("accept_rederive")),
                conform_paused=bool(params.get("conform_paused")), policy=policy,
                max_windows=int(params["max_windows"]) if params.get("max_windows") else None,
                say=say, on_progress=on_progress,
            )
    except MigrationRefused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
    actions = list(out.get("steps") or [])
    if out.get("dump"):
        actions.append("backup: " + out["dump"])
    actions += [f"blocker: {b}" for b in out.get("blockers") or []]
    return SkillResult(ok=not out.get("blockers"), value=out, actions_taken=actions)
