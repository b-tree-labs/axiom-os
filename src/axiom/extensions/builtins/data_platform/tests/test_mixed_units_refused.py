# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An answer that spans two units was never a quantity.

Measured on a live install: a single-site aggregate over the signals table
spans EIGHT units at once — percent, degC, L/min, psi, %RH, seconds and sccm,
plus rows declaring none — and it returned a number. Averaging seconds with
degrees Celsius is not a wrong number; it is a number that was never a
quantity, and it is the single most likely first query anyone runs.
"""

from __future__ import annotations

import pytest

from ..gold_query import MixedPopulation, _unit_verdict, aggregate, series

COLUMNS = [
    (n, "text") for n in ("site", "ts", "value", "unit", "source_class", "role", "basis")
]


class FakeCursor:
    """Answers by what is asked, not by the order it is asked in.

    ``units`` is the population: ``{unit: row count}``, with ``""`` for rows
    declaring none. The answer itself is ``result`` (one aggregate row) or
    ``rows`` (a series).
    """

    def __init__(self, units, *, result=None, rows=None, source_classes=None):
        self.comp = {"unit": units, "source_class": source_classes or {}}
        self.result = result
        self.rows = rows
        self.sql: list[str] = []
        self._pending = None

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.sql.append(flat)
        if "information_schema.columns" in flat:
            self._pending = list(COLUMNS)
        elif "GROUP BY 1 ORDER BY 2 DESC" in flat:
            col = flat.split('"')[1]
            self._pending = [(None if v == "" else v, n) for v, n in self.comp.get(col, {}).items()]
        else:
            self._pending = list(self.rows) if self.rows is not None else None

    def fetchall(self):
        return self._pending or []

    def fetchone(self):
        return self._pending[0] if self._pending else self.result


class TestTheVerdict:
    def test_one_stated_unit_is_reported(self):
        assert _unit_verdict(["degC"], allow_mixed=False) == ("degC", None)

    def test_two_stated_units_refuse(self):
        unit, refusal = _unit_verdict(["degC", "psi"], allow_mixed=False)
        assert unit is None
        assert "spans 2 units" in refusal
        assert "never a quantity" in refusal

    def test_the_real_eight(self):
        units = ["%", "degC", "L/min", "", "psi", "%RH", "s", "sccm"]
        unit, refusal = _unit_verdict(units, allow_mixed=False)
        assert unit is None
        assert "spans 7 units" in refusal
        assert "rows declaring none" in refusal

    def test_a_stated_unit_mixed_with_an_unstated_one_refuses(self):
        """An undeclared unit is not a matching one. Treating absence as
        agreement is how a third of served rows would join any answer."""
        unit, refusal = _unit_verdict(["degC", ""], allow_mixed=False)
        assert unit is None
        assert "declaring no unit" in refusal

    def test_all_undeclared_is_answerable_and_labelled(self):
        """Every row agreeing that nobody said is consistent, and the answer
        must not look dimensionless."""
        assert _unit_verdict(["", ""], allow_mixed=False) == ("unit not declared", None)

    def test_the_flag_stops_the_refusal_without_changing_arithmetic(self):
        unit, refusal = _unit_verdict(["degC", "psi"], allow_mixed=True)
        assert refusal is None

    def test_a_table_with_no_unit_column_is_unaffected(self):
        assert _unit_verdict([], allow_mixed=False) == (None, None)


class TestAggregate:
    def test_a_mixed_answer_is_refused(self):
        cur = FakeCursor({"degC": 5, "psi": 5}, result=(42.0, 10, 10))
        with pytest.raises(MixedPopulation) as exc:
            aggregate(cur, table="signals", column="value", fn="mean")
        assert "never a quantity" in str(exc.value)

    def test_and_says_how_many_rows_it_refused(self):
        """A refusal that hides the scale of what it refused is less useful
        than one that names it."""
        cur = FakeCursor({"degC": 90, "psi": 9}, result=(42.0, 99, 99))
        with pytest.raises(MixedPopulation) as exc:
            aggregate(cur, table="signals", column="value", fn="mean")
        assert "degC (90)" in str(exc.value) and "psi (9)" in str(exc.value)

    def test_a_single_unit_answer_carries_it(self):
        cur = FakeCursor({"degC": 10}, result=(42.0, 10, 10))
        out = aggregate(cur, table="signals", column="value", fn="mean")
        assert out["data"]["value"] == 42.0
        assert out["unit"] == {"value": "degC"}

    def test_the_flag_lets_it_through(self):
        cur = FakeCursor({"degC": 5, "psi": 5}, result=(42.0, 10, 10))
        out = aggregate(cur, table="signals", column="value", fn="mean",
                        allow_mixed_units=True)
        assert out["data"]["value"] == 42.0
        assert "unit" not in out

    def test_the_flag_does_not_excuse_a_blend_of_source_classes(self):
        """A shared scale says nothing about whether a number was measured."""
        cur = FakeCursor({"degC": 5, "psi": 5}, result=(42.0, 10, 10),
                         source_classes={"measured": 5, "predicted": 5})
        with pytest.raises(MixedPopulation) as exc:
            aggregate(cur, table="signals", column="value", fn="mean",
                      allow_mixed_units=True)
        assert "source_class" in str(exc.value)

    def test_a_count_across_units_is_still_a_count(self):
        cur = FakeCursor({"degC": 5, "psi": 5}, result=(10, 10, 10))
        out = aggregate(cur, table="signals", column="value", fn="count")
        assert out["data"]["value"] == 10

    def test_the_unit_reported_is_the_population_the_guard_judged(self):
        """One reading of the units, not two: a second would be a second
        snapshot of a moving table, and the number and its unit could then
        describe different rows."""
        cur = FakeCursor({"degC": 1}, result=(1.0, 1, 1))
        aggregate(cur, table="signals", column="value", fn="mean")
        assert sum('"unit"' in q and "GROUP BY 1" in q for q in cur.sql) == 1
        assert not any("array_agg" in q for q in cur.sql)


class TestSeries:
    def test_a_line_whose_units_change_halfway_is_refused(self):
        """The worst version of this: each bucket is internally consistent
        and the line is a lie. The population is judged over the whole window,
        so a mix across buckets is a mix."""
        cur = FakeCursor({"degC": 1, "psi": 1},
                         rows=[("t1", 20.0, 1), ("t2", 30.0, 1)])
        with pytest.raises(MixedPopulation) as exc:
            series(cur, table="signals", column="value", bucket="1 hour",
                   time_column="ts")
        assert "spans 2 units" in str(exc.value)

    def test_a_consistent_line_carries_its_unit(self):
        cur = FakeCursor({"degC": 2}, rows=[("t1", 20.0, 1), ("t2", 21.0, 1)])
        out = series(cur, table="signals", column="value", bucket="1 hour",
                     time_column="ts")
        assert len(out["data"]["series"]) == 2
        assert out["unit"] == {"value": "degC"}

    def test_grouping_by_unit_draws_one_line_per_unit_and_claims_none(self):
        cur = FakeCursor({"degC": 1, "psi": 1},
                         rows=[("degC", "t1", 20.0, 1), ("psi", "t1", 30.0, 1)])
        out = series(cur, table="signals", column="value", bucket="1 hour",
                     time_column="ts", group_by=["unit"])
        assert [s["key"] for s in out["data"]["series"]] == [{"unit": "degC"}, {"unit": "psi"}]
        assert "unit" not in out
