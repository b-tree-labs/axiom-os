# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""The partition lifecycle, tested where the risk actually is.

Two failure modes matter more than the happy path. Creating a partition that
overlaps rows already in DEFAULT triggers an ACCESS EXCLUSIVE scan of the whole
default partition, which is an outage on a 174 GB table. Dropping a partition
that overlaps the retention window silently discards data the policy said to
keep. Both have their own tests below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.data_platform.timeseries.partitions import (
    PartitionSpec,
    default_guard_sql,
    next_period,
    partition_name,
    period_start,
    plan_partitions,
    plan_retirements,
)

NOW = datetime(2026, 9, 28, 17, 42, tzinfo=UTC)


def _spec(**kw):
    base = {"schema": "public", "table": "measurements", "granularity": "day", "ahead": 3}
    base.update(kw)
    return PartitionSpec(**base)


# -- pure period arithmetic ---------------------------------------------------


@pytest.mark.parametrize(
    "gran,expected",
    [
        ("day", datetime(2026, 9, 28, tzinfo=UTC)),
        ("week", datetime(2026, 9, 28, tzinfo=UTC)),  # 2026-09-28 is a Monday
        ("month", datetime(2026, 9, 1, tzinfo=UTC)),
    ],
)
def test_period_start(gran, expected):
    assert period_start(NOW, gran) == expected


def test_month_rollover_is_not_thirty_days():
    assert next_period(datetime(2026, 1, 31, tzinfo=UTC).replace(day=1), "month") == datetime(
        2026, 2, 1, tzinfo=UTC
    )
    assert next_period(datetime(2026, 2, 1, tzinfo=UTC), "month") == datetime(
        2026, 3, 1, tzinfo=UTC
    )
    assert next_period(datetime(2026, 12, 1, tzinfo=UTC), "month") == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_partition_names_are_deterministic_and_sort_chronologically():
    days = [partition_name("t", datetime(2026, 9, d, tzinfo=UTC), "day") for d in (8, 9, 28)]
    assert days == ["t_p20260908", "t_p20260909", "t_p20260928"]
    assert days == sorted(days)
    assert partition_name("t", NOW, "month") == "t_p202609"


# -- planning -----------------------------------------------------------------


def test_plan_covers_today_through_ahead_and_skips_existing():
    planned = plan_partitions(NOW, _spec(ahead=3), existing={"measurements_p20260929"})
    assert (
        [p.name for p in planned]
        == [
            "measurements_p20260928",  # today: a table partitioned now needs somewhere for now's rows
            "measurements_p20260930",
            "measurements_p20261001",
        ]
    )
    first = planned[0]
    assert first.start == datetime(2026, 9, 28, tzinfo=UTC)
    assert first.end == datetime(2026, 9, 29, tzinfo=UTC)


def test_create_sql_is_bounded_and_idempotent():
    sql = plan_partitions(NOW, _spec(ahead=1), existing=set())[0].create_sql(_spec())
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert '"public"."measurements_p20260928"' in sql
    assert "FOR VALUES FROM ('2026-09-28T00:00:00+00:00') TO ('2026-09-29T00:00:00+00:00')" in sql


def test_ahead_of_zero_is_refused():
    with pytest.raises(ValueError, match="too late"):
        _spec(ahead=0)


@pytest.mark.parametrize("bad", ["public; drop table x", "pg catalog", "", "1abc"])
def test_identifiers_are_validated_not_interpolated(bad):
    with pytest.raises(ValueError):
        PartitionSpec(schema=bad, table="t")
    with pytest.raises(ValueError):
        PartitionSpec(schema="public", table=bad)


# -- retention: the dangerous direction ---------------------------------------


def _parts():
    out = []
    for d in range(20, 29):
        lo = datetime(2026, 9, d, tzinfo=UTC)
        out.append((f"t_p202609{d}", lo, lo + timedelta(days=1)))
    return out


def test_retirement_drops_only_partitions_entirely_before_the_window():
    names = plan_retirements(NOW, _spec(retain_periods=3), _parts())
    # window starts 2026-09-25; 09-25 itself is retained, everything before goes
    assert names == ["t_p20260920", "t_p20260921", "t_p20260922", "t_p20260923", "t_p20260924"]


def test_a_partition_overlapping_the_cutoff_is_never_dropped():
    lo = datetime(2026, 9, 24, tzinfo=UTC)
    straddling = [("t_straddle", lo, lo + timedelta(days=7))]  # ends inside the window
    assert plan_retirements(NOW, _spec(retain_periods=3), straddling) == []


def test_no_retention_configured_drops_nothing():
    assert plan_retirements(NOW, _spec(retain_periods=None), _parts()) == []


def test_retain_periods_must_be_positive():
    with pytest.raises(ValueError):
        _spec(retain_periods=0)


# -- the online-safety guard --------------------------------------------------


def test_guard_emits_not_valid_then_validate_so_writers_keep_running():
    add, validate, name = default_guard_sql(
        _spec(), "measurements_default", NOW + timedelta(days=1), "ts", now=NOW
    )
    assert "NOT VALID" in add, "the ADD must not scan; that is the whole point"
    assert "CHECK" in add and "2026-09-29" in add
    assert validate.endswith(f'VALIDATE CONSTRAINT "{name}"')
    assert "NOT VALID" not in validate
    # Separate statements: VALIDATE holds SHARE UPDATE EXCLUSIVE for a long time
    # and must not share a transaction with the partition DDL.
    assert add != validate


def test_a_boundary_in_the_past_is_refused():
    """Rows are still arriving into DEFAULT. A constraint with a past boundary
    would be violated by the next insert and validation would fail."""
    with pytest.raises(ValueError, match="future"):
        default_guard_sql(_spec(), "measurements_default", NOW - timedelta(days=1), "ts", now=NOW)


def test_guard_refuses_a_missing_default_partition():
    with pytest.raises(ValueError, match="no default partition"):
        default_guard_sql(_spec(), "", NOW + timedelta(days=1), "ts", now=NOW)


# -- lifecycle: refusing the dangerous thing ----------------------------------


class _RecordingCur:
    def __init__(self, partkey="RANGE (ts)", rows=(), guarded=0):
        self.partkey, self.rows, self.guarded = partkey, list(rows), guarded
        self.executed: list[str] = []
        self._result: list = []

    def execute(self, sql, args=None):
        self.executed.append(sql)
        if "pg_get_partkeydef" in sql:
            self._result = [(self.partkey,)] if self.partkey else []
        elif "pg_constraint" in sql:
            self._result = [(self.guarded,)]
        elif "pg_inherits" in sql:
            self._result = list(self.rows)
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


DEFAULT_ROW = ("measurements_default", "DEFAULT", 337_281_888, 355 * 1024**3)


def test_ensure_refuses_to_create_against_a_populated_unguarded_default():
    """The outage-shaped mistake: each CREATE would scan 174 GB under
    ACCESS EXCLUSIVE and stop every writer."""
    from axiom.extensions.builtins.data_platform.timeseries.lifecycle import ensure

    cur = _RecordingCur(rows=[DEFAULT_ROW], guarded=0)

    out = ensure(cur, _spec(), apply=True, now=NOW)

    assert out.applied == []
    assert out.planned == []
    assert out.skipped, "the partitions it would have made are reported as skipped"
    assert "ACCESS EXCLUSIVE" in out.errors[0]
    assert not any(s.startswith("CREATE TABLE") for s in cur.executed)


def test_ensure_creates_once_the_default_is_guarded():
    from axiom.extensions.builtins.data_platform.timeseries.lifecycle import ensure

    cur = _RecordingCur(rows=[DEFAULT_ROW], guarded=1)

    out = ensure(cur, _spec(ahead=1), apply=True, now=NOW)

    assert out.applied == ["measurements_p20260928", "measurements_p20260929"]
    assert out.errors == []
    assert all("SET LOCAL lock_timeout" in s for s in cur.executed if s.startswith("SET LOCAL"))


def test_ensure_is_dry_run_by_default():
    from axiom.extensions.builtins.data_platform.timeseries.lifecycle import ensure

    cur = _RecordingCur(rows=[DEFAULT_ROW], guarded=1)
    out = ensure(cur, _spec(ahead=1), now=NOW)
    assert out.dry_run and out.planned and out.applied == []
    assert not any(s.startswith("CREATE TABLE") for s in cur.executed)


def test_retire_without_configured_retention_drops_nothing():
    from axiom.extensions.builtins.data_platform.timeseries.lifecycle import retire

    cur = _RecordingCur(rows=[DEFAULT_ROW], guarded=1)
    out = retire(cur, _spec(), apply=True, now=NOW)
    assert out.applied == [] and out.planned == []
    assert "no retention configured" in out.errors[0]
    assert not any(s.startswith("DROP TABLE") for s in cur.executed)


def test_retire_never_drops_the_default_partition():
    """DEFAULT has no upper bound, so it can always hold rows inside the window."""
    from axiom.extensions.builtins.data_platform.timeseries.lifecycle import retire

    cur = _RecordingCur(rows=[DEFAULT_ROW], guarded=1)
    out = retire(cur, _spec(retain_periods=1), apply=True, now=NOW)
    assert out.applied == []
    assert not any("DROP TABLE" in s for s in cur.executed)


def test_the_guard_does_not_depend_on_the_day_the_suite_runs():
    """The regression this file earned on 2026-09-30.

    `NOW` is frozen at 2026-09-28, so `NOW + 1 day` is 2026-09-29. The guard
    compared it against the WALL clock, so the assertion held on the 28th and
    the 29th and became impossible on the 30th — a green suite turning red
    with no commit in between. Every clock in this module is injectable now,
    and this asserts the property rather than the date: the same arguments
    give the same answer whenever the suite runs.
    """
    far_past = datetime(2020, 1, 1, tzinfo=UTC)
    far_future = datetime(2099, 1, 1, tzinfo=UTC)
    # Relative to a frozen `now`, a boundary one day later is ALWAYS future,
    # whatever today happens to be.
    for frozen in (far_past, NOW, far_future):
        add, _, _ = default_guard_sql(
            _spec(), "measurements_default", frozen + timedelta(days=1), "ts", now=frozen
        )
        assert "NOT VALID" in add
        with pytest.raises(ValueError, match="future"):
            default_guard_sql(
                _spec(), "measurements_default", frozen - timedelta(days=1), "ts", now=frozen
            )
