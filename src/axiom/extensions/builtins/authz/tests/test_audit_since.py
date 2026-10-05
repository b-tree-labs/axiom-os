# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pure-unit tests for the ``--since`` parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.authz.skills._since import parse_since


_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "value,delta",
    [
        ("30m", timedelta(minutes=30)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("2w", timedelta(weeks=2)),
        (" 7d ", timedelta(days=7)),
        ("7D", timedelta(days=7)),
    ],
)
def test_shorthand_subtracts_from_now(value, delta):
    assert parse_since(value, now=_NOW) == _NOW - delta


def test_iso_with_z_suffix():
    assert parse_since("2026-05-30T00:00:00Z", now=_NOW) == datetime(
        2026, 5, 30, 0, 0, 0, tzinfo=UTC
    )


def test_iso_naive_is_assumed_utc():
    assert parse_since("2026-05-30T00:00:00", now=_NOW) == datetime(
        2026, 5, 30, 0, 0, 0, tzinfo=UTC
    )


def test_empty_value_rejected():
    with pytest.raises(ValueError, match="--since requires a value"):
        parse_since("")


def test_garbage_value_rejected():
    with pytest.raises(ValueError, match="must be shorthand"):
        parse_since("yesterday")
