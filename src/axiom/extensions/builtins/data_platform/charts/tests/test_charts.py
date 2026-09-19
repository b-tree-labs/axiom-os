# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The catalog's job is to stop a picture lying about what it is made of."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.charts import (
    CHART_KINDS,
    ChartRegistry,
    ChartSpec,
    ChartSpecError,
)


def _spec(**over) -> ChartSpec:
    base = dict(
        chart_id="fluid.temp",
        name="Fluid temperature",
        kind="timeseries",
        provenance="measured",
        roles=("fluid_temperature",),
        unit="degC",
        unit_authority="site channel map",
        verb="series",
    )
    base.update(over)
    return ChartSpec(**base)


# --- the labelling rule -----------------------------------------------------


def test_a_measured_chart_drawing_a_prediction_is_refused():
    """The failure this module exists to prevent.

    Nothing downstream can tell a model's output from a reading once it has
    been drawn on the same axes with the same styling, so the objection has to
    be raised at the moment of drawing.
    """
    spec = _spec(roles=("fluid_temperature", "rom_fluid_temperature"))
    errors = spec.check_inputs({
        "fluid_temperature": "measured",
        "rom_fluid_temperature": "predicted",
    })
    assert errors and "must never render as a measurement" in errors[0]


def test_simulated_input_also_disqualifies_a_measured_chart():
    """Synthetic data has never been a measurement either.

    The flow-loop sim rows sat in silver stamped `measured` for exactly as long
    as the vocabulary had nowhere else to put them.
    """
    spec = _spec()
    errors = spec.check_inputs({"fluid_temperature": "simulated"})
    assert errors and "simulated" in errors[0]


def test_a_hybrid_chart_may_draw_both():
    spec = _spec(provenance="hybrid", roles=("fluid_temperature", "rom_fluid_temperature"))
    assert spec.check_inputs({
        "fluid_temperature": "measured",
        "rom_fluid_temperature": "predicted",
    }) == []


def test_overclaiming_model_derived_is_flagged_too():
    """A label that overstates gets discounted, and then so do the true ones."""
    spec = _spec(provenance="model_derived")
    errors = spec.check_inputs({"fluid_temperature": "measured"})
    assert errors and "overstates" in errors[0]


def test_a_parity_plot_cannot_call_itself_measured():
    with pytest.raises(ChartSpecError, match="mislabelling"):
        ChartRegistry().register(_spec(
            chart_id="crh.parity", kind="measured_vs_predicted",
            provenance="measured", min_series=2,
        ))


# --- the catalog's own integrity --------------------------------------------


def test_a_kind_nothing_can_draw_is_refused():
    with pytest.raises(ChartSpecError, match="not one of"):
        ChartRegistry().register(_spec(kind="sankey"))


def test_a_unit_with_no_authority_is_refused():
    """An axis label nobody can trace is how a wrong unit survives."""
    with pytest.raises(ChartSpecError, match="unit_authority"):
        ChartRegistry().register(_spec(unit_authority=None))


def test_ids_are_unique_because_they_are_references():
    reg = ChartRegistry()
    reg.register(_spec())
    with pytest.raises(ChartSpecError, match="already registered"):
        reg.register(_spec(name="Something else"))


def test_a_chart_with_nothing_to_plot_is_refused():
    with pytest.raises(ChartSpecError, match="nothing to plot"):
        ChartRegistry().register(_spec(roles=(), channels=()))


def test_a_parity_plot_needs_two_series():
    with pytest.raises(ChartSpecError, match="at least two series"):
        ChartRegistry().register(_spec(
            chart_id="p", kind="measured_vs_predicted", provenance="hybrid", min_series=1,
        ))


# --- portability, which is what a peer comparison is built from -------------


def test_role_based_charts_are_portable_and_channel_based_ones_are_not():
    assert _spec().portable
    assert not _spec(roles=(), channels=("STC1",)).portable
    # naming a channel alongside roles pins it to one site, deliberately
    assert not _spec(channels=("STC1",)).portable


def test_drawable_with_does_not_offer_a_chart_that_would_come_back_empty():
    """An empty chart reads as broken data, not as missing coverage."""
    reg = ChartRegistry()
    reg.register(_spec())
    reg.register(_spec(
        chart_id="wall.vs.fluid", name="Wall vs fluid",
        roles=("fluid_temperature", "wall_temperature"),
    ))
    only_fluid = [s.chart_id for s in reg.drawable_with(["fluid_temperature"])]
    assert only_fluid == ["fluid.temp"]
    both = {s.chart_id for s in reg.drawable_with(["fluid_temperature", "wall_temperature"])}
    assert both == {"fluid.temp", "wall.vs.fluid"}


def test_the_catalog_reports_its_own_verb_gaps():
    """A chart with no verb is a data job, not a rendering job.

    Confusing the two sends someone to build a picture for numbers nobody can
    fetch.
    """
    reg = ChartRegistry()
    reg.register(_spec())
    reg.register(_spec(chart_id="pulses", name="Pulse catalog", kind="event_table",
                       roles=(), channels=("Mode",), unit=None, unit_authority=None,
                       verb=None))
    assert [s.chart_id for s in reg.verb_gaps()] == ["pulses"]
    assert reg.verbs_required() == {"series": ["fluid.temp"]}


def test_every_kind_is_reachable_and_documented():
    """A vocabulary entry nothing uses is a promise nobody keeps."""
    assert len(set(CHART_KINDS)) == len(CHART_KINDS)
    assert all(k.islower() and " " not in k for k in CHART_KINDS)


# --- grouping and like-for-like comparison ---------------------------------


def test_groups_come_back_in_reading_order_not_alphabetical():
    """operations before parity before spatial is how someone reads a plant."""
    reg = ChartRegistry()
    reg.register(_spec(chart_id="flux", name="Flux", kind="field_2d",
                       provenance="model_derived", group="spatial", verb=None))
    reg.register(_spec(chart_id="power", name="Power", group="operations"))
    reg.register(_spec(chart_id="wallfluid", name="Wall vs fluid", group="thermal"))
    assert list(reg.grouped()) == ["operations", "thermal", "spatial"]


def test_order_decides_layout_within_a_group():
    reg = ChartRegistry()
    reg.register(_spec(chart_id="second", name="Second", order=20))
    reg.register(_spec(chart_id="first", name="First", order=10))
    assert [s.chart_id for s in reg.grouped()["operations"]] == ["first", "second"]


def test_defaults_are_opt_in():
    """A default view crowded with everything is the same failure as no grouping."""
    reg = ChartRegistry()
    reg.register(_spec(chart_id="shown", name="Shown", default=True))
    reg.register(_spec(chart_id="hidden", name="Hidden"))
    assert [s.chart_id for s in reg.defaults()] == ["shown"]


def test_an_unknown_group_is_refused():
    with pytest.raises(ChartSpecError, match="group"):
        ChartRegistry().register(_spec(group="misc"))


def test_comparable_finds_the_sites_a_portable_chart_can_be_drawn_for():
    """This is the apples-to-apples question, answered from roles alone."""
    reg = ChartRegistry()
    reg.register(_spec(chart_id="wall.vs.fluid", name="Wall vs fluid",
                       roles=("fluid_temperature", "wall_temperature")))
    sites = {
        "vcu-flowloop": ["fluid_temperature", "wall_temperature", "tank_temperature"],
        "tamu-bubbleloop": ["fluid_temperature", "wall_temperature"],
        "acu-flowloop": ["fluid_temperature"],           # no wall channels mapped yet
    }
    assert reg.comparable("wall.vs.fluid", sites) == ["tamu-bubbleloop", "vcu-flowloop"]


def test_a_site_specific_chart_is_never_offered_for_comparison():
    """Answering it for another site would mean pretending two instruments match."""
    reg = ChartRegistry()
    reg.register(_spec(chart_id="vcu.panel", name="VCU panel", roles=(),
                       channels=("STC1", "PTC1")))
    sites = {"vcu-flowloop": ["fluid_temperature"], "tamu-bubbleloop": ["fluid_temperature"]}
    assert reg.comparable("vcu.panel", sites) == []
