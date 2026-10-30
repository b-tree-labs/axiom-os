# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.reading_identity`` — move a populated silver onto its reading key.

Three actions, in the order the operator runbook runs them
(``docs/runbooks/silver-reading-identity-migration.md``):

``preflight``
    Read-only. One bounded query per ``--window-days`` window (default 31),
    each under ``--statement-timeout-s`` (default 900), progress on stderr.
    Which readings would violate the identity key, per site and
    feed, re-sends against divergent; the table's layout; how the index can
    be built here and what that locks.
``collapse``
    Keep the newest conform of every duplicated identity and copy every other
    row to ``silver.signals_identity_audit`` before deleting it, one window per
    transaction. A dry run unless ``apply``; divergent identities only with
    ``accept_divergent``.
``build-index``
    Build the unique index by the path preflight named. Refuses while
    duplicates remain or when the TimescaleDB version cannot build it.
"""

from __future__ import annotations

import sys
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..conformance import reading_identity as ri

ACTIONS = ("preflight", "collapse", "build-index")


def _first(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .._dsn import resolve_dsn
    from ..conformance.runner import _aware

    action = str(params.get("action") or "preflight").replace("_", "-")
    if action not in ACTIONS:
        return SkillResult(ok=False, errors=[f"action must be one of {', '.join(ACTIONS)}"])
    dsn = resolve_dsn(params)
    if not dsn:
        return SkillResult(ok=False, errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL"])
    try:
        start = _aware(params.get("start"), "start")
        end = _aware(params.get("end"), "end")
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    site = params.get("site") or None

    import psycopg

    timeout_s = float(params.get("statement_timeout_s") or ri.DEFAULT_STATEMENT_TIMEOUT_S)

    def progress(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    if action == "preflight":
        try:
            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                report = ri.preflight(
                    cur, site=site, start=start, end=end,
                    window_days=int(params.get("window_days") or ri.PREFLIGHT_WINDOW_DAYS),
                    statement_timeout_s=timeout_s, progress=progress,
                )
                conn.rollback()
        except ri.WindowError as exc:
            return SkillResult(ok=False, errors=[str(exc)])
        except psycopg.Error as exc:
            return SkillResult(ok=False, errors=[f"preflight could not reach the database: {_first(exc)}"])
        totals = report["totals"]
        actions = [
            f"identity key {'PRESENT' if report['index_present'] else 'absent'} on silver.signals",
            f"{totals['extra_rows']:,} row(s) would violate it over {totals['identities']:,} "
            f"identities ({totals['resend']:,} re-send, {totals['divergent']:,} divergent)",
            *(f"  {d['site']} / {d['feed']}: {d['extra_rows']:,} extra, {d['divergent']:,} divergent, "
              f"{d['first_ts']} .. {d['last_ts']}" for d in report["duplicates"]),
            *(f"plan: {step}" for step in report["plan"]),
        ]
        return SkillResult(ok=True, value=report, actions_taken=actions)

    if action == "collapse":
        apply = bool(params.get("apply"))
        try:
            with psycopg.connect(dsn) as conn:
                report = ri.collapse(
                    conn, site=site, start=start, end=end, apply=apply,
                    accept_divergent=bool(params.get("accept_divergent")),
                    window_days=int(params.get("window_days") or 1),
                    statement_timeout_s=timeout_s, progress=progress,
                )
        except ri.WindowError as exc:
            note = " Windows before it are committed; re-run to resume." if apply else ""
            return SkillResult(ok=False, errors=[f"{exc}.{note}"])
        except psycopg.Error as exc:
            return SkillResult(ok=False, errors=[f"collapse could not reach the database: {_first(exc)}"])
        verb = "removed" if apply else "would remove (dry run; pass --apply)"
        actions = [
            f"{verb} {report['removed']:,} row(s) over {report['windows']} window(s): "
            f"{report['removed_resend']:,} re-send, {report['removed_divergent']:,} divergent; "
            f"each copied to {report['audit_table']}",
        ]
        if report["divergent_left"]:
            actions.append(
                f"⚠️ {report['divergent_left']:,} divergent identities left in place: the rows "
                "disagree. Pass --accept-divergent to keep the newest conform of each (the rest "
                "stay recoverable in the audit table), or fix the producer's timestamps first."
            )
        return SkillResult(ok=not report["divergent_left"], value=report, actions_taken=actions)

    # No duplicate scan first: it would double the time on a large table, and
    # the build itself refuses on the first duplicate (a CONCURRENTLY build
    # leaves an invalid index, which the next attempt drops).
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            report = ri.build_index(conn, lock_timeout_s=float(params.get("lock_timeout_s") or 10))
    except psycopg.errors.UniqueViolation as exc:
        return SkillResult(
            ok=False,
            errors=[f"duplicates remain, run collapse first: {str(exc).splitlines()[0]}"],
        )
    except psycopg.errors.LockNotAvailable:
        return SkillResult(
            ok=False,
            errors=["could not take the lock within lock_timeout_s; is a conform pass writing? "
                    "pause the conform timer and retry"],
        )
    if not report.get("built"):
        ok = report.get("reason") == "already present and valid"
        return SkillResult(ok=ok, value=report, actions_taken=[f"not built: {report.get('reason')}"])
    return SkillResult(
        ok=True,
        value=report,
        actions_taken=[f"built {report['index']} ({report['method']}); conform now upserts on it"],
    )
