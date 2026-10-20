# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Declared-not-discovered config, and the checks that would have caught a
337-million-row DEFAULT partition on the first heartbeat."""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.extensions.builtins.data_platform.timeseries import safety
from axiom.extensions.builtins.data_platform.timeseries.config import specs_from_entries
from axiom.extensions.builtins.data_platform.timeseries.partitions import PartitionSpec, report

NOW = datetime(2026, 9, 28, 17, 42, tzinfo=UTC)


# -- config -------------------------------------------------------------------


def test_declared_tables_are_parsed_with_defaults():
    specs = specs_from_entries([{"schema": "public", "table": "measurements"}])
    assert len(specs) == 1
    s = specs[0]
    assert (s.schema, s.table, s.granularity, s.ahead, s.retain_periods) == (
        "public",
        "measurements",
        "day",
        7,
        None,
    )


def test_nothing_declared_means_nothing_managed():
    """The safe default for a job that can drop data."""
    assert specs_from_entries(None) == []
    assert specs_from_entries([]) == []


def test_one_malformed_entry_does_not_stop_the_others():
    specs = specs_from_entries(
        [
            {"schema": "public"},  # no table
            {"schema": "public", "table": "b", "granularity": "fortnight"},  # bad granularity
            "not-a-table",
            {"schema": "public", "table": "good", "retain_periods": 30},
        ]
    )
    assert [s.table for s in specs] == ["good"]
    assert specs[0].retain_periods == 30


def test_a_single_table_may_be_given_as_one_stanza():
    assert [s.table for s in specs_from_entries({"schema": "public", "table": "t"})] == ["t"]


# -- report -------------------------------------------------------------------


class _Cur:
    """Scripted cursor: partkeydef, then the partition rows."""

    def __init__(self, partkey, rows):
        self._partkey, self._rows, self._result = partkey, rows, []

    def execute(self, sql, args=None):
        if "pg_get_partkeydef" in sql:
            # The real query filters relkind='p', so a non-partitioned table
            # returns NO ROW. A fake that returned (None,) would be more
            # permissive than Postgres and would hide the branch under test.
            self._result = [(self._partkey,)] if self._partkey else []
        else:
            self._result = list(self._rows)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


def _spec(**kw):
    base = {"schema": "public", "table": "measurements", "granularity": "day", "ahead": 2}
    base.update(kw)
    return PartitionSpec(**base)


def test_report_names_the_default_partition_and_its_rows():
    """The shape that motivated this module: one DEFAULT partition holding everything."""
    cur = _Cur(
        "RANGE (ts)", [("measurements_default", "DEFAULT", 337_281_888, 355 * 1024**3)]
    )

    rep = report(cur, _spec(), now=NOW)

    assert rep.partition_key == "RANGE (ts)"
    assert rep.default_partition == "measurements_default"
    assert rep.default_rows == 337_281_888
    assert rep.default_occupied is True
    assert rep.partitions == []
    assert len(rep.missing) == 3  # today + 2 ahead
    assert any("cannot drop" in n for n in rep.notes)


def test_report_on_a_healthy_table_is_quiet():
    rows = [
        (
            f"measurements_p2026{m:02d}{d:02d}",
            f"FOR VALUES FROM ('2026-{m:02d}-{d:02d} 00:00:00+00') TO ('2026-{m:02d}-{d + 1:02d} 00:00:00+00')",
            1000,
            1024,
        )
        for m, d in ((9, 28), (9, 29), (9, 30))
    ]
    rep = report(_Cur("RANGE (ts)", rows), _spec(), now=NOW)
    assert rep.missing == [] and rep.default_occupied is False and rep.notes == []


def test_report_says_so_when_the_table_is_not_partitioned():
    rep = report(_Cur(None, []), _spec(), now=NOW)
    assert "not a partitioned table" in rep.notes[0]


# -- safety checks ------------------------------------------------------------


def test_checks_report_nothing_when_no_table_is_declared(monkeypatch):
    monkeypatch.setattr(safety, "configured_specs", lambda: [])
    assert safety.check_partition_coverage() == []
    assert safety.check_table_size_budget() == []


def test_a_database_that_cannot_be_reached_yields_no_findings_not_an_exception(monkeypatch):
    """A heartbeat check must never raise: one unreachable database would
    otherwise take down every other check in the sweep."""
    monkeypatch.setattr(safety, "configured_specs", lambda: [_spec()])
    monkeypatch.setattr(safety, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    assert safety.check_partition_coverage() == []
    assert safety.check_table_size_budget() == []


def test_occupied_default_and_missing_partitions_are_reported_with_remediation(monkeypatch):
    cur = _Cur(
        "RANGE (ts)", [("measurements_default", "DEFAULT", 337_281_888, 355 * 1024**3)]
    )

    class _Conn:
        def cursor(self):
            class _Ctx:
                def __enter__(self_inner):
                    return cur

                def __exit__(self_inner, *a):
                    return False

            return _Ctx()

        def close(self):
            pass

    monkeypatch.setattr(safety, "configured_specs", lambda: [_spec()])
    monkeypatch.setattr(safety, "_connect", _Conn)

    findings = safety.check_partition_coverage()

    names = {f.check_name for f in findings}
    assert names == {"data_platform.partition_missing", "data_platform.default_partition_occupied"}
    occupied = next(f for f in findings if f.check_name.endswith("default_partition_occupied"))
    assert occupied.severity == safety.SEVERITY_CRITICAL
    assert "337,281,888" in occupied.title
    assert "DELETE" in occupied.detail  # says WHY retention cannot fix it
    assert occupied.remediation
