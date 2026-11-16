# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for interval-cadence next_fire_at computation.

Per spec-axiom-schedule §3 + §6.3.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule.api import Cadence
from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at


NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


def test_interval_first_fire_is_now_plus_interval():
    cadence = Cadence(kind="interval", interval=timedelta(hours=1))
    nxt = compute_next_fire_at(cadence, last_fire=None, now=NOW)
    assert nxt == NOW + timedelta(hours=1)


def test_interval_subsequent_fires_anchor_on_last_fire():
    cadence = Cadence(kind="interval", interval=timedelta(minutes=15))
    last = NOW
    nxt = compute_next_fire_at(cadence, last_fire=last, now=NOW + timedelta(minutes=20))
    # Anchored on last_fire, NOT on now() — guards against drift.
    assert nxt == last + timedelta(minutes=15)


def test_interval_respects_not_after():
    cadence = Cadence(
        kind="interval",
        interval=timedelta(hours=1),
        not_after=NOW + timedelta(minutes=30),
    )
    nxt = compute_next_fire_at(cadence, last_fire=None, now=NOW)
    assert nxt is None  # next fire would exceed not_after


def test_interval_jitter_within_bound():
    cadence = Cadence(
        kind="interval",
        interval=timedelta(hours=1),
        randomized_delay=timedelta(seconds=60),
    )
    rng = random.Random(42)
    nxt = compute_next_fire_at(cadence, last_fire=None, now=NOW, rng=rng)
    delta = (nxt - (NOW + timedelta(hours=1))).total_seconds()
    assert 0 <= delta <= 60


def test_interval_missing_interval_raises():
    cadence = Cadence(kind="interval", interval=None)
    try:
        compute_next_fire_at(cadence, last_fire=None, now=NOW)
    except ValueError as e:
        assert "interval" in str(e)
    else:
        raise AssertionError("expected ValueError")
