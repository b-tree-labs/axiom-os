# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for cron-cadence next_fire_at computation.

Per spec-axiom-schedule §6.2: only 5-field POSIX form accepted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.schedule.api import Cadence
from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
from axiom.extensions.builtins.schedule.manifest import parse_manifest_block


NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


def test_cron_hourly_next_fire_is_top_of_next_hour():
    pytest.importorskip("croniter")
    cadence = Cadence(kind="cron", cron="0 * * * *")
    nxt = compute_next_fire_at(cadence, last_fire=None, now=NOW)
    assert nxt == datetime(2026, 5, 31, 13, 0, 0, tzinfo=UTC)


def test_cron_six_field_seconds_precision_rejected_at_manifest_parse():
    block = {
        "name": "bad",
        "action": "x.y",
        "cadence": {"kind": "cron", "cron": "*/5 * * * * *"},
    }
    with pytest.raises(ValueError, match="5-field POSIX"):
        parse_manifest_block(block)


def test_cron_missing_expr_raises():
    cadence = Cadence(kind="cron", cron=None)
    with pytest.raises(ValueError, match="cron"):
        compute_next_fire_at(cadence, last_fire=None, now=NOW)
