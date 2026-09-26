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
    assert gq.tier_clause(plain, ("public",), table="signals", restricted_tables=frozenset()) is None


def test_restricted_table_without_a_tier_column_FAILS_CLOSED():
    plain = frozenset({"site", "value"})
    with pytest.raises(gq.AccessTierUnavailable):
        gq.tier_clause(plain, ("public",), table="secrets", restricted_tables=frozenset({"secrets"}))


def test_negative_control_the_tier_check_can_fail():
    # Proves the guard above is not vacuously green: same call, tier column
    # present, must NOT raise.
    with_tier = frozenset({"site", "value", "access_tier"})
    assert (
        gq.tier_clause(with_tier, ("public",), table="secrets", restricted_tables=frozenset({"secrets"}))
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
    env = gq.envelope(data=None, source="gold.signals", method="avg(value)", note="no rows in window")
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
            [(42.0, 7)],
        ]
    )
    out = gq.aggregate(cur, table="signals", column="value", fn="mean", filter="quality = 'good'")
    agg_sql, agg_params = cur.calls[1]
    assert "avg" in agg_sql.lower()
    assert agg_params == ["good"]            # the literal is bound, not spliced
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
        gq.series(cur, table="signals", column="value", bucket="1 hour; DROP TABLE x", time_column="ts")


def test_window_columns_are_validated_too():
    cur = FakeCursor([[("ts", "timestamp"), ("value", "double precision")]])
    with pytest.raises(gq.UnknownObject):
        gq.aggregate(
            cur, table="signals", column="value", fn="mean",
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
    "verb", ["gold_tables", "gold_describe", "gold_aggregate", "gold_series"]
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
    agg = reg.spec("data.gold_aggregate")
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
        names, window=None, filter=None, tiers=("public",),
        table="signals", restricted_tables=(),
    )
    assert any("public" in d for d in described), described


def test_negative_control_an_untiered_table_describes_no_tier():
    # Same call shape on a table with no tier column: nothing to describe.
    _, _, described = gq._window_and_filter(
        {"value", "ts"}, window=None, filter=None, tiers=("public",),
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
