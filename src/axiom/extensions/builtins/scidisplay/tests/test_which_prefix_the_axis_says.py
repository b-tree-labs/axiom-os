# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""Watts, kilowatts or megawatts.

A prefix exists for one reason: so the numbers on the axis are ones somebody
would say out loud. "946 kW" is how an operator says it; "0.946 MW" is the same
quantity said badly, and it leads with a zero, which is precisely what the
prefix was supposed to remove.

Two things decide it. If the caller DECLARED a unit to display in, that wins —
the question being asked establishes it, a panel has to match its neighbour, a
site says power is in kilowatts. If nobody declared one, it is inferred from the
data, so the largest reading lands between 1 and 1000.

Inferred from the DATA, never from the axis bounds. The bounds are padded, and a
maximum sitting within a few percent below a decade gets pushed over it: 946,000
watts became "0.946 MW" because the padded bound crossed a million, not because
any reading did.

And once chosen, it holds. A reader who zooms into a quiet stretch must not find
the axis has silently changed from megawatts to milliwatts underneath them.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

T0 = datetime(2026, 9, 22, tzinfo=UTC)


def _pts(values):
    return [(T0 + timedelta(minutes=i), v) for i, v in enumerate(values)]


def _axis_unit(svg: str) -> str:
    found = re.findall(r'letter-spacing="0.05em">([^<]+)<', svg)
    return found[0] if found else ""


def _label_reading(svg: str) -> str:
    """The direct label's value, which must agree with the axis."""
    return re.findall(r'font-size="[\d.]+" ?>([^<]+)</tspan>', svg)[0]


class TestInferredFromTheDataNotTheBounds:
    @pytest.mark.parametrize(("top", "expected"), [
        (946_000.0, "kW"),    # padded to 1,021,680 and wrongly read as MW
        (999.0, "W"),         # padded to 1,078.9 and wrongly read as kW
        (1_030_000.0, "MW"),
        (1_100.0, "kW"),
        (0.00021, "µW"),
        (919_000.0, "kW"),
    ])
    def test_the_prefix_follows_the_largest_reading(self, top, expected):
        svg = render_svg([Series(name="P", points=_pts([top / 1000, top]), unit="W")],
                         title="t")
        assert _axis_unit(svg) == expected

    def test_the_label_agrees_with_the_axis(self):
        svg = render_svg([Series(name="P", points=_pts([1.0, 946_000.0]), unit="W")],
                         title="t")
        assert _axis_unit(svg) == "kW"
        assert "MW" not in svg


class TestDeclaredBeatsInferred:
    def test_a_declared_unit_is_used(self):
        svg = render_svg([Series(name="P", points=_pts([1.0, 1_030_000.0]), unit="W")],
                         title="t", display_units={"W": "kW"})
        assert _axis_unit(svg) == "kW"

    def test_declaring_the_bare_unit_turns_the_prefix_off(self):
        svg = render_svg([Series(name="P", points=_pts([1.0, 1_030_000.0]), unit="W")],
                         title="t", display_units={"W": "W"})
        assert _axis_unit(svg) == "W"

    def test_the_direct_label_follows_the_declaration_too(self):
        """An axis in kW beside a label in MW is the small inconsistency that
        makes a reader stop trusting the figure."""
        svg = render_svg([Series(name="P", points=_pts([1.0, 1_030_000.0]), unit="W")],
                         title="t", display_units={"W": "kW"})
        assert "MW" not in svg

    def test_a_declaration_for_another_unit_is_ignored_not_applied(self):
        svg = render_svg([Series(name="T", points=_pts([20.0, 30.0]), unit="degC")],
                         title="t", display_units={"W": "MW"})
        assert _axis_unit(svg) == "degC"

    def test_a_declaration_that_is_not_this_unit_is_refused(self):
        """Silently ignoring it is the worst outcome: the caller asked for
        kilowatts and the figure would quietly answer in something else."""
        with pytest.raises(ValueError, match="kJ"):
            render_svg([Series(name="P", points=_pts([1.0, 2.0]), unit="W")],
                       title="t", display_units={"W": "kJ"})

    def test_an_unknown_prefix_is_refused(self):
        with pytest.raises(ValueError, match="zW"):
            render_svg([Series(name="P", points=_pts([1.0, 2.0]), unit="W")],
                       title="t", display_units={"W": "zW"})

    def test_a_prefix_on_a_unit_that_takes_none_is_refused(self):
        """"kpct" is not a unit anybody uses."""
        with pytest.raises(ValueError, match="percent"):
            render_svg([Series(name="N", points=_pts([1.0, 2.0]), unit="percent")],
                       title="t", display_units={"percent": "kpercent"})

    def test_the_second_axis_can_be_declared_too(self):
        svg = render_svg(
            [Series(name="P", points=_pts([1.0, 1_030_000.0]), unit="W"),
             Series(name="T", points=_pts([20.0, 30.0]), unit="degC")],
            title="t", secondary_unit="degC", display_units={"W": "kW"})
        assert sorted(re.findall(r'letter-spacing="0.05em">([^<]+)<', svg)) == ["degC", "kW"]


class TestItHoldsWhenTheWindowChanges:
    """Zooming must not change the unit under the reader. A window is chosen by
    a person moving a scroll wheel; the axis unit is not."""

    def test_two_windows_of_one_series_can_disagree_when_inferred(self):
        wide = render_svg([Series(name="P", points=_pts([1e-4, 1_030_000.0]), unit="W")],
                          title="t")
        quiet = render_svg([Series(name="P", points=_pts([1e-4, 3e-4]), unit="W")],
                           title="t")
        assert _axis_unit(wide) != _axis_unit(quiet)

    def test_and_agree_once_it_is_declared(self):
        kw = {"display_units": {"W": "MW"}}
        wide = render_svg([Series(name="P", points=_pts([1e-4, 1_030_000.0]), unit="W")],
                          title="t", **kw)
        quiet = render_svg([Series(name="P", points=_pts([1e-4, 3e-4]), unit="W")],
                           title="t", **kw)
        assert _axis_unit(wide) == _axis_unit(quiet) == "MW"


class TestALogAxisStillKeepsItBare:
    def test_inferred(self):
        svg = render_svg([Series(name="P", points=_pts([1e-4, 1.1e6]), unit="W")],
                         title="t", log_units=("W",))
        assert _axis_unit(svg) == "W"

    def test_and_a_declaration_cannot_put_one_back(self):
        """Across ten decades a prefix only shifts every exponent by a constant.
        Declaring one would label the bottom of the axis 1e-10."""
        with pytest.raises(ValueError, match="logarithmic"):
            render_svg([Series(name="P", points=_pts([1e-4, 1.1e6]), unit="W")],
                       title="t", log_units=("W",), display_units={"W": "MW"})
