# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A producing site's [share] table decides what the forwarder sends (ADR-180 §4).

The forwarder calls ``allows(record, rows)`` and sends what comes back, and
``hold_until(record)`` to embargo a batch. Nothing in here deletes anything
upstream: a policy only decides what leaves from now on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.data_platform.forward_policy import TableSharePolicy


def _row(feed="loop.live", ts="2026-10-08T10:00:00+00:00", **values):
    return {"feed": feed, "ts": ts, "values": dict(values or {"TC1": 1.0}), "tags": {}}


def _rec(received_at="2026-10-08T10:00:05+00:00"):
    return {"seq": 1, "source": "site-a-src", "received_at": received_at}


def test_a_feed_not_listed_is_not_shared():
    p = TableSharePolicy({"feeds": ["loop.live"]})
    assert p.allows(_rec(), [_row(feed="loop.other")]) == []
    assert len(p.allows(_rec(), [_row()])) == 1


def test_star_shares_every_feed():
    p = TableSharePolicy({"feeds": ["*"]})
    assert len(p.allows(_rec(), [_row(feed="anything")])) == 1


def test_no_feeds_key_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="feeds"):
        TableSharePolicy({})


def test_channels_narrow_a_row_and_mark_it_a_subset():
    p = TableSharePolicy({"feeds": ["*"], "channels": ["TC1"]})
    out = p.allows(_rec(), [_row(TC1=1.0, TC2=2.0)])
    assert out[0]["values"] == {"TC1": 1.0}
    assert out[0]["tags"]["share_subset"] == "channels"


def test_a_row_left_with_no_channels_is_dropped():
    p = TableSharePolicy({"feeds": ["*"], "channels": ["TC9"]})
    assert p.allows(_rec(), [_row(TC1=1.0)]) == []


def test_rows_before_since_are_not_shared():
    p = TableSharePolicy({"feeds": ["*"], "since": "2026-10-01"})
    rows = [_row(ts="2026-09-30T23:59:59+00:00"), _row(ts="2026-10-01T00:00:00+00:00")]
    assert [r["ts"] for r in p.allows(_rec(), rows)] == ["2026-10-01T00:00:00+00:00"]


def test_the_original_rows_are_never_mutated():
    p = TableSharePolicy({"feeds": ["*"], "channels": ["TC1"]})
    row = _row(TC1=1.0, TC2=2.0)
    p.allows(_rec(), [row])
    assert row["values"] == {"TC1": 1.0, "TC2": 2.0}
    assert "share_subset" not in row["tags"]


def test_a_delay_holds_a_batch_until_it_has_aged():
    p = TableSharePolicy({"feeds": ["*"], "delay_minutes": 30})
    hold = p.hold_until(_rec(received_at="2026-10-08T10:00:00+00:00"))
    assert hold == datetime(2026, 10, 8, 10, 30, tzinfo=UTC)


def test_no_delay_means_no_hold():
    assert TableSharePolicy({"feeds": ["*"]}).hold_until(_rec()) is None


def test_it_describes_itself_for_status():
    p = TableSharePolicy({"feeds": ["loop.live"], "since": "2026-10-01", "delay_minutes": 5})
    text = p.describe()
    assert "loop.live" in text and "2026-10-01" in text and "5 minutes" in text
    assert "never deletes" in text
