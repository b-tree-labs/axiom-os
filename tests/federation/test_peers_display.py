# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""How the peers view reports what it knows.

The distinction this protects: when a peer's state last CHANGED is not when
it was last REACHED. The display conflated them for months, so a node
unreachable since April reported a recent "last seen" every time anything
re-decided its state.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from axiom.extensions.builtins.federation.cli import _ago, _contact_column, _peer_glyph
from axiom.infra.cli_format import Glyph


def _peer(*, last_seen="", verified=True):
    return SimpleNamespace(last_seen=last_seen, has_verified_identity=verified)


def _iso(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


class TestAgesReadAsAges:
    def test_seconds_minutes_hours_and_days(self):
        assert _ago(_iso(seconds=5)).endswith("s ago")
        assert _ago(_iso(minutes=10)).endswith("m ago")
        assert _ago(_iso(hours=9)).endswith("h ago")
        assert _ago(_iso(days=4)).endswith("d ago")

    def test_nothing_recorded_is_not_dressed_up_as_a_time(self):
        assert _ago("") == "unknown"

    def test_an_unparseable_value_degrades_rather_than_raises(self):
        """A tautology here would pass on any behaviour at all. The contract
        is that a bad value returns something short and printable, not that
        it raises inside a status command."""
        out = _ago("not-a-timestamp")
        assert isinstance(out, str) and 0 < len(out) <= 10, repr(out)

    def test_a_future_timestamp_does_not_render_a_negative_age(self):
        ahead = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        assert not _ago(ahead).startswith("-"), _ago(ahead)


class TestContactIsNeverInvented:
    def test_a_peer_never_reached_says_so(self):
        assert _contact_column(_peer(), None) == "never probed"

    def test_a_live_probe_beats_a_record(self):
        peer = _peer(last_seen=_iso(days=30))
        assert _contact_column(peer, {"healthy": True, "latency_ms": 12}) == "up 12ms"

    def test_a_failed_probe_is_reported_as_such(self):
        assert _contact_column(_peer(last_seen=_iso(hours=1)), {"healthy": False}) == "unreachable"


class TestTheGlyphMatchesTheEvidence:
    def test_never_reached_is_absent_not_a_tick(self):
        """A tick would claim we had reached it. We have not."""
        assert _peer_glyph(_peer(), None) == Glyph.ABSENT

    def test_a_failed_probe_is_a_failure(self):
        assert _peer_glyph(_peer(last_seen=_iso(hours=1)), {"healthy": False}) == Glyph.FAIL

    def test_a_successful_probe_wins_over_everything(self):
        assert _peer_glyph(_peer(verified=False), {"healthy": True}) == Glyph.OK

    def test_an_unverified_identity_is_flagged_when_unprobed(self):
        assert _peer_glyph(_peer(last_seen=_iso(hours=1), verified=False), None) == Glyph.WARN


class TestTheStatusVerdict:
    """The one-line answer, across states this machine cannot show.

    It has to be a verdict rather than a count: reporting "1 peer" invites
    reading it as "1 connected", which is what made a node nobody had
    contacted since April look healthy.
    """

    @staticmethod
    def _verdict(peers):
        from axiom.extensions.builtins.federation.cli import _federation_verdict

        return _federation_verdict(peers)

    def test_no_peers_is_not_a_tick(self):
        glyph, summary = self._verdict([])
        assert glyph == Glyph.ABSENT
        assert "no peers" in summary

    def test_peers_that_have_never_been_reached_are_not_a_tick(self):
        """Nothing has been verified, so nothing may claim to be."""
        glyph, summary = self._verdict([_peer(), _peer()])
        assert glyph == Glyph.ABSENT, "membership is not connectivity"
        assert "never reached" in summary
        assert "2 peers" in summary

    def test_a_reached_peer_reports_how_recently(self):
        glyph, summary = self._verdict([_peer(last_seen=_iso(minutes=3))])
        assert glyph == Glyph.OK
        assert "1 of 1 peer reached" in summary
        assert "ago" in summary

    def test_partial_reachability_says_how_many(self):
        glyph, summary = self._verdict(
            [_peer(last_seen=_iso(minutes=3)), _peer(), _peer()]
        )
        assert glyph == Glyph.OK
        assert "1 of 3 peers reached" in summary

    def test_it_reports_the_most_recent_contact_not_the_oldest(self):
        _, summary = self._verdict(
            [_peer(last_seen=_iso(days=30)), _peer(last_seen=_iso(minutes=2))]
        )
        assert "d ago" not in summary, f"reported the stale contact: {summary}"

    def test_singular_and_plural_agree_with_the_count(self):
        assert "1 peer," in self._verdict([_peer()])[1]
        assert "2 peers," in self._verdict([_peer(), _peer()])[1]
