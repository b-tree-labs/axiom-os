# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Correlation must survive a boundary, and must not be invented at one.

The `uncertainties` package tracks correlation through in-process object
identity, which cannot cross a database write, an MCP call, or a node.
These tests are the proof that a NAME can, and — more importantly — that
two nodes' identically-named sources do not silently become one.
"""

from __future__ import annotations

import json

import pytest

from axiom.uncertainty import Quantity, add, correlation, difference, mean
from axiom.uncertainty.wire import (
    WIRE_VERSION,
    WireError,
    alias,
    check_origin,
    from_wire,
    is_qualified,
    local_part,
    origin_of,
    qualify,
    to_wire,
)

SITE_A = "@site-alpha"
SITE_B = "@site-beta"


def _reading(value: float, *, cal: float = 0.5, rep: float = 0.1, channel: str = "tc-14"):
    return Quantity(
        value=value,
        unit="degC",
        terms={
            "signals:cal-bath-a:offset": cal,
            f"signals:{channel}:repeatability": rep,
        },
    )


class TestCorrelationSurvivesTheRoundTrip:
    """What no library gives: a correlation that is a name."""

    def test_a_quantity_survives_json_intact(self):
        original = _reading(21.4)
        revived = from_wire(json.loads(json.dumps(to_wire(original, origin=SITE_A))))

        assert revived.value == pytest.approx(original.value)
        assert revived.unit == original.unit
        assert revived.u == pytest.approx(original.u)
        assert len(revived.terms) == len(original.terms)

    def test_two_quantities_stay_correlated_through_the_wire(self):
        """The load-bearing property.

        Serialise two readings that share a calibration bath, revive them as
        a peer would, and they must still know they share it. In-process
        object identity cannot do this; a shared symbol can.
        """
        a, b = _reading(21.4), _reading(21.9)
        assert correlation(a, b) == pytest.approx(1.0)

        wire = json.dumps([to_wire(a, origin=SITE_A), to_wire(b, origin=SITE_A)])
        ra, rb = (from_wire(p) for p in json.loads(wire))

        assert correlation(ra, rb) == pytest.approx(correlation(a, b))

    def test_a_shared_systematic_still_cancels_after_a_round_trip(self):
        """Two sensors on one bath differ more precisely than either is
        known. That has to remain true on the far side of the wire, or a
        peer computing a difference overstates its uncertainty."""
        a = _reading(21.4, channel="tc-14")
        b = _reading(21.9, channel="tc-15")
        local = difference(a, b)

        ra = from_wire(to_wire(a, origin=SITE_A))
        rb = from_wire(to_wire(b, origin=SITE_A))
        remote = difference(ra, rb)

        assert remote.u == pytest.approx(local.u)
        # The bath cancelled; only the two repeatabilities remain.
        assert remote.u == pytest.approx((0.1**2 + 0.1**2) ** 0.5)

    def test_a_mean_over_revived_readings_cannot_average_the_bath_away(self):
        readings = [from_wire(to_wire(_reading(21.0 + i * 0.1), origin=SITE_A)) for i in range(400)]
        got = mean(readings)
        # The bath is shared, so it stays at 0.5 however many readings arrive.
        assert got.low >= 0.5
        assert got.exact


class TestTwoNodesDoNotSilentlyBecomeOne:
    """The collision that would make a federated answer WRONG.

    Both sites mint `signals:tc-14:repeatability` for DIFFERENT physical
    sensors. Bare symbols would make the algebra conclude they share a
    source — which narrows, and narrow is the direction that is wrong
    rather than merely unhelpful.
    """

    def test_identical_local_symbols_from_different_origins_are_uncorrelated(self):
        mine = from_wire(to_wire(_reading(21.4), origin=SITE_A))
        theirs = from_wire(to_wire(_reading(21.4), origin=SITE_B))

        # Same local names, every one of them.
        assert {local_part(s) for s in mine.terms} == {local_part(s) for s in theirs.terms}
        # And zero shared sources, so zero correlation.
        assert set(mine.terms).isdisjoint(theirs.terms)
        assert correlation(mine, theirs) == pytest.approx(0.0)

    def test_a_cross_origin_difference_does_not_cancel_anything(self):
        """The failure this prevents, stated as arithmetic.

        If the two baths were treated as one, the difference between the
        sites would cancel 0.5 of calibration that is genuinely independent
        — a served figure roughly seven times too precise.
        """
        mine = from_wire(to_wire(_reading(21.4), origin=SITE_A))
        theirs = from_wire(to_wire(_reading(21.9), origin=SITE_B))

        honest = difference(mine, theirs).u
        if_collided = difference(mine, from_wire(to_wire(_reading(21.9), origin=SITE_A))).u

        assert honest == pytest.approx((0.5**2 + 0.1**2 + 0.5**2 + 0.1**2) ** 0.5)
        assert if_collided < honest / 5
        # Wider is the safe direction, and qualification lands on it.
        assert honest > if_collided

    def test_composing_across_origins_widens_and_never_narrows(self):
        """The invariant: anyone may widen, nobody may narrow by assertion."""
        mine = from_wire(to_wire(_reading(21.4), origin=SITE_A))
        alone = add([mine])
        with_theirs = add([mine, from_wire(to_wire(_reading(21.4), origin=SITE_B))])
        assert with_theirs.low >= alone.low
        assert with_theirs.high >= alone.high


class TestThePayloadRefusesWhatCouldComposeWrongly:
    def test_an_unqualified_symbol_on_the_wire_is_refused(self):
        """Not qualified on arrival with the sender's claimed origin: the
        receiver cannot tell "local source" from "forgot to qualify", and
        guessing local creates a false correlation."""
        payload = {
            "v": WIRE_VERSION,
            "value": 21.4,
            "unit": "degC",
            "origin": SITE_A,
            "terms": {"signals:cal-bath-a:offset": 0.5},
        }
        with pytest.raises(WireError, match="unqualified"):
            from_wire(payload)

    def test_one_unqualified_symbol_among_many_still_refuses(self):
        payload = to_wire(_reading(21.4), origin=SITE_A)
        payload["terms"]["signals:sneaky:offset"] = 0.9
        with pytest.raises(WireError, match="unqualified"):
            from_wire(payload)

    def test_an_unknown_wire_version_is_refused_rather_than_guessed(self):
        payload = to_wire(_reading(21.4), origin=SITE_A)
        payload["v"] = WIRE_VERSION + 1
        with pytest.raises(WireError, match="wire version"):
            from_wire(payload)

    def test_a_missing_unit_is_refused(self):
        payload = to_wire(_reading(21.4), origin=SITE_A)
        del payload["unit"]
        with pytest.raises(WireError, match="not a fact"):
            from_wire(payload)

    def test_a_non_numeric_value_is_refused(self):
        payload = to_wire(_reading(21.4), origin=SITE_A)
        payload["value"] = "21.4"
        with pytest.raises(WireError, match="must be a number"):
            from_wire(payload)

    def test_the_origin_must_be_a_principal(self):
        for bad in ("site-alpha", "@@site", "site@alpha", "", "@site alpha"):
            with pytest.raises(WireError, match="principal"):
                check_origin(bad)

    def test_a_valid_origin_may_carry_a_context(self):
        assert check_origin("@node-7:site-alpha") == "@node-7:site-alpha"
        assert check_origin("@ben.booth") == "@ben.booth"

    def test_to_wire_has_no_default_origin(self):
        """A default is the one thing that could reintroduce the collision:
        a caller who forgot would emit bare symbols."""
        with pytest.raises(TypeError):
            to_wire(_reading(21.4))


class TestQualification:
    def test_qualifying_is_idempotent_for_the_same_origin(self):
        once = qualify("signals:tc-14:rep", origin=SITE_A)
        assert qualify(once, origin=SITE_A) == once

    def test_re_origining_a_foreign_symbol_is_refused(self):
        """It would claim somebody else's measurement as your own, and make
        it correlate with your sources when it does not."""
        theirs = qualify("signals:tc-14:rep", origin=SITE_B)
        with pytest.raises(WireError, match="already carries origin"):
            qualify(theirs, origin=SITE_A)

    def test_a_malformed_symbol_is_refused_before_qualification(self):
        with pytest.raises(ValueError):
            qualify("not a symbol", origin=SITE_A)

    def test_origin_and_local_part_round_trip(self):
        q = qualify("signals:tc-14:repeatability", origin="@node-7:site-alpha")
        assert origin_of(q) == "@node-7:site-alpha"
        assert local_part(q) == "signals:tc-14:repeatability"
        assert is_qualified(q)

    def test_a_bare_symbol_has_no_origin(self):
        assert origin_of("signals:tc-14:rep") is None
        assert local_part("signals:tc-14:rep") == "signals:tc-14:rep"
        assert not is_qualified("signals:tc-14:rep")


class TestAliasIsTheOnlyThingThatMayNarrow:
    """Occasionally correct, never a default.

    Two sites calibrated against the same national standard genuinely share
    that source, so a difference between them IS more precise than either.
    Refusing to say so overstates. But the claim must be made by somebody
    who knows, not inferred from two names matching.
    """

    def test_declaring_a_shared_standard_makes_a_difference_tighter(self):
        shared = "@nist/standards:srm-1747:offset"
        mine = from_wire(to_wire(_reading(21.4), origin=SITE_A))
        theirs = from_wire(to_wire(_reading(21.9), origin=SITE_B))

        before = difference(mine, theirs).u
        mine_a = alias(mine, {qualify("signals:cal-bath-a:offset", origin=SITE_A): shared})
        theirs_a = alias(theirs, {qualify("signals:cal-bath-a:offset", origin=SITE_B): shared})
        after = difference(mine_a, theirs_a).u

        assert after < before
        # With the standard shared it cancels, leaving the two repeatabilities.
        assert after == pytest.approx((0.1**2 + 0.1**2) ** 0.5)

    def test_an_empty_mapping_changes_nothing(self):
        q = from_wire(to_wire(_reading(21.4), origin=SITE_A))
        assert alias(q, {}).terms == q.terms

    def test_merged_coefficients_add_because_that_is_what_sharing_means(self):
        shared = "@nist/standards:srm-1747:offset"
        q = Quantity(
            value=0.0,
            unit="degC",
            terms={"@a/e:x:t": 0.3, "@b/e:x:t": 0.4},
        )
        merged = alias(q, {"@a/e:x:t": shared, "@b/e:x:t": shared})
        assert merged.terms == {shared: pytest.approx(0.7)}

    def test_aliasing_onto_a_bare_symbol_is_refused(self):
        """A canonical shared source must say whose it is, or it collides
        with a local one."""
        q = from_wire(to_wire(_reading(21.4), origin=SITE_A))
        with pytest.raises(WireError, match="bare"):
            alias(q, {qualify("signals:cal-bath-a:offset", origin=SITE_A): "standards:srm:offset"})
