# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Generic medallion answering (ADR-115 / spec-medallion-answering).

The security properties are the point, so they are tested as properties, not
anecdotes:

- identifiers are validated against the INTROSPECTED schema; a literal never
  reaches SQL text (it is a bound parameter), so the filter grammar cannot
  splice SQL by construction;
- an unknown table/column is a typed error, not a database error;
- access tiers fail CLOSED — a table declared restricted whose shape carries no
  tier column refuses rather than answering (with a negative control proving
  the check can fail);
- an empty window is ``data: null`` + a provenance note, never an error and
  never a fabricated zero.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform import gold_query as gq

# --------------------------------------------------------------------------
# filter grammar — pure, no database
# --------------------------------------------------------------------------

COLUMNS = frozenset({"site", "channel", "ts", "value", "quality", "access_tier"})


@pytest.mark.parametrize(
    "expr,column,op,values",
    [
        ("quality = 'good'", "quality", "=", ("good",)),
        ("quality != 'corrupt'", "quality", "!=", ("corrupt",)),
        ("value >= 10", "value", ">=", (10,)),
        ("value > 1.5", "value", ">", (1.5,)),
        ("channel in ('a','b')", "channel", "in", ("a", "b")),
    ],
)
def test_parse_single_clause(expr, column, op, values):
    (clause,) = gq.parse_filter(expr)
    assert (clause.column, clause.op, clause.values) == (column, op, values)


def test_parse_and_joined_clauses():
    clauses = gq.parse_filter("quality = 'good' AND value >= 10")
    assert [c.column for c in clauses] == ["quality", "value"]


def test_literal_becomes_a_bound_parameter_never_sql_text():
    # The injection property: whatever the caller writes lands in params.
    hostile = "'; DROP TABLE gold.signals; --"
    clauses = gq.parse_filter(f"quality = '{hostile}'")
    sql, params = gq.compile_where(clauses, COLUMNS)
    assert "DROP TABLE" not in sql
    assert params == [hostile]
    assert sql.count("%s") == 1


def test_unknown_column_in_filter_is_a_typed_error():
    clauses = gq.parse_filter("nope = 'x'")
    with pytest.raises(gq.UnknownObject):
        gq.compile_where(clauses, COLUMNS)


def test_bare_identifier_value_is_rejected():
    # Forces quoting; blocks column-to-column comparison as a smuggling vector.
    with pytest.raises(gq.FilterSyntaxError):
        gq.parse_filter("quality = other_column")


@pytest.mark.parametrize("expr", ["quality", "quality ~ 'x'", "quality = ", "= 'x'"])
def test_malformed_filter_is_a_typed_error(expr):
    with pytest.raises(gq.FilterSyntaxError):
        gq.parse_filter(expr)


def test_empty_filter_is_no_clauses():
    assert gq.parse_filter("") == ()
    assert gq.parse_filter(None) == ()


# --------------------------------------------------------------------------
# aggregate function whitelist
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fn", sorted(gq.AGGREGATE_FNS))
def test_every_declared_fn_compiles(fn):
    assert gq.sql_for_fn(fn)


def test_unknown_fn_is_a_typed_error():
    with pytest.raises(gq.UnknownObject):
        gq.sql_for_fn("; DROP TABLE x")


# --------------------------------------------------------------------------
# access tiers — fail closed, with a negative control
# --------------------------------------------------------------------------


def test_tier_filter_applied_when_the_table_carries_a_tier_column():
    clause = gq.tier_clause(COLUMNS, ("public",), table="signals", restricted_tables=frozenset())
    assert clause is not None
    assert clause.column == "access_tier" and clause.values == ("public",)


def test_no_tier_column_and_not_restricted_is_allowed():
    plain = frozenset({"site", "value"})
    assert (
        gq.tier_clause(plain, ("public",), table="signals", restricted_tables=frozenset()) is None
    )


def test_restricted_table_without_a_tier_column_FAILS_CLOSED():
    plain = frozenset({"site", "value"})
    with pytest.raises(gq.AccessTierUnavailable):
        gq.tier_clause(
            plain, ("public",), table="secrets", restricted_tables=frozenset({"secrets"})
        )


def test_negative_control_the_tier_check_can_fail():
    # Proves the guard above is not vacuously green: same call, tier column
    # present, must NOT raise.
    with_tier = frozenset({"site", "value", "access_tier"})
    assert (
        gq.tier_clause(
            with_tier, ("public",), table="secrets", restricted_tables=frozenset({"secrets"})
        )
        is not None
    )


# --------------------------------------------------------------------------
# provenance envelope
# --------------------------------------------------------------------------


def test_envelope_shape():
    env = gq.envelope(data={"mean": 1.0}, source="gold.signals", method="avg(value)", rows=12)
    assert env["data"] == {"mean": 1.0}
    assert env["provenance"]["source"] == "gold.signals"
    assert env["provenance"]["rows"] == 12


def test_empty_result_is_null_data_with_a_note_not_an_error():
    env = gq.envelope(
        data=None, source="gold.signals", method="avg(value)", note="no rows in window"
    )
    assert env["data"] is None
    assert "no rows" in env["provenance"]["note"]


# --------------------------------------------------------------------------
# execution against a fake cursor (DBAPI shape)
# --------------------------------------------------------------------------


class FakeCursor:
    """Minimal DBAPI cursor: records SQL+params, replays queued results."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self._last = None

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), list(params or [])))
        self._last = self._results.pop(0) if self._results else []

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def test_list_tables_reads_the_gold_schema_only():
    cur = FakeCursor([[("signals",), ("signals_latest",)]])
    out = gq.list_tables(cur)
    sql, params = cur.calls[0]
    assert "information_schema" in sql
    assert gq.GOLD_SCHEMA in params
    assert [t["table"] for t in out["data"]["tables"]] == ["signals", "signals_latest"]


def test_describe_unknown_table_is_a_typed_error():
    cur = FakeCursor([[]])  # no columns -> table does not exist
    with pytest.raises(gq.UnknownObject):
        gq.describe(cur, "does_not_exist")


def test_describe_returns_columns_with_provenance():
    cur = FakeCursor([[("ts", "timestamp without time zone"), ("value", "double precision")]])
    out = gq.describe(cur, "signals")
    assert [c["column"] for c in out["data"]["columns"]] == ["ts", "value"]
    assert out["provenance"]["source"] == "gold.signals"


def test_aggregate_validates_column_against_introspection():
    cur = FakeCursor([[("ts", "timestamp"), ("value", "double precision")]])
    with pytest.raises(gq.UnknownObject):
        gq.aggregate(cur, table="signals", column="not_a_column", fn="mean")


def test_aggregate_builds_parameterized_sql_and_returns_envelope():
    cur = FakeCursor(
        [
            [("ts", "timestamp"), ("value", "double precision"), ("quality", "text")],
            [("good", 7)],  # composition of `quality` — one value, so no refusal
            [(42.0, 7)],
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean", filter="quality = 'good'")
    # The executor also asks for the population's composition, so find the
    # aggregate by what it is rather than by where it falls in the sequence.
    agg_sql, agg_params = next(c for c in cur.calls if "avg(" in c[0].lower())
    assert "avg" in agg_sql.lower()
    assert agg_params == ["good"]  # the literal is bound, not spliced
    assert out["data"]["value"] == 42.0
    assert out["data"]["fn"] == "mean"
    assert out["provenance"]["rows"] == 7


def test_aggregate_empty_window_is_null_not_zero():
    cur = FakeCursor(
        [
            [("ts", "timestamp"), ("value", "double precision")],
            [(None, 0)],
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")
    assert out["data"] is None
    assert "note" in out["provenance"]


def test_series_buckets_and_returns_rows():
    cur = FakeCursor(
        [
            [("ts", "timestamp"), ("value", "double precision")],
            [("2026-09-01T00:00:00", 1.0), ("2026-09-01T01:00:00", 2.0)],
        ]
    )
    out = gq.series(cur, table="signals", column="value", bucket="1 hour", time_column="ts")
    assert len(out["data"]["series"]) == 2
    assert out["data"]["series"][0]["value"] == 1.0
    assert out["provenance"]["rows"] == 2


def test_series_rejects_a_non_interval_bucket():
    cur = FakeCursor([[("ts", "timestamp"), ("value", "double precision")]])
    with pytest.raises(gq.FilterSyntaxError):
        gq.series(
            cur, table="signals", column="value", bucket="1 hour; DROP TABLE x", time_column="ts"
        )


def test_window_columns_are_validated_too():
    cur = FakeCursor([[("ts", "timestamp"), ("value", "double precision")]])
    with pytest.raises(gq.UnknownObject):
        gq.aggregate(
            cur,
            table="signals",
            column="value",
            fn="mean",
            window={"column": "bogus", "start": "2026-01-01", "end": "2026-02-01"},
        )


# --------------------------------------------------------------------------
# projection: the verbs must actually reach the surfaces (ADR-072/073)
# --------------------------------------------------------------------------


def _bound_registry():
    from axiom.extensions.builtins.data_platform.skills import bind
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    return reg


@pytest.mark.parametrize(
    "verb", ["catalog", "describe", "aggregate", "series"]
)
def test_gold_verb_is_registered_and_projected_read_only(verb):
    reg = _bound_registry()
    spec = reg.spec(f"data.{verb}")
    assert spec is not None, f"data.{verb} is not registered"
    # Discovered, not hardcoded: a chat agent finds these through projection.
    assert "mcp" in (spec.surfaces or ()) and "agent_tool" in (spec.surfaces or ())
    # Read-only is load-bearing — these must never be approval-gated writes.
    assert spec.side_effects is False
    assert spec.description


def test_gold_verbs_declare_their_required_inputs():
    reg = _bound_registry()
    agg = reg.spec("data.aggregate")
    for required in ("table", "column", "fn"):
        assert agg.inputs.get(required, "").endswith("!"), f"{required} should be required"


# --- an empty grant set must refuse, not emit invalid SQL --------------------


def test_empty_tier_grant_refuses_rather_than_building_in_nothing():
    """A caller holding NO tiers must be refused with a typed error.

    ``IN ()`` is a Postgres syntax error, so emitting it turns a deny into a
    database error — breaking the module's contract that a caller gets a typed
    error rather than a DB one. It is also the wrong shape of answer: "you have
    no grants" is not "no rows matched".
    """
    with_tier = frozenset({"site", "value", "access_tier"})
    with pytest.raises(gq.AccessTierUnavailable):
        gq.tier_clause(with_tier, (), table="signals")


def test_negative_control_a_nonempty_grant_still_builds_a_clause():
    # Proves the guard above is not vacuously green.
    with_tier = frozenset({"site", "value", "access_tier"})
    clause = gq.tier_clause(with_tier, ("public",), table="signals")
    assert clause is not None and clause.values == ("public",)


def test_compile_where_never_emits_an_empty_in_list():
    """Defense in depth: no caller path should reach here with an empty ``in``
    (``parse_filter`` already refuses one), but if one ever does, the renderer
    must raise rather than produce ``IN ()``."""
    empty = gq.Clause(column="site", op="in", values=())
    with pytest.raises(gq.FilterSyntaxError):
        gq.compile_where([empty], ["site"])


# --- an applied access filter must appear in the provenance -----------------


def test_tier_restriction_is_recorded_in_the_provenance():
    """Two callers with different grants get different numbers; if the tier
    filter is invisible in ``method``, those answers carry identical provenance
    and the difference is unexplainable after the fact."""
    names = {"access_tier", "value", "ts"}
    _, _, described = gq._window_and_filter(
        names, window=None, filter=None, access_tiers=("public",),
        table="signals", restricted_tables=(),
    )
    assert any("public" in d for d in described), described


def test_negative_control_an_untiered_table_describes_no_tier():
    # Same call shape on a table with no tier column: nothing to describe.
    _, _, described = gq._window_and_filter(
        {"value", "ts"}, window=None, filter=None, access_tiers=("public",),
        table="signals", restricted_tables=(),
    )
    assert not any("public" in d for d in described), described


# --- "no rows" and "no values" are different facts ---------------------------


class _Cur:
    """Cursor stub: fixed column shape, caller-supplied aggregate row."""

    def __init__(self, agg_row, columns=(("value", "numeric"), ("ts", "timestamp"))):
        self._agg_row = agg_row
        self._columns = list(columns)
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)

    def fetchall(self):
        return self._columns

    def fetchone(self):
        return self._agg_row


def test_an_all_null_column_is_not_reported_as_no_rows():
    """Rows matched, but the aggregated column holds no values.

    Reporting that as "no rows matched" sends the caller to widen a window that
    was never the problem. The two cases need different notes because they need
    different responses.
    """
    env = gq.aggregate(_Cur((None, 0, 500)), table="signals", column="value", fn="mean")
    assert env["data"] is None
    note = env["provenance"]["note"]
    assert "no rows matched" not in note, note
    assert "500" in note or "null" in note.lower(), note


def test_negative_control_a_genuinely_empty_window_still_says_no_rows():
    # Proves the distinction above is real and not just a reworded note.
    env = gq.aggregate(_Cur((None, 0, 0)), table="signals", column="value", fn="mean")
    assert env["data"] is None
    assert "no rows matched" in env["provenance"]["note"]


# --------------------------------------------------------------------------
# uncertainty at the served boundary
#
# Before this, `aggregate` and `series` mapped `mean -> avg` and dropped
# uncertainty entirely, so whatever an ingest declared in
# `silver.signals.uncertainty` died at the exact boundary a decision
# consumes it. These tests are about the number SURVIVING, and about the
# absence of one being transmitted rather than omitted.
# --------------------------------------------------------------------------

_SIGNAL_COLS = [
    ("ts", "timestamp with time zone"),
    ("value", "double precision"),
    ("uncertainty", "double precision"),
]


def test_a_mean_carries_a_bound_rather_than_a_bare_number():
    cur = FakeCursor(
        [
            _SIGNAL_COLS,
            # avg, count(value), count(*), quantified, sum_u, sum_sq, max_u
            [(21.4, 1000, 1000, 1000, 500.0, 250.0, 0.5)],
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")

    u = out["uncertainty"]
    assert u["claimable"] is True
    # A thousand readings at +/-0.5 from ONE sensor. Independent says 0.0158;
    # a shared calibration says 0.5. The bound spans both, which is what
    # stops the served surface asserting the first one silently.
    assert u["low"] == pytest.approx(0.5 / 1000**0.5)
    assert u["high"] == pytest.approx(0.5)
    assert u["quantified"] == 1000
    assert u["unquantified"] == 0
    assert u["complete"] is True
    assert "non-negatively correlated" in u["premise"]


def test_the_uncertainty_query_is_one_pass_over_the_same_window():
    """Sufficient statistics, in the SAME statement as the aggregate.

    A second query with its own window would be two answers to one
    question, and pulling rows back to combine them would defeat the point
    of aggregating in the database.
    """
    cur = FakeCursor([_SIGNAL_COLS, [(21.4, 3, 3, 3, 1.5, 0.75, 0.5)]])
    gq.aggregate(cur, table="signals", column="value", fn="mean")

    sql, _ = cur.calls[1]
    assert sql.count("SELECT") == 1
    assert 'avg("value")' in sql
    assert 'sum("uncertainty")' in sql
    assert 'sum("uncertainty" * "uncertainty")' in sql
    # FILTER scopes every statistic to rows that carried a value, so "no
    # value" and "value with no uncertainty" stay distinguishable.
    assert 'FILTER (WHERE "value" IS NOT NULL' in sql


def test_rows_with_no_uncertainty_are_counted_and_left_outside_the_bound():
    cur = FakeCursor(
        [
            _SIGNAL_COLS,
            # 100 values, only 30 of them carrying an uncertainty
            [(21.4, 100, 100, 30, 15.0, 7.5, 0.5)],
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")

    u = out["uncertainty"]
    assert u["quantified"] == 30
    assert u["unquantified"] == 70
    assert u["complete"] is False
    # Divided by all 100 rows the mean averaged, not by the 30 that reported.
    # A shared source on 30 of 100 rows contributes a*30/100 to the mean of
    # all 100, because it only perturbs the rows it applies to. Dividing by 30
    # would report the uncertainty of the mean of THOSE 30 while serving the
    # value of the mean of 100 — a cautious-looking number attached to a
    # different quantity.
    #
    # The narrowing that causes is not hidden: `complete: false` and the
    # unquantified count say outright that 70 rows contribute an amount
    # nothing bounds.
    assert u["high"] == pytest.approx(15.0 / 100)


def test_a_window_where_nothing_reported_an_uncertainty_says_so_explicitly():
    """`claimable: false` with a note is a statement. A missing key is a
    silence, and the two are indistinguishable to a reader."""
    cur = FakeCursor([_SIGNAL_COLS, [(21.4, 100, 100, 0, None, None, None)]])
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")

    u = out["uncertainty"]
    assert u["claimable"] is False
    assert u["low"] is None and u["high"] is None
    assert u["unquantified"] == 100
    assert "none is claimed" in u["note"]
    # Never zero. Zero over 100 silent rows is a claim of perfect precision.
    assert u["low"] != 0


def test_a_table_with_no_uncertainty_column_gets_no_uncertainty_key():
    """Nothing is invented. A table that cannot carry uncertainty does not
    get a fabricated bound, and no column is guessed from its type."""
    cur = FakeCursor(
        [
            [("ts", "timestamp"), ("value", "double precision"), ("pressure", "double precision")],
            [(21.4, 10, 10)],
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")
    assert "uncertainty" not in out
    sql, _ = cur.calls[1]
    assert "pressure" not in sql


def test_a_sum_adds_magnitudes_rather_than_averaging_them():
    cur = FakeCursor([_SIGNAL_COLS, [(2140.0, 100, 100, 100, 50.0, 25.0, 0.5)]])
    out = gq.aggregate(cur, table="signals", column="value", fn="sum")
    u = out["uncertainty"]
    assert u["high"] == pytest.approx(50.0)
    assert u["low"] == pytest.approx(25.0**0.5)


def test_a_maximum_reports_the_winning_rows_own_uncertainty():
    cur = FakeCursor(
        [
            _SIGNAL_COLS,
            # max, count, count(*), quantified, sum_u, sum_sq, max_u, selected_u
            [(91.2, 100, 100, 100, 50.0, 25.0, 0.9, 0.5)],
            [(1,)],  # the rivals pass: only the winning row itself
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="max")
    u = out["uncertainty"]
    # An extremum is one row's reading, not a combination, so it is exact.
    assert u["low"] == u["high"] == pytest.approx(0.5)
    assert u["premise"] == "exact"
    assert u["note"] is None


def test_a_maximum_that_is_not_distinguishable_from_other_rows_says_so():
    """The finding that looks least like a problem.

    A served peak of 91.2 +/- 0.5 with forty samples inside its own error
    bar is not the location of a peak, and a threshold check reading it as
    one is acting on noise.
    """
    cur = FakeCursor(
        [
            _SIGNAL_COLS,
            [(91.2, 100, 100, 100, 50.0, 25.0, 0.9, 0.5)],
            [(41,)],  # the winning row plus 40 rivals
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="max")
    u = out["uncertainty"]
    assert "40 other row(s) fall within that interval" in u["note"]
    assert "not determined by the data" in u["note"]

    # The rivals pass reuses the SAME window: identical WHERE and params,
    # one answer to one question.
    agg_sql, agg_params = cur.calls[1]
    rivals_sql, rivals_params = cur.calls[2]
    assert rivals_params[:-1] == agg_params
    assert rivals_params[-1] == pytest.approx(91.2 - 0.5)
    assert '"value" >= %s' in rivals_sql


def test_a_minimum_looks_the_other_way_for_rivals():
    cur = FakeCursor(
        [
            _SIGNAL_COLS,
            [(1.1, 100, 100, 100, 50.0, 25.0, 0.9, 0.5)],
            [(3,)],
        ]
    )
    gq.aggregate(cur, table="signals", column="value", fn="min")
    rivals_sql, rivals_params = cur.calls[2]
    assert '"value" <= %s' in rivals_sql
    assert rivals_params[-1] == pytest.approx(1.1 + 0.5)


def test_a_standard_deviation_reports_the_measurement_floor_not_a_bound():
    """Measurement error INFLATES a dispersion rather than adding to it, so
    quoting the magnitude as the dispersion's uncertainty would be a
    fiction."""
    cur = FakeCursor([_SIGNAL_COLS, [(5.0, 100, 100, 100, 50.0, 25.0, 0.5)]])
    out = gq.aggregate(cur, table="signals", column="value", fn="std")
    u = out["uncertainty"]
    assert u["claimable"] is False
    assert "includes it rather than adding to it" in u["note"]


def test_a_spread_that_may_be_entirely_instrumental_says_so():
    cur = FakeCursor([_SIGNAL_COLS, [(0.4, 100, 100, 100, 50.0, 25.0, 0.5)]])
    out = gq.aggregate(cur, table="signals", column="value", fn="std")
    assert "may be entirely instrumental" in out["uncertainty"]["note"]


def test_a_count_is_exactly_known():
    cur = FakeCursor([_SIGNAL_COLS, [(100, 100, 100, 100, 50.0, 25.0, 0.5)]])
    out = gq.aggregate(cur, table="signals", column="value", fn="count")
    u = out["uncertainty"]
    assert u["low"] == u["high"] == 0.0
    assert "exactly known" in u["note"]


def test_every_aggregate_in_the_closed_set_has_an_uncertainty_rule():
    """A new aggregate must not be able to serve a bare number.

    `sql_for_fn` admits a closed set. If somebody adds to it without adding
    a rule here, this test fails rather than the surface quietly dropping
    uncertainty again — which is precisely how it was lost the first time.
    """
    for fn in sorted(gq.AGGREGATE_FNS):
        n_stats = 5 if fn in ("min", "max") else 4
        cur = FakeCursor(
            [
                _SIGNAL_COLS,
                [(1.0, 10, 10, 10, 5.0, 2.5, 0.5, 0.5)[: 3 + n_stats]],
                [(1,)],
            ]
        )
        out = gq.aggregate(cur, table="signals", column="value", fn=fn)
        assert "uncertainty" in out, f"{fn} served without an uncertainty key"
        u = out["uncertainty"]
        assert u["claimable"] or u["note"], f"{fn} was silent about uncertainty"
        assert "no uncertainty rule is defined" not in (u["note"] or ""), (
            f"{fn} is in AGGREGATE_FNS but _served_uncertainty has no rule for it"
        )


def test_every_point_in_a_series_carries_its_own_bound():
    """One figure for a whole series would fit none of its points."""
    cur = FakeCursor(
        [
            _SIGNAL_COLS,
            [
                ("2026-09-28T00:00:00Z", 21.4, 60, 60, 30.0, 15.0, 0.5),
                ("2026-09-28T01:00:00Z", 21.9, 60, 0, None, None, None),
            ],
        ]
    )
    out = gq.series(
        cur, table="signals", column="value", bucket="1 hour", time_column="ts", fn="mean"
    )
    points = out["data"]["series"]
    assert points[0]["uncertainty"]["claimable"] is True
    # 60 rows, all quantified, magnitudes summing to 30 -> a mean bounded
    # above by 30/60.
    assert points[0]["uncertainty"]["high"] == pytest.approx(30.0 / 60)
    # The second bucket had values and no uncertainties. It says so rather
    # than inheriting the first bucket's bound or reporting zero.
    assert points[1]["uncertainty"]["claimable"] is False
    assert points[1]["uncertainty"]["unquantified"] == 60


def test_a_series_extremum_does_not_silently_claim_to_be_unambiguous():
    """Establishing rivals per bucket would be one query per bucket. A
    silent 0 would read as "unambiguous" — a positive claim nobody made."""
    cur = FakeCursor(
        [
            _SIGNAL_COLS,
            [("2026-09-28T00:00:00Z", 91.2, 60, 60, 30.0, 15.0, 0.9, 0.5)],
        ]
    )
    out = gq.series(
        cur, table="signals", column="value", bucket="1 hour", time_column="ts", fn="max"
    )
    note = out["data"]["series"][0]["uncertainty"]["note"]
    assert "was not determined" in note


def test_the_envelope_emits_every_uncertainty_field_including_nulls():
    """A reader must never infer absence from a missing key."""
    from axiom.uncertainty.serving import for_mean

    out = gq.envelope(
        data={"value": 1.0},
        source="gold.signals",
        method="avg(value)",
        uncertainty=for_mean(quantified=0, sum_u=0.0, sum_sq=0.0, max_u=0.0, unquantified=5),
    )
    assert set(out["uncertainty"]) == {
        "low",
        "high",
        "low_unconstrained",
        "premise",
        "quantified",
        "unquantified",
        "complete",
        "claimable",
        "note",
        "structured",
        "terms",
        "dominant",
    }


def test_the_envelope_omits_the_key_only_when_nothing_was_computed():
    out = gq.envelope(data=None, source="gold.signals", method="avg(value)")
    assert "uncertainty" not in out


# --------------------------------------------------------------------------
# the structured-uncertainty companion, through the real SQL path
# --------------------------------------------------------------------------

_COMPANION_COLS = [
    ("row_hash", "text"),
    ("channel", "text"),
    ("symbol", "text"),
    ("coefficient", "double precision"),
    ("independent", "boolean"),
]
_SIGNAL_COLS_WITH_KEYS = [
    ("row_hash", "text"),
    ("channel", "text"),
    ("ts", "timestamp with time zone"),
    ("value", "double precision"),
    ("uncertainty", "double precision"),
]


def _companion_cursor(term_rows, scalar_row, agg_row=(21.4, 1000, 1000)):
    """A cursor replaying the four reads the structured path makes."""
    return FakeCursor(
        [
            _SIGNAL_COLS_WITH_KEYS,  # introspect the base table
            [agg_row],  # the aggregate + scalar statistics
            _COMPANION_COLS,  # introspect the companion
            term_rows,  # GROUP BY symbol, independent
            [scalar_row],  # structured / loose / silent split
        ]
    )


def test_a_mean_over_declared_sources_is_exact_not_a_bound():
    """The payoff of the companion table.

    1000 readings sharing one calibration bath at +/-0.5. The scalar path can
    only say "somewhere between 0.0158 and 0.5". With the sources declared it
    says 0.5, exactly -- because the correlation is computed from the shared
    symbol rather than assumed.
    """
    cur = _companion_cursor(
        term_rows=[("signals:cal-bath-a:offset", False, 500.0, 250.0)],
        scalar_row=(1000, 0, None, None, None, 0),
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")

    u = out["uncertainty"]
    assert u["structured"] is True
    assert u["low"] == u["high"] == pytest.approx(0.5)
    assert u["premise"] == "exact"
    assert u["terms"] == {"signals:cal-bath-a:offset": pytest.approx(0.5)}


def test_a_per_reading_source_averages_down_through_the_same_path():
    cur = _companion_cursor(
        # 1000 draws at 0.1: sum 100, sum of squares 10.
        term_rows=[("signals:tc-14:repeatability", True, 100.0, 10.0)],
        scalar_row=(1000, 0, None, None, None, 0),
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")
    assert out["uncertainty"]["low"] == pytest.approx(0.1 / 1000**0.5)


def test_a_shared_and_a_per_reading_source_compose_on_their_symbols():
    cur = _companion_cursor(
        term_rows=[
            ("signals:cal-bath-a:offset", False, 500.0, 250.0),
            ("signals:tc-14:repeatability", True, 100.0, 10.0),
        ],
        scalar_row=(1000, 0, None, None, None, 0),
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")
    u = out["uncertainty"]
    expected = (0.5**2 + (0.1 / 1000**0.5) ** 2) ** 0.5
    assert u["low"] == pytest.approx(expected)
    # The dominant source is named, so effort can be aimed at the bath rather
    # than at the sensor.
    assert u["dominant"][0]["symbol"] == "signals:cal-bath-a:offset"
    assert u["dominant"][0]["share"] > 0.99


def test_the_structured_query_scopes_the_window_before_joining():
    """`wheres` carry bare identifiers and a companion shares key names with
    its base -- `channel` is on both -- so filtering across the join would be
    ambiguous SQL. The window goes into a derived table instead."""
    cur = _companion_cursor(
        term_rows=[("signals:cal-bath-a:offset", False, 500.0, 250.0)],
        scalar_row=(1000, 0, None, None, None, 0),
    )
    gq.aggregate(cur, table="signals", column="value", fn="mean", filter="channel = 'tc-14'")
    join_sql, join_params = cur.calls[3]
    assert '(SELECT * FROM "gold"."signals" WHERE' in join_sql
    assert 'JOIN "gold"."signals_uncertainty" c ON' in join_sql
    assert 'c."channel" = s."channel"' in join_sql
    assert "tc-14" in join_params


def test_both_structured_passes_see_the_identical_window():
    """Two passes over one window, never two windows."""
    cur = _companion_cursor(
        term_rows=[("signals:cal-bath-a:offset", False, 500.0, 250.0)],
        scalar_row=(1000, 0, None, None, None, 0),
    )
    gq.aggregate(cur, table="signals", column="value", fn="mean", filter="channel = 'tc-14'")
    _, terms_params = cur.calls[3]
    _, split_params = cur.calls[4]
    assert terms_params == split_params


def test_a_row_that_declared_structure_does_not_also_count_its_scalar():
    """The double count the split exists to prevent.

    A scalar summarises the same sources as the terms. If both were counted
    the served figure would be inflated by exactly the thing it already
    contains, and every row carrying both would inflate it again.
    """
    cur = _companion_cursor(
        term_rows=[("signals:cal-bath-a:offset", False, 500.0, 250.0)],
        # 1000 structured rows; the loose columns are all zero because EXISTS
        # excluded every structured row from them.
        scalar_row=(1000, 0, None, None, None, 0),
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")
    assert out["uncertainty"]["low"] == pytest.approx(0.5)

    _, _ = cur.calls[4]
    split_sql, _ = cur.calls[4]
    assert "NOT s.has_terms" in split_sql
    assert "FILTER (WHERE s.has_terms)" in split_sql


def test_a_mixed_window_bounds_the_undeclared_part_and_says_so():
    cur = _companion_cursor(
        term_rows=[("signals:cal-bath-a:offset", False, 350.0, 175.0)],
        # 700 structured, 200 magnitude-only, 100 silent
        scalar_row=(700, 200, 40.0, 8.0, 0.2, 100),
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="sum")
    u = out["uncertainty"]
    assert u["structured"] is True
    assert u["low"] < u["high"]
    assert u["premise"] != "exact"
    assert u["quantified"] == 900
    assert u["unquantified"] == 100
    assert u["complete"] is False
    assert "without its sources" in u["note"]


def test_no_companion_table_falls_back_to_the_scalar_bound():
    """Absence of a companion is the common case and must cost nothing."""
    cur = FakeCursor(
        [
            _SIGNAL_COLS_WITH_KEYS,
            [(21.4, 1000, 1000, 1000, 500.0, 250.0, 0.5)],
            [],  # the companion introspection finds no such table
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")
    u = out["uncertainty"]
    assert u["structured"] is False
    assert u["low"] == pytest.approx(0.5 / 1000**0.5)
    assert u["high"] == pytest.approx(0.5)


def test_a_companion_whose_keys_the_base_view_lacks_is_unreachable():
    """Returning it would build SQL that fails at query time rather than
    falling back cleanly."""
    cur = FakeCursor(
        [
            # no row_hash on the base view
            [
                ("ts", "timestamp"),
                ("value", "double precision"),
                ("uncertainty", "double precision"),
            ],
            [(21.4, 10, 10, 10, 5.0, 2.5, 0.5)],
            _COMPANION_COLS,
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")
    assert out["uncertainty"]["structured"] is False


def test_an_extremum_ignores_the_companion_and_keeps_the_row_rule():
    """An extremum is one row's reading, not a combination, so composing
    sources across the window would answer a different question."""
    cur = FakeCursor(
        [
            _SIGNAL_COLS_WITH_KEYS,
            [(91.2, 100, 100, 100, 50.0, 25.0, 0.9, 0.5)],
            [(1,)],
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="max")
    assert out["uncertainty"]["structured"] is False
    assert out["uncertainty"]["low"] == pytest.approx(0.5)


def test_every_ddl_list_is_applied():
    """A DDL list nobody applies gives two installs different schemas.

    The conformance DDL is applied from `skills/ensure_schema.py` AND from
    `conformance/runner.py`. Both read `SILVER_DDL`, so a new list has to be
    reachable from it -- and this test fails if one is added and left out,
    rather than a node quietly missing a table.
    """
    from axiom.extensions.builtins.data_platform import conformance as cf

    aggregate = "\n".join(cf.SILVER_DDL)
    for name in dir(cf):
        if not name.startswith("SILVER_") or not name.endswith("_DDL"):
            continue
        if name == "SILVER_DDL":
            continue
        for stmt in getattr(cf, name):
            assert stmt in aggregate, (
                f"{name} contains a statement absent from SILVER_DDL, so only "
                "some appliers would run it"
            )


def test_the_conformance_ddl_is_idempotent_by_construction():
    """It runs on every deploy AND on the nightly conformance sweep, so a
    statement that fails the second time takes a live node's schema pass
    down with it.

    `ADD CONSTRAINT` has no IF NOT EXISTS in Postgres -- that is why the
    budget table's CHECK is inline in CREATE TABLE rather than a later ALTER.
    """
    from axiom.extensions.builtins.data_platform import conformance as cf

    for stmt in (*cf.SILVER_DDL, *cf.GOLD_SIGNALS_DDL):
        head = " ".join(stmt.split())[:80].upper()
        if head.startswith("CREATE TABLE"):
            assert "IF NOT EXISTS" in head
        if head.startswith("CREATE INDEX"):
            assert "IF NOT EXISTS" in head
        if head.startswith("CREATE VIEW") or "CREATE OR REPLACE VIEW" in head:
            assert "OR REPLACE" in head
        if "ADD COLUMN" in head:
            assert "IF NOT EXISTS" in head
        assert "ADD CONSTRAINT" not in head, (
            "ADD CONSTRAINT is not idempotent in Postgres; put the constraint "
            "inline in CREATE TABLE"
        )


# --------------------------------------------------------------------------
# the coverage surface
#
# An uncertainty apparatus with nothing declared behind it serves
# `claimable: false` on every read and looks entirely healthy. That is the
# channel-map failure one level down -- a map can be complete and match
# nothing -- so coverage has to be a surface rather than something a reader
# goes counting for themselves.
# --------------------------------------------------------------------------

_COVERAGE_COLS = [
    "site",
    "stream",
    "points",
    "channels",
    "with_magnitude",
    "with_structure",
    "silent",
    "first_ts",
    "last_ts",
]


def _coverage_cursor(coverage_rows, source_rows=()):
    return FakeCursor([list(coverage_rows), list(source_rows)])


def test_a_surface_where_nothing_declared_anything_says_so_bluntly():
    """The case that matters, and the one a percentage would obscure.

    Nothing is broken, nothing will report an error, and every aggregate
    reports no uncertainty. That has to be stated as the absence of a
    declaration rather than as partial progress.
    """
    cur = _coverage_cursor([("site-a", "loop", 500_000, 40, 0, 0, 500_000, None, None)])
    out = gq.uncertainty_coverage(cur, site="site-a")

    note = out["provenance"]["note"]
    assert "none of 500000 served point(s) carries an uncertainty" in note
    assert "Nothing is broken and nothing will report an error" in note
    assert "what its instruments are worth" in note
    assert out["data"]["streams"][0]["unclaimable"] is True
    assert out["data"]["totals"]["structured"] == 0


def test_a_fully_declared_surface_says_aggregates_are_exact():
    cur = _coverage_cursor([("site-a", "loop", 1200, 2, 1200, 1200, 0, None, None)])
    out = gq.uncertainty_coverage(cur)
    assert "aggregates over them are exact rather than bounded" in out["provenance"]["note"]
    assert out["data"]["streams"][0]["exact"] is True


def test_the_three_populations_are_disjoint_and_sum_to_the_points():
    """A row carrying structure also carries a summary scalar, so counting
    it in both would make the fractions sum past one."""
    cur = _coverage_cursor([("site-a", "loop", 1350, 4, 1300, 1200, 50, None, None)])
    s = gq.uncertainty_coverage(cur)["data"]["streams"][0]

    assert s["structured"] == 1200
    assert s["magnitude_only"] == 100  # 1300 with a magnitude, minus the 1200 structured
    assert s["silent"] == 50
    assert s["structured"] + s["magnitude_only"] + s["silent"] == s["points"]
    assert s["structured_fraction"] + s["magnitude_fraction"] + s[
        "silent_fraction"
    ] == pytest.approx(1.0)


def test_a_mixed_surface_names_the_streams_that_are_entirely_unclaimable():
    cur = _coverage_cursor(
        [
            ("site-a", "loop", 1000, 4, 1000, 1000, 0, None, None),
            ("site-a", "aux", 400, 2, 0, 0, 400, None, None),
            ("site-a", "spare", 100, 1, 0, 0, 100, None, None),
        ]
    )
    note = gq.uncertainty_coverage(cur)["provenance"]["note"]
    assert "1000 exact" in note
    assert "500 unclaimable" in note
    assert "site-a/aux" in note and "site-a/spare" in note


def test_many_unclaimable_streams_are_summarised_rather_than_listed():
    rows = [("site-a", f"s{i}", 10, 1, 0, 0, 10, None, None) for i in range(9)]
    rows.insert(0, ("site-a", "good", 100, 1, 100, 100, 0, None, None))
    note = gq.uncertainty_coverage(cur=_coverage_cursor(rows))["provenance"]["note"]
    assert "and 6 more" in note


def test_an_empty_surface_does_not_pretend_to_a_verdict():
    out = gq.uncertainty_coverage(_coverage_cursor([]))
    assert "nothing to have an uncertainty about" in out["provenance"]["note"]
    assert out["data"]["totals"]["points"] == 0


def test_the_source_inventory_surfaces_the_independent_flag():
    """The field that decides whether an error averages away, and the one a
    human has to be able to review -- a source declared per-reading when it
    is shared gives a figure too confident by sqrt(n), and nothing
    downstream can tell."""
    cur = _coverage_cursor(
        [("site-a", "loop", 1200, 2, 1200, 1200, 0, None, None)],
        [
            ("site-a", "loop", "signals:cal-bath-a:offset", False, 1200, 2, 0.5, 0.5),
            ("site-a", "loop", "signals:tc-14:repeatability", True, 600, 1, 0.1, 0.1),
        ],
    )
    sources = gq.uncertainty_coverage(cur)["data"]["sources"]
    assert sources[0]["symbol"] == "signals:cal-bath-a:offset"
    assert sources[0]["independent"] is False
    assert sources[1]["independent"] is True
    assert sources[0]["min_coefficient"] == pytest.approx(0.5)


def test_the_inventory_can_be_skipped_without_a_second_query():
    cur = _coverage_cursor([("site-a", "loop", 10, 1, 10, 10, 0, None, None)])
    out = gq.uncertainty_coverage(cur, include_sources=False)
    assert out["data"]["sources"] == []
    assert len(cur.calls) == 1


def test_the_site_filter_is_bound_not_interpolated():
    cur = _coverage_cursor([("site-a", "loop", 10, 1, 10, 10, 0, None, None)])
    gq.uncertainty_coverage(cur, site="site-a'; DROP TABLE silver.signals; --")
    sql, params = cur.calls[0]
    assert "site = %s" in sql
    assert "DROP TABLE" not in sql
    assert "DROP TABLE" in params[0]


def test_coverage_reads_the_view_rather_than_joining_per_row():
    """A per-row EXISTS over a partitioned signals table is not a surface a
    dashboard can call. The view pre-aggregates the companion."""
    cur = _coverage_cursor([("site-a", "loop", 10, 1, 10, 10, 0, None, None)])
    gq.uncertainty_coverage(cur, include_sources=False)
    sql, _ = cur.calls[0]
    assert "uncertainty_coverage" in sql
    assert "EXISTS" not in sql


def test_the_coverage_view_does_not_multiply_points_by_source_count():
    """A plain LEFT JOIN against the companion turns one signal with three
    sources into three rows and inflates `points` by the source count."""
    from axiom.extensions.builtins.data_platform.conformance import GOLD_SIGNALS_DDL

    ddl = next(s for s in GOLD_SIGNALS_DDL if "VIEW gold.uncertainty_coverage" in s)
    assert "GROUP BY row_hash, channel" in ddl
    assert "count(su.sources)" in ddl


def test_the_coverage_view_counts_values_not_rows():
    """A withheld value is not an uncharacterised measurement.

    When a device asserts a fault, the fault TRAVELS as `quality` and the
    reading is withheld as NULL so SQL aggregates self-correct. Such a row
    has nothing to be uncertain about — counting it in `points` reported it
    as a measurement nobody had characterised, which is a third thing it is
    not.

    It also has to agree with the serving path, which already scopes its
    statistics to `value IS NOT NULL`. Two denominators for one question
    would make the coverage figure disagree with the aggregate it describes.
    """
    from axiom.extensions.builtins.data_platform.conformance import GOLD_SIGNALS_DDL

    ddl = next(s for s in GOLD_SIGNALS_DDL if "VIEW gold.uncertainty_coverage" in s)
    assert "count(s.value)" in ddl
    assert "count(*)" not in ddl.split("AS points")[0], "points must count values, not rows"
    # `silent` is measured against the same denominator.
    assert "count(s.value) - count(s.uncertainty)" in ddl


def test_the_served_statistics_use_the_same_denominator_as_the_view():
    """The serving path and the coverage surface must not disagree about how
    many points a window had."""
    import inspect

    from axiom.extensions.builtins.data_platform import gold_query

    src = inspect.getsource(gold_query._structured_terms)
    # Every loose/silent statistic is scoped to rows carrying a value.
    assert "IS NOT NULL" in src
    assert 'f"NOT s.has_terms AND {cq} IS NOT NULL"' in src or "NOT s.has_terms" in src


def test_a_withheld_value_is_excluded_from_an_aggregate_entirely():
    """`count(column)` counts values, so a faulted row never enters `n` and
    therefore never dilutes a mean's divisor."""
    cur = FakeCursor(
        [
            _SIGNAL_COLS,
            # 100 rows matched, 90 carried a value, 90 of those had a magnitude
            [(21.4, 90, 100, 90, 45.0, 22.5, 0.5)],
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean")
    u = out["uncertainty"]
    assert out["provenance"]["rows"] == 90
    assert u["quantified"] == 90
    assert u["unquantified"] == 0
    assert u["complete"] is True
    # Divided by the 90 that carried a value, not the 100 rows matched.
    assert u["high"] == pytest.approx(45.0 / 90)
