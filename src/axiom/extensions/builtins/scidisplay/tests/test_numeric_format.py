# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""How many digits to show is a scientific question, not a formatting one.

The table renderer formatted every float with ``:g`` — six significant
figures. That is wrong in both directions at once, and both are the kind of
wrong that looks fine.

**It invents precision.** Padding 51.2 to 51.200 asserts the instrument
resolved thousandths. It did not, and a reader cannot tell the difference
between a digit that was measured and a zero that was added.

**It destroys precision.** ``f"{1234.5678:g}"`` is ``'1234.57'``. Two digits
the source carried are gone. A reader who copies that number into a
calculation has been handed a value that is wrong by more than the
instrument's own error, and every subsequent step compounds it. Display
rounding is a contribution to aggregate uncertainty that nobody budgeted
for and nobody can see.

The principles this encodes:

1. **Never show a digit the value does not have.** The shortest
   representation that round-trips is exactly the set of digits the value
   carries — no more.
2. **Never drop a digit the value does have**, unless something says the
   digit is not meaningful.
3. **Uncertainty is what says so.** A reading of 51.2456 ± 0.01 is
   significant to the hundredths; showing more is noise and showing less is
   loss. Where an uncertainty is carried, it governs.
4. **A column is read down, not across.** Aligning on the decimal point
   lets each value keep its own precision while the digits still line up —
   which is how a scientific table has always been set, and why it does not
   need to pad every cell to a common width.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay import numeric_format as nf


class TestItDoesNotInventPrecision:
    @pytest.mark.parametrize("value,shown", [
        (51.2, "51.2"),
        (45.0, "45"),
        (0.5, "0.5"),
        (100.0, "100"),
    ])
    def test_no_trailing_zeros_are_added(self, value, shown):
        """A zero a reader cannot distinguish from a measured digit."""
        assert nf.format_number(value) == shown

    def test_an_integer_valued_float_is_not_dressed_as_a_measurement(self):
        assert nf.format_number(45.0) == "45"


class TestItDoesNotDestroyPrecision:
    def test_the_case_that_started_this(self):
        """f"{1234.5678:g}" is '1234.57'. Two digits, silently gone."""
        assert nf.format_number(1234.5678) == "1234.5678"

    @pytest.mark.parametrize("value", [
        1234.5678, 0.123456789, 98765.4321, 1.0000001,
    ])
    def test_every_digit_round_trips(self, value):
        """The displayed number must parse back to the same value. A display
        a reader can retype is a display that has not silently changed the
        data."""
        assert float(nf.format_number(value)) == value

    def test_more_than_six_significant_figures_survive(self):
        assert nf.format_number(123456.789) == "123456.789"


class TestUncertaintyGovernsWhenItIsKnown:
    """A reading of 51.2456 ± 0.01 is significant to the hundredths."""

    def test_the_value_is_rounded_to_the_uncertainty(self):
        assert nf.format_number(51.2456, uncertainty=0.01) == "51.25"

    def test_a_coarser_uncertainty_shows_fewer_digits(self):
        assert nf.format_number(51.2456, uncertainty=0.5) == "51.2"

    def test_a_finer_uncertainty_shows_more(self):
        assert nf.format_number(51.2456, uncertainty=0.0001) == "51.2456"

    def test_zero_uncertainty_does_not_mean_infinite_precision(self):
        """It means unstated, not exact. Fall back rather than printing
        seventeen digits as though the instrument were perfect."""
        assert nf.format_number(51.2456, uncertainty=0.0) == "51.2456"

    def test_a_nonsense_uncertainty_is_ignored_rather_than_obeyed(self):
        assert nf.format_number(51.2456, uncertainty=float("nan")) == "51.2456"


class TestVeryLargeAndVerySmall:
    def test_a_small_number_does_not_become_zero(self):
        """Rounding a real reading to 0 is the worst available answer."""
        assert float(nf.format_number(0.0000001234)) == pytest.approx(0.0000001234)

    def test_a_large_number_is_not_shown_in_full_digits_forever(self):
        shown = nf.format_number(1.23e18)
        assert "e" in shown.lower(), f"got {shown}"
        assert float(shown) == 1.23e18

    def test_scientific_notation_still_round_trips(self):
        for v in (1e-12, 6.022e23, -3.5e-9):
            assert float(nf.format_number(v)) == v


class TestThingsThatAreNotNumbers:
    def test_none_is_a_dash_not_a_zero(self):
        assert nf.format_number(None) == "—"

    def test_nan_says_nan_rather_than_pretending(self):
        assert nf.format_number(float("nan")).lower() == "nan"

    def test_infinity_is_shown_as_infinity(self):
        assert "inf" in nf.format_number(float("inf")).lower()

    def test_a_bool_is_not_rendered_as_a_number(self):
        """True is an int in Python and a state everywhere else."""
        assert nf.format_number(True) in {"true", "True"}

    def test_an_integer_keeps_its_exact_value(self):
        assert nf.format_number(10**20) == str(10**20)


class TestAColumnIsReadDown:
    """Aligning on the decimal point lets each value keep its own precision
    while the digits still line up."""

    def test_the_decimal_points_line_up(self):
        cells = nf.align_column([51.24, 12.4, 45.0, 1234.5678, None])
        dots = [c.index(".") for c in cells if "." in c]
        assert len(set(dots)) == 1, cells

    def test_no_value_is_padded_with_invented_digits(self):
        cells = nf.align_column([51.24, 12.4, 45.0])
        stripped = [c.strip() for c in cells]
        assert "12.4" in stripped and "12.40" not in stripped
        assert "45" in stripped

    def test_every_cell_is_the_same_width(self):
        cells = nf.align_column([51.24, 12.4, 45.0, 1234.5678, None])
        assert len({len(c) for c in cells}) == 1, cells

    def test_an_absent_value_takes_a_dash_and_still_aligns(self):
        cells = nf.align_column([1.5, None])
        assert "—" in cells[1]
        assert len(cells[0]) == len(cells[1])

    def test_a_column_of_integers_needs_no_decimal_point(self):
        cells = nf.align_column([1, 22, 333])
        assert "." not in "".join(cells)

    def test_an_empty_column_is_not_an_error(self):
        assert nf.align_column([]) == []
