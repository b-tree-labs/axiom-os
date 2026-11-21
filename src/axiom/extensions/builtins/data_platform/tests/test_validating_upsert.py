# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Wiring VALIDATE into the conform path.

Conform writes one channel at a time; a cross-channel rule needs every channel
at one instant. The two are reconciled by buffering, and buffering is only safe
because one bronze record expands to all of its channels at once —
`signals = list(fn(rec))`, upserted in a tight loop. These tests pin that
assumption and prove the guard fires when it is violated.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import company
from axiom.extensions.builtins.data_platform.conformance import validating_upsert

WARM_FUEL_ZERO = company.ZeroWhileCompanionAbove(
    zero=["FuelTemp1", "FuelTemp2"],
    companion=["WaterTemp"],
    above=5.0,
    subject_reason="company.zero_while_companion_warm",
)


def rows(ts: str, **channels):
    return [
        {"site": "s", "feed": "reactor.console", "channel": c, "ts": ts, "value": v}
        for c, v in channels.items()
    ]


class TestItJudgesTheWholeInstant:
    def test_a_zeroed_frame_is_nulled_on_the_way_through(self):
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for r in rows("2026-09-15T12:45:35.5", FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0):
            up(r)
        up.flush()
        by = {w["channel"]: w for w in written}
        assert by["FuelTemp1"]["value"] is None
        assert by["FuelTemp1"]["quality"] == "bad"
        assert by["WaterTemp"]["quality"] == "suspect"
        assert by["WaterTemp"]["value"] == 20.0

    def test_every_row_still_reaches_the_inner_upsert(self):
        """Judging must not drop rows. A `bad` reading is a row with a NULL
        value, not an absent row — the instant still happened."""
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for r in rows("t1", FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0):
            up(r)
        up.flush()
        assert len(written) == 3

    def test_a_clean_frame_passes_through_untouched(self):
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for r in rows("t1", FuelTemp1=305.0, FuelTemp2=366.0, WaterTemp=23.0):
            up(r)
        up.flush()
        assert [w["value"] for w in written] == [305.0, 366.0, 23.0]
        assert all("quality" not in w for w in written)


class TestTheBufferBoundary:
    def test_a_new_instant_flushes_the_previous_one(self):
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for r in rows("t1", FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0):
            up(r)
        assert written == []  # nothing written until the instant completes
        for r in rows("t2", FuelTemp1=1.0, FuelTemp2=1.0, WaterTemp=20.0):
            up(r)
        assert len(written) == 3  # t1 went out when t2 arrived
        up.flush()
        assert len(written) == 6

    def test_nothing_is_lost_without_a_flush_call(self):
        """A caller that forgets to flush loses the last instant, so the report
        says how many rows are still held. Silently dropping the final frame of
        every run would be invisible and wrong."""
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for r in rows("t1", FuelTemp1=0.0, WaterTemp=20.0):
            up(r)
        assert report["held"] == 2
        up.flush()
        assert report["held"] == 0

    def test_a_different_feed_at_the_same_instant_is_a_different_frame(self):
        """Two instruments reading at the same moment are not in each other's
        company: a rule about fuel temperature must not be answered by a
        channel from another feed."""
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        up({"site": "s", "feed": "a", "channel": "FuelTemp1", "ts": "t1", "value": 0.0})
        up({"site": "s", "feed": "b", "channel": "WaterTemp", "ts": "t1", "value": 20.0})
        up.flush()
        by = {w["feed"]: w for w in written}
        assert by["a"].get("quality") is None  # no warm companion in ITS frame


class TestTheGuardOnItsOwnAssumption:
    def test_a_reopened_instant_is_counted_rather_than_silently_judged_twice(self):
        """The buffer is safe only because a bronze record expands to all its
        channels at once. If rows ever interleave, frames get judged with only
        the channels seen so far — under-judging, invisibly. So a key that
        reopens after being flushed is counted and reported."""
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        up({"site": "s", "feed": "f", "channel": "FuelTemp1", "ts": "t1", "value": 0.0})
        up({"site": "s", "feed": "f", "channel": "X", "ts": "t2", "value": 1.0})
        up({"site": "s", "feed": "f", "channel": "WaterTemp", "ts": "t1", "value": 20.0})
        up.flush()
        assert report["reopened"] == 1

    def test_ordered_input_never_reopens(self):
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for ts in ("t1", "t2", "t3"):
            for r in rows(ts, FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0):
                up(r)
        up.flush()
        assert report["reopened"] == 0
        assert report["frames"] == 3


class TestWhatItReports:
    def test_it_counts_what_it_changed_so_a_run_can_say_so(self):
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for ts in ("t1", "t2"):
            for r in rows(ts, FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0):
                up(r)
        up.flush()
        assert report["nulled"] == 4  # two instants x two thermocouples
        assert report["flagged"] == 2  # two instants x one implicated companion
        assert report["frames"] == 2

    def test_the_reason_reaches_its_own_column_and_the_report(self):
        """ADR-132 splits `quality` (closed consumer contract) from
        `quality_reason` (open producer diagnosis), and they must never share a
        field.

        This test used to assert the opposite — that the reason could only reach
        the run report, because `silver.signals` had no column for it. It does
        now, so a row can say `bad` and say why. The report keeps its tally
        because a run wants counts, not seventy million rows.
        """
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for r in rows("t1", FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0):
            up(r)
        up.flush()
        by = {w["channel"]: w for w in written}
        assert by["FuelTemp1"]["quality_reason"] == "company.zero_while_companion_warm"
        assert by["FuelTemp1"]["quality"] == "bad"
        assert report["reasons"]["company.zero_while_companion_warm"] == 2

    def test_the_two_fields_never_carry_each_others_content(self):
        """The whole point of the split. `quality` must stay inside the closed
        vocabulary and never grow a dotted diagnosis; `quality_reason` must never
        be handed a bare verdict."""
        from axiom.extensions.builtins.data_platform.company import QUALITY

        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[WARM_FUEL_ZERO])
        for r in rows("t1", FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0):
            up(r)
        up.flush()
        for w in written:
            assert w["quality"] in QUALITY
            assert "." in w["quality_reason"]
            assert w["quality_reason"] not in QUALITY


class TestRulesAreLookedUpPerSite:
    """One conform pass reads every tenant's bronze in one interpreter.

    A flat rule list would judge every site by whichever site's declaration was
    loaded — one institution's physics applied to another institution's
    instrument. The same reasoning the normalizer discovery boundary rests on:
    the process is shared, so nothing site-specific may be global in it.
    """

    ZERO = company.ZeroWhileCompanionAbove(
        zero=["FuelTemp1"],
        companion=["WaterTemp"],
        above=5.0,
        subject_reason="company.zero_while_companion_warm",
    )

    def _rows(self, site, ts, **channels):
        return [
            {"site": site, "feed": "f", "channel": c, "ts": ts, "value": v}
            for c, v in channels.items()
        ]

    def test_a_site_with_no_declaration_is_not_judged_by_anothers(self):
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules_for={"ut": [self.ZERO]}.get)
        for r in self._rows("ut", "t1", FuelTemp1=0.0, WaterTemp=20.0):
            up(r)
        for r in self._rows("other", "t1", FuelTemp1=0.0, WaterTemp=20.0):
            up(r)
        up.flush()
        by = {(w["site"], w["channel"]): w for w in written}
        assert by[("ut", "FuelTemp1")]["value"] is None
        # Same numbers, no declaration, untouched.
        assert by[("other", "FuelTemp1")]["value"] == 0.0
        assert "quality" not in by[("other", "FuelTemp1")]

    def test_each_site_gets_its_own_thresholds(self):
        """Two reactors can legitimately disagree about what is impossible."""
        strict = company.ZeroWhileCompanionAbove(
            zero=["FuelTemp1"],
            companion=["WaterTemp"],
            above=5.0,
            subject_reason="company.zero_while_companion_warm",
        )
        lax = company.ZeroWhileCompanionAbove(
            zero=["FuelTemp1"],
            companion=["WaterTemp"],
            above=50.0,
            subject_reason="company.zero_while_companion_warm",
        )
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules_for={"a": [strict], "b": [lax]}.get)
        for site in ("a", "b"):
            for r in self._rows(site, "t1", FuelTemp1=0.0, WaterTemp=20.0):
                up(r)
        up.flush()
        by = {(w["site"], w["channel"]): w for w in written}
        assert by[("a", "FuelTemp1")]["quality"] == "bad"  # 20 > 5
        assert "quality" not in by[("b", "FuelTemp1")]  # 20 < 50

    def test_a_lookup_that_raises_does_not_sink_the_pass(self):
        """A malformed declaration for one site must not stop every other site's
        rows from conforming. It is counted instead."""

        def boom(site):
            if site == "bad-decl":
                raise ValueError("unknown rule kind 'vibes'")
            return [self.ZERO]

        written: list[dict] = []
        up, report = validating_upsert(written.append, rules_for=boom)
        for r in self._rows("bad-decl", "t1", FuelTemp1=0.0, WaterTemp=20.0):
            up(r)
        for r in self._rows("ut", "t1", FuelTemp1=0.0, WaterTemp=20.0):
            up(r)
        up.flush()
        by = {(w["site"], w["channel"]): w for w in written}
        assert by[("bad-decl", "FuelTemp1")]["value"] == 0.0  # written, unjudged
        assert by[("ut", "FuelTemp1")]["value"] is None  # the good site still judged
        assert "bad-decl" in report["unreadable"]


class TestARuleThatCanNeverFireIsReported:
    """A declaration naming a channel the store does not have is a silent no-op.

    Measured on the live node: the site declared `companion: [WaterTemp]`, which
    is the staged-CSV name. Silver calls the same physical thing `PoolTemp` and
    has **zero** rows named WaterTemp. So the rule matched 0 instants — while
    24,812 fuel-temperature rows sat at exactly 0 degC with the pool between 19
    and 26 degC.

    The rule loaded, validated, exported and deployed. Nothing failed. A
    re-derive would have reported no changes and that would have read as
    "history is clean". The only way to catch it is to notice that a channel a
    rule NAMES never appeared in any frame.
    """

    ZERO = company.ZeroWhileCompanionAbove(
        zero=["FuelTemp1"],
        companion=["WaterTemp"],
        above=5.0,
        subject_reason="company.zero_while_companion_warm",
    )

    def _rows(self, ts, **channels):
        return [
            {"site": "s", "feed": "f", "channel": c, "ts": ts, "value": v}
            for c, v in channels.items()
        ]

    def test_a_named_channel_no_frame_ever_carried_is_reported(self):
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[self.ZERO])
        # The store calls it PoolTemp; the rule asked for WaterTemp.
        for r in self._rows("t1", FuelTemp1=0.0, PoolTemp=20.0):
            up(r)
        up.flush()
        assert "WaterTemp" in report["never_seen"]

    def test_a_rule_whose_channels_all_appear_reports_nothing(self):
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[self.ZERO])
        for r in self._rows("t1", FuelTemp1=0.0, WaterTemp=20.0):
            up(r)
        up.flush()
        assert report["never_seen"] == []

    def test_seeing_it_once_anywhere_is_enough(self):
        """A channel absent from one frame is normal — instruments report at
        different cadences. Only a channel absent from EVERY frame is evidence
        the name is wrong."""
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[self.ZERO])
        for r in self._rows("t1", FuelTemp1=0.0):
            up(r)
        for r in self._rows("t2", FuelTemp1=0.0, WaterTemp=20.0):
            up(r)
        up.flush()
        assert report["never_seen"] == []

    def test_it_reports_the_name_not_a_count(self):
        """A count cannot be acted on. The name is what somebody greps the
        channel map for."""
        written: list[dict] = []
        up, report = validating_upsert(written.append, rules=[self.ZERO])
        for r in self._rows("t1", FuelTemp1=0.0, PoolTemp=20.0):
            up(r)
        up.flush()
        assert report["never_seen"] == ["WaterTemp"]
