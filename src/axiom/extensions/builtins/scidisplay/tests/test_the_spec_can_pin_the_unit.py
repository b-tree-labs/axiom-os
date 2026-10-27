# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""A spec may say which unit to draw in.

The renderer can infer a prefix from the data, and left to itself it does. But
inference moves: the same chart over a quiet window infers milliwatts and over a
busy one infers megawatts, so a reader who zooms finds the axis has changed
underneath them, and a share link re-rendered after late rows arrive is not the
figure that was shared.

So the answer belongs in the document, next to the window it has to survive.
`display_units` maps a measured unit to the unit to draw it in. It is keyed by
UNIT and not by channel, because the axis is per unit: two channels in watts
share one axis, and keying by channel would let them disagree on it, which is
the whole failure this exists to prevent.

The spec does not check that `kW` is a prefixed `W`. This schema names no domain
noun and never learns what a value means; it carries the declaration and the
renderer refuses a bad one by name.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.chart_spec import (
    SCHEMA_VERSION,
    ChartSpec,
    SpecFormatError,
    Transform,
    Window,
    parse_document,
    parse_json,
)

WINDOW = Window(basis="absolute", fields={"from": "2026-09-22T14:00:00Z",
                                          "to": "2026-09-22T16:00:00Z"})
TRANSFORM = Transform(bucket="1m", agg="mean")


def _spec(**over):
    base = dict(kind="timeseries", site="s", channels=("Power",),
                window=WINDOW, transform=TRANSFORM, title="t")
    base.update(over)
    return ChartSpec(**base)


class TestItIsCarried:
    def test_a_spec_accepts_it(self):
        assert _spec(display_units={"W": "kW"}).display_units == {"W": "kW"}

    def test_it_round_trips(self):
        spec = _spec(display_units={"W": "kW", "degC": "degC"})
        assert parse_json(spec.to_json()) == spec

    def test_it_changes_the_spec_id(self):
        """A figure in kilowatts is not the same figure as one in megawatts, so
        it cannot cite the same id."""
        assert _spec(display_units={"W": "kW"}).spec_id != _spec().spec_id

    def test_a_spec_without_one_emits_no_key(self):
        """Absent stays absent, so documents written before this field round
        trip byte-identically."""
        assert "display_units" not in _spec().to_document()


class TestWhatIsRefused:
    def test_an_empty_mapping(self):
        """It renders identically to absent, and a field that diffs as
        different while drawing the same thing is not diffable."""
        with pytest.raises(SpecFormatError, match="display_units"):
            _spec(display_units={})

    def test_a_non_mapping(self):
        with pytest.raises(SpecFormatError, match="display_units"):
            _spec(display_units=["W", "kW"])

    def test_an_empty_unit_name(self):
        with pytest.raises(SpecFormatError, match="display_units"):
            _spec(display_units={"W": ""})

    def test_a_kind_with_no_value_axis(self):
        """A state panel draws spans, not values. A display unit there would
        draw nothing while diffing as though it mattered."""
        with pytest.raises(SpecFormatError, match="display_units"):
            ChartSpec(kind="state", site="s", channels=("Mode",), window=WINDOW,
                      title="t", display_units={"W": "kW"})

    def test_an_unknown_key_is_still_unknown(self):
        with pytest.raises(SpecFormatError):
            parse_document({"schema_version": SCHEMA_VERSION, "kind": "timeseries",
                            "site": "s", "display_unit": {"W": "kW"}})


class TestTheSchemaSaysSoOutLoud:
    def test_the_version_moved(self):
        """A reader that does not know this field must refuse the document
        rather than drop it, which is what the version is for."""
        assert SCHEMA_VERSION == "1.1"

    def test_a_document_written_at_1_0_still_reads(self):
        doc = {"schema_version": "1.0", "kind": "timeseries", "site": "s",
               "channels": ["Power"],
               "window": {"from": "2026-09-22T14:00:00Z", "to": "2026-09-22T16:00:00Z"},
               "transform": {"bucket": "1m", "agg": "mean"}, "title": "t"}
        assert parse_document(doc).display_units is None

    def test_and_keeps_its_own_version_through_a_round_trip(self):
        doc = {"schema_version": "1.0", "kind": "timeseries", "site": "s",
               "channels": ["Power"],
               "window": {"from": "2026-09-22T14:00:00Z", "to": "2026-09-22T16:00:00Z"},
               "transform": {"bucket": "1m", "agg": "mean"}, "title": "t"}
        assert parse_document(doc).to_document() == doc


class TestComparisonTakesItToo:
    def test_measured_against_modelled_shares_one_axis(self):
        spec = ChartSpec(kind="comparison", site="s", channels=("Power",),
                         window=WINDOW, transform=TRANSFORM, title="t",
                         display_units={"W": "kW"})
        assert spec.display_units == {"W": "kW"}


class TestASpecCanPinAColourToo:
    """`display_units` is keyed by UNIT because a scale belongs to an axis.
    `series_colours` is keyed by CHANNEL because a colour belongs to a line."""

    def test_it_is_carried(self):
        assert _spec(series_colours={"Power": "#0072b2"}).series_colours == {
            "Power": "#0072b2"
        }

    def test_it_round_trips(self):
        spec = _spec(series_colours={"Power": "#0072b2", "T": "red"})
        assert parse_json(spec.to_json()) == spec

    def test_it_changes_the_spec_id(self):
        assert _spec(series_colours={"Power": "red"}).spec_id != _spec().spec_id

    def test_an_empty_mapping_is_refused(self):
        with pytest.raises(SpecFormatError, match="series_colours"):
            _spec(series_colours={})

    def test_a_kind_with_no_lines_refuses_it(self):
        with pytest.raises(SpecFormatError, match="series_colours"):
            ChartSpec(kind="state", site="s", channels=("Mode",), window=WINDOW,
                      title="t", series_colours={"Mode": "red"})

    def test_the_schema_does_not_check_the_colour(self):
        """It names no domain noun and never learns what a value means. A
        colour nobody can resolve is refused by the renderer, by name."""
        assert _spec(series_colours={"Power": "not-a-colour"}).series_colours
