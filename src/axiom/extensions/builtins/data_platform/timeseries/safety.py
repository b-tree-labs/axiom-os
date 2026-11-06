# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Safety checks for partitioned time-series tables, registered with TRIAGE.

These are the checks that, had they existed, would have said something on the
first heartbeat instead of after 337 million rows: the next partition is
missing, rows are landing in DEFAULT, or a table has outgrown its budget.

Each check reads configuration from the node's data-platform config, so a node
that declares no time-series tables reports nothing rather than guessing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from axiom.extensions.builtins.diagnostics.safety import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    Finding,
)

from .config import configured_specs
from .partitions import report

log = logging.getLogger(__name__)

#: A table past this many bytes with no retention configured is reported. Not a
#: failure in itself — a large table can be correct — but an unbounded one that
#: nobody has decided a lifetime for is how a volume fills.
DEFAULT_SIZE_BUDGET_BYTES = 100 * 1024**3


def _connect():
    """Resolve the platform connection lazily, so importing this module costs
    nothing and a node without a database reports no findings rather than
    raising inside a heartbeat."""
    from axiom.infra.db import get_engine

    return get_engine().raw_connection()


def check_partition_coverage() -> list[Finding]:
    """The next partition exists, and rows are not falling into DEFAULT."""
    specs = configured_specs()
    if not specs:
        return []
    findings: list[Finding] = []
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001 - a heartbeat must not raise
        log.debug("partition coverage check skipped: %s", exc)
        return []
    try:
        with conn.cursor() as cur:
            for spec in specs:
                try:
                    rep = report(cur, spec, now=datetime.now(UTC))
                except Exception as exc:  # noqa: BLE001
                    log.debug("partition report failed for %s: %s", spec.qualified, exc)
                    continue
                if rep.missing:
                    findings.append(
                        Finding(
                            check_name="data_platform.partition_missing",
                            severity=SEVERITY_CRITICAL
                            if len(rep.missing) > 1
                            else SEVERITY_WARNING,
                            title=f"{rep.parent}: {len(rep.missing)} time partition(s) missing",
                            detail=(
                                "Rows whose timestamps fall in these periods route to the DEFAULT "
                                f"partition instead: {', '.join(rep.missing[:5])}"
                            ),
                            remediation=f"axi data timeseries ensure --table {spec.schema}.{spec.table} --apply",
                        )
                    )
                if rep.default_occupied:
                    findings.append(
                        Finding(
                            check_name="data_platform.default_partition_occupied",
                            severity=SEVERITY_CRITICAL,
                            title=f"{rep.parent}: ~{rep.default_rows:,} rows in the DEFAULT partition",
                            detail=(
                                f"{rep.default_bytes / 1024**3:.1f} GB sits in {rep.default_partition}. "
                                "Retention cannot drop these rows — a DEFAULT partition can only be "
                                "emptied by DELETE, which bloats instead of returning space."
                            ),
                            remediation=(
                                f"axi data timeseries drain --table {spec.schema}.{spec.table} "
                                "(decide the retention window first: draining only the retained range is far cheaper)"
                            ),
                        )
                    )
    finally:
        with_close = getattr(conn, "close", None)
        if with_close:
            with_close()
    return findings


def check_table_size_budget() -> list[Finding]:
    """A configured table past its size budget, or unbounded with no retention."""
    specs = configured_specs()
    if not specs:
        return []
    findings: list[Finding] = []
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        log.debug("size budget check skipped: %s", exc)
        return []
    try:
        with conn.cursor() as cur:
            for spec in specs:
                try:
                    cur.execute(
                        "SELECT pg_total_relation_size(c.oid) FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = %s AND c.relname = %s",
                        (spec.schema, spec.table),
                    )
                    row = cur.fetchone()
                except Exception as exc:  # noqa: BLE001
                    log.debug("size probe failed for %s: %s", spec.qualified, exc)
                    continue
                if not row or not row[0]:
                    continue
                size = int(row[0])
                if size < DEFAULT_SIZE_BUDGET_BYTES:
                    continue
                unbounded = spec.retain_periods is None
                findings.append(
                    Finding(
                        check_name="data_platform.timeseries_size_budget",
                        severity=SEVERITY_CRITICAL if unbounded else SEVERITY_WARNING,
                        title=f"{spec.schema}.{spec.table} is {size / 1024**3:.0f} GB",
                        detail=(
                            "No retention is configured, so it grows without bound."
                            if unbounded
                            else f"Retention keeps {spec.retain_periods} {spec.granularity}(s)."
                        ),
                        remediation=(
                            "Set retain_periods for this table in the data-platform config, then "
                            f"axi data timeseries retire --table {spec.schema}.{spec.table} --apply"
                        ),
                    )
                )
    finally:
        close = getattr(conn, "close", None)
        if close:
            close()
    return findings


__all__ = ["DEFAULT_SIZE_BUDGET_BYTES", "check_partition_coverage", "check_table_size_budget"]
