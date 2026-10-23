# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Scaffolding is not a result.

Ben, on finding two sites drawn from one synthetic producer: "I view this
not as simulated data, maybe it is, but it's simulated test fixture data.
And it's an important distinction. So we have two different kinds of
simulated data. We have physics simulated and we have test fixture
generated."

`source_class` cannot carry that difference: both are `simulated`, because
both are numbers a model produced rather than an instrument. What differs is
whether anybody meant it — a property of the PRODUCER.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.provenance import (
    SHOW_EXCLUDE,
    SHOW_INCLUDE,
    SHOW_ONLY,
    declared_fixtures,
    is_fixture,
    sql_predicate,
)

SITE = ("flowloop-sim/*",)


class TestNothingIsScaffoldingUntilSomebodySaysSo:
    def test_an_undeclared_deployment_has_no_fixtures(self):
        # A platform that guessed would eventually guess that somebody's real
        # model was scaffolding and drop it from a figure without saying so.
        assert declared_fixtures({}) == ()
        assert is_fixture("flowloop-sim/instrument-v0", ()) is False

    def test_a_measurement_can_never_be_scaffolding(self):
        # No model_ref means no model: an instrument produced it.
        assert is_fixture(None, SITE) is False
        assert is_fixture("", SITE) is False

    def test_a_physics_model_is_not_scaffolding_either(self):
        # The distinction this exists to draw. Both are `simulated`.
        #
        # The producer id is deliberately a generic one. It named a real
        # controlled solver, which the governance guard refuses in the
        # substrate and is right to: the registry mechanism is the platform's,
        # the entries belong to the domain, and a controlled artifact's name is
        # the one thing that must not travel with the mechanism. The assertion
        # does not depend on which model it is — only that a model is not
        # scaffolding.
        assert is_fixture("corral:physics-model-v1", SITE) is False
        assert is_fixture("flowloop-sim/instrument-v0", SITE) is True

    def test_a_family_is_one_line(self):
        assert is_fixture("flowloop-sim/thermal-v3", SITE) is True

    def test_an_operator_cannot_undeclare_what_a_consumer_ships(self):
        # A site that ships its own simulator knows it is one. The
        # environment ADDS; it does not replace.
        got = declared_fixtures({"AXIOM_FIXTURE_MODELS": "other/*"}, extra=SITE)
        assert set(got) == {"flowloop-sim/*", "other/*"}

    def test_the_same_pattern_twice_is_one_pattern(self):
        got = declared_fixtures({"AXIOM_FIXTURE_MODELS": "flowloop-sim/*"}, extra=SITE)
        assert got == ("flowloop-sim/*",)


class TestTheDatabaseDoesTheFiltering:
    def test_excluding_keeps_every_row_that_has_no_model(self):
        # `NOT (NULL LIKE …)` is NULL, and a WHERE clause treats NULL as
        # false — so an unqualified NOT would silently drop every measured
        # reading in the table. That is the whole reason this is a function.
        clause, params = sql_predicate(SHOW_EXCLUDE, SITE)
        assert "model_ref IS NULL OR NOT" in clause
        assert params == ["flowloop-sim/%"]

    def test_only_keeps_nothing_else(self):
        clause, params = sql_predicate(SHOW_ONLY, SITE)
        assert clause == "(model_ref LIKE %s)"
        assert params == ["flowloop-sim/%"]

    def test_including_restricts_nothing(self):
        assert sql_predicate(SHOW_INCLUDE, SITE) == ("", [])

    def test_a_deployment_with_no_declaration_is_unrestricted_in_every_mode(self):
        for show in (SHOW_EXCLUDE, SHOW_INCLUDE, SHOW_ONLY):
            assert sql_predicate(show, ()) == ("", [])

    def test_a_declaration_is_a_producer_name_and_not_a_regex(self):
        # `%` and `_` are LIKE's own wildcards. A producer called `v1_0`
        # must not match `v1X0`.
        clause, params = sql_predicate(SHOW_ONLY, ("sim/v1_0",))
        assert params == [r"sim/v1\_0"]

    def test_it_names_the_column_the_caller_uses(self):
        clause, _ = sql_predicate(SHOW_ONLY, SITE, column="s.model_ref")
        assert "s.model_ref LIKE" in clause

    def test_an_unknown_mode_is_refused_rather_than_guessed(self):
        # Getting this wrong means serving scaffolding as a result, so it
        # fails rather than falling back to a default.
        with pytest.raises(ValueError, match="show must be one of"):
            sql_predicate("hide", SITE)
