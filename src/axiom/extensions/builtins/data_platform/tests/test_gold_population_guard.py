# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A gold answer must say which population it summarised.

`gold.signals` carries `source_class`, `model_ref`, `unit`, `role`,
`derivation` and `quality`. Two rows there can look identical and mean
different things: one came off an instrument, the other out of a model. A
mean over both is not a measurement of anything, and it is indistinguishable
from a real one once it leaves this module.

These tests drive the executor with a fake cursor, so the refusal is proven
where it is implemented rather than through a database.
"""

from __future__ import annotations

import pytest

from ..gold_query import (
    MIXING_HOSTILE,
    MIXING_NOTABLE,
    MixedPopulation,
    aggregate,
    attribution,
    guard_population,
    provenance_columns,
    series,
)

SIGNAL_COLUMNS = [
    ("site", "text"),
    ("stream", "text"),
    ("channel", "text"),
    ("ts", "timestamp with time zone"),
    ("value", "double precision"),
    ("unit", "text"),
    ("quality", "text"),
    ("source_class", "text"),
    ("model_ref", "text"),
    ("role", "text"),
    ("derivation", "text"),
]


class FakeCursor:
    """Answers introspection, composition and the final aggregate in order.

    ``composition`` is keyed by column name so a test states the population
    it wants without caring which order the executor asks in.
    """

    def __init__(self, *, columns=SIGNAL_COLUMNS, comp=None, result=None, rows=None):
        self.columns = columns
        self.comp = comp or {}
        self.result = result
        self.rows = rows
        self._pending = None
        self.executed: list[tuple[str, list]] = []

    #: The shape :func:`composition` emits, and nothing else emits.
    _COMPOSITION_SIG = "GROUP BY 1 ORDER BY 2 DESC"

    def execute(self, sql, params=None):
        self.executed.append((sql, list(params or [])))
        if "information_schema.columns" in sql:
            self._pending = list(self.columns)
            return
        if self._COMPOSITION_SIG in sql:
            # The executor asks once per provenance column the table has. A
            # column the test did not describe has no rows, which is how a
            # real table with that column always null-free would answer.
            col = sql.split('"')[1]
            values = self.comp.get(col, {})
            self._pending = [
                (None if v == "" else v, n) for v, n in values.items()
            ]
            return
        self._pending = list(self.rows) if self.rows is not None else None

    def fetchall(self):
        return self._pending or []

    def fetchone(self):
        if self._pending:
            return self._pending[0]
        return self.result


def _cur(comp, *, result=None, rows=None, columns=SIGNAL_COLUMNS):
    return FakeCursor(columns=columns, comp=comp, result=result, rows=rows)


# --- the refusal ------------------------------------------------------------


def test_mean_across_two_units_is_refused():
    cur = _cur({"unit": {"degC": 500, "K": 20}}, result=(42.0, 520, 520))

    with pytest.raises(MixedPopulation) as exc:
        aggregate(cur, table="signals", column="value", fn="mean")

    assert "unit" in str(exc.value)
    assert "degC" in str(exc.value) and "K" in str(exc.value)


def test_mean_across_measured_and_predicted_is_refused():
    """The failure the source manifest exists to prevent, at read time."""
    cur = _cur(
        {"source_class": {"measured": 900, "predicted": 100}}, result=(7.0, 1000, 1000)
    )

    with pytest.raises(MixedPopulation) as exc:
        aggregate(cur, table="signals", column="value", fn="mean")

    assert "source_class" in str(exc.value)
    assert "measured" in str(exc.value) and "predicted" in str(exc.value)


def test_the_refusal_names_the_way_through():
    """A caller told only 'no' reaches for raw SQL, which has no guard at all."""
    cur = _cur({"source_class": {"measured": 9, "predicted": 1}}, result=(1.0, 10, 10))

    with pytest.raises(MixedPopulation) as exc:
        aggregate(cur, table="signals", column="value", fn="mean")

    message = str(exc.value)
    assert "group_by" in message
    assert "filter" in message
    assert "allow_mixed" in message


def test_an_unstated_unit_is_its_own_population():
    """39% of a real corpus has no unit. Blending that into degC invents one."""
    cur = _cur({"unit": {"degC": 100, "": 60}}, result=(20.0, 160, 160))

    with pytest.raises(MixedPopulation) as exc:
        aggregate(cur, table="signals", column="value", fn="mean")

    assert "unstated" in str(exc.value)


# --- the ways through -------------------------------------------------------


def test_group_by_splits_the_answer_instead_of_refusing_it():
    """This is the measured-versus-predicted comparison, as one call."""
    cur = _cur(
        {"source_class": {"measured": 900, "predicted": 100}},
        rows=[("measured", 7.1, 900, 900), ("predicted", 7.4, 100, 100)],
    )

    out = aggregate(
        cur, table="signals", column="value", fn="mean", group_by=["source_class"]
    )

    groups = out["data"]["groups"]
    assert [g["source_class"] for g in groups] == ["measured", "predicted"]
    assert [g["value"] for g in groups] == [7.1, 7.4]
    assert out["data"]["group_by"] == ["source_class"]


def test_grouping_on_one_column_does_not_excuse_a_mix_in_another():
    cur = _cur(
        {
            "source_class": {"measured": 900, "predicted": 100},
            "unit": {"degC": 900, "K": 100},
        },
        rows=[("measured", 7.1, 900, 900)],
    )

    with pytest.raises(MixedPopulation) as exc:
        aggregate(
            cur, table="signals", column="value", fn="mean", group_by=["source_class"]
        )

    assert "unit" in str(exc.value)


def test_allow_mixed_answers_and_says_it_blended():
    cur = _cur({"source_class": {"measured": 9, "predicted": 1}}, result=(7.0, 10, 10))

    out = aggregate(
        cur, table="signals", column="value", fn="mean", allow_mixed=True
    )

    assert out["data"]["value"] == 7.0
    assert "on purpose" in out["provenance"]["note"]


def test_a_filter_that_narrows_to_one_population_needs_no_group_by():
    cur = _cur({"source_class": {"predicted": 100}}, result=(7.4, 100, 100))

    out = aggregate(
        cur, table="signals", column="value", fn="mean", filter="source_class = 'predicted'"
    )

    assert out["data"]["value"] == 7.4
    assert out["provenance"]["attribution"]["source_class"] == "predicted"


def test_count_is_not_refused_because_counting_rows_is_not_a_quantity():
    cur = _cur({"unit": {"degC": 100, "K": 20}}, result=(120, 120, 120))

    out = aggregate(cur, table="signals", column="value", fn="count")

    assert out["data"]["value"] == 120
    assert out["provenance"]["composition"]["unit"] == {"degC": 100, "K": 20}


# --- what the answer carries ------------------------------------------------


def test_a_single_valued_population_becomes_attribution():
    cur = _cur(
        {
            "unit": {"degC": 100},
            "source_class": {"predicted": 100},
            "model_ref": {"corral:forecaster-v2": 100},
        },
        result=(7.4, 100, 100),
    )

    out = aggregate(cur, table="signals", column="value", fn="mean")

    assert out["provenance"]["attribution"] == {
        "unit": "degC",
        "source_class": "predicted",
        "model_ref": "corral:forecaster-v2",
    }


def test_a_prediction_always_arrives_with_what_produced_it():
    cur = _cur(
        {"source_class": {"predicted": 100}, "model_ref": {"corral:estimator-v1": 100}},
        result=(1.0, 100, 100),
    )

    out = aggregate(cur, table="signals", column="value", fn="mean")

    assert out["provenance"]["attribution"]["source_class"] == "predicted"
    assert out["provenance"]["attribution"]["model_ref"] == "corral:estimator-v1"


def test_composition_reports_every_provenance_column_it_found():
    cur = _cur(
        {"source_class": {"measured": 5}, "quality": {"good": 4, "suspect": 1}},
        result=(1.0, 5, 5),
    )

    out = aggregate(cur, table="signals", column="value", fn="mean")

    assert out["provenance"]["composition"]["quality"] == {"good": 4, "suspect": 1}


def test_two_models_averaged_together_is_reported_not_refused():
    """Attributable to neither, but not nonsense — the caller is told."""
    cur = _cur(
        {
            "source_class": {"predicted": 200},
            "model_ref": {"corral:forecaster-v2": 100, "corral:estimator-v1": 100},
        },
        result=(7.0, 200, 200),
    )

    out = aggregate(cur, table="signals", column="value", fn="mean")

    assert out["data"]["value"] == 7.0
    assert "model_ref spans 2 values" in out["provenance"]["note"]
    assert "model_ref" not in out["provenance"].get("attribution", {})


def test_mixing_raw_and_derived_is_reported_because_it_double_counts():
    cur = _cur(
        {"derivation": {"raw": 90, "derived": 10}, "source_class": {"measured": 100}},
        result=(3.0, 100, 100),
    )

    out = aggregate(cur, table="signals", column="value", fn="mean")

    assert "derivation spans 2 values" in out["provenance"]["note"]


# --- series -----------------------------------------------------------------


def test_series_grouped_gives_one_line_per_population():
    """The measured-against-predicted overlay: same buckets, two lines."""
    cur = _cur(
        {"source_class": {"measured": 4, "predicted": 4}},
        rows=[
            ("measured", "t1", 1.0),
            ("measured", "t2", 1.1),
            ("predicted", "t1", 1.2),
            ("predicted", "t2", 1.3),
        ],
    )

    out = series(
        cur,
        table="signals",
        column="value",
        bucket="1 hour",
        time_column="ts",
        group_by=["source_class"],
    )

    lines = out["data"]["series"]
    assert [line["key"]["source_class"] for line in lines] == ["measured", "predicted"]
    assert [p["value"] for p in lines[0]["points"]] == [1.0, 1.1]
    assert [p["value"] for p in lines[1]["points"]] == [1.2, 1.3]


def test_an_ungrouped_series_over_a_mixed_population_is_refused():
    cur = _cur(
        {"source_class": {"measured": 4, "predicted": 4}},
        rows=[("t1", 1.0)],
    )

    with pytest.raises(MixedPopulation):
        series(cur, table="signals", column="value", bucket="1 hour", time_column="ts")


def test_series_method_names_the_split():
    cur = _cur(
        {"source_class": {"measured": 2, "predicted": 2}},
        rows=[("measured", "t1", 1.0), ("predicted", "t1", 1.2)],
    )

    out = series(
        cur, table="signals", column="value", bucket="1 hour",
        time_column="ts", group_by=["source_class"],
    )

    assert "split on source_class" in out["provenance"]["method"]


# --- tables without provenance columns are unaffected -----------------------


def test_a_table_with_no_provenance_columns_answers_as_before():
    plain = [("id", "integer"), ("reading", "double precision"), ("t", "timestamp")]
    cur = _cur({}, result=(5.0, 3, 3), columns=plain)

    out = aggregate(cur, table="rod_calibration", column="reading", fn="mean")

    assert out["data"]["value"] == 5.0
    assert "composition" not in out["provenance"]
    assert "attribution" not in out["provenance"]


def test_provenance_columns_are_detected_from_the_table_shape():
    assert provenance_columns({n for n, _ in SIGNAL_COLUMNS}) == (
        "unit", "source_class", "model_ref", "derivation", "role", "quality",
    )
    assert provenance_columns({"id", "reading"}) == ()


def test_group_by_an_unknown_column_is_a_typed_error():
    from ..gold_query import UnknownObject

    cur = _cur({"source_class": {"measured": 1}}, result=(1.0, 1, 1))

    with pytest.raises(UnknownObject, match="group by"):
        aggregate(cur, table="signals", column="value", fn="mean", group_by=["nope"])


# --- the guard in isolation -------------------------------------------------


def test_the_two_mixing_classes_do_not_overlap():
    assert not set(MIXING_HOSTILE) & set(MIXING_NOTABLE)


def test_guard_is_silent_on_a_uniform_population():
    comp = {"unit": {"degC": 10}, "source_class": {"measured": 10}}
    assert guard_population(comp, group_by=(), allow_mixed=False, fn="mean") == []


def test_attribution_omits_a_column_it_cannot_speak_for():
    comp = {"unit": {"degC": 10}, "source_class": {"measured": 5, "predicted": 5}}
    assert attribution(comp) == {"unit": "degC"}


def test_attribution_reports_an_unstated_value_as_none_not_empty_string():
    assert attribution({"unit": {"": 10}}) == {"unit": None}
