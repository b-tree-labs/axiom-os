# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the idempotency window key shape.

Per spec-axiom-schedule §5: the key is
``(schedule_id, fire_time_bucket, params_hash)`` where
``fire_time_bucket = floor(fire_time_unix / window_seconds)``.

This test exercises the *key-shape* contract — same inputs in the
same window collide, different params_hash do not, edge-of-window
falls into the next bucket.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta


def _bucket(t: datetime, window: int) -> int:
    return int(t.timestamp()) // window


def _params_hash(actor: str, intent: str, resource: str, action: str) -> str:
    payload = f"{actor}|{intent}|{resource}|{action}".encode()
    return hashlib.sha256(payload).hexdigest()


T0 = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
WINDOW = 60


def test_same_inputs_same_bucket_collide():
    a = (_bucket(T0, WINDOW), _params_hash("@u:c", "schedule.fire.s1", "r", "a"))
    b = (_bucket(T0 + timedelta(seconds=30), WINDOW),
         _params_hash("@u:c", "schedule.fire.s1", "r", "a"))
    assert a == b


def test_different_params_do_not_collide():
    a = _params_hash("@u:c", "schedule.fire.s1", "r", "action_v1")
    b = _params_hash("@u:c", "schedule.fire.s1", "r", "action_v2")
    assert a != b


def test_edge_of_window_advances_bucket():
    b_at_t = _bucket(T0, WINDOW)
    b_next = _bucket(T0 + timedelta(seconds=WINDOW), WINDOW)
    assert b_next == b_at_t + 1


def test_config_edit_creates_legitimate_new_fire():
    """Per spec §5.2: including params_hash means pause → edit → resume
    within the same window creates a NEW fire-log entry."""
    bucket = _bucket(T0, WINDOW)
    key_before = (bucket, _params_hash("@u:c", "schedule.fire.s1", "r", "old"))
    key_after = (bucket, _params_hash("@u:c", "schedule.fire.s1", "r", "new"))
    assert key_before != key_after
