# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Public-API surface tests.

Verifies the API shape (per spec-axiom-schedule §3 + PRD §5.1) and that
PULSE-1 rejects trigger-style cadences at register-time per spec §7.
The end-to-end register → fire → receipt path lands in
test_engine_tick once the DB harness is wired.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.schedule import (
    Cadence,
    register,
)
from axiom.extensions.builtins.schedule.manifest import parse_manifest_block


def test_register_trigger_cadence_raises_not_implemented():
    """Per spec §7: trigger-style schedules ship in PULSE-2."""
    cadence = Cadence(kind="trigger")
    with pytest.raises(NotImplementedError, match="PULSE-2"):
        register(envelope=object(), cadence=cadence, action="x.y")


def test_manifest_parse_minimal_interval_schedule():
    block = {
        "name": "tidy_heartbeat",
        "description": "TIDY heartbeat",
        "action": "hygiene.scheduled.heartbeat",
        "cadence": {"kind": "interval", "interval_seconds": 3600},
    }
    parsed = parse_manifest_block(block)
    assert parsed.name == "tidy_heartbeat"
    assert parsed.cadence.kind == "interval"
    assert parsed.cadence.interval == timedelta(hours=1)
    assert parsed.raci_default == "autonomous"


def test_manifest_parse_raci_default_propose_first():
    block = {
        "name": "x",
        "action": "x.y",
        "cadence": {"kind": "interval", "interval_seconds": 60},
        "raci_default": "propose_first",
        "classification_ceiling": "internal",
    }
    parsed = parse_manifest_block(block)
    assert parsed.raci_default == "propose_first"
    assert parsed.classification_ceiling == "internal"


def test_manifest_rejects_missing_required_keys():
    with pytest.raises(ValueError, match="name"):
        parse_manifest_block({"action": "x.y", "cadence": {"kind": "interval", "interval_seconds": 1}})


def test_manifest_rejects_unknown_cadence_kind():
    with pytest.raises(ValueError, match="unknown cadence kind"):
        parse_manifest_block({
            "name": "x", "action": "x.y",
            "cadence": {"kind": "bogus"},
        })


def test_manifest_rejects_negative_interval():
    with pytest.raises(ValueError, match="positive integer"):
        parse_manifest_block({
            "name": "x", "action": "x.y",
            "cadence": {"kind": "interval", "interval_seconds": -1},
        })


def test_manifest_rejects_negative_jitter():
    with pytest.raises(ValueError, match="non-negative"):
        parse_manifest_block({
            "name": "x", "action": "x.y",
            "cadence": {
                "kind": "interval",
                "interval_seconds": 60,
                "jitter_seconds": -5,
            },
        })
