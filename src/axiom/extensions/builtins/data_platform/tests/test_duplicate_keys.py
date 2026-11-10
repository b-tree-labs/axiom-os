# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classify a collision before deciding what to do about it.

The table is keyed on a hash of the transport artifact, so one reading sent
twice and two different readings at one instant are indistinguishable in the
key — while everything downstream treats (site, feed, channel, ts) as the
identity of a reading.

The two need opposite fixes. Measured on a live install: one channel had
370,889 colliding instants and ZERO re-sends among them. A dedupe on the
natural key would have deleted half that channel's history.
"""

from __future__ import annotations

from ..duplicate_keys import NATURAL_KEY, classify, summary


def _row(channel, colliding, resend, divergent, worst=2, site="site-a", feed="s"):
    return {
        "site": site,
        "feed": feed,
        "channel": channel,
        "colliding_keys": colliding,
        "resend": resend,
        "divergent": divergent,
        "worst": worst,
    }


class TestTheNaturalKey:
    def test_it_is_what_downstream_treats_as_identity(self):
        """Not the storage key. For a time series, identity is the channel at
        an instant, which is exactly what the storage key is NOT."""
        assert NATURAL_KEY == ("site", "feed", "channel", "ts")


class TestClassifying:
    def test_all_same_value_is_safe_to_collapse(self):
        c = classify([_row("a", 10, 10, 0)])[0]
        assert c.safe_to_dedupe
        assert "same value arriving again" in c.verdict

    def test_any_divergence_is_not(self):
        c = classify([_row("a", 10, 9, 1)])[0]
        assert not c.safe_to_dedupe
        assert "Do NOT dedupe" in c.verdict

    def test_mostly_resend_is_still_not_safe(self):
        """A channel that is 99% re-sends is not 99% safe. The 1% is a real
        reading and losing it is the same harm as losing all of them — which
        is why the verdict is not a percentage."""
        c = classify([_row("a", 100_000, 99_000, 1_000)])[0]
        assert not c.safe_to_dedupe

    def test_the_verdict_says_what_to_do_instead(self):
        c = classify([_row("a", 10, 0, 10)])[0]
        assert "timestamp resolution" in c.verdict

    def test_the_worst_channels_come_first(self):
        got = classify([_row("quiet", 5, 5, 0), _row("loud", 9, 0, 9), _row("mid", 7, 4, 3)])
        assert [c.channel for c in got] == ["loud", "mid", "quiet"]

    def test_the_real_shape_from_the_node(self):
        """370,889 colliding instants, zero re-sends, up to three rows at one
        instant."""
        c = classify([_row("NCDT1:HEAT:HE-DT1:Time", 370_889, 0, 370_889, worst=3)])[0]
        assert not c.safe_to_dedupe
        assert c.worst == 3
        assert "370,889" in c.verdict


class TestTheSummary:
    def test_no_collisions_says_so_plainly(self):
        assert summary([])["verdict"] == "no collisions"
        assert summary([])["channels_affected"] == 0

    def test_a_divergent_install_is_told_not_to_dedupe(self):
        got = summary(classify([_row("a", 10, 0, 10), _row("b", 4, 4, 0)]))
        assert got["divergent_instants"] == 10
        assert "would delete a real reading" in got["verdict"]
        assert got["channels_safe_to_dedupe"] == 1

    def test_an_all_resend_install_is_cleared(self):
        got = summary(classify([_row("a", 10, 10, 0), _row("b", 4, 4, 0)]))
        assert got["divergent_instants"] == 0
        assert "can be collapsed without losing a reading" in got["verdict"]

    def test_it_counts_instants_not_channels(self):
        """A report saying "2 channels affected" hides that one of them has
        370,889 bad instants."""
        got = summary(classify([_row("a", 370_889, 0, 370_889), _row("b", 2, 0, 2)]))
        assert got["colliding_instants"] == 370_891
        assert got["channels_affected"] == 2


class TestTheQueryIsScoped:
    def test_it_takes_a_site(self):
        """Unscoped it is a full group-by over every row, which is a
        maintenance operation rather than something to run behind a partner
        waiting for an answer."""
        from ..duplicate_keys import GROUPED_SQL

        assert "%(site)s" in GROUPED_SQL

    def test_it_only_groups_instants_that_actually_collide(self):
        from ..duplicate_keys import GROUPED_SQL

        assert "HAVING count(*) > 1" in GROUPED_SQL

    def test_it_counts_distinct_values_which_is_what_decides_the_verdict(self):
        from ..duplicate_keys import GROUPED_SQL

        assert "count(DISTINCT value)" in GROUPED_SQL


class TestSignalsLatestCannotReturnADifferentAnswerEachRun:
    """`DISTINCT ON ... ORDER BY ts DESC` leaves the tie unbroken.

    With two rows at the latest instant Postgres may return either, so "the
    current value" of a channel could change between two runs of the same
    query with no change in the data. On a live install one channel had
    370,889 such instants.
    """

    def _ddl(self):
        from ..conformance import GOLD_SIGNALS_DDL

        return next(s for s in GOLD_SIGNALS_DDL if "signals_latest" in s and "VIEW" in s)

    def test_the_tie_is_broken_deterministically(self):
        assert "ts DESC, row_hash DESC" in self._ddl()

    def test_it_says_when_it_had_to_choose(self):
        """Deterministic makes the answer STABLE, not RIGHT — neither row is
        more correct. A consumer has to be able to tell."""
        ddl = self._ddl()
        assert "ts_ambiguous" in ddl
        assert "PARTITION BY site, feed, channel, ts" in ddl

    def test_nothing_is_ever_inserted_before_an_existing_column(self):
        """CREATE OR REPLACE VIEW may only append. Inserting a column stops a
        deploy, which is how role/uncertainty/derivation were learned.

        Asserted as the INVARIANT rather than as a snapshot. This used to check
        that `ts_ambiguous` came last, which was true until `quality_reason` was
        added — and `quality_reason` had to go AFTER `ts_ambiguous`, because
        `signals_latest` grew the flag first and moving it would be exactly the
        refused reorder. A test pinned to "the flag is last" therefore fails on
        a correct change, so what it pins now is that every column this view has
        ever had keeps its position, whatever arrives next.
        """
        from ..conformance import GOLD_SIGNALS_BASE_COLUMNS

        ddl = self._ddl()
        # The order `signals_latest` has always had, oldest first.
        settled = [
            *GOLD_SIGNALS_BASE_COLUMNS,
            "role",
            "uncertainty",
            "derivation",
            "ts_ambiguous",
        ]
        seen = [ddl.index(c) for c in settled]
        assert seen == sorted(seen), (
            "a column moved relative to one that was already deployed; "
            "CREATE OR REPLACE VIEW will refuse this"
        )

    def test_a_newly_added_column_lands_after_the_settled_ones(self):
        ddl = self._ddl()
        assert ddl.index("ts_ambiguous") < ddl.rindex("quality_reason")

    def test_gold_signals_itself_is_untouched(self):
        """Only the one-row-per-channel view has a tie to break. The
        pass-through has no DISTINCT ON and must keep every row."""
        from ..conformance import GOLD_SIGNALS_DDL

        plain = next(s for s in GOLD_SIGNALS_DDL if "VIEW gold.signals AS" in s)
        assert "DISTINCT" not in plain
        assert "ts_ambiguous" not in plain


class TestItDeclaresTheTierItRead:
    """ADR-128 E5: a diagnostic may read a working tier provided it says
    which one. The guard caught this module the moment it was written, which
    is the guard doing its job."""

    def test_the_tier_is_named_in_every_result(self):
        from ..duplicate_keys import TIER_READ

        assert TIER_READ == "silver"
        assert summary([])["tier_read"] == "silver"

    def test_the_module_claims_the_allowance_explicitly(self):
        import pathlib

        from ..tiers import ALLOWANCES

        source = (
            pathlib.Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("duplicate_keys.py")
            .read_text(encoding="utf-8")
        )
        assert "ADR-128 E5" in source
        assert "E5" in ALLOWANCES
