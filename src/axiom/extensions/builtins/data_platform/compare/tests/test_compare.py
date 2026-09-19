# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What a comparison must refuse to do quietly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.data_platform.compare import parity, series_compare

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _row(i, value, **over):
    row = {
        "ts": T0 + timedelta(seconds=i),
        "value": value,
        "channel": "ch",
        "unit": "degC",
        "source_class": "measured",
        "derivation": "raw",
    }
    row.update(over)
    return row


# --- series_compare ---------------------------------------------------------


def test_two_loops_compare_on_a_role_without_either_being_renamed():
    """The payoff of the role column: STC1 and Tc_01 line up as one question."""
    result = series_compare("fluid_temperature", {
        "vcu-flowloop": [_row(0, 500.0, channel="STC1"), _row(1, 501.0, channel="STC1")],
        "tamu-bubbleloop": [_row(0, 498.0, channel="Tc_01"), _row(1, 499.0, channel="Tc_01")],
    })
    assert result.comparable
    assert {s.site for s in result.series} == {"vcu-flowloop", "tamu-bubbleloop"}
    assert {s.channel for s in result.series} == {"STC1", "Tc_01"}
    assert result.warnings == []


def test_a_simulated_site_is_excluded_and_says_so():
    """Silently omitting it reads as 'no data', which calls for a different fix."""
    result = series_compare("fluid_temperature", {
        "vcu-flowloop": [_row(0, 500.0)],
        "tamu-flowloop": [_row(0, 8.6, source_class="simulated")],
    })
    assert [s.site for s in result.series] == ["vcu-flowloop"]
    assert "simulated" in result.excluded["tamu-flowloop"]
    assert not result.comparable  # one site is not a comparison


def test_simulated_can_be_asked_for_explicitly():
    result = series_compare("fluid_temperature", {
        "a": [_row(0, 1.0)], "b": [_row(0, 2.0, source_class="simulated")],
    }, include_simulated=True)
    assert {s.site for s in result.series} == {"a", "b"}


def test_derived_channels_are_left_out_so_physics_is_not_counted_twice():
    """T_lm is computed from the thermocouples in the same stream."""
    result = series_compare("temperature_difference", {
        "tamu-bubbleloop": [_row(0, 12.0, channel="T_lm", derivation="derived")],
    })
    assert result.series == ()
    assert "derived" in result.excluded["tamu-bubbleloop"]


def test_mixed_units_are_refused_rather_than_plotted():
    """One axis, two quantities, is a wrong answer that looks right."""
    result = series_compare("fluid_temperature", {
        "a": [_row(0, 500.0, unit="degC")],
        "b": [_row(0, 773.0, unit="K")],
    })
    assert not result.comparable
    assert any("different units" in w for w in result.warnings)


def test_a_site_with_no_rows_is_named_not_omitted():
    result = series_compare("fluid_temperature", {"a": [_row(0, 1.0)], "b": []})
    assert result.excluded["b"] == "no rows in this window"


# --- parity -----------------------------------------------------------------


def _pred(i, value, **over):
    return _row(i, value, source_class="predicted",
                model_ref=over.pop("model_ref", "rom@1.2.0"), **over)


def test_the_pair_is_never_called_a_measurement():
    result = parity([_row(0, 100.0)], [_pred(0, 102.0)])
    assert result.provenance == "hybrid"


def test_the_residual_sign_is_stated_not_guessed():
    """Positive means the model is running high."""
    (point,) = parity([_row(0, 100.0)], [_pred(0, 102.0)]).paired
    assert point.residual == 2.0


def test_points_with_no_counterpart_are_counted_not_dropped():
    """A residual over only the overlap is the most flattering possible view."""
    result = parity(
        [_row(0, 100.0), _row(10, 101.0), _row(20, 102.0)],
        [_pred(0, 100.5)],
    )
    assert len(result.paired) == 1
    assert result.unpaired_measured == 2
    assert result.coverage == 1 / 3
    assert any("reads as agreement" in w for w in result.warnings)


def test_pairing_respects_the_tolerance():
    far = parity([_row(0, 100.0)], [_pred(30, 100.0)], tolerance=timedelta(seconds=1))
    assert far.paired == () and far.unpaired_measured == 1 and far.unpaired_predicted == 1

    near = parity([_row(0, 100.0)], [_pred(30, 100.0)], tolerance=timedelta(seconds=60))
    assert len(near.paired) == 1


def test_each_predicted_point_is_used_at_most_once():
    """Otherwise one prediction flatters several measurements."""
    result = parity(
        [_row(0, 100.0), _row(1, 100.0)],
        [_pred(0, 100.0)],
        tolerance=timedelta(seconds=5),
    )
    assert len(result.paired) == 1
    assert result.unpaired_measured == 1


def test_a_residual_with_no_model_ref_is_flagged():
    """It cannot be attributed to a model version, so it cannot be retired."""
    result = parity([_row(0, 100.0)], [_row(0, 102.0, source_class="predicted", model_ref=None)])
    assert any("cannot be reproduced" in w for w in result.warnings)


def test_a_clean_parity_run_warns_about_nothing():
    result = parity(
        [_row(i, 100.0 + i) for i in range(5)],
        [_pred(i, 100.5 + i) for i in range(5)],
    )
    assert result.coverage == 1.0
    assert result.warnings == []
    assert result.model_ref == "rom@1.2.0"
    assert len(result.residuals()) == 5
