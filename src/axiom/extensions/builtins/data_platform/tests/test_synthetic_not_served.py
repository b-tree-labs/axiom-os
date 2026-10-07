# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An install's own smoke test must not be served as data.

Every new site proves its install by emitting a synthetic signal. The rows
are correctly marked: on one node three sites carry about 2,935 each, all
`source_class = 'simulated'`. Nothing read the marker, so a partner's first
chart of their own loop would draw the smoke test and the real readings on
one line, correctly labelled and indistinguishable at a glance.

That is about to be run by every new site by design, which is why this comes
before the smoke test is institutionalised rather than after.
"""

from __future__ import annotations

from ..gold_query import (
    BASIS_COLUMN,
    SELFTEST_BASIS,
    synthetic_clause,
)

SIGNAL_COLUMNS = {"site", "feed", "channel", "ts", "value", "unit", "source_class", "basis"}


class TestWhatIsHeldOut:
    def test_it_filters_basis_not_source_class(self):
        """`source_class` names what produced a value, and by that measure a
        physics run and an onboarding self test are both simulations. One is
        high-fidelity model output somebody asked for, archived as HDF5 and
        used to train reduced-order models; the other is plumbing exercise.

        Filtering `source_class = 'simulated'` would today catch only self
        tests by accident, and would begin hiding the digital twin's own
        output the moment those runs are ingested under their correct class.
        """
        assert SELFTEST_BASIS == "selftest"
        clause = synthetic_clause(SIGNAL_COLUMNS)
        assert clause.column == "basis"

    def test_a_simulated_physics_run_is_still_served(self):
        """The regression this exists to prevent."""
        physics = {"site", "ts", "value", "source_class", "basis"}
        clause = synthetic_clause(physics)
        assert clause.values == ("selftest",)
        assert "simulated" not in clause.values

    def test_the_clause_excludes_rather_than_selects(self):
        """`!= simulated` keeps a row whose class nobody stated. `IN
        (measured, predicted, estimated)` would drop it, and an unstated
        class is not a claim that the row is synthetic."""
        clause = synthetic_clause(SIGNAL_COLUMNS)
        assert clause is not None
        assert clause.column == BASIS_COLUMN
        assert clause.op == "!="
        assert clause.values == (SELFTEST_BASIS,)


class TestItIsOnByDefault:
    def test_a_signal_table_gets_the_clause_with_no_argument(self):
        assert synthetic_clause(SIGNAL_COLUMNS) is not None

    def test_asking_for_it_turns_it_off(self):
        assert synthetic_clause(SIGNAL_COLUMNS, include_synthetic=True) is None


class TestATableThatCannotCarrySyntheticRows:
    def test_gets_no_clause_rather_than_a_refusal(self):
        """Unlike the tier guard this does NOT fail closed, and the
        difference is deliberate. A missing access_tier on a restricted table
        is a table that cannot enforce a rule it is subject to. A table with
        no source_class holds no signals — a catalogue, a configuration — and
        there is nothing to hold out."""
        assert synthetic_clause({"table_name", "rows"}) is None

    def test_which_is_what_keeps_the_generic_verbs_usable(self):
        """Most of gold is not the signals table. Refusing there would make
        `gold_aggregate` unusable on it."""
        for columns in ({"site", "value"}, set(), {"ts"}):
            assert synthetic_clause(columns) is None


class TestTheAnswerSaysItHeldSomethingOut:
    """Ben's rule for `suspect`, applied here: excluded by default, always
    counted. An answer that quietly dropped the install's own smoke test
    looks exactly like one that never met it."""

    def _described(self, include_synthetic=False):
        from ..gold_query import _window_and_filter

        _wheres, _params, described = _window_and_filter(
            SIGNAL_COLUMNS,
            window=None,
            filter="site='site-b'",
            access_tiers=("public",),
            table="signals",
            restricted_tables=(),
            include_synthetic=include_synthetic,
        )
        return " ".join(described)

    def test_the_provenance_names_the_exclusion(self):
        assert "basis != 'selftest'" in self._described()

    def test_and_does_not_when_they_were_included(self):
        assert "selftest" not in self._described(include_synthetic=True)


class TestTheSqlItCompilesTo:
    def test_the_predicate_reaches_the_where_clause(self):
        from ..gold_query import _window_and_filter

        wheres, params, _ = _window_and_filter(
            SIGNAL_COLUMNS,
            window=None,
            filter="site='site-b'",
            access_tiers=("public",),
            table="signals",
            restricted_tables=(),
        )
        sql = " ".join(wheres)
        assert BASIS_COLUMN in sql
        assert "!=" in sql
        # Bound, not interpolated — a source class named `'; --` is a value.
        assert SELFTEST_BASIS not in sql
        assert SELFTEST_BASIS in params

    def test_it_is_absent_when_included(self):
        from ..gold_query import _window_and_filter

        wheres, params, _ = _window_and_filter(
            SIGNAL_COLUMNS,
            window=None,
            filter="site='site-b'",
            access_tiers=("public",),
            table="signals",
            restricted_tables=(),
            include_synthetic=True,
        )
        assert SELFTEST_BASIS not in params
        assert BASIS_COLUMN not in " ".join(wheres)
