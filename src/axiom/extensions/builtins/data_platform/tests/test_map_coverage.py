# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A channel map can be complete, correct, and match nothing.

Measured on a live install: one site's map declared 21 channels, every one
carrying a unit; that site had 21 channels in silver with no unit; the overlap
was ZERO. The map said `TW_01`, the data said `TW_1`, and 496,923 rows went
out unitless while the map sat there looking finished.
"""

from __future__ import annotations

from ..map_coverage import (
    compare,
    compare_refs,
    joined_by_role,
    normalize,
    roleless,
    unjoined_lookalikes,
)


class TestNormalizingForNearMissesOnly:
    def test_zero_padding_is_decoration(self):
        assert normalize("TW_01") == normalize("TW_1")

    def test_case_and_separators_are_decoration(self):
        assert normalize("NCDT1:GAS:PT31") == normalize("ncdt1_gas_pt31")

    def test_a_real_difference_survives(self):
        """`TW_1` and `TW_2` must NOT normalize together, or the suggestion
        would pair up two real instruments."""
        assert normalize("TW_1") != normalize("TW_2")
        assert normalize("TW_10") != normalize("TW_1")

    def test_an_interior_zero_is_not_padding(self):
        assert normalize("TW_101") != normalize("TW_11")


class TestTheRealFailureFromTheNode:
    """21 declared, 21 unitless, zero overlap."""

    DECLARED = {f"TW_{i:02d}": "degC" for i in range(1, 10)}
    OBSERVED = {f"TW_{i}": 23_663 for i in range(1, 10)}

    def _cov(self):
        return compare("site/mat-v1", self.DECLARED, self.OBSERVED)

    def test_nothing_matches(self):
        assert self._cov().matched == 0

    def test_every_channel_is_reported_on_both_sides(self):
        cov = self._cov()
        assert len(cov.declared_never_seen) == 9
        assert len(cov.seen_never_declared) == 9

    def test_the_near_misses_name_the_fix(self):
        """"You declared 21 and none matched" is a report. "You declared
        TW_01 and the data has TW_1" is a fix."""
        cov = self._cov()
        assert ("TW_01", "TW_1") in cov.near_misses
        assert len(cov.near_misses) == 9

    def test_the_verdict_leads_with_the_near_miss(self):
        verdict = self._cov().verdict
        assert "differ only in how the names are written" in verdict
        assert "TW_01 → TW_1" in verdict

    def test_it_is_not_healthy(self):
        assert not self._cov().healthy


class TestADeclaredChannelWithNoUnit:
    """The quiet one: the map names it, so the map looks like it covered it,
    and the data still lands with no unit."""

    def test_it_is_told_apart_from_a_channel_with_a_unit(self):
        cov = compare("r", {"a": "degC", "b": None, "c": ""}, {"a": 1, "b": 1, "c": 1})
        assert cov.matched_with_unit == ("a",)
        assert cov.matched_without_unit == ("b", "c")

    def test_it_is_not_healthy_even_though_everything_matched(self):
        cov = compare("r", {"a": None}, {"a": 1})
        assert cov.declared_never_seen == ()
        assert cov.seen_never_declared == ()
        assert not cov.healthy
        assert "without a unit" in cov.verdict


class TestTheHealthyCase:
    def test_a_map_that_covers_its_data_says_so(self):
        cov = compare("r", {"a": "degC", "b": "psi"}, {"a": 10, "b": 20})
        assert cov.healthy
        assert cov.verdict == "the map covers the data"
        assert cov.rows == 30

    def test_a_channel_that_stopped_arriving_is_not_a_failure_on_its_own(self):
        """A producer may legitimately retire a channel. It is reported, not
        counted against the map's health."""
        cov = compare("r", {"a": "degC", "retired": "psi"}, {"a": 10})
        assert cov.declared_never_seen == ("retired",)
        assert cov.healthy


class TestNearMissesDoNotFireOnThingsThatMatched:
    def test_a_matched_channel_is_not_also_a_near_miss(self):
        cov = compare("r", {"TW_1": "degC"}, {"TW_1": 5, "TW_01": 5})
        assert cov.matched_with_unit == ("TW_1",)
        assert cov.near_misses == ()

    def test_an_unmatched_pair_with_no_resemblance_is_not_suggested(self):
        cov = compare("r", {"alpha": "degC"}, {"omega": 5})
        assert cov.near_misses == ()
        assert "matched none" in cov.verdict


class TestTheSameFailureOneLevelUp:
    """A map keyed to a schema_ref nothing uses is a map for nobody."""

    def test_a_ref_mismatch_is_found(self):
        got = compare_refs(["site-b/mat-v1"], ["site-a/mat-v1"])
        assert got.maps_for_nobody == ("site-b/mat-v1",)
        assert got.data_with_no_map == ("site-a/mat-v1",)

    def test_data_with_no_map_at_all_is_reported(self):
        """The site whose 19.1 million rows are covered by nothing."""
        got = compare_refs([], ["andretti-rom/state-v1", "andretti-rom/flux-v1"])
        assert set(got.data_with_no_map) == {"andretti-rom/state-v1", "andretti-rom/flux-v1"}
        assert got.maps_for_nobody == ()

    def test_a_ref_that_matches_is_neither(self):
        got = compare_refs(["a/v1"], ["a/v1"])
        assert got.maps_for_nobody == ()
        assert got.data_with_no_map == ()

    def test_a_ref_near_miss_is_suggested(self):
        got = compare_refs(["site-a/mat_v1"], ["site-a/mat-v1"])
        assert got.near_misses == (("site-a/mat_v1", "site-a/mat-v1"),)


class TestAnEmptyComparisonIsNotASuccess:
    """Found by running this against the real maps: a map keyed to a
    schema_ref nothing uses compared a full declaration against an empty set
    and reported "the map covers the data".

    Vacuously true is not true, and it read that way for exactly the two maps
    that covered nothing — which is the shape of green-that-cannot-fail this
    module exists to find, in this module.
    """

    def test_a_map_against_no_data_says_nothing_was_compared(self):
        cov = compare("site/never-used-v1", {"a": "degC", "b": "psi"}, {})
        assert "nothing was compared" in cov.verdict
        assert "data that does not exist under this ref" in cov.verdict

    def test_it_is_not_healthy(self):
        assert not compare("r", {"a": "degC"}, {}).healthy

    def test_a_map_with_nothing_to_say_about_nothing_is_fine(self):
        """An empty map against empty data is not a defect — there is simply
        nothing there, and reporting it would be noise."""
        assert compare("r", {}, {}).healthy

    def test_real_coverage_is_still_reported_as_coverage(self):
        assert compare("r", {"a": "degC"}, {"a": 5}).healthy


# ------------------------------------------------- joinability, by role


#: The real shape, before it was fixed: one site, three feeds, the same
#: instruments under three vocabularies and nothing joining them.
THREE_VOCABULARIES = {
    "feed-a/v1": {
        "XR": {"unit": "console_units"},
        "ChanA1": {"unit": "degC"},
        "Power": {"unit": "W"},
    },
    "feed-b/v1": {
        "long_name_for_xr": {"unit": "console_units"},
        "chan_a_1": {"unit": "degC"},
        "power": {"unit": "W"},
    },
}

JOINED = {
    "feed-a/v1": {
        "XR": {"role": "position"},
        "ChanA1": {"role": "temperature"},
    },
    "feed-b/v1": {
        "long_name_for_xr": {"role": "position"},
        "chan_a_1": {"role": "temperature"},
    },
}


class TestRolelessIsTheCompleteCheck:
    def test_a_channel_with_no_role_cannot_be_compared_with_anything(self):
        """Unjoinable by construction, not by accident, and not recoverably
        by anyone downstream."""
        gaps = roleless(THREE_VOCABULARIES)
        assert len(gaps) == 6
        assert all("no role" in g.reason for g in gaps)

    def test_a_declared_role_clears_it(self):
        assert roleless(JOINED) == []

    def test_an_empty_role_counts_as_none(self):
        assert roleless({"r": {"c": {"role": "   "}}})

    def test_it_is_exhaustive_rather_than_a_heuristic(self):
        """Every channel is either joinable or named. Nothing is inferred, so
        nothing is missed."""
        decls = {"r": {f"ch{i}": {} for i in range(50)}}
        assert len(roleless(decls)) == 50


class TestLookalikesAreAHeuristicAndSayItPlainly:
    def test_it_catches_a_spelling_difference(self):
        gaps = unjoined_lookalikes(THREE_VOCABULARIES)
        names = {g.channel for g in gaps}
        assert {"ChanA1", "chan_a_1", "Power", "power"} <= names

    def test_it_names_the_counterpart_so_the_fix_is_obvious(self):
        gaps = {g.channel: g for g in unjoined_lookalikes(THREE_VOCABULARIES)}
        assert "feed-b/v1:chan_a_1" in gaps["ChanA1"].candidates

    def test_it_misses_an_abbreviation_and_that_is_the_point(self):
        """`XR` and `long_name_for_xr` are not a spelling difference.
        Abbreviation to word is not a string problem and no normaliser will
        make it one, which is why a role is declared rather than inferred."""
        names = {g.channel for g in unjoined_lookalikes(THREE_VOCABULARIES)}
        assert "XR" not in names
        assert "long_name_for_xr" not in names
        # ...but `roleless` catches both, which is why it is the real check.
        assert {"XR", "long_name_for_xr"} <= {g.channel for g in roleless(THREE_VOCABULARIES)}

    def test_consistently_joined_lookalikes_are_not_flagged(self):
        assert unjoined_lookalikes(JOINED) == []

    def test_lookalikes_carrying_DIFFERENT_roles_are_flagged(self):
        """Two feeds calling the same-looking channel different quantities
        is worse than neither declaring one, because it reads as settled."""
        decls = {
            "a/v1": {"Power": {"role": "power"}},
            "b/v1": {"power": {"role": "power_b"}},
        }
        gaps = unjoined_lookalikes(decls)
        assert len(gaps) == 2
        assert all("different role" in g.reason for g in gaps)

    def test_a_channel_on_one_stream_only_is_not_a_gap(self):
        assert unjoined_lookalikes({"a/v1": {"Power": {}}}) == []


class TestThePositiveReport:
    def test_it_shows_which_roles_actually_join_streams(self):
        got = joined_by_role(JOINED)
        assert got["position"] == (
            "feed-a/v1:XR", "feed-b/v1:long_name_for_xr",
        )

    def test_a_role_on_one_stream_only_is_still_listed(self):
        """A role doing no joining yet is worth seeing beside the ones that
        are."""
        got = joined_by_role({"a/v1": {"x": {"role": "lonely"}}})
        assert got["lonely"] == ("a/v1:x",)

    def test_roleless_channels_do_not_appear(self):
        assert joined_by_role(THREE_VOCABULARIES) == {}
